"""Deep tier: long multi-file directory jobs and a duration-based soak.

Real failures were seen on a directory job ~24 minutes in, so long jobs with MANY files
matter more than one big file. The fixtures cost little scratch (the long run is 893 MB)
but a lot of wall time: ~0.5 s of per-file overhead x 2,833 files."""
import json
import shutil
import statistics
import time

import pytest

from .harness import env, fed, jobs, run
from .helpers import base, count_files, run_job

pytestmark = pytest.mark.tier("deep")
_C = run.get()


@pytest.mark.timeout(4800)
def test_many_file_directory_job(make_app, fx, ctx):
    """Ten GOES hours (1,200 files) submitted as ONE job, exactly as ticking ten folders would."""
    s, api = make_app("many")
    mf = fx["many_files"]
    dest = ctx.dest("many")
    job = jobs.start(api, dest, mf["paths"], name="pv-many-files")
    st = jobs.wait_terminal(api, job, timeout=3600, stall=ctx.stall_seconds)
    assert st["status"] == "complete", (f"job ended {st['status']!r}: "
                                        f"{[(base(f['path']), f['status'], f.get('error_category'), f.get('error')) for f in st['files'] if f['status'] != 'succeeded']}")
    expected = {}
    for p in mf["paths"]:
        for rel, size in fed.walk_files(p).items():          # what the federation reports NOW
            expected[f"{base(p)}/{rel}"] = size
    assert len(expected) == mf["file_count"] and sum(expected.values()) == mf["total_bytes"], "federation no longer matches the pin"
    sha = {f"{x['hour']}/{x['path']}": x["sha256"] for x in mf["samples"]}
    jobs.assert_tree(dest, expected, sha, label="many_files")
    ctx.note(f"{len(expected)} files / {sum(expected.values())} bytes in {st['_elapsed']:.0f}s "
             f"({len(expected) / st['_elapsed']:.2f} files/s)")


@pytest.mark.timeout(_C.duration + 3000)
def test_long_running_directory_job(make_app, fx, ctx, mode):
    """One long job (a whole GOES day: 24 directories, 2,833 files). Watched for STALLS and
    unknown-category errors rather than asserted on speed. DURATION / LONG_MAX_GB cap it; a capped
    job that is still making progress and has produced no failure PASSES (and says so)."""
    if mode not in ctx.long_modes:
        pytest.skip(f"long run limited to server mode(s) {ctx.long_modes} (set LONG_MODES=uvicorn,wsgi to change)")
    s, api = make_app("long")
    lr = fx["long_run"]
    dest = ctx.dest("long")
    job = jobs.start(api, dest, [lr["path"]], name="pv-long-run")
    cap_bytes = ctx.long_max_gb * 1e9

    def stop(st, disk):
        if time.time() - job["t0"] >= ctx.duration:
            return f"DURATION cap ({ctx.duration}s) reached"
        if disk >= cap_bytes:
            return f"LONG_MAX_GB cap ({ctx.long_max_gb} GB) reached"
        return None

    st = jobs.wait_terminal(api, job, timeout=ctx.duration + 900, stall=ctx.long_stall_seconds, stop_when=stop)
    n_on_disk = count_files(dest)
    elapsed = st["_elapsed"]
    if st.get("_stopped"):
        s.allow_unfinished = True          # the job is abandoned on purpose when the server is stopped
        bad = [f for f in st.get("files", []) if f["status"] == "failed"]
        assert not bad, f"a failure was recorded before the cap: {bad}"
        # every completed file must be whole; only the newest one may still be in flight
        remote = fed.walk_files(lr["path"])
        local = env.dir_tree(dest / base(lr["path"]))
        newest = max(local, key=lambda k: (dest / base(lr["path"]) / k).stat().st_mtime_ns) if local else None
        wrong = [k for k, sz in local.items() if k != newest and remote.get(k) != sz]
        assert not wrong, f"{len(wrong)} finished file(s) have the wrong size, e.g. {wrong[:3]}"
        ctx.note(f"CAPPED ({st['_stopped']}): {n_on_disk}/{lr['file_count']} files, {st['_disk']} bytes in {elapsed:.0f}s "
                 f"({n_on_disk / max(elapsed, 1):.2f} files/s); still progressing, no stalls, no failures, no unknown-category errors")
        return
    assert st["status"] == "complete", (f"long job ended {st['status']!r} after {elapsed:.0f}s: "
                                        f"{[(f['status'], f.get('error_category'), f.get('error')) for f in st['files']]}")
    root = dest / base(lr["path"])
    local = env.dir_tree(root)
    assert len(local) == lr["file_count"] and sum(local.values()) == lr["total_bytes"], \
        f"{len(local)} files / {sum(local.values())} bytes on disk vs pinned {lr['file_count']} / {lr['total_bytes']}"
    for smp in lr["samples"]:
        jobs.assert_file(root / smp["path"], smp["size"], sha256=smp["sha256"], label=smp["path"])
    ctx.note(f"COMPLETED in {elapsed:.0f}s: {len(local)} files, {sum(local.values())} bytes ({len(local) / elapsed:.2f} files/s)")


@pytest.mark.timeout(_C.soak_duration + 3600)
def test_soak_repeated_download_and_verify(app, api, fx, ctx):
    """Download -> verify -> delete, over and over for SOAK_DURATION seconds against one long-lived server.
    Records every iteration (soak.jsonl); fails if ANY iteration failed, but keeps going so the pass rate is real."""
    targets = [("tiny", fx["tiny_file"]), ("small", fx["small_file"]), ("nested", fx["nested_dir"])]
    big = fx["medium_files"][0]
    t_end = time.time() + ctx.soak_duration
    out = ctx.run_dir / f"soak-{app.mode}.jsonl"
    rows, i = [], 0
    while time.time() < t_end:
        i += 1
        kind, entry = ("64MiB", big) if i % 10 == 0 else targets[i % len(targets)]
        d = ctx.dest(f"soak{i}")
        t0, ok, err = time.time(), True, ""
        try:
            job, st = run_job(api, d, [entry["path"]], timeout=600, stall=300)
            assert st["status"] == "complete", f"{st['status']}: {[(f.get('error_category'), f.get('error')) for f in st['files']]}"
            if kind == "nested":
                jobs.assert_tree(d / base(entry["path"]), {f["path"]: f["size"] for f in entry["files"]},
                                 {f["path"]: f["sha256"] for f in entry["files"]}, "nested")
            else:
                p = jobs.find_file(d, base(entry["path"]), entry["size"])
                assert p is not None, f"{base(entry['path'])} missing or wrong size"
                jobs.assert_file(p, entry["size"], sha256=entry.get("sha256"), md5=entry.get("md5"))
        except (AssertionError, jobs.JobFailure) as e:
            ok, err = False, str(e)[:400]
        finally:
            shutil.rmtree(d, ignore_errors=True)
        row = {"iter": i, "target": kind, "seconds": round(time.time() - t0, 2), "ok": ok, "error": err}
        rows.append(row)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    durs = sorted(r["seconds"] for r in rows)
    passed = sum(r["ok"] for r in rows)
    p95 = durs[min(len(durs) - 1, int(len(durs) * 0.95))] if durs else 0
    ctx.note(f"{len(rows)} iterations, {passed} ok ({100 * passed / max(1, len(rows)):.1f}%); seconds median={statistics.median(durs) if durs else 0:.1f} "
             f"p95={p95:.1f} max={durs[-1] if durs else 0:.1f}; details in {out.name}")
    failed = [r for r in rows if not r["ok"]]
    assert not failed, f"{len(failed)}/{len(rows)} iterations failed; first: {failed[0]}"
