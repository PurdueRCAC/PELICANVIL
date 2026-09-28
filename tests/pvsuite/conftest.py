"""pytest wiring for the PELICANVIL suite (run via tests/run_suite.py, which
exports the PV_* variables this reads).

  * tier selection : each test carries @pytest.mark.tier("quick|standard|deep");
                     a run at depth D executes every test whose tier <= D.
  * server modes   : any test that uses the `app` / `make_app` fixtures is
                     parametrised over PV_MODES (uvicorn; deep adds wsgi).
  * hard timeouts  : every test gets a hard timeout (@pytest.mark.timeout(s),
                     default 300s) enforced with SIGALRM (async exception on
                     Windows), so a hang becomes `FAIL [timeout]`, never a stuck job.
"""
import ctypes
import os
import signal
import threading

import pytest

from .harness import run
from .harness.api import Api
from .harness.server import ServerStartError

if "PV_RUN_DIR" not in os.environ:
    raise RuntimeError("PV_RUN_DIR is not set: run the suite through tests/run_suite.py "
                       "(or `sbatch tests/pelicanvil_tests.sbatch <depth>`).")

CTX = run.get()
TIER_RANK = {"quick": 0, "standard": 1, "deep": 2}
DEFAULT_TIMEOUT = 300


class PVTimeout(BaseException):
    """BaseException so a broad `except Exception` inside a helper can't swallow it."""


# ------------------------------------------------------------------ hooks
def pytest_configure(config):
    config.addinivalue_line("markers", "tier(name): quick | standard | deep")
    config.addinivalue_line("markers", "timeout(seconds): hard per-test timeout")


def pytest_collection_modifyitems(config, items):
    keep, drop = [], []
    for it in items:
        m = it.get_closest_marker("tier")
        tier = m.args[0] if m else "quick"
        (keep if TIER_RANK[tier] <= TIER_RANK[CTX.depth] else drop).append(it)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


_wd = {"fired": 0, "secs": 0, "timer": None}


def _fire_async(main_ident):
    _wd["fired"] += 1
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(main_ident), ctypes.py_object(PVTimeout))
    if _wd["fired"] == 1:                       # grace period for teardown
        t = threading.Timer(90, _fire_async, args=(main_ident,))
        t.daemon = True
        _wd["timer"] = t
        t.start()


def _sig_handler(signum, frame):
    _wd["fired"] += 1
    if _wd["fired"] == 1:
        signal.alarm(90)                        # grace period for teardown
    raise PVTimeout(f"hard timeout: test exceeded {_wd['secs']}s")


def _arm(secs):
    _wd.update(fired=0, secs=secs)
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _sig_handler)
        signal.alarm(int(secs))
    else:
        t = threading.Timer(secs, _fire_async, args=(threading.main_thread().ident,))
        t.daemon = True
        _wd["timer"] = t
        t.start()


def _disarm():
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
    t = _wd.get("timer")
    if t is not None:
        t.cancel()
        _wd["timer"] = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    m = item.get_closest_marker("timeout")
    _arm(m.args[0] if m else DEFAULT_TIMEOUT)
    try:
        yield
    finally:
        _disarm()


# --------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def ctx():
    return CTX


@pytest.fixture(scope="session")
def fx():
    return CTX.fx


@pytest.fixture(scope="session", params=CTX.modes, ids=lambda m: m)
def mode(request):
    return request.param


@pytest.fixture(params=range(1, CTX.reps + 1), ids=lambda i: f"rep{i}")
def rep(request):
    return request.param


def _start(server):
    try:
        server.start()
    except ServerStartError as e:
        msg = str(e)
        if "[wsgi-unavailable]" in msg:
            CTX.wsgi_unavailable = msg.splitlines()[0]
            pytest.skip("SERVER=wsgi UNAVAILABLE (waitress not installed?): " + msg.splitlines()[0])
        if "[isolation-failure]" in msg:
            pytest.exit("ISOLATION FAILURE: the launcher could not guarantee the app is using temp paths. "
                        "Aborting before anything real can be touched.\n" + msg, returncode=2)
        raise


@pytest.fixture(scope="session")
def app(mode):
    """One long-lived isolated server per server mode, shared by the simple tests."""
    s = CTX.make_server("shared", mode)
    _start(s)
    yield s
    s.stop(timeout=5)


@pytest.fixture(scope="session")
def api(app):
    return Api(app)


@pytest.fixture
def make_app(mode, request):
    """Factory for a private, freshly seeded server (kill / concurrency tests)."""
    made = []

    def _make(name=None, start=True):
        s = CTX.make_server(name or request.node.name, mode)
        made.append(s)
        if start:
            _start(s)
        return s, Api(s)
    yield _make
    for s in made:
        try:
            s.stop(timeout=3)
        except Exception:       # noqa: BLE001
            pass


@pytest.fixture
def dest(request):
    return CTX.dest(request.node.name)
