from fastapi import FastAPI, Request, HTTPException
import logging
import os
from datetime import date
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from api.routes.dataset import datasetRouter, buildCatalog
from api.routes.pelican import pelicanRouter
from api.routes.local import localRouter
from api.routes.database import dbRouter
from api.routes.downloads import downloadsRouter
from api.routes.token_auth import tokenAuthRouter
from api.routes.indexing import indexingRouter
from api.routes.blind_mode import blindModeRouter
from scripts.migrate_category_icons import migrate_category_icons

from api.auth import is_authorized
from api.core.blind_mode import is_blind_mode
from api.core.git_info import get_git_version_info

USER = os.environ.get("USER")

# Backfills the DB-backed category icon columns from each category's legacy
# static-file icon (see scripts/migrate_category_icons.py's own docstring)
# so existing categories keep their real icons with no manual admin
# action. Runs once per process start (every PUN launch, not just a true
# one-time deploy step — this is a multi-process-per-user deployment with
# no single global "run once" hook) — safe because it's idempotent, same
# "lazy migration on every start" shape as downloads.py's _init_db().
# Best-effort: a hiccup here shouldn't prevent the app from serving,
# mirroring api/routes/database.py's own best-effort auto-indexing trigger.
try:
    migrate_category_icons()
except Exception:
    logging.getLogger("pelican-ui.startup").exception("Category icon migration failed at startup")

app = FastAPI()
# Computed once here rather than per-request — see get_git_version_info's
# docstring-equivalent comment for why startup-time caching is correct
# for this Passenger-restart deployment model.
app.state.git_version = get_git_version_info()
app.include_router(datasetRouter)
app.include_router(pelicanRouter)
app.include_router(localRouter)
app.include_router(dbRouter)
app.include_router(downloadsRouter)
app.include_router(tokenAuthRouter)
app.include_router(indexingRouter)
app.include_router(blindModeRouter)

app.mount("/api/static", StaticFiles(directory="api/static"), name="static")


def _blind_mode_context(request: Request) -> dict:
    # Read fresh on every render (not cached at startup) so the admin
    # toggle takes effect on the very next page load, for every page,
    # without touching each individual route handler below to pass it
    # through by hand.
    return {"BLIND_MODE": is_blind_mode()}


templates = Jinja2Templates(directory="api/templates", context_processors=[_blind_mode_context])




@app.get('/datasets', response_class=HTMLResponse)
async def dataset_page(request: Request):
    return templates.TemplateResponse(request,'categories.html', {"ROOT_URL": request.scope.get('root_path', '')})

@app.get('/', response_class=HTMLResponse)
async def main_page(request: Request):
    # dataset_count/category_count/category_names come from the same
    # dataset<->category tag matching /datasets/catalog already exposes —
    # two small SELECT * queries, cheap enough to run on every homepage
    # load. If the dataset table grows large enough for this to matter,
    # revisit once the separate local-indexing/caching item is built.
    catalog = buildCatalog()
    return templates.TemplateResponse(request, "index.html", {
        "ROOT_URL": request.scope.get('root_path', ''),
        "dataset_count": catalog["dataset_count"],
        "category_count": catalog["category_count"],
        "category_names": catalog["category_names"],
        "today": date.today().strftime("%B %d, %Y"),
    })

@app.get('/quick-access', response_class=HTMLResponse)
async def main_page(request: Request):
    return templates.TemplateResponse(request, "quick-access.html", {"ROOT_URL": request.scope.get('root_path', ''),  "USER": USER})

@app.get('/about', response_class=HTMLResponse)
async def main_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {"ROOT_URL": request.scope.get('root_path', '')})

@app.get('/documentation', response_class=HTMLResponse)
async def documentation_page(request: Request):
    return templates.TemplateResponse(request, "docs.html", {"ROOT_URL": request.scope.get('root_path', '')})

@app.get('/datasets/search', response_class=HTMLResponse)
async def dataset_search_page(request: Request):
    return templates.TemplateResponse(request, "datasets.html", {"ROOT_URL": request.scope.get('root_path', ''), "CATEGORY": "", "USER": USER})

@app.get('/datasets/category/{category}')
async def category_page(category, request: Request):
    return templates.TemplateResponse(request, "datasets.html", {"ROOT_URL": request.scope.get('root_path', ''), "CATEGORY": category, "USER": USER})

@app.get('/admin', response_class=HTMLResponse)
async def admin_page(request: Request):
    if not is_authorized(USER):
        raise HTTPException(status_code=403, detail="Not Authorized")
    return templates.TemplateResponse(request, "admin.html", {"ROOT_URL": request.scope.get('root_path', ''),  "USER": USER, "GIT_VERSION": app.state.git_version})

@app.get('/downloads', response_class=HTMLResponse)
async def downloads_page(request: Request):
    return templates.TemplateResponse(request, "downloads.html", {"ROOT_URL": request.scope.get('root_path', '')})