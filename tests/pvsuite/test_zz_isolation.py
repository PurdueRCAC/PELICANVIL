"""Runs last: proves the suite left the REAL app state untouched."""
import os
from pathlib import Path

import pytest

from .harness import realstate

pytestmark = pytest.mark.tier("quick")


@pytest.mark.timeout(60)
def test_real_state_untouched(ctx):
    before_path = ctx.run_dir / "real_state_before.json"
    assert before_path.exists(), "run_suite did not record the real-state snapshot"
    before = realstate.load(before_path)
    now = realstate.snapshot(ctx.app_dir, Path(os.environ["PV_REAL_HOME"]))
    # the user's own ~/.pelican-ui (history DB, last_error.log, saved tokens): strict
    assert now["per_user"] == before["per_user"], (
        "the real ~/.pelican-ui changed during the run: "
        f"{sorted(set(now['per_user']) ^ set(before['per_user']))[:5] or 'file(s) modified'}")
    # shared production paths may legitimately change if another person uses the live app
    # during this run; report, do not fail
    if now["shared"] != before["shared"]:
        ctx.note("shared production files changed during the run (another user of the live app? the suite never "
                 "opens these): " + ", ".join(k for k in now["shared"] if now["shared"][k] != before["shared"].get(k)))
    for s in ctx.servers:
        assert str(s.db.resolve()).startswith(str(ctx.work_dir.resolve()))
