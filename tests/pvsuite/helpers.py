"""Shared building blocks for the test modules."""
import os
import threading
import time
from pathlib import Path

import pytest

from .harness import dbutil, env, jobs, run
from .harness.run import KNOWN_CATEGORIES


def base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def run_job(api, dest, paths, timeout=240, name=None, stall=None):
    job = jobs.start(api, dest, paths, name=name)
    st = jobs.wait_terminal(api, job, timeout=timeout, stall=stall)
    return job, st


def assert_single_file(dest, entry, label="", expect_dest_root=True):
    """A single-file download must land as a correct copy. Where fsspec puts a
    lone file is an implementation detail, so search for it; but note (and
    fail on) a location other than dest/<basename> only if expect_dest_root."""
    name = base(entry["path"])
    p = jobs.find_file(dest, name, entry.get("size"))
    if p is None:
        others = [q for q in Path(dest).rglob("*") if q.is_file() and q.stat().st_size == entry.get("size")]
        if len(others) == 1:
            ok = env.sha256_file(others[0]) == entry["sha256"] if entry.get("sha256") else "unchecked"
            raise AssertionError(f"{label or name}: the download is on disk but under the WRONG NAME: expected {name!r}, "
                                 f"found {others[0].relative_to(dest).as_posix()!r} (content sha256 matches pin: {ok})")
    assert p is not None, (f"{label or name}: no file named {name!r} with size {entry.get('size')} anywhere under {dest}; "
                           f"tree={sorted(env.dir_tree(dest))[:10]}")
    jobs.assert_file(p, entry["size"], sha256=entry.get("sha256"), md5=entry.get("md5"), label=label or name)
    rel = p.relative_to(dest).as_posix()
    if expect_dest_root:
        assert rel == name, f"{label or name}: landed at {rel!r}, expected {name!r} directly under the destination"
    return p


def assert_job_ok(st, n_items):
    files = st.get("files", [])
    assert st["status"] == "complete", (f"job ended {st['status']!r}: "
                                        f"{[(base(f['path']), f['status'], f.get('error_category'), f.get('error')) for f in files if f['status'] != 'succeeded']}"
                                        f" error_message={st.get('error_message')!r}")
    assert len(files) == n_items and all(f["status"] == "succeeded" for f in files), f"file states: {files}"


def hour_expected(fx, prefix="12"):
    """{relpath under destination: size} for the pinned GOES hour dir downloaded as <dest>/<prefix>/..."""
    return {f"{prefix}/{k}": int(v) for k, v in fx["hour_dir"]["sizes"].items()}


def block_writes(d: Path):
    """Make a directory genuinely unwritable, or skip if the OS/user can't."""
    os.chmod(d, 0o555)
    if os.access(d, os.W_OK):
        os.chmod(d, 0o755)
        pytest.skip("cannot make a directory unwritable here (Windows, or running as root)")


def unblock_writes(d: Path):
    os.chmod(d, 0o755)


def count_files(root) -> int:
    n = 0
    for _, _, fs in os.walk(root):
        n += len(fs)
    return n


def wait_for(cond, timeout, poll=0.5, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(poll)
    raise jobs.JobTimeout(f"timed out after {timeout}s waiting for {what}")


def known_category(cat):
    return cat in KNOWN_CATEGORIES


class _DbSampler(threading.Thread):
    """Samples download_jobs with ONE atomic query every ~20 ms, so 'how many jobs are in_progress
    right now' is a consistent snapshot. (Polling N status endpoints one after another is not:
    with short jobs a job can be seen in_progress early in a sweep and its successor later in the
    same sweep, inflating the apparent concurrency.)"""

    def __init__(self, db_path):
        super().__init__(daemon=True)
        self.db_path, self._stop_evt = db_path, threading.Event()
        self.peak, self.saw_pending_with_full_pool, self.samples, self.error = 0, False, 0, None

    def run(self):
        while not self._stop_evt.is_set():
            try:
                rows = dbutil.query(self.db_path, "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status")
                by = {r["status"]: r["n"] for r in rows}
                self.samples += 1
                self.peak = max(self.peak, by.get("in_progress", 0))
                if by.get("in_progress", 0) >= 3 and by.get("pending", 0) >= 1:
                    self.saw_pending_with_full_pool = True
            except Exception as e:      # noqa: BLE001 - a transient lock while sampling must not fail the test
                self.error = f"{type(e).__name__}: {e}"
            time.sleep(0.02)

    def stop(self):
        self._stop_evt.set()
        self.join(5)


def poll_concurrency(api, job_list, timeout, poll=0.3):
    """Watch several jobs at once. Peak concurrency and 'pending while the pool is full' come from
    consistent DB snapshots; per-job first-in_progress / terminal times come from the API."""
    t0 = time.time()
    first_ip, done_at, final = {}, {}, {}
    sampler = _DbSampler(api.server.downloads_db)
    sampler.start()
    try:
        while len(done_at) < len(job_list):
            if time.time() - t0 > timeout:
                raise jobs.JobTimeout(f"{len(job_list) - len(done_at)} of {len(job_list)} jobs not terminal after {timeout}s")
            for j in job_list:
                if j["job_id"] in done_at:
                    continue
                r = api.status(j["job_id"])
                assert r.status_code == 200, f"status {j['job_id']} -> {r.status_code}: {r.text[:200]}"
                st = r.json()
                now = time.time()
                if st["status"] == "in_progress":
                    first_ip.setdefault(j["job_id"], now)
                if st["status"] in run.TERMINAL:
                    first_ip.setdefault(j["job_id"], now)
                    done_at[j["job_id"]] = now
                    final[j["job_id"]] = st
            time.sleep(poll)
    finally:
        sampler.stop()
    return {"first_in_progress": first_ip, "done_at": done_at, "final": final, "peak": sampler.peak,
            "saw_pending_with_full_pool": sampler.saw_pending_with_full_pool, "elapsed": time.time() - t0,
            "db_samples": sampler.samples}
