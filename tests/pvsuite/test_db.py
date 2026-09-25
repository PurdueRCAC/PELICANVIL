"""Basic DB behaviour, driven through the API and verified in the app's SQLite files."""
import time

import pytest

from .harness import dbutil, jobs
from .helpers import assert_job_ok, assert_single_file

pytestmark = pytest.mark.tier("quick")


@pytest.mark.timeout(240)
def test_schema_init_is_idempotent(make_app):
    """Every process start runs downloads._init_db() at import; restarting many
    times against the same DB must change nothing and lose nothing."""
    s, api = make_app("idempotent")
    before = dbutil.schema(s.downloads_db)
    assert {"download_history", "download_jobs"} <= set(before["columns"]), before["columns"]
    assert "files" in before["columns"]["download_history"], "download_history is missing the files column"
    assert dbutil.integrity(s.downloads_db) == "ok"
    assert dbutil.query(s.downloads_db, "PRAGMA journal_mode")[0]["journal_mode"].lower() == "wal", "expected WAL mode"
    for i in range(2):
        s.stop()
        s.start()
        after = dbutil.schema(s.downloads_db)
        assert after == before, f"schema changed after restart #{i + 1}"
        assert dbutil.history(s) == [] and dbutil.jobs(s) == []
    assert api.history() == []


@pytest.mark.timeout(240)
def test_history_and_job_rows_lifecycle(api, app, dest, fx):
    """created -> updated -> listed -> deleted, through the same endpoints the UI uses."""
    tiny = fx["tiny_file"]
    job = jobs.start(api, dest, [tiny["path"]], name="pv lifecycle")
    hid, jid = job["history_id"], job["job_id"]

    # created: both rows exist immediately, and the list endpoint joins them
    row = api.history_row(hid)
    assert row is not None and row["job_id"] == jid, f"history row missing or not joined to job: {row}"
    assert row["name"] == "pv lifecycle" and row["destination"] == str(dest) and row["item_count"] == 1
    assert row["status"] in ("in_progress", "complete"), row["status"]
    jrow = dbutil.job(app, jid)
    assert jrow is not None and jrow["history_id"] == hid and jrow["status"] in ("pending", "in_progress", "complete")

    st = jobs.wait_terminal(api, job, timeout=180)
    assert_job_ok(st, 1)
    assert_single_file(dest, tiny)

    # updated: terminal state agrees across API, history table and job table
    row = api.history_row(hid)
    assert row["status"] == "complete" and row["finished_at"] and row["error_message"] is None
    assert row["files"] == [{"path": tiny["path"], "status": "succeeded"}], row["files"]
    jrow = dbutil.job(app, jid)
    assert jrow["status"] == "complete" and jrow["error_message"] is None
    assert jrow["updated_at"] >= jrow["started_at"]
    h_db = [h for h in dbutil.history(app) if h["id"] == hid]
    assert len(h_db) == 1 and h_db[0]["status"] == "complete"

    # deleted: rows gone, status endpoint 404s, second delete 404s, unknown id 404s
    r = api.delete_record(hid)
    assert r.status_code == 200 and r.json() == {"status": "success"}, (r.status_code, r.text)
    assert api.history_row(hid) is None
    assert dbutil.job(app, jid) is None and not [h for h in dbutil.history(app) if h["id"] == hid]
    assert api.status(jid).status_code == 404
    assert api.delete_record(hid).status_code == 404
    assert api.delete_record(987654321).status_code == 404


@pytest.mark.tier("standard")
@pytest.mark.timeout(240)
def test_catalog_crud_through_admin_routes(api, app, ctx):
    """The shared-catalog DB (temp copy): dataset / category / user CRUD. Adding a
    dataset also calls enqueue_indexing_request, whose only effect is appending to
    INDEXING_QUEUE_PATH -- which the launcher points at a scratch file in the run dir."""
    ds = {"name": "PV suite dataset", "description": "temp", "path": "/pelicanplatform/test", "format": "txt",
          "streamable": False, "access": "public", "tags": ["pv-cat"]}
    assert api.request("POST", "/admin/add-category", json={"name": "PV cat", "url": "pv-cat", "description": "d"}).status_code == 200
    assert api.request("POST", "/admin/add-category", json={"name": "PV cat", "url": "pv-cat", "description": "d"}).status_code == 409
    r = api.request("POST", "/admin/add-dataset", json=ds)
    assert r.status_code == 200, f"add-dataset -> {r.status_code}: {r.text[:300]}"
    assert api.request("POST", "/admin/add-dataset", json=ds).status_code == 409, "duplicate path must be rejected"
    rows = [d for d in api.get("/retrieve-datasets").json() if d["path"] == ds["path"]]
    assert len(rows) == 1 and rows[0]["name"] == ds["name"], rows
    did = rows[0]["id"]
    cat = api.get("/datasets/catalog").json()
    assert cat["dataset_count"] == 1 and cat["category_count"] == 1, cat

    ds2 = dict(ds, name="PV suite dataset (renamed)")
    assert api.request("POST", "/admin/modify-dataset", params={"dataset_id": did}, json=ds2).status_code == 200
    assert api.get("/retrieve-datasets").json()[0]["name"] == "PV suite dataset (renamed)"
    assert api.request("POST", "/admin/modify-dataset", params={"dataset_id": 99999}, json=ds2).status_code == 404

    assert api.request("POST", "/admin/add-user", params={"user": "pv-user"}).status_code == 200
    assert api.request("POST", "/admin/add-user", params={"user": "pv-user"}).status_code == 409
    assert "pv-user" in [u["name"] for u in api.get("/admin/retrieve-users").json()]
    assert api.request("POST", "/admin/remove-user", params={"user": "pv-user"}).status_code == 200
    assert api.request("POST", "/admin/remove-user", params={"user": "pv-user"}).status_code == 404

    assert api.request("POST", "/admin/remove-dataset", params={"dataset_id": did}).status_code == 200
    assert api.request("POST", "/admin/remove-dataset", params={"dataset_id": did}).status_code == 404
    assert api.request("POST", "/admin/remove-category", params={"category_url": "pv-cat"}).status_code == 200
    assert api.get("/retrieve-datasets").json() == []

    # the indexing side effect stayed inside the run dir (nothing real was touched)
    assert str(app.queue.resolve()).startswith(str(ctx.work_dir.resolve()))
    ctx.note(f"indexing queue scratch file exists={app.queue.exists()} at {app.queue}")
