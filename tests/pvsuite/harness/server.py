"""Lifecycle of one isolated app server process (uvicorn or waitress+a2wsgi)."""
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import env

LAUNCHER = Path(__file__).with_name("launcher.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_db(repo: Path, db_path: Path, user: str) -> None:
    """Create the shared-catalog schema by running the repo's own
    scripts/init_db.py with its hard-coded DB_PATH swapped for the temp path
    (so the schema can never drift from what the app expects), then authorise
    the running user so the admin routes can be exercised."""
    import sqlite3
    src = (repo / "scripts" / "init_db.py").read_text(encoding="utf-8")
    # callable replacement: a Windows path's backslashes must not be read as regex escapes
    src, n = re.subn(r'^DB_PATH\s*=.*$', lambda _m: f'DB_PATH = {str(db_path)!r}', src, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("could not locate DB_PATH assignment in scripts/init_db.py")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ns = {"__name__": "pv_seed"}
    exec(compile(src, "init_db.py(seed)", "exec"), ns)      # noqa: S102 - our own repo file
    ns["con"].close()
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR IGNORE INTO authorizedUsers (name) VALUES (?)", (user,))
        con.commit()
    finally:
        con.close()


class AppServer:
    def __init__(self, name: str, mode: str, work_dir: Path, log_dir: Path, repo: Path, python: str | None = None):
        self.name, self.mode, self.repo = name, mode, Path(repo)
        self.python = python or sys.executable
        self.root = Path(work_dir) / "servers" / name
        self.home = self.root / "home"
        self.db = self.root / "data" / "pelican.db"
        self.downloads_db = self.home / ".pelican-ui" / "downloads_history.db"
        self.queue = self.root / "data" / "indexing_queue.json"      # scratch file: nothing ever consumes it
        self.blind = self.root / "data" / "blind_mode.flag"
        self.log_path = Path(log_dir) / f"{name}.log"
        self.last_error_log = self.home / ".pelican-ui" / "last_error.log"
        self.token_dir = self.home / ".pelican-ui" / "tokens"
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.allow_unfinished = False      # set by a test that deliberately abandons a running job (the capped long run)
        self.kills = 0
        self.started_at: float | None = None
        self._log_fh = None
        self.root.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        seed_db(self.repo, self.db, env.current_user())

    # ------------------------------------------------------------------ basics
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 90.0) -> None:
        if self.alive():
            return
        self._log_fh = open(self.log_path, "ab", buffering=0)
        self._log_fh.write(f"\n===== PV start #{self.starts + 1} mode={self.mode} port={self.port} "
                           f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
        cmd = [self.python, "-u", str(LAUNCHER), "--repo", str(self.repo), "--mode", self.mode,
               "--port", str(self.port), "--run-root", str(self.root),
               "--db", str(self.db), "--downloads-db", str(self.downloads_db),
               "--queue", str(self.queue), "--blind", str(self.blind)]
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.repo), env=env.sanitized_env(self.home),
            stdout=self._log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
        self.starts += 1
        self.started_at = time.time()
        self._wait_ready(timeout)

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ServerStartError(self._diagnose(f"exited with code {self.proc.returncode} during startup"))
            try:
                r = requests.get(self.base_url + "/downloads/history", timeout=3)
                if r.status_code == 200:
                    return
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
            time.sleep(0.4)
        self.kill9()
        raise ServerStartError(self._diagnose(f"not ready after {timeout:.0f}s (last: {last})"))

    def _diagnose(self, msg: str) -> str:
        tail = "\n".join(self.log_tail(40))
        tag = ""
        if "PV-WSGI-UNAVAILABLE" in tail:
            tag = " [wsgi-unavailable]"
        if "PV-ISOLATION-FAILURE" in tail:
            tag = " [isolation-failure]"
        return f"server '{self.name}' ({self.mode}) {msg}{tag}\n--- server.log tail ---\n{tail}"

    def stop(self, timeout: float = 8.0) -> None:
        """Graceful stop, escalating to kill. Only ever signals the process
        this harness spawned."""
        if self.proc is None:
            return
        if self.alive():
            try:
                self.proc.terminate()
                self.proc.wait(timeout)
            except Exception:       # noqa: BLE001
                self.kill9()
        self._close_log()

    def kill9(self) -> None:
        """SIGKILL (TerminateProcess on Windows) of OUR spawned server only."""
        if self.proc is not None and self.proc.poll() is None:
            self.kills += 1
            try:
                if os.name == "posix":
                    os.kill(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
            except Exception:       # noqa: BLE001
                pass
            try:
                self.proc.wait(15)
            except Exception:       # noqa: BLE001
                pass
        self._close_log()

    def restart_after_kill(self, timeout: float = 90.0) -> None:
        self.start(timeout)

    def _close_log(self):
        if self._log_fh and not self._log_fh.closed:
            self._log_fh.close()

    # ------------------------------------------------------------------- logs
    def log_tail(self, n: int = 200) -> list:
        try:
            data = self.log_path.read_bytes()[-400_000:].decode("utf-8", "replace")
        except OSError:
            return []
        return env.redact(data).splitlines()[-n:]

    def log_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def log_since(self, offset: int) -> str:
        """Everything the server logged after byte `offset` (immune to log-window sliding)."""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(offset)
                return env.redact(fh.read().decode("utf-8", "replace"))
        except OSError:
            return ""

    def log_text(self) -> str:
        try:
            return env.redact(self.log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""


class ServerStartError(RuntimeError):
    pass
