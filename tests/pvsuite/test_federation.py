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
def test_reset_shaped_construction_yields_a_fresh_filesystem(ctx):
    """api/routes/pelican.py::reset_default_filesystem() 'resets' by constructing
    OSDFFileSystem(direct_reads=False) again. If that returns the SAME object (fsspec caches
    instances by their arguments), nothing is reset: the aiohttp session, the per-namespace cache
    lists and every cache already marked bad all survive, so a reset-then-retry cannot recover from
    stale-session or bad-cache state. (Library-level identity check; no app code is imported.)"""
    from pelicanfs import OSDFFileSystem
    a = OSDFFileSystem(direct_reads=False)
    b = OSDFFileSystem(direct_reads=False)
    ctx.note(f"same object: {a is b}; shares the aiohttp session: {getattr(a, '_session', None) is getattr(b, '_session', None)}")
    assert a is not b, ("OSDFFileSystem(direct_reads=False) returned the identical cached instance: "
                        "constructing it again (as reset_default_filesystem() does) does not reset anything")
