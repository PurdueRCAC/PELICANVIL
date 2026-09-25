"""Subprocess entry point that runs the REAL PELICANVIL app against an isolated
temp DB / HOME, with no production-code changes.

Why this works: every module does `from api.core.config import DB_PATH` (etc.)
at import time, and nothing imports config before this file does (all package
__init__.py files are empty). So we import api.core.config FIRST, overwrite the
four path attributes, and only then import `main` (or `passenger_wsgi`). Every
`from ... import NAME` executed afterwards binds the patched value. main's
import also runs migrate_category_icons(), downloads._init_db() and
downloads._recover_interrupted_jobs() -- all against the temp paths.

Safety net: after the import we assert that every module that captured a path
holds the temp value, and refuse to serve otherwise (exit 97). We also refuse
to run at all if a patched path is not under --run-root.

  python launcher.py --repo R --mode uvicorn|wsgi --port P --run-root RR \
      --db D --downloads-db DD --queue Q --blind B
"""
import argparse
import logging
import os
import sys


def _under(path, root):
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mode", choices=["uvicorn", "wsgi"], default="uvicorn")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--downloads-db", required=True)
    ap.add_argument("--queue", required=True)
    ap.add_argument("--blind", required=True)
    a = ap.parse_args()

    for label, p in (("db", a.db), ("downloads-db", a.downloads_db), ("queue", a.queue), ("blind", a.blind)):
        if not _under(p, a.run_root):
            print(f"PV-ISOLATION-FAILURE: --{label} {p} is not under run root {a.run_root}", file=sys.stderr)
            sys.exit(97)

    os.chdir(a.repo)                       # the app uses cwd-relative api/static, api/templates
    sys.path.insert(0, a.repo)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s",
    )

    # Diagnostics only (does not touch app code): the app never logs a plain not_found, and we need to see
    # director/cache selection when a failure cannot be reproduced on demand.
    logging.getLogger("fsspec.pelican").setLevel(logging.DEBUG)

    import api.core.config as cfg          # noqa: E402  (must precede every other api.* import)

    cfg.DB_PATH = a.db
    cfg.DOWNLOADS_DB_PATH = a.downloads_db
    cfg.INDEXING_QUEUE_PATH = a.queue
    cfg.BLIND_MODE_PATH = a.blind

    if a.mode == "wsgi":
        # Exactly what Passenger loads: passenger_wsgi.application is
        # a2wsgi.ASGIMiddleware(main.app). We only supply the WSGI server.
        try:
            import waitress
        except ImportError:
            print("PV-WSGI-UNAVAILABLE: waitress is not installed in this environment", file=sys.stderr)
            sys.exit(98)
        import passenger_wsgi              # noqa: E402
        app_obj = passenger_wsgi.application
    else:
        import main                        # noqa: E402
        app_obj = main.app

    _assert_isolated(a)
    print(f"PV-READY mode={a.mode} port={a.port} db={a.db}", file=sys.stderr, flush=True)

    if a.mode == "wsgi":
        waitress.serve(app_obj, host=a.host, port=a.port, threads=8, ident="pelicanvil-test", _quiet=True)
    else:
        import uvicorn
        uvicorn.run(app_obj, host=a.host, port=a.port, log_level="info", access_log=True)


def _assert_isolated(a):
    """Every module-level capture of a path must equal the temp value."""
    import importlib
    checks = [
        ("api.auth", "DB_PATH", a.db), ("api.routes.database", "DB_PATH", a.db),
        ("api.routes.dataset", "DB_PATH", a.db), ("api.routes.pelican", "DB_PATH", a.db),
        ("api.routes.indexing", "DB_PATH", a.db),
        ("api.routes.downloads", "DOWNLOADS_DB_PATH", a.downloads_db),
        ("api.core.indexing_queue", "INDEXING_QUEUE_PATH", a.queue),
        ("scripts.migrate_category_icons", "DB_PATH", a.db),
    ]
    bad = []
    for mod, attr, want in checks:
        m = sys.modules.get(mod)
        if m is None or not hasattr(m, attr):
            continue                       # older commits may lack a module/attr
        if os.path.abspath(getattr(m, attr)) != os.path.abspath(want):
            bad.append(f"{mod}.{attr}={getattr(m, attr)!r}")
    bm = sys.modules.get("api.core.blind_mode")
    if bm is not None and hasattr(bm, "_FLAG_PATH") and os.path.abspath(str(bm._FLAG_PATH)) != os.path.abspath(a.blind):
        bad.append(f"api.core.blind_mode._FLAG_PATH={bm._FLAG_PATH!r}")
    pa = sys.modules.get("api.core.pelican_auth")
    if pa is not None:
        for attr in ("TOKEN_DIR", "LAST_ERROR_LOG"):
            v = getattr(pa, attr, None)
            if v is not None and not _under(str(v), a.run_root):
                bad.append(f"api.core.pelican_auth.{attr}={v!r}")
    if bad:
        print("PV-ISOLATION-FAILURE: " + "; ".join(bad), file=sys.stderr, flush=True)
        os._exit(97)


if __name__ == "__main__":
    main()
