import sqlite3
import threading
from datetime import datetime, timezone
from api.core.config import DB_PATH

_write_lock = threading.Lock()


def _get_conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_usage_tables():
    con = _get_conn()
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS download_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_name TEXT NOT NULL,
            dataset_path TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            user TEXT NOT NULL DEFAULT '',
            file_count INTEGER NOT NULL DEFAULT 1,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_events_created ON download_events(created_at)"
    )
    con.commit()
    con.close()


def record_download(dataset_name: str, dataset_path: str, category: str, user: str, file_count: int, size_bytes: int):
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        con = _get_conn()
        con.execute(
            "INSERT INTO download_events (dataset_name, dataset_path, category, user, file_count, size_bytes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dataset_name, dataset_path, category, user, file_count, size_bytes, now),
        )
        con.commit()
        con.close()


def get_usage_summary(days: int = 30) -> dict:
    con = _get_conn()
    cur = con.cursor()

    cur.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes,"
        " COALESCE(SUM(file_count), 0) AS total_files,"
        " COUNT(*) AS total_events"
        " FROM download_events"
        " WHERE created_at >= datetime('now', ?)",
        (f"-{days} days",),
    )
    row = cur.fetchone()
    total_bytes = row["total_bytes"]
    total_files = row["total_files"]
    total_events = row["total_events"]

    cur.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes"
        " FROM download_events"
    )
    all_time_bytes = cur.fetchone()["total_bytes"]

    cur.execute(
        "SELECT date(created_at) AS day, SUM(size_bytes) AS bytes"
        " FROM download_events"
        " WHERE created_at >= datetime('now', ?)"
        " GROUP BY date(created_at)"
        " ORDER BY day",
        (f"-{days} days",),
    )
    daily = [{"day": r["day"], "bytes": r["bytes"]} for r in cur.fetchall()]

    cur.execute(
        "SELECT category, SUM(size_bytes) AS bytes, COUNT(*) AS events"
        " FROM download_events"
        " WHERE created_at >= datetime('now', ?) AND category != ''"
        " GROUP BY category"
        " ORDER BY bytes DESC"
        " LIMIT 6",
        (f"-{days} days",),
    )
    by_category = [{"category": r["category"], "bytes": r["bytes"], "events": r["events"]} for r in cur.fetchall()]

    con.close()
    return {
        "period_days": days,
        "total_bytes": total_bytes,
        "total_files": total_files,
        "total_events": total_events,
        "all_time_bytes": all_time_bytes,
        "daily": daily,
        "by_category": by_category,
    }


def get_dataset_rankings(limit: int = 10) -> dict:
    con = _get_conn()
    cur = con.cursor()

    cur.execute(
        "SELECT dataset_name, dataset_path, category,"
        " COUNT(*) AS download_count,"
        " COALESCE(SUM(size_bytes), 0) AS total_bytes,"
        " COALESCE(SUM(file_count), 0) AS total_files"
        " FROM download_events"
        " GROUP BY dataset_name"
        " ORDER BY download_count DESC"
        " LIMIT ?",
        (limit,),
    )
    all_time = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT dataset_name, dataset_path, category,"
        " COUNT(*) AS download_count,"
        " COALESCE(SUM(size_bytes), 0) AS total_bytes"
        " FROM download_events"
        " WHERE created_at >= datetime('now', '-7 days')"
        " GROUP BY dataset_name"
        " ORDER BY download_count DESC"
        " LIMIT ?",
        (limit,),
    )
    this_week = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT dataset_name, dataset_path, category,"
        " COUNT(*) AS download_count,"
        " COALESCE(SUM(size_bytes), 0) AS total_bytes"
        " FROM download_events"
        " WHERE created_at >= datetime('now', '-7 days')"
        " GROUP BY dataset_name"
        " ORDER BY total_bytes DESC"
        " LIMIT ?",
        (limit,),
    )
    biggest_this_week = [dict(r) for r in cur.fetchall()]

    trending_names = set()
    cur.execute(
        "SELECT dataset_name,"
        " SUM(CASE WHEN created_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS recent,"
        " SUM(CASE WHEN created_at < datetime('now', '-7 days') AND created_at >= datetime('now', '-14 days') THEN 1 ELSE 0 END) AS previous"
        " FROM download_events"
        " WHERE created_at >= datetime('now', '-14 days')"
        " GROUP BY dataset_name"
        " HAVING recent > previous AND recent >= 2"
    )
    for r in cur.fetchall():
        trending_names.add(r["dataset_name"])

    con.close()
    return {
        "all_time": all_time,
        "this_week": this_week,
        "biggest_this_week": biggest_this_week,
        "trending": list(trending_names),
    }
