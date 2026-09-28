"""Concurrency. The app runs downloads on a ThreadPoolExecutor(max_workers=3)
(api/routes/downloads.py::_job_executor): never more than 3 jobs in_progress, the
rest stay pending until a slot frees, and everything still ends correct."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from .harness import dbutil, env, jobs
from .helpers import (assert_single_file, base, block_writes, count_files, hour_expected, poll_concurrency,
                      run_job, wait_for)

S = pytest.mark.tier("standard")
D = pytest.mark.tier("deep")


def _start_many(api, ctx, label, n, paths_for):
    """POST n jobs simultaneously (threads), each to its own destination."""
    dests = [ctx.dest(f"{label}{i}") for i in range(n)]
    with ThreadPoolExecutor(n) as ex:
        futs = [ex.submit(jobs.start, api, dests[i], paths_for(i), f"pv-{label}-{i}") for i in range(n)]
        return [f.result() for f in futs]


def _assert_cap_behaviour(res, n, cap=3):
    assert res["peak"] <= cap, f"{res['peak']} jobs were in_progress at once; the executor cap is {cap}"
    if n > cap:
        assert res["peak"] == cap, f"never reached {cap} concurrent jobs (peak {res['peak']}) although {n} were queued"
        assert res["saw_pending_with_full_pool"], "never observed a pending job while the pool was full"
        order = sorted(res["first_in_progress"].items(), key=lambda kv: kv[1])
        finishes = sorted(res["done_at"].values())
        for k in range(cap, len(order)):
            started = order[k][1]
            assert started >= finishes[k - cap] - 1.5, (
                f"job #{k + 1} to start began {finishes[k - cap] - started:.1f}s BEFORE any slot had freed")
    assert all(s["status"] == "complete" for s in res["final"].values()), \
        {j: s["status"] for j, s in res["final"].items() if s["status"] != "complete"}


def _verify_hour_dests(fx, dests):
    expected = hour_expected(fx)
    for d in dests:
        jobs.assert_tree(d, expected, label=f"hour_dir at {d}")


def _assert_db_and_api_clean(api, server, jl):
    hist = dbutil.history(server)
    jrows = dbutil.jobs(server)
    ids = {j["job_id"] for j in jl}
    assert len([h for h in hist if h["id"] in {j["history_id"] for j in jl}]) == len(jl), "not exactly one history row per job"
    assert len({r["history_id"] for r in jrows if r["job_id"] in ids}) == len(jl), "two job rows share a history row"
    assert "database is locked" not in server.log_text().lower(), "'database is locked' appeared in the server log"
    assert not api.errors, f"transport errors / 5xx seen by the client: {api.errors[:5]}"


def _cap_scenario(make_app, fx, ctx, n):
    s, api = make_app("cap")
    jl = _start_many(api, ctx, "cap", n, lambda i: [fx["hour_dir"]["path"]])
    res = poll_concurrency(api, jl, timeout=1200)
    ctx.note(f"{n} jobs, peak in_progress={res['peak']} (from {res['db_samples']} atomic DB samples), all done in {res['elapsed']:.0f}s")
    _assert_cap_behaviour(res, n)
    _verify_hour_dests(fx, [j["dest"] for j in jl])
    _assert_db_and_api_clean(api, s, jl)


# ------------------------------------------------------------------ standard
@S
@pytest.mark.timeout(1500)
def test_executor_cap_of_three_with_five_jobs(make_app, fx, ctx):
    _cap_scenario(make_app, fx, ctx, 5)


# 2026-09-28: a flat "5+ overlapping listings" tripped on a real Anvil deep-tier
# run (only 3 overlaps, vs 29 seen on the same test at standard tier) -- deep
# tier runs this alongside many other concurrent servers/tests sharing the same
# node's CPU/network, so each poll iteration (and the downloads themselves) run
# slower and less predictably. That's contention, not a regression: scale the
# required overlap count down for the heavier tier instead of asserting a fixed
# number regardless of how much else is competing for resources.
MIN_LISTING_OVERLAP_BY_DEPTH = {"quick": 1, "standard": 5, "deep": 2}


@S
@pytest.mark.timeout(1500)
def test_directory_listing_works_while_downloads_run(make_app, fx, ctx):
    s, api = make_app("listing")
    jl = _start_many(api, ctx, "lst", 2, lambda i: [fx["hour_dir"]["path"]])
    nd = fx["nested_dir"]["path"]
    first_names = None
    listed_while_running, total_polls, worst, t0 = 0, 0, 0.0, time.time()
    done = {}
    while len(done) < len(jl):
        assert time.time() - t0 < 900, "downloads did not finish"
        running = False
        for j in jl:
            if j["job_id"] in done:
                continue
            st = api.status(j["job_id"]).json()
            if st["status"] in ("complete", "partial", "failed"):
                done[j["job_id"]] = st
            else:
                running = True
        t = time.time()
        r = api.list_path(nd)
        worst = max(worst, time.time() - t)
        assert r.status_code == 200, f"list-path during downloads -> {r.status_code}: {r.text[:200]}"
        names = sorted(e["name"] for e in r.json())
        first_names = first_names or names
        assert names == first_names, "directory listing changed between calls"
        total_polls += 1
        if running:
            listed_while_running += 1
        time.sleep(0.5)
    required = MIN_LISTING_OVERLAP_BY_DEPTH.get(ctx.depth, 2)
    assert listed_while_running >= required, (
        f"only {listed_while_running} of {total_polls} listings overlapped the downloads "
        f"(need >= {required} at depth={ctx.depth!r}); worst list-path latency {worst:.2f}s")
    assert all(s["status"] == "complete" for s in done.values()), {k: v["status"] for k, v in done.items()}
    _verify_hour_dests(fx, [j["dest"] for j in jl])
    _assert_db_and_api_clean(api, s, jl)
    ctx.note(f"{listed_while_running} listings during downloads, slowest {worst:.1f}s")


# ---------------------------------------------------------------------- deep
@D
@pytest.mark.timeout(1800)
def test_executor_cap_of_three_repeated(make_app, fx, ctx, rep):
    _cap_scenario(make_app, fx, ctx, 4)


@D
@pytest.mark.timeout(2400)
def test_queue_of_24_jobs_drains_correctly(make_app, fx, ctx):
    s, api = make_app("queue24")
    targets = [fx["tiny_file"], fx["small_file"]]
    jl = _start_many(api, ctx, "q", 24, lambda i: [targets[i % 2]["path"]])
    res = poll_concurrency(api, jl, timeout=1800, poll=0.25)
    _assert_cap_behaviour(res, 24)
    for i, j in enumerate(jl):
        assert_single_file(j["dest"], targets[[t["path"] for t in targets].index(j["paths"][0])], f"job{i}")
    _assert_db_and_api_clean(api, s, jl)
    ctx.note(f"24 jobs drained in {res['elapsed']:.0f}s, peak {res['peak']} concurrent")


@D
@pytest.mark.timeout(2400)
def test_three_larger_files_download_concurrently(make_app, fx, ctx):
    s, api = make_app("large")
    files = fx["medium_files"]
    jl = _start_many(api, ctx, "big", len(files), lambda i: [files[i]["path"]])
    res = poll_concurrency(api, jl, timeout=2000, poll=1.0)
    assert all(st["status"] == "complete" for st in res["final"].values()), \
        {j: (st["status"], st.get("error_message")) for j, st in res["final"].items()}
    for i, j in enumerate(jl):
        f = files[i]
        p = jobs.find_file(j["dest"], base(f["path"]), f["size"])
        assert p is not None, f"{base(f['path'])}: not found with size {f['size']}"
        jobs.assert_file(p, f["size"], sha256=f.get("sha256"), md5=f.get("md5"), label=base(f["path"]))
        ctx.note(f"{base(f['path'])}: {f['size']} bytes ok, {'md5' if f.get('md5') else 'sha256'} matches the pin")
    _assert_db_and_api_clean(api, s, jl)


@D
@pytest.mark.timeout(1800)
def test_same_file_requested_twice_to_one_destination(make_app, fx, ctx):
    """Behaviour is DOCUMENTED, not assumed. Requirements: both jobs reach a terminal state,
    the server survives, and if any job claims success the file on disk is byte-correct."""
    s, api = make_app("twice")
    f = fx["medium_files"][0]                              # 64 MiB, origin-published md5
    dest = ctx.dest("twice")
    with ThreadPoolExecutor(2) as ex:
        jl = [fu.result() for fu in [ex.submit(jobs.start, api, dest, [f["path"]], f"pv-twice-{i}") for i in range(2)]]
    finals = []
    for j in jl:
        try:
            finals.append(jobs.wait_terminal(api, j, timeout=900))
        except jobs.JobFailure as e:
            finals.append({"status": f"UNRESOLVED ({e.kind})", "files": []})
    statuses = [st["status"] for st in finals]
    hits = [p for p in dest.rglob(base(f["path"])) if p.is_file()]
    size_md5 = [(p.relative_to(dest).as_posix(), p.stat().st_size, env.md5_file(p)) for p in hits]
    ctx.note(f"job statuses={statuses}; files on disk={size_md5}; expected size={f['size']} md5={f['md5']}")
    assert all(st in ("complete", "partial", "failed") for st in statuses), f"a job never finished: {statuses}"
    assert s.alive() and api.get("/downloads/history").status_code == 200, "server died or stopped answering"
    if any(st == "complete" for st in statuses):
        assert hits, "a job reported complete but no file exists"
        good = [h for h in size_md5 if h[1] == f["size"] and h[2] == f["md5"]]
        assert good, f"jobs reported {statuses} but no on-disk copy is byte-correct: {size_md5}"
    assert api.errors == [], api.errors[:3]


@D
@pytest.mark.timeout(1500)
def test_delete_record_while_job_is_running(make_app, fx, ctx):
    """Deleting a running job's record: the thread cannot be cancelled, but nothing may be
    resurrected in the DB, the server must stay healthy, and the files it writes stay whole."""
    s, api = make_app("delrun")
    dest = ctx.dest("delrun")
    job = jobs.start(api, dest, [fx["hour_dir"]["path"]], name="pv-delete-running")
    hid, jid = job["history_id"], job["job_id"]
    wait_for(lambda: count_files(dest) >= 3, 240, what="the job to start writing files")
    log_offset = s.log_size()
    r = api.delete_record(hid)
    assert r.status_code == 200, (r.status_code, r.text)
    assert api.status(jid).status_code == 404, "deleted job still answers on the status endpoint"
    assert api.history_row(hid) is None
    total = fx["hour_dir"]["file_count"]
    wait_for(lambda: count_files(dest) >= total, 600, poll=2, what="the orphaned thread to finish its downloads")
    time.sleep(6)                                          # let its final DB updates (no-ops) happen
    assert api.history_row(hid) is None, "the deleted history record was resurrected"
    assert dbutil.job(s, jid) is None and not [h for h in dbutil.history(s) if h["id"] == hid], "rows reappeared in the DB"
    assert s.alive() and api.get("/downloads/history").status_code == 200
    new_errors = [ln for ln in s.log_since(log_offset).splitlines()
                  if "Traceback" in ln or (" ERROR " in ln and "asyncio [" not in ln)]     # asyncio "Unclosed connection" is pelicanfs noise
    assert not new_errors, f"{len(new_errors)} ERROR/Traceback line(s) in the server log after the delete, e.g. {new_errors[:3]}"
    jobs.assert_tree(dest, hour_expected(fx), label="files written by the orphaned job")
