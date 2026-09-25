"""Download-job helpers: start, wait (with stall detection), and verification."""
import json
import time

import requests

from . import env, run
from .run import FUTURES_NEEDLE, TERMINAL


class JobFailure(AssertionError):
    kind = "failure"


class JobTimeout(JobFailure):
    kind = "timeout"


class JobStall(JobFailure):
    kind = "STALL"


class ServerDied(JobFailure):
    kind = "server-died"


def start(api, dest, paths, name=None, sizes=None) -> dict:
    ctx = run.get()
    r = api.start_download(name or "pv-suite", dest, paths, sizes)
    assert r.status_code == 200, f"POST /datasets/download/start -> {r.status_code}: {r.text[:300]}"
    body = r.json()
    job = {"job_id": body["job_id"], "history_id": body["history_id"], "dest": str(dest), "paths": list(paths),
           "t0": time.time(), "server": api.server}
    ctx.track(api.server, job)
    return job


def _sig(status):
    return json.dumps([(f.get("path"), f.get("status")) for f in status.get("files", [])] + [status.get("status")])


def _direct_check(path: str) -> str:
    """Ask the federation directly (real pelicanfs, same library) whether `path` exists right now."""
    from . import fed
    try:
        parent, _, name = path.rstrip("/").rpartition("/")
        for e in fed.ls(parent):
            if e["name"].rstrip("/").rsplit("/", 1)[-1] == name:
                return f"EXISTS per direct listing ({e['type']}, size={e.get('size')}) -- the app's not_found contradicts the federation"
        return "NOT in the parent listing (federation itself no longer has it)"
    except Exception as ex:      # noqa: BLE001
        return f"direct check failed: {type(ex).__name__}: {str(ex)[:150]}"


class _Watch:
    """Watches one job's polled statuses for failures the suite must surface
    even when the test itself passes."""

    def __init__(self, api, job):
        self.api, self.job, self.seen = api, job, set()

    def observe(self, st):
        ctx = run.get()
        files = st.get("files", [])
        done = sum(1 for f in files if f.get("status") in ("succeeded", "failed"))
        for f in files:
            if f.get("status") != "failed" or f["path"] in self.seen:
                continue
            self.seen.add(f["path"])
            err, cat = f.get("error") or "", f.get("error_category") or "unknown"
            into = time.time() - self.job["t0"]
            base = dict(job_id=self.job["job_id"], path=f["path"], error=err, category=cat,
                        seconds_into_job=round(into, 1), files_done=done, files_total=len(files),
                        job_started_at=st.get("started_at"))
            if cat == "not_found" and f["path"] in ctx.known_existing:
                base["federation_direct_check"] = _direct_check(f["path"])
                ctx.anomaly("false-not-found", self.api.server, **base)
            elif FUTURES_NEEDLE in err:
                ctx.anomaly("futures-shutdown", self.api.server, **base)
            elif cat == "unknown":
                ctx.anomaly("unknown-category", self.api.server, **base)


def wait_terminal(api, job, timeout=300, stall=None, poll=1.0, stop_when=None) -> dict:
    """Poll until the job is complete/partial/failed. Raises JobStall if neither
    the per-file statuses nor the bytes on disk change for `stall` seconds,
    JobTimeout past `timeout`, ServerDied if the server process exits.
    `stop_when(status, bytes_on_disk)` may return a reason string to stop
    waiting early (used to cap the long-running job); the returned status then
    carries `_stopped`."""
    ctx = run.get()
    stall = stall if stall is not None else ctx.stall_seconds
    dest = job["dest"]
    watch = _Watch(api, job)
    t0 = time.time()
    last_sig, last_change, next_disk, disk = None, t0, 0.0, 0
    st = {}
    while True:
        now = time.time()
        try:
            r = api.status(job["job_id"])
        except requests.RequestException:
            if not api.server.alive():
                raise ServerDied(f"server '{api.server.name}' died while waiting for job {job['job_id']}")
            time.sleep(poll)
            if time.time() - t0 > timeout:
                raise JobTimeout(f"job {job['job_id']}: server unreachable until timeout ({timeout}s)")
            continue
        if r.status_code == 404:
            raise JobFailure(f"job {job['job_id']}: status endpoint returned 404 (job row vanished)")
        assert r.status_code == 200, f"status -> {r.status_code}: {r.text[:200]}"
        st = r.json()
        watch.observe(st)
        if st["status"] in TERMINAL:
            st["_elapsed"] = time.time() - job["t0"]
            return st
        if now >= next_disk:
            disk = env.dir_bytes(dest)
            next_disk = now + 3.0
        if stop_when:
            why = stop_when(st, disk)
            if why:
                st["_stopped"] = why
                st["_elapsed"] = time.time() - job["t0"]
                st["_disk"] = disk
                return st
        sig = _sig(st) + f"|{disk}"
        if sig != last_sig:
            last_sig, last_change = sig, now
        if now - last_change > stall:
            raise JobStall(_context(api, job, st, f"no change in per-file status or bytes on disk for {int(now - last_change)}s "
                                                 f"(threshold {stall}s); {disk} bytes on disk"))
        if now - t0 > timeout:
            raise JobTimeout(_context(api, job, st, f"job still '{st['status']}' after {timeout}s"))
        if not api.server.alive():
            raise ServerDied(f"server '{api.server.name}' died while waiting for job {job['job_id']}")
        time.sleep(poll)


def _context(api, job, st, msg):
    from . import dbutil
    try:
        row = dbutil.job(api.server, job["job_id"])
        row_s = json.dumps({k: v for k, v in (row or {}).items() if k != "files"}, default=str)
        fs = row.get("files", []) if row else []
        counts = {}
        for f in fs:
            counts[f.get("status")] = counts.get(f.get("status"), 0) + 1
        row_s += f" file_status_counts={counts}"
    except Exception as e:      # noqa: BLE001
        row_s = f"<db unreadable: {e}>"
    tail = "\n    ".join(api.server.log_tail(15))
    return f"job {job['job_id']}: {msg}\n  DB row: {row_s}\n  server.log tail:\n    {tail}"


def files_by_path(status) -> dict:
    return {f["path"]: f for f in status.get("files", [])}


# ------------------------------------------------------------------ verification
def find_file(dest, basename, size=None):
    """Locate a downloaded single file anywhere under dest (where fsspec puts
    a lone file is an implementation detail); returns Path or None."""
    from pathlib import Path
    hits = [p for p in Path(dest).rglob(basename) if p.is_file() and (size is None or p.stat().st_size == size)]
    return hits[0] if hits else None


def assert_file(path, size, sha256=None, md5=None, label=""):
    from pathlib import Path
    p = Path(path)
    assert p is not None and p.is_file(), f"{label} missing on disk: {path}"
    got = p.stat().st_size
    assert got == size, f"{label} size {got} != expected {size} ({path})"
    if sha256:
        h = env.sha256_file(p)
        assert h == sha256, f"{label} sha256 {h} != expected {sha256} ({path})"
    if md5:
        h = env.md5_file(p)
        assert h == md5, f"{label} md5 {h} != expected {md5} ({path})"


def assert_tree(root, expected: dict, sha: dict | None = None, label="tree"):
    """expected = {relpath: size}; sha = {relpath: sha256} for the checked subset.
    Requires an EXACT match: nothing missing, nothing extra, no wrong sizes."""
    from pathlib import Path
    root = Path(root)
    got = env.dir_tree(root)
    missing = sorted(set(expected) - set(got))
    extra = sorted(set(got) - set(expected))
    wrong = sorted(k for k in expected if k in got and got[k] != expected[k])
    problems = []
    if missing:
        problems.append(f"{len(missing)} missing, e.g. {missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} unexpected extra file(s), e.g. {extra[:5]}")
    if wrong:
        problems.append(f"{len(wrong)} wrong size (truncated/oversized), e.g. "
                        f"{[(k, got[k], expected[k]) for k in wrong[:5]]}")
    if not problems and sha:
        bad = [k for k, h in sha.items() if env.sha256_file(root / k) != h]
        if bad:
            problems.append(f"{len(bad)} sha256 mismatch, e.g. {bad[:5]}")
    assert not problems, f"{label} at {root}: " + "; ".join(problems)
