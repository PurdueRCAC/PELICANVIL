from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import json, os
from pathlib import Path
import sqlite3
from api.core.config import DB_PATH
from api.core.category_icons import ensure_icon_columns

ROOTPATH = Path.cwd()
DATASET_PATH = os.path.join(ROOTPATH, "data", "datasets.json")
CATEGORY_PATH = os.path.join(ROOTPATH, "data", "categories.json")
datasetRouter = APIRouter()

def _indexed_sizes_by_path() -> dict:
    """dataset.path -> total size in bytes, from the indexing worker's
    dataset_folder_sizes table — a dataset's own row (folder_path == its own
    root path) already *is* the recursive total, see
    scripts/indexing_worker.py's _finalize_folder_sizes. Table may not exist
    yet on a fresh deployment (created lazily on the worker's first
    successful run) — that's not an error, just "nothing indexed yet"."""
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT dfs.folder_path, dfs.size_bytes FROM dataset_folder_sizes dfs"
                " INNER JOIN datasets d ON dfs.folder_path = RTRIM(d.path, '/')"
            )
            return dict(cur.fetchall())
        finally:
            con.close()
    except sqlite3.OperationalError:
        return {}


@datasetRouter.get("/retrieve-datasets")
def serveDatasets():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute('SELECT * FROM datasets')
    rows = cur.fetchall()
    try:
        cur.execute(
            "SELECT dfs.folder_path, dfs.size_bytes FROM dataset_folder_sizes dfs"
            " INNER JOIN datasets d ON dfs.folder_path = RTRIM(d.path, '/')"
        )
        indexed_sizes = dict(cur.fetchall())
    except sqlite3.OperationalError:
        indexed_sizes = {}
    con.close()
    result = []
    for row in rows:
        current_row = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "path": row["path"],
            "format": row["format"],
            "streamable": bool(row["streamable"]),
            "access": row["access"],
            "tags" : json.loads(row["tags"]),
            "indexed_size_bytes": indexed_sizes.get(row["path"].rstrip("/")),
        }
        result.append(current_row)
    return result

def buildCatalog():
    """
    Shared by /datasets/catalog and the homepage overview section — both need
    the same dataset_count / category_count / category_names derived from the
    same tag<->category.url matching, so this is the one place that logic lives.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute('SELECT * FROM datasets')
    dataset_rows = cur.fetchall()
    cur.execute('SELECT * FROM categories')
    category_rows = cur.fetchall()
    con.close()

    # tags aren't a foreign key — a dataset's category membership is whichever
    # of its freeform tags happens to match a categories.url, same lookup
    # datasets.js already does client-side for the per-category pages
    category_name_by_url = {row["url"]: row["name"] for row in category_rows}

    catalog = []
    category_names_used = set()
    for row in dataset_rows:
        tags = json.loads(row["tags"])
        matched_categories = [category_name_by_url[tag] for tag in tags if tag in category_name_by_url]
        category_names_used.update(matched_categories)
        catalog.append({
            "id": row["id"],
            "name": row["name"],
            "path": row["path"],
            "categories": matched_categories,
        })

    return {
        "dataset_count": len(catalog),
        "category_count": len(category_names_used),
        "category_names": sorted(category_names_used),
        "datasets": catalog,
    }

@datasetRouter.get("/datasets/catalog")
def serveCatalog():
    return buildCatalog()

@datasetRouter.get("/datasets/recent")
def serveRecentDatasets():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT id, name, description, path, format FROM datasets ORDER BY id DESC LIMIT 5")
    rows = cur.fetchall()
    con.close()
    return [dict(row) for row in rows]

@datasetRouter.get("/retrieve-categories")
def serveCategories():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute('SELECT * FROM categories')
    rows = cur.fetchall()
    cur.execute('SELECT tags FROM datasets')
    dataset_rows = cur.fetchall()
    con.close()
    counts = {}
    for d in dataset_rows:
        for tag in json.loads(d["tags"]):
            counts[tag] = counts.get(tag, 0) + 1
    result = []
    for row in rows:
        current_row = {
            "name": row["name"],
            "url": row["url"],
            "icon": row["icon"],
            "description": row["description"],
            "dataset_count": counts.get(row["url"], 0),
        }
        result.append(current_row)
    return result


@datasetRouter.get("/categories/{category_url}/icon")
def serveCategoryIcon(category_url: str):
    # Public — same trust level as /retrieve-categories above (category
    # icons are catalog-wide, not access-controlled data). Reads the DB-
    # backed icon straight into the HTTP response, not base64-inlined, so
    # the frontend can reference it with a normal <img src="..."> and get
    # normal browser caching behavior — see the design conversation's Phase
    # 1 note on why this beats embedding the bytes into the page payload.
    con = sqlite3.connect(DB_PATH)
    try:
        ensure_icon_columns(con)
        cur = con.cursor()
        cur.execute(
            "SELECT icon_data, icon_content_type FROM categories WHERE url = ?",
            (category_url,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None or row[0] is None:
        # No icon uploaded for this category yet (or the category doesn't
        # exist) — a plain 404, not a placeholder image. The frontend's own
        # <img onerror> is what shows the generic fallback graphic; keeping
        # that a frontend concern means every render site (categories.js,
        # index.html, admin.js's preview) gets the same fallback for free
        # from the same mechanism instead of this route needing to guess
        # what a good default image would look like.
        raise HTTPException(status_code=404, detail="No icon set for this category.")
    data, content_type = row
    return Response(content=data, media_type=content_type or "application/octet-stream")
