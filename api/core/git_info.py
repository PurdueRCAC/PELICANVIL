import subprocess
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return result.stdout.strip()


def get_git_version_info() -> dict:
    # Computed once at process startup (see main.py) rather than per-request:
    # this deployment runs under Passenger, which only picks up code changes
    # after `tmp/restart.txt` is touched (see .gitignore's "OOD / Passenger
    # runtime artifacts" section), not live autoreload. A per-request git call
    # would report whatever's on disk even if the running process hasn't
    # actually reloaded that code yet — startup-time caching instead reflects
    # the commit this process actually has loaded in memory.
    try:
        commit_hash = _run_git("rev-parse", "--short", "HEAD")
        commit_iso = _run_git("show", "-s", "--format=%cI", "HEAD")
        commit_time = datetime.fromisoformat(commit_iso).strftime("%Y-%m-%d %H:%M")
        dirty = bool(_run_git("status", "--porcelain"))
        return {
            "available": True,
            "hash": commit_hash,
            "commit_time": commit_time,
            "dirty": dirty,
        }
    except Exception:
        return {
            "available": False,
            "hash": "unknown",
            "commit_time": "",
            "dirty": False,
        }
