"""Restart flow: whole-record and per-file semantics, plus the unwritable-destination failure."""
import os

import pytest

from .harness import dbutil, env, jobs
from .helpers import assert_single_file, block_writes, run_job, unblock_writes

pytestmark = pytest.mark.tier("standard")


@pytest.mark.timeout(600)
def test_unwritable_destination_then_restart_succeeds(api, app, dest, fx):
    """A real local write failure (chmod'd directory) is classified `permission`;
    after the permissions are fixed, the Restart endpoint recovers the same record."""
    tiny = fx["tiny_file"]
    block_writes(dest)
    try:
        job, st = run_job(api, dest, [tiny["path"]])
        f = st["files"][0]
        assert st["status"] == "failed" and f["status"] == "failed", st
        assert f.get("error_category") == "permission", f"category {f.get('error_category')!r} error={f.get('error')!r}"
        assert "permission" in f["error"].lower(), f["error"]
        assert not env.dir_tree(dest)
    finally:
        unblock_writes(dest)

    r = api.restart_record(job["history_id"])
    assert r.status_code == 200, f"restart -> {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body["history_id"] == job["history_id"] and body["job_id"] != job["job_id"], body
    job2 = dict(job, job_id=body["job_id"])
    st2 = jobs.wait_terminal(api, job2, timeout=180)
    assert st2["status"] == "complete" and [f["status"] for f in st2["files"]] == ["succeeded"], st2
    assert_single_file(dest, tiny)
    row = api.history_row(job["history_id"])
    assert row["status"] == "complete" and row["error_message"] is None and row["job_id"] == body["job_id"], row
    assert len([j for j in dbutil.jobs(app) if j["history_id"] == job["history_id"]]) == 1, "expected exactly one job row after restart"


@pytest.mark.timeout(900)
def test_restart_file_retries_only_that_file_then_restart_all(api, dest, fx):
    tiny, small, nested = fx["tiny_file"], fx["small_file"], fx["nested_dir"]
    paths = [tiny["path"], small["path"], nested["path"]]      # a plain directory, not the +name file (that has its own tests)
    block_writes(dest)
    try:
        job, st = run_job(api, dest, paths)
        assert st["status"] == "failed" and all(f["status"] == "failed" for f in st["files"]), st
        assert all(f.get("error_category") == "permission" for f in st["files"]), [f.get("error_category") for f in st["files"]]
    finally:
        unblock_writes(dest)
    hid = job["history_id"]

    # a path that isn't recorded as failed, and an unknown record, are rejected
    assert api.restart_file(hid, "/not/in/this/download").status_code == 400
    assert api.restart_file(987654321, small["path"]).status_code == 404

    r = api.restart_file(hid, small["path"])
    assert r.status_code == 200, f"restart-file -> {r.status_code}: {r.text[:300]}"
    st2 = jobs.wait_terminal(api, dict(job, job_id=r.json()["job_id"]), timeout=180)
    by = jobs.files_by_path(st2)
    assert st2["status"] == "partial", f"status {st2['status']!r}"
    assert by[small["path"]]["status"] == "succeeded"
    assert by[tiny["path"]]["status"] == "failed" and by[nested["path"]]["status"] == "failed", \
        "restart-file must leave the OTHER failed files failed (not retry or clear them)"
    assert by[tiny["path"]].get("error_category") == "permission", "preserved failure lost its reason"
    assert_single_file(dest, small, "small (restarted)")
    assert jobs.find_file(dest, "hello-world.txt") is None and not (dest / "metadata").exists(), \
        "restart-file downloaded files it was not asked to"

    # whole-record restart retries every still-failed file and keeps the succeeded one untouched
    mtime = jobs.find_file(dest, "beliefbank-data-sep2021.zip").stat().st_mtime_ns
    r = api.restart_record(hid)
    assert r.status_code == 200, r.text[:300]
    st3 = jobs.wait_terminal(api, dict(job, job_id=r.json()["job_id"]), timeout=240)
    assert st3["status"] == "complete" and len(st3["files"]) == 3 and all(f["status"] == "succeeded" for f in st3["files"]), st3
    assert_single_file(dest, tiny, "tiny")
    jobs.assert_tree(dest / "metadata", {f["path"]: f["size"] for f in nested["files"]},
                     {f["path"]: f["sha256"] for f in nested["files"]}, label="nested dir after whole-record restart")
    assert jobs.find_file(dest, "beliefbank-data-sep2021.zip").stat().st_mtime_ns == mtime, \
        "a file that had already succeeded was downloaded again by the whole-record restart"
    assert api.restart_record(hid).status_code == 400, "a complete record must not be restartable"


@pytest.mark.timeout(600)
def test_restart_of_partial_keeps_succeeded_file_untouched(api, dest, fx):
    """Works everywhere (no chmod): one good file + one path that will never exist."""
    good, bad = fx["tiny_file"], fx["missing"]["file"]
    job, st = run_job(api, dest, [good["path"], bad])
    assert st["status"] == "partial", st["status"]
    p = assert_single_file(dest, good, "good")
    before = p.stat().st_mtime_ns
    r = api.restart_record(job["history_id"])
    assert r.status_code == 200 and r.json()["history_id"] == job["history_id"], r.text[:300]
    st2 = jobs.wait_terminal(api, dict(job, job_id=r.json()["job_id"]), timeout=180)
    by = jobs.files_by_path(st2)
    assert st2["status"] == "partial", f"still-failing item should keep the record partial, got {st2['status']!r}"
    assert by[good["path"]]["status"] == "succeeded" and by[bad]["status"] == "failed" and by[bad].get("error"), by
    assert p.stat().st_mtime_ns == before, "the already-succeeded file was re-downloaded on restart"
    row = api.history_row(job["history_id"])
    assert row["item_count"] == 2 and len(row["files"]) == 2, row
    assert api.restart_record(987654321).status_code == 404
