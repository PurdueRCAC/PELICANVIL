"""Kill -9 the app mid-download, restart it, and check what a user would see.

The server killed is always the one this harness spawned. Expected (issue #6,
"jobs stuck In progress for days"): the SAME job_id is resumed and reaches a terminal
state; items that had already succeeded are preserved and NOT re-downloaded; no
truncated file is left masquerading as complete; no duplicate history rows."""
import os
import time

import pytest

from .harness import dbutil, env, jobs
from .harness.run import TERMINAL
from .helpers import base, count_files, hour_expected, wait_for

S = pytest.mark.tier("standard")
D = pytest.mark.tier("deep")


def _expected(fx, keys):
    """(size map, sha map) for the whole destination after downloading `keys` in one job."""
    exp, sha = {}, {}
    if "tiny" in keys:
        t = fx["tiny_file"]; exp[base(t["path"])] = t["size"]; sha[base(t["path"])] = t["sha256"]
    if "small" in keys:
        t = fx["small_file"]; exp[base(t["path"])] = t["size"]; sha[base(t["path"])] = t["sha256"]
    if "nested" in keys:
        nd = fx["nested_dir"]
        for f in nd["files"]:
            exp[f"{base(nd['path'])}/{f['path']}"] = f["size"]; sha[f"{base(nd['path'])}/{f['path']}"] = f["sha256"]
    if "hour" in keys:
        exp.update(hour_expected(fx))
    return exp, sha


def _path(fx, key):
    return {"tiny": fx["tiny_file"]["path"], "small": fx["small_file"]["path"],
            "nested": fx["nested_dir"]["path"], "hour": fx["hour_dir"]["path"]}[key]


def _snapshot(dest, rels):
    out = {}
    for r in rels:
        p = dest / r
        if p.is_file():
            st = p.stat()
            out[r] = (st.st_size, st.st_mtime_ns)
    return out


def _status(api, job):
    r = api.status(job["job_id"])
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _kill_and_recover(s, api, job, dest, fx, keys, ctx, preserved_rels=(), stall=180, second_kill=False):
    s.kill9()
    orphan = dbutil.job(s, job["job_id"])
    assert orphan is not None, "job row vanished when the process died"
    ctx.note(f"at death: job status={orphan['status']!r}; on-disk files={count_files(dest)}; "
             f"features={ctx.features}")
    assert orphan["status"] in ("pending", "in_progress", "complete", "partial", "failed")
    s.start()
    if second_kill:
        time.sleep(8)
        if _status(api, job)["status"] not in TERMINAL:
            s.kill9()
            ctx.note("second kill during the recovered run")
            s.start()
    t0 = time.time()
    st = jobs.wait_terminal(api, job, timeout=1200, stall=stall)
    ctx.note(f"recovered to {st['status']!r} {time.time() - t0:.0f}s after restart")
    return st


def _verify_final(api, s, job, st, dest, fx, keys, preserved_before):
    assert st["job_id"] == job["job_id"], "recovery minted a different job_id"
    assert st["status"] == "complete", (f"job ended {st['status']!r} after recovery: "
                                        f"{[(base(f['path']), f['status'], f.get('error')) for f in st['files'] if f['status'] != 'succeeded']}")
    exp, sha = _expected(fx, keys)
    jobs.assert_tree(dest, exp, sha, label="destination after recovery (sizes exact, sha256 checked)")
    for rel, (size, mtime) in preserved_before.items():
        now = (dest / rel).stat()
        assert (now.st_size, now.st_mtime_ns) == (size, mtime), \
            f"{rel} had already succeeded before the kill but was rewritten during recovery"
    hist = [h for h in dbutil.history(s) if h["id"] == job["history_id"]]
    assert len(hist) == 1 and hist[0]["status"] == "complete", hist
    assert len([j for j in dbutil.jobs(s) if j["history_id"] == job["history_id"]]) == 1
    row = api.history_row(job["history_id"])
    assert row["status"] == "complete" and all(f["status"] == "succeeded" for f in row["files"]), row


@S
@pytest.mark.timeout(2400)
def test_kill_mid_download_resumes_the_same_job(make_app, fx, ctx):
    s, api = make_app("kill")
    dest = ctx.dest("kill")
    keys = ["small", "nested", "hour"]
    paths = [_path(fx, k) for k in keys]
    job = jobs.start(api, dest, paths, name="pv-kill-mid")

    def in_window():
        st = _status(api, job)
        assert st["status"] not in TERMINAL, f"job finished ({st['status']}) before the kill window opened"
        by = jobs.files_by_path(st)
        return (by[paths[0]]["status"] == "succeeded" and by[paths[1]]["status"] == "succeeded"
                and count_files(dest / "12") >= 5)
    wait_for(in_window, 600, poll=0.3, what="items 1-2 succeeded and the hour directory partly written")
    exp, _ = _expected(fx, ["small", "nested"])
    preserved = _snapshot(dest, list(exp))
    assert len(preserved) == len(exp), "preserved items were not fully on disk before the kill"
    st = _kill_and_recover(s, api, job, dest, fx, keys, ctx)
    _verify_final(api, s, job, st, dest, fx, keys, preserved)


@D
@pytest.mark.parametrize("point", ["at_start", "between_files", "near_end", "double_kill"])
@pytest.mark.timeout(2700)
def test_kill_recover_cycle(make_app, fx, ctx, point, rep):
    """Kill at different points (job start, between items, right before the end, twice in a row)."""
    s, api = make_app(f"cycle-{point}")
    dest = ctx.dest(f"cycle-{point}")
    if point == "at_start":
        keys = ["tiny", "hour"]
    elif point == "between_files":
        keys = ["tiny", "small", "hour"]
    elif point == "near_end":
        keys = ["hour"]
    else:
        keys = ["small", "hour"]
    paths = [_path(fx, k) for k in keys]
    job = jobs.start(api, dest, paths, name=f"pv-cycle-{point}")
    hour_total = fx["hour_dir"]["file_count"]
    if point == "between_files":
        wait_for(lambda: _status(api, job)["status"] in TERMINAL or
                 jobs.files_by_path(_status(api, job))[paths[0]]["status"] == "succeeded", 300, poll=0.2, what="first item to finish")
    elif point == "near_end":
        wait_for(lambda: count_files(dest / "12") >= hour_total - 9, 600, poll=0.2, what="the last few files")
    elif point == "double_kill":
        wait_for(lambda: count_files(dest / "12") >= 5, 600, poll=0.3, what="hour dir partly written")
    if _status(api, job)["status"] in TERMINAL:
        pytest.skip(f"job finished before the '{point}' kill point could be reached (too fast to kill)")
    preserved = {}
    if point != "at_start" and keys[0] != "hour":
        if jobs.files_by_path(_status(api, job))[paths[0]]["status"] == "succeeded":
            preserved = _snapshot(dest, list(_expected(fx, [keys[0]])[0]))
    st = _kill_and_recover(s, api, job, dest, fx, keys, ctx, second_kill=(point == "double_kill"))
    _verify_final(api, s, job, st, dest, fx, keys, preserved)
