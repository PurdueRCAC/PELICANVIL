"""Read-only views of an app server's SQLite databases (the per-user downloads
DB and the temp shared catalog DB). Every connection sets query_only, so the
harness can never modify what it inspects."""
import json
import sqlite3


def _connect(path):
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def query(path, sql, params=()):
    con = _connect(path)
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def jobs(server):
    rows = query(server.downloads_db, "SELECT * FROM download_jobs ORDER BY started_at")
    for r in rows:
        r["files"] = json.loads(r["files"]) if r.get("files") else []
    return rows


def history(server):
    rows = query(server.downloads_db, "SELECT * FROM download_history ORDER BY id")
    for r in rows:
        r["files"] = json.loads(r["files"]) if r.get("files") else []
    return rows


def job(server, job_id):
    rows = [r for r in jobs(server) if r["job_id"] == job_id]
    return rows[0] if rows else None


def schema(path):
    """{table: [column names]} plus the raw CREATE statements, for idempotency checks."""
    rows = query(path, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
    cols = {}
    for r in rows:
        if r["type"] == "table":
            cols[r["name"]] = [c["name"] for c in query(path, f"PRAGMA table_info({r['name']})")]
    return {"objects": rows, "columns": cols}


def integrity(path):
    return query(path, "PRAGMA integrity_check")[0]["integrity_check"]


def dump_for_capture(server, job_ids=(), history_ids=()):
    """Everything relevant to a failed test, as plain JSON-able data."""
    out = {"server": server.name, "mode": server.mode, "downloads_db": str(server.downloads_db)}
    try:
        js, hs = jobs(server), history(server)
    except Exception as e:      # noqa: BLE001
        return {**out, "error": f"could not read DB: {type(e).__name__}: {e}"}
    wanted_j = set(job_ids)
    wanted_h = set(history_ids) | {j["history_id"] for j in js if j["job_id"] in wanted_j}
    if wanted_j or wanted_h:
        js = [j for j in js if j["job_id"] in wanted_j or j["history_id"] in wanted_h]
        hs = [h for h in hs if h["id"] in wanted_h]
    else:
        js, hs = js[-20:], hs[-20:]
    return {**out, "download_jobs": js, "download_history": hs}
