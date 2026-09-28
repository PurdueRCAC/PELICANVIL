"""Snapshots of the REAL state the suite must never modify (stat only; never opened)."""
import json
import os
import re
from pathlib import Path


def _stat(p: Path):
    try:
        st = p.stat()
        return {"exists": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return {"exists": False}


def _tree(root: Path):
    out = {}
    if not root.exists():
        return out
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = Path(dp) / f
            out[str(p)] = _stat(p)
    return out


def snapshot(app_dir: Path, real_home: Path) -> dict:
    per_user = _tree(Path(real_home) / ".pelican-ui")
    shared = {}
    try:
        cfg = (Path(app_dir) / "api" / "core" / "config.py").read_text(encoding="utf-8")
        m = re.search(r'^DB_PATH\s*=\s*"([^"]+)"', cfg, re.M)
        if m:
            db = Path(m.group(1))
            for p in (db, db.with_name("indexing_queue.json"), db.with_name("indexing_queue.json.lock"),
                      db.with_name("blind_mode.flag")):
                shared[str(p)] = _stat(p)
    except OSError:
        pass
    return {"per_user": per_user, "shared": shared}


def save(path: Path, snap: dict):
    Path(path).write_text(json.dumps(snap, indent=2), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
