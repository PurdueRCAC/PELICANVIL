"""Runs last. Consistency invariants over EVERY download DB the run created: after a settle
period, no job is left pending/in_progress without a live thread, and every history row agrees
with its job row and its file list."""
import time

import pytest

from .harness import dbutil
from .harness.run import KNOWN_CATEGORIES, TERMINAL

pytestmark = pytest.mark.tier("standard")

SETTLE_SECONDS = 90


def _violations(server):
    out = []
    jobs_, hist = dbutil.jobs(server), dbutil.history(server)
    hist_by_id = {h["id"]: h for h in hist}
    seen_hist = {}
    for j in jobs_:
        tag = f"[{server.name}] job {j['job_id'][:8]}"
        if j["status"] not in TERMINAL and not server.allow_unfinished:
            out.append(f"{tag}: left {j['status']!r} (server alive={server.alive()}): no live thread will ever finish it")
        seen_hist.setdefault(j["history_id"], []).append(j["job_id"])
        h = hist_by_id.get(j["history_id"])
        if h is None:
            out.append(f"{tag}: history row {j['history_id']} does not exist")
            continue
        if h["status"] != j["status"]:
            out.append(f"{tag}: job status {j['status']!r} != history status {h['status']!r}")
        if h["item_count"] != len(j["files"]):
            out.append(f"{tag}: item_count {h['item_count']} != {len(j['files'])} files in the job row")
    for hid, ids in seen_hist.items():
        if len(ids) > 1:
            out.append(f"[{server.name}] history {hid} has {len(ids)} job rows")
    for h in hist:
        tag = f"[{server.name}] history {h['id']}"
        if h["status"] not in TERMINAL:
            if not server.allow_unfinished:
                out.append(f"{tag}: left {h['status']!r}")
            continue
        files = h["files"]
        st = [f.get("status") for f in files]
        if h["status"] == "complete" and (any(s != "succeeded" for s in st) or h["error_message"]):
            out.append(f"{tag}: 'complete' but files={st} error_message={h['error_message']!r}")
        if h["status"] == "failed" and any(s != "failed" for s in st):
            out.append(f"{tag}: 'failed' but files={st}")
        if h["status"] == "partial" and not ("succeeded" in st and "failed" in st):
            out.append(f"{tag}: 'partial' but files={st}")
        if h["status"] != "complete" and not h["error_message"]:
            out.append(f"{tag}: {h['status']!r} with no error_message")
        if h["files"] and h["item_count"] != len(files):
            out.append(f"{tag}: item_count {h['item_count']} != len(files) {len(files)}")
        for f in files:
            if f.get("status") == "failed":
                if not f.get("error"):
                    out.append(f"{tag}: failed file {f['path']!r} has NO error text")
                if f.get("error_category") not in KNOWN_CATEGORIES:
                    out.append(f"{tag}: failed file {f['path']!r} has category {f.get('error_category')!r}")
    return out


@pytest.mark.timeout(400)
def test_download_db_consistency_invariants(ctx):
    servers = [s for s in ctx.servers if s.downloads_db.exists()]
    assert servers, "no server with a downloads DB was created"
    deadline = time.time() + SETTLE_SECONDS
    while True:
        problems = []
        for s in servers:
            try:
                problems += _violations(s)
            except Exception as e:      # noqa: BLE001
                problems.append(f"[{s.name}] could not read DB: {type(e).__name__}: {e}")
        stuck_only = all("left " in p for p in problems)
        if not problems or not stuck_only or time.time() >= deadline:
            break
        time.sleep(3)                    # give live threads a moment to finish their last update
    ctx.note(f"checked {len(servers)} server DB(s): " + ", ".join(f"{s.name}({len(dbutil.jobs(s))} jobs)" for s in servers))
    assert not problems, f"{len(problems)} invariant violation(s):\n" + "\n".join(problems[:30])
