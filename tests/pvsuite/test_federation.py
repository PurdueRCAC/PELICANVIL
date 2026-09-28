"""Diagnostics that talk to the federation with the SAME real pelicanfs the app uses, but WITHOUT the app.
They exist to attribute a failure correctly: if a scenario fails here too, the app is not the (only) cause."""
import tempfile
import time
from pathlib import Path

import pytest

from .harness import fed

pytestmark = pytest.mark.tier("standard")


def _get(fs, path, dest):
    """The exact call sequence api/routes/pelican.py::download_one_file makes for a single file."""
    try:
        fs.isdir(path)
        fs.get(path, str(dest), recursive=True)
        return "ok"
    except Exception as e:      # noqa: BLE001
        return type(e).__name__


@pytest.mark.timeout(600)
def test_pelicanfs_direct_existing_file_after_a_404(fx, ctx):
    """pelicanfs, no app: request a missing path, then an existing one in the same namespace, 12 times.
    Controls (existing file twice in a row) are run first and must be perfect, so a failure below is
    the 404-then-good sequence, not general flakiness."""
    good, bad = fx["tiny_file"]["path"], fx["missing"]["file"]
    work = Path(tempfile.mkdtemp(dir=ctx.work_dir))
    fs = fed.fs()
    control = [_get(fs, good, work) for _ in range(6)]
    assert control == ["ok"] * 6, f"control (no 404 involved) is already failing: {control}; the federation itself looks unhealthy"
    outcomes = []
    for _ in range(12):
        _get(fs, bad, work)
        outcomes.append(_get(fs, good, work))
        time.sleep(0.3)
    failed = [o for o in outcomes if o != "ok"]
    ctx.note(f"existing file requested right after a 404 in its namespace: {len(outcomes) - len(failed)}/{len(outcomes)} ok; "
             f"failures: {sorted(set(failed))}; control 6/6 ok")
    assert not failed, (f"{len(failed)}/{len(outcomes)} requests for an EXISTING file failed ({sorted(set(failed))}) "
                        f"immediately after a 404 for a different path in the same namespace (control was 6/6 ok)")


@pytest.mark.timeout(120)
def test_osdf_filesystem_instance_caching_mechanism(ctx):
    """Documents the underlying fsspec mechanism api/routes/pelican.py::
    reset_default_filesystem() has to route around, so a library upgrade that changes
    this behavior shows up here rather than as a silent, unexplained change in the
    fix's own effectiveness. fsspec's caching metaclass (fsspec.spec._Cached) memoizes
    a filesystem instance by class + pid + constructor args/kwargs — for a class whose
    async_impl doesn't include the calling thread in that token (true for
    OSDFFileSystem constructed the normal synchronous way), plain
    `OSDFFileSystem(direct_reads=False)` called again returns the IDENTICAL cached
    instance, same aiohttp session and all. This is exactly what made the pre-fix
    reset_default_filesystem() (which just called the constructor again) a no-op.
    (Library-level identity check; no app code is imported.)"""
    from pelicanfs import OSDFFileSystem
    a = OSDFFileSystem(direct_reads=False)
    b = OSDFFileSystem(direct_reads=False)
    ctx.note(f"same object: {a is b}; shares the aiohttp session: {getattr(a, '_session', None) is getattr(b, '_session', None)}")
    assert a is b, ("OSDFFileSystem(direct_reads=False) returned a DIFFERENT instance than before — "
                    "if pelicanfs/fsspec's caching behavior has changed, api/routes/pelican.py's own "
                    "skip_instance_cache=True workaround may no longer be necessary (harmless either way, "
                    "but worth knowing)")


@pytest.mark.timeout(120)
def test_skip_instance_cache_alone_leaves_the_real_session_shared(ctx):
    """The half-fix that looked sufficient but wasn't, kept here as a regression guard
    and to document exactly why api/routes/pelican.py::reset_default_filesystem needs a
    second step. skip_instance_cache=True (an fsspec-recognized kwarg _Cached.__call__
    special-cases to skip the cache lookup/store) does make the OUTER OSDFFileSystem a
    genuinely new object — but OSDFFileSystem.__init__ separately builds its own
    fsspec.implementations.http.HTTPFileSystem internally
    (self.http_file_system = fshttp.HTTPFileSystem(...)), and THAT is an ordinary,
    separately-cached fsspec construction — skip_instance_cache is popped off by the
    caching metaclass before __init__ ever runs, so it never reaches that inner call.
    http_file_system (not the outer OSDFFileSystem) is what actually holds the aiohttp
    session behind every .get()/.isdir()/.open() call — the real download path — so two
    "genuinely different" OSDFFileSystem objects built this way still shared the exact
    session that needed resetting. Confirmed directly, not assumed."""
    from pelicanfs import OSDFFileSystem
    a = OSDFFileSystem(direct_reads=False)
    a.isdir("/pelicanplatform/test/hello-world.txt")   # force real http_file_system/session creation
    b = OSDFFileSystem(direct_reads=False, skip_instance_cache=True)
    b.isdir("/pelicanplatform/test/hello-world.txt")
    ctx.note(f"outer objects differ: {a is not b}; "
             f"http_file_system shared: {a.http_file_system is b.http_file_system}; "
             f"session shared: {a.http_file_system._session is b.http_file_system._session}")
    assert a is not b, "skip_instance_cache=True didn't even make the outer object different"
    assert a.http_file_system is b.http_file_system, (
        "http_file_system is no longer shared between skip_instance_cache instances — if pelicanfs's own "
        "__init__ has changed to pass skip_instance_cache down (or to build http_file_system some other way), "
        "api/routes/pelican.py's reset_default_filesystem may no longer need its own "
        "HTTPFileSystem.clear_instance_cache() call")


@pytest.mark.timeout(120)
def test_skip_instance_cache_plus_clearing_http_cache_gives_a_real_new_session(ctx):
    """The actual two-part fix api/routes/pelican.py::reset_default_filesystem uses:
    HTTPFileSystem.clear_instance_cache() (emptying that class's process-wide lookup
    table so the imminent inner construction inside OSDFFileSystem.__init__ misses the
    cache and builds fresh) followed by OSDFFileSystem(..., skip_instance_cache=True).
    Confirms both the http_file_system object AND the actual aiohttp session it holds
    are genuinely new — not just the outer OSDFFileSystem wrapper — and that the new
    instance still works against the real federation. (Library-level; no app code
    imported — see test_reset_default_filesystem_produces_a_working_new_session in
    test_downloads.py for the identical check through the app's own function.)"""
    from pelicanfs import OSDFFileSystem
    from fsspec.implementations.http import HTTPFileSystem
    a = OSDFFileSystem(direct_reads=False)
    a.isdir("/pelicanplatform/test/hello-world.txt")
    HTTPFileSystem.clear_instance_cache()
    b = OSDFFileSystem(direct_reads=False, skip_instance_cache=True)
    b.isdir("/pelicanplatform/test/hello-world.txt")
    ctx.note(f"http_file_system shared: {a.http_file_system is b.http_file_system}; "
             f"session shared: {a.http_file_system._session is b.http_file_system._session}")
    assert a.http_file_system is not b.http_file_system, "http_file_system is still the same object"
    assert a.http_file_system._session is not b.http_file_system._session, "the aiohttp session is still shared"
    entries = b.ls("/pelicanplatform/test", detail=True)
    assert len(entries) > 0, "the freshly constructed instance could not list a real path"
