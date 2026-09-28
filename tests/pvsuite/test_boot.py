"""App boots under the isolated launcher and serves its pages."""
import pytest

pytestmark = pytest.mark.tier("quick")

PAGES = [("home", "/"), ("categories", "/datasets"), ("dataset_search", "/datasets/search"),
         ("quick_access", "/quick-access"), ("downloads", "/downloads"),
         ("documentation", "/documentation"), ("admin", "/admin")]


@pytest.mark.timeout(120)
@pytest.mark.parametrize("url", [p for _, p in PAGES], ids=[n for n, _ in PAGES])
def test_page_renders(app, api, url):
    """One result per page, so a single broken page cannot mask the others."""
    r = api.get(url)
    assert r.status_code == 200, f"GET {url} -> {r.status_code}: {r.text[:200]}"
    assert "<html" in r.text.lower(), f"GET {url} did not return an HTML page"


@pytest.mark.timeout(60)
def test_about_route_removed(app, api):
    """2026-09-28: /about was never linked from anywhere in the app (not the navbar, not
    any page) and its template never existed -- a route that was never fully wired up,
    not a regression. Removed from main.py rather than fixed; this just confirms the
    removal is clean (a real 404, not the 500 it used to be)."""
    r = api.get("/about")
    assert r.status_code == 404, f"GET /about -> {r.status_code}, expected a clean 404 now that the route is removed"


@pytest.mark.timeout(120)
def test_static_assets_and_json_endpoints(app, api, ctx):
    assert app.alive(), "server process is not running"
    for p in ("/api/static/js/downloads.js", "/api/static/js/datasets.js", "/api/static/js/quick-access.js"):
        r = api.get(p)
        assert r.status_code == 200 and len(r.text) > 500, f"static asset {p} -> {r.status_code}"
    assert api.get("/retrieve-datasets").json() == [], "fresh temp catalog should have no datasets"
    assert api.get("/retrieve-categories").json() == [], "fresh temp catalog should have no categories"
    assert api.get("/datasets/catalog").status_code == 200
    assert api.history() == [], "fresh temp downloads DB should have no history"
    ctx.note(f"mode={app.mode} port={app.port} features={ctx.features}")


@pytest.mark.timeout(60)
def test_server_uses_only_temp_paths(app, ctx):
    """The launcher's own post-import assertion passed and every path is under the scratch dir."""
    text = app.log_text()
    assert "PV-READY" in text, "launcher never reported ready"
    assert "PV-ISOLATION-FAILURE" not in text, "launcher reported an isolation failure"
    run_root = str(ctx.work_dir.resolve())
    for label, p in (("catalog db", app.db), ("downloads db", app.downloads_db),
                     ("indexing queue (scratch file)", app.queue), ("blind flag", app.blind)):
        assert str(p.resolve()).startswith(run_root), f"{label} {p} is outside the scratch dir {run_root}"
    assert app.db.exists() and app.downloads_db.exists()
