"""Per-run shared state. Built lazily from the PV_* environment variables that
tests/run_suite.py exports (so `pytest tests/suite` also works standalone if
those are set)."""
import atexit
import json
import os
import re
import threading
import time
from pathlib import Path

import yaml

from . import env
from .server import AppServer

TERMINAL = ("complete", "partial", "failed")
KNOWN_CATEGORIES = ("auth_required", "not_found", "permission", "connection", "unknown")
FUTURES_NEEDLE = "cannot schedule new futures"

_ctx = None


def get() -> "RunContext":
    global _ctx
    if _ctx is None:
        _ctx = RunContext()
    return _ctx


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:80] or "x"


class RunContext:
    def __init__(self):
        e = os.environ
        self.run_dir = Path(e["PV_RUN_DIR"])
        self.work_dir = Path(e["PV_WORK_DIR"])          # scratch: temp homes, temp DBs, downloaded data
        self.data_dir = self.work_dir / "data"
        self.depth = e.get("PV_DEPTH", "standard")
        self.app_dir = Path(e.get("PV_APP_DIR") or env.DEFAULT_REPO)
        self.modes = [m for m in e.get("PV_MODES", "uvicorn").split(",") if m]
        self.long_modes = [m for m in e.get("PV_LONG_MODES", ",".join(self.modes)).split(",") if m]
        self.reps = int(e.get("REPS", "3"))
        self.duration = int(e.get("DURATION", "3600"))            # cap for the long-running job (s)
        self.soak_duration = int(e.get("SOAK_DURATION", "1800"))  # soak loop length (s)
        self.stall_seconds = int(e.get("STALL_SECONDS", "600"))
        self.long_stall_seconds = int(e.get("LONG_STALL_SECONDS", "1200"))
        self.long_max_gb = float(e.get("LONG_MAX_GB", "20"))
        self.fixtures_path = Path(e.get("PV_FIXTURES") or (env.TESTS_DIR / "fixtures.yaml"))
        self.fx = yaml.safe_load(self.fixtures_path.read_text(encoding="utf-8"))
        self.servers = []
        self.wsgi_unavailable = None
        self.current_test = None
        self._notes = []
        self._tracked = {}           # test id -> list of (server, job_id, history_id)
        self._lock = threading.Lock()
        self._seq = 0
        self._anom_n = 0
        self.anomalies_path = self.run_dir / "anomalies.jsonl"
        (self.run_dir / "anomalies").mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.features = detect_features(self.app_dir)
        self.known_existing = known_existing_paths(self.fx)
        atexit.register(self.kill_all_servers)

    # ---------------------------------------------------------------- servers
    def make_server(self, name: str, mode: str) -> AppServer:
        with self._lock:
            self._seq += 1
            uniq = f"{self._seq:02d}-{_slug(name)}-{mode}"
        s = AppServer(uniq, mode, self.work_dir, self.run_dir / "server-logs", self.app_dir)
        self.servers.append(s)
        return s

    def kill_all_servers(self):
        for s in list(self.servers):
            try:
                if s.alive():
                    s.kill9()
            except Exception:       # noqa: BLE001
                pass

    # ------------------------------------------------------------------ paths
    def dest(self, label: str) -> Path:
        with self._lock:
            self._seq += 1
            p = self.data_dir / f"{self._seq:03d}-{_slug(label)}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------- notes / tracking
    def note(self, msg: str) -> None:
        self._notes.append(msg)

    def drain_notes(self) -> list:
        n, self._notes = self._notes, []
        return n

    def track(self, server: AppServer, job: dict) -> None:
        self._tracked.setdefault(self.current_test, []).append((server, job.get("job_id"), job.get("history_id")))

    def tracked_for(self, test_id) -> list:
        return self._tracked.get(test_id, [])

    def tested_servers(self, test_id) -> list:
        seen, out = set(), []
        for s, _, _ in self.tracked_for(test_id):
            if s.name not in seen:
                seen.add(s.name)
                out.append(s)
        return out

    # -------------------------------------------------------------- anomalies
    def anomaly(self, kind: str, server: AppServer, **info) -> None:
        """Record something a green run must still surface (unknown-category
        failure, the 'cannot schedule new futures' error, ...). Snapshots the
        evidence immediately, because these can't be reproduced on demand."""
        from . import dbutil
        with self._lock:
            self._anom_n += 1
            n = self._anom_n
        rec = {"n": n, "kind": kind, "test": self.current_test, "server": server.name, "mode": server.mode,
               "time": time.strftime("%Y-%m-%d %H:%M:%S"), **info}
        d = self.run_dir / "anomalies" / f"{n:03d}-{kind}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "server.log.tail").write_text("\n".join(server.log_tail(400)), encoding="utf-8")
            if server.last_error_log.exists():
                (d / "last_error.log").write_text(env.redact(server.last_error_log.read_text(errors="replace")), encoding="utf-8")
            (d / "db_rows.json").write_text(
                json.dumps(dbutil.dump_for_capture(server, job_ids=[info.get("job_id")] if info.get("job_id") else ()),
                           indent=2, default=str), encoding="utf-8")
            tb = extract_tracebacks(server.log_text(), FUTURES_NEEDLE if kind == "futures-shutdown" else info.get("error", "")[:60])
            if tb:
                (d / "traceback.txt").write_text(tb, encoding="utf-8")
                rec["traceback_file"] = str((d / "traceback.txt").relative_to(self.run_dir))
        except Exception as ex:     # noqa: BLE001
            rec["capture_error"] = f"{type(ex).__name__}: {ex}"
        rec["evidence_dir"] = str(d.relative_to(self.run_dir))
        with open(self.anomalies_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_anomalies(self) -> list:
        if not self.anomalies_path.exists():
            return []
        return [json.loads(line) for line in self.anomalies_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def extract_tracebacks(text: str, needle: str, before: int = 45, after: int = 6, limit: int = 3) -> str:
    """Log excerpts around occurrences of `needle` (a full Python traceback
    normally sits in the ~45 lines before the final exception line)."""
    if not needle:
        return ""
    lines = text.splitlines()
    out, last_end = [], -1
    for i, line in enumerate(lines):
        if needle in line and i > last_end:
            lo = max(0, i - before)
            # walk back to the nearest "Traceback (most recent call last)" if present
            for j in range(i, lo - 1, -1):
                if lines[j].startswith("Traceback (most recent call last)"):
                    lo = j
                    break
            hi = min(len(lines), i + after + 1)
            out.append(f"----- server.log lines {lo + 1}-{hi} -----\n" + "\n".join(lines[lo:hi]))
            last_end = hi
            if len(out) >= limit:
                break
    return env.redact("\n\n".join(out))


def detect_features(repo: Path) -> dict:
    """Which fixes exist in the checkout under test (read from source text, so
    the same suite can run on the pre-fix commit 4766e6d and report the
    difference instead of crashing)."""
    def has(rel, needle):
        try:
            return needle in (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return {
        "connection_retry(cdfb44f)": has("api/routes/pelican.py", "_with_connection_retry"),
        "interrupted_job_recovery(900e898)": has("api/routes/downloads.py", "_recover_interrupted_jobs"),
        "restart_routes": has("api/routes/downloads.py", "/restart-file"),
        "passenger_wsgi.py": (repo / "passenger_wsgi.py").exists(),
    }


def known_existing_paths(fx) -> set:
    """Federation paths the fixtures guarantee exist. If the app ever reports
    `not_found` for one of these, that is not a normal 404: the harness checks
    the federation directly and records an anomaly (see jobs._Watch)."""
    out = {fx["canary"], fx["tiny_file"]["path"], fx["small_file"]["path"], fx["special_name_file"]["path"],
           fx["nested_dir"]["path"], fx["hour_dir"]["path"], fx["long_run"]["path"]}
    out.update(m["path"] for m in fx["medium_files"])
    out.update(fx["many_files"]["paths"])
    return out
