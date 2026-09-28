"""Download behaviour against the real federation, through the app's endpoints."""
import shutil
import time
from pathlib import Path

import pytest

from .harness import dbutil, env, jobs
from .harness.run import KNOWN_CATEGORIES
from .helpers import assert_job_ok, assert_single_file, base, run_job, wait_history_terminal

Q = pytest.mark.tier("quick")
S = pytest.mark.tier("standard")


# --------------------------------------------------------------------- quick
@Q
@pytest.mark.timeout(240)
def test_small_file_downloads_with_correct_sha256(api, dest, fx):
    entry = fx["small_file"]
    job, st = run_job(api, dest, [entry["path"]])
    assert_job_ok(st, 1)
    assert_single_file(dest, entry, label="small_file")


@Q
@pytest.mark.timeout(240)
def test_real_404_reports_not_found_with_reason(api, app, dest, fx):
    missing = fx["missing"]["file"]
    job, st = run_job(api, dest, [missing])
    assert st["status"] == "failed", f"status {st['status']!r}"
    f = st["files"][0]
    assert f["status"] == "failed" and f.get("error"), f"failed file has no reason: {f}"
    assert f.get("error_category") == "not_found", f"category {f.get('error_category')!r} (error: {f.get('error')!r})"
    assert not f.get("auth_required"), "a 404 must not be flagged auth_required"
    assert st["error_message"] == "1 of 1 item(s) failed to download.", st["error_message"]
    row = api.history_row(job["history_id"])
    assert row["status"] == "failed" and row["files"][0].get("error") and row["files"][0].get("error_category") == "not_found", row
    assert not env.dir_tree(dest), f"a failed download left files behind: {sorted(env.dir_tree(dest))}"


@Q
@pytest.mark.timeout(300)
def test_token_required_namespace_without_a_token(api, app, dest, fx):
    ns = fx["protected"]["namespaces"][0]
    assert not app.token_dir.exists() or not any(app.token_dir.iterdir()), "a token already exists in the temp home"
    assert api.token_status(ns).json() == {"has_token": False}
    assert shutil.which("pelican", path=env.sanitized_env(app.home)["PATH"]) is None, \
        "the pelican binary is on the app's PATH; pelicanfs could start an interactive OIDC flow"

    r = api.list_path(ns)
    assert r.status_code == 401, f"list-path {ns} -> {r.status_code}: {r.text[:300]}"
    detail = r.json()["detail"]
    assert detail.get("error") == "auth_required" and detail.get("namespace") == ns, detail

    t0 = time.time()
    job, st = run_job(api, dest, [ns], timeout=240)
    f = st["files"][0]
    assert st["status"] == "failed" and f["status"] == "failed", st
    assert f.get("error_category") == "auth_required", f"category {f.get('error_category')!r}: {f.get('error')!r}"
    assert f.get("auth_required") is True and f.get("namespace") == ns, f
    assert time.time() - t0 < 120, "auth-required detection took suspiciously long (interactive token flow?)"
    assert not app.token_dir.exists() or not any(app.token_dir.iterdir()), "the app saved a token it was never given"
    assert not env.dir_tree(dest)


# ------------------------------------------------------------------ standard
@S
@pytest.mark.timeout(300)
def test_two_files_in_one_job(api, dest, fx):
    a, b = fx["tiny_file"], fx["small_file"]
    job, st = run_job(api, dest, [a["path"], b["path"]])
    assert_job_ok(st, 2)
    assert_single_file(dest, a, "tiny")
    assert_single_file(dest, b, "small")


@S
@pytest.mark.timeout(600)
def test_nested_directory_produces_exactly_the_expected_tree(api, dest, fx):
    """Regression guard for issue #3: no stray host-named folders, no HTML listing
    saved as a file, nothing missing."""
    nd = fx["nested_dir"]
    job, st = run_job(api, dest, [nd["path"]], timeout=480)
    assert_job_ok(st, 1)
    root = dest / base(nd["path"])
    expected = {f["path"]: f["size"] for f in nd["files"]}
    sha = {f["path"]: f["sha256"] for f in nd["files"]}
    jobs.assert_tree(root, expected, sha, label="nested_dir")
    stray = set(env.dir_tree(dest)) - {f"{base(nd['path'])}/{k}" for k in expected}
    assert not stray, f"files outside the expected tree under the destination: {sorted(stray)[:10]}"
    assert not [p for p in Path(dest).rglob("*.html")], "an .html file appeared (raw directory listing saved as a file?)"


@S
@pytest.mark.timeout(120)
def test_plus_name_appears_in_listing(api, fx):
    sp = fx["special_name_file"]
    r = api.list_path(sp["parent"])
    assert r.status_code == 200, f"list-path -> {r.status_code}: {r.text[:300]}"
    hits = [e for e in r.json() if e["name"].rstrip("/").rsplit("/", 1)[-1] == sp["listed_name"]]
    assert len(hits) == 1, f"expected exactly one entry named {sp['listed_name']!r}, got {[e['name'] for e in r.json()]}"
    assert hits[0]["type"] == "file" and int(hits[0]["size"]) == sp["size"], hits[0]


@S
@pytest.mark.timeout(300)
def test_plus_name_downloads_via_the_listed_path(api, dest, fx):
    """The browse -> tick -> Download flow sends the path exactly as listed (name with '+')."""
    sp = fx["special_name_file"]
    listed = next(e for e in api.list_path(sp["parent"]).json() if e["name"].endswith(sp["listed_name"]))["name"]
    job, st = run_job(api, dest, [listed])
    assert_job_ok(st, 1)
    assert_single_file(dest, {"path": listed, "size": sp["size"], "sha256": sp["sha256"]}, "plus-name file")


@S
@pytest.mark.timeout(120)
def test_reset_default_filesystem_produces_a_working_new_session(ctx, fx):
    """The actual fix in api/routes/pelican.py::reset_default_filesystem — imports the
    APP's own function (in a subprocess against ctx.app_dir, so this stays correct for a
    before/after run against a different commit's checkout via APP_DIR) and confirms more
    than outer-object identity: the REAL aiohttp session (osdf.http_file_system._session
    — the thing that actually backs every .get()/.isdir() call, not the outer
    OSDFFileSystem wrapper) genuinely differs after a reset, the OLD instance keeps
    working (a concurrent caller mid-request must not be broken by a reset happening
    elsewhere), and the NEW instance — the one _resolve_filesystem hands out to everyone
    from now on — successfully completes a real download. See test_federation.py's
    test_osdf_filesystem_instance_caching_mechanism /
    test_skip_instance_cache_alone_leaves_the_real_session_shared /
    test_skip_instance_cache_plus_clearing_http_cache_gives_a_real_new_session for the
    library-level mechanism this exercises through the app's own code path."""
    import subprocess
    import sys
    script = f'''
import os, sys, tempfile, pathlib
sys.path.insert(0, {str(ctx.app_dir)!r})
os.chdir({str(ctx.app_dir)!r})
for v in ("BEARER_TOKEN", "BEARER_TOKEN_FILE", "TOKEN", "_CONDOR_CREDS"):
    os.environ.pop(v, None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="pv-reset-check-")
import api.routes.pelican as pelican_mod
old_fs = pelican_mod.osdf
old_fs.isdir({fx["tiny_file"]["path"]!r})  # force real http_file_system/session creation
old_http, old_session = old_fs.http_file_system, old_fs.http_file_system._session
pelican_mod.reset_default_filesystem()
new_fs = pelican_mod.osdf
assert new_fs is not old_fs, "reset_default_filesystem returned the SAME outer object"
new_fs.isdir({fx["tiny_file"]["path"]!r})
new_http, new_session = new_fs.http_file_system, new_fs.http_file_system._session
assert new_http is not old_http, "http_file_system (the real download-path session holder) is STILL SHARED"
assert new_session is not old_session, "the aiohttp session is STILL SHARED"
old_fs.ls({fx["tiny_file"]["path"]!r})  # old instance must still work post-reset
dest = tempfile.mkdtemp(prefix="pv-reset-check-dl-")
pelican_mod.download_one_file({fx["tiny_file"]["path"]!r}, dest)
got = list(pathlib.Path(dest).rglob("*"))
assert any(p.is_file() and p.stat().st_size == {fx["tiny_file"]["size"]} for p in got), got
print("PV_RESET_CHECK_OK")
'''
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=90,
                       cwd=str(ctx.app_dir))
    assert "PV_RESET_CHECK_OK" in r.stdout, (
        f"reset_default_filesystem check failed (exit {r.returncode})\nstdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}")


@S
@pytest.mark.timeout(300)
def test_destination_path_with_space_and_plus(api, dest, fx):
    d = dest / "my data+1 (pv test)"
    d.mkdir()
    entry = fx["tiny_file"]
    job, st = run_job(api, d, [entry["path"]])
    assert_job_ok(st, 1)
    assert_single_file(d, entry, "tiny in odd destination")
    assert api.history_row(job["history_id"])["destination"] == str(d)


@S
@pytest.mark.timeout(400)
def test_mixed_good_and_bad_batch_is_partial(api, app, dest, fx):
    good1, bad, good2 = fx["tiny_file"], fx["missing"]["file"], fx["small_file"]
    job, st = run_job(api, dest, [good1["path"], bad, good2["path"]])
    assert st["status"] == "partial", f"status {st['status']!r}"
    by = jobs.files_by_path(st)
    assert by[good1["path"]]["status"] == "succeeded" and by[good2["path"]]["status"] == "succeeded"
    assert by[bad]["status"] == "failed" and by[bad].get("error") and by[bad].get("error_category") == "not_found", by[bad]
    assert st["error_message"] == "1 of 3 item(s) failed to download.", st["error_message"]
    assert_single_file(dest, good1, "good1")
    assert_single_file(dest, good2, "good2")
    # failure reasons persisted in download_history.files and served by the history endpoint.
    # wait_terminal above only guarantees the JOB (download_jobs) reached a terminal status --
    # the HISTORY row is a separate, later write (_finish_history_record, api/routes/downloads.py)
    # that can still be a beat behind for a batch this fast; poll for it too rather than
    # asserting on a single read taken right after wait_terminal returns.
    api_row = wait_history_terminal(api, job["history_id"])
    assert api_row is not None and api_row["status"] == "partial", f"history row never reached 'partial': {api_row}"
    for row in (api_row, [h for h in dbutil.history(app) if h["id"] == job["history_id"]][0]):
        assert row["status"] == "partial" and row["item_count"] == 3, row
        hb = {f["path"]: f for f in row["files"]}
        assert hb[good1["path"]]["status"] == "succeeded" and hb[good2["path"]]["status"] == "succeeded"
        assert hb[bad]["status"] == "failed" and hb[bad].get("error") and hb[bad].get("error_category") == "not_found", hb[bad]


@S
@pytest.mark.timeout(400)
def test_missing_directory_and_missing_nested_file_are_not_found(api, dest, fx):
    a, b = fx["missing"]["dir"], fx["missing"]["nested_file"]
    job, st = run_job(api, dest, [a, b])
    assert st["status"] == "failed", st["status"]
    for f in st["files"]:
        assert f["status"] == "failed" and f.get("error"), f
        assert f.get("error_category") == "not_found", f"{f['path']}: category {f.get('error_category')!r} ({f.get('error')!r})"
    assert not env.dir_tree(dest)


@S
@pytest.mark.timeout(400)
def test_nonexistent_namespace_is_reported_not_hung(api, dest, fx, ctx):
    """Behaviour is RECORDED, not dictated: a path in a namespace that does not exist
    makes the director answer badly (BadDirectorResponse). We require a terminal state
    with a non-empty reason and a known category, and report what actually happened."""
    p = fx["missing"]["namespace_file"]
    t0 = time.time()
    job, st = run_job(api, dest, [p], timeout=300)
    f = st["files"][0]
    assert st["status"] == "failed" and f["status"] == "failed", st
    assert f.get("error"), "no reason recorded"
    assert f.get("error_category") in KNOWN_CATEGORIES, f.get("error_category")
    ctx.note(f"nonexistent namespace -> category={f.get('error_category')!r} error={f.get('error')!r} "
             f"after {time.time() - t0:.1f}s")


@S
@pytest.mark.timeout(240)
def test_large_directory_listing_flags_truncation_instead_of_hiding_it(api, fx, ctx):
    """A real directory here holds 1919 files (S3 ground truth, pinned in fixtures.yaml).
    Investigated 2026-09-28: the app's list-path returns exactly 1000 of them, and that
    cap is the ORIGIN/cache server's own PROPFIND response limit (XRootD's webdav plugin —
    confirmed by issuing the same PROPFIND directly and reading a complete, well-formed,
    non-error HTTP 207 response with no truncation signal of its own), not something
    PELICANVIL, pelicanfs, or aiowebdav2 adds — see api/routes/pelican.py's
    LISTING_TRUNCATION_SUSPECT_COUNT. There is no way for this app to actually return all
    1919 entries; what it can and must do is say so rather than silently pretending the
    listing is complete, via the X-Listing-Truncated response header."""
    bl = fx["big_listing"]
    r = api.list_path(bl["path"])
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    n = len(r.json())
    ctx.note(f"list-path returned {n} entries; true count {bl['true_entries']}")
    assert n == bl["observed_entries_at_pin_time"], (
        f"listing returned {n} entries, expected the known origin cap of {bl['observed_entries_at_pin_time']} — "
        f"either the origin server's behavior changed, or (if n == {bl['true_entries']}) it's genuinely fixed upstream")
    assert r.headers.get("X-Listing-Truncated") == "1", (
        f"listing hit the known truncation cap ({n} entries) but the response never said so via "
        f"X-Listing-Truncated — a caller has no way to know {bl['true_entries'] - n} entries are missing")


@S
@pytest.mark.timeout(900)
def test_existing_file_after_a_404_in_the_same_namespace_still_downloads(api, ctx, fx):
    """One genuinely missing item followed by a genuinely existing one in the SAME namespace (exactly
    what a batch with one stale path looks like). Every repetition must download the good file.
    Found while building the suite: pelicanfs marks the cache that answered a 404 as bad, and the very
    next request for an EXISTING file in that namespace then frequently fails with a false not_found
    (see test_federation for the same sequence without the app). No automatic retry: the rate is the signal."""
    good, bad = fx["tiny_file"], fx["missing"]["file"]         # both under /pelicanplatform/test
    n, failures = 8, []
    for i in range(n):
        d = ctx.dest(f"after404-{i}")
        job, st = run_job(api, d, [bad, good["path"]])
        got = jobs.files_by_path(st)[good["path"]]
        if got["status"] != "succeeded":
            failures.append(f"round {i + 1}: {got.get('error_category')}: {got.get('error')}")
        else:
            assert_single_file(d, good, f"round {i + 1}")
    ctx.note(f"{n - len(failures)}/{n} rounds downloaded the existing file after a 404 for its neighbour")
    assert not failures, (f"{len(failures)}/{n} rounds FAILED to download an existing file that followed a 404 "
                          f"in the same namespace: {failures[:3]}")
