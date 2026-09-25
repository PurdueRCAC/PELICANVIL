#!/usr/bin/env python
"""Entry point behind tests/pelicanvil_tests.sbatch.

  python tests/run_suite.py <quick|standard|deep|selftest>     (env: see tests/README.md)

Exit codes:  0 all tests passed | 1 one or more tests failed |
             2 harness error (bad usage, isolation failure, nothing selected) |
             3 ENVIRONMENT UNHEALTHY (federation / fixtures / scratch), no tests run |
             4 run aborted (SIGTERM from Slurm walltime/scancel); results are partial
"""
import importlib.metadata as md
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

EXIT_OK, EXIT_FAILED, EXIT_HARNESS, EXIT_ENV, EXIT_ABORTED = 0, 1, 2, 3, 4
DEPTHS = ("quick", "standard", "deep", "selftest")


def _pkg(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return "not installed"


def _git(repo, *args):
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:       # noqa: BLE001
        return ""


def main(argv):
    depth = (argv[1] if len(argv) > 1 and argv[1] else os.environ.get("DEPTH", "standard")).lower()
    if depth not in DEPTHS:
        print(f"unknown depth {depth!r}; choose one of {', '.join(DEPTHS)}", file=sys.stderr)
        return EXIT_HARNESS

    repo = Path(os.environ.get("APP_DIR") or TESTS.parent).resolve()
    if not (repo / "main.py").exists():
        print(f"APP_DIR {repo} does not look like a PELICANVIL checkout (no main.py)", file=sys.stderr)
        return EXIT_HARNESS

    jobid = os.environ.get("PV_RUN_ID") or os.environ.get("SLURM_JOB_ID") or f"local{time.strftime('%Y%m%d-%H%M%S')}"
    log_root = Path(os.environ.get("PV_LOG_ROOT") or (TESTS.parent / "logs" / "test-runs"))
    run_dir = Path(os.environ.get("PV_RUN_DIR") or (log_root / f"{jobid}-{depth}"))
    if Path("/anvil/scratch").is_dir():
        scratch = Path(os.environ.get("PV_SCRATCH_BASE") or f"/anvil/scratch/{os.environ.get('USER', 'unknown')}/pelicanvil-tests")
    else:
        scratch = Path(os.environ.get("PV_SCRATCH_BASE") or (Path(os.environ.get("TEMP") or "/tmp") / "pelicanvil-tests"))
    work_dir = scratch / f"{jobid}-{depth}"
    run_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    modes = os.environ.get("SERVER") or ("uvicorn,wsgi" if depth == "deep" else "uvicorn")
    real_home = os.path.expanduser("~")
    os.environ.update({
        "PV_RUN_DIR": str(run_dir), "PV_WORK_DIR": str(work_dir), "PV_DEPTH": "deep" if depth == "selftest" else depth,
        "PV_APP_DIR": str(repo), "PV_MODES": modes, "PV_REAL_HOME": real_home,
        "PV_LONG_MODES": os.environ.get("PV_LONG_MODES") or os.environ.get("LONG_MODES") or modes,
    })

    from pvsuite.harness import env, realstate           # noqa: E402  (after PV_* are exported)
    realstate.save(run_dir / "real_state_before.json", realstate.snapshot(repo, Path(real_home)))
    env.apply_sanitized_env_to_this_process(work_dir / "harness_home")

    from pvsuite.harness import run                       # noqa: E402
    from pvsuite import preflight, reporting              # noqa: E402
    ctx = run.get()

    only = os.environ.get("ONLY", "")
    overrides = {k: os.environ[k] for k in ("ONLY", "REPS", "DURATION", "SOAK_DURATION", "SERVER", "STALL_SECONDS",
                                            "LONG_STALL_SECONDS", "LONG_MAX_GB", "LONG_MODES", "APP_DIR", "PELICAN_BIN", "KEEP_DATA")
                 if k in os.environ}
    rev = _git(repo, "rev-parse", "--short", "HEAD")
    dirty = "dirty" if _git(repo, "status", "--porcelain", "--", ".") else "clean"
    header = [
        "=" * 78,
        f"PELICANVIL TEST RUN  job={jobid}  depth={depth}  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "=" * 78,
        f"host: {socket.gethostname()}   user: {env.current_user()}   python: {platform.python_version()} ({sys.executable})",
        f"app under test: {repo}  @ {rev or '?'} ({dirty})   suite dir: {TESTS}",
        f"server mode(s): {modes}   (uvicorn = plain ASGI; wsgi = passenger_wsgi:application via a2wsgi under waitress)",
        f"app features present: {ctx.features}",
        f"versions: pelicanfs {_pkg('pelicanfs')}, aiowebdav2 {_pkg('aiowebdav2')}, fsspec {_pkg('fsspec')}, "
        f"aiohttp {_pkg('aiohttp')}, fastapi {_pkg('fastapi')}, uvicorn {_pkg('uvicorn')}, a2wsgi {_pkg('a2wsgi')}, "
        f"waitress {_pkg('waitress')}, pytest {_pkg('pytest')}",
        f"overrides: {overrides or 'none'}",
        f"results dir: {run_dir}",
        f"scratch dir: {work_dir}   (removed after a fully passing run; kept on any failure)",
        f"pelican CLI: {env.find_pelican_cli() or 'not found (parity tests will SKIP)'}",
        "isolation: temp DB + temp HOME per server; credentials scrubbed; pelican binary removed from the app's PATH",
        "KNOWN LIMITS OF A GREEN RUN (repeated in the summary):",
    ] + ["  - " + k for k in reporting.KNOWN_LIMITS]
    rep = reporting.Reporter(ctx, header)

    aborted = {"why": None}

    def on_term(signum, frame):
        aborted["why"] = f"signal {signum} (Slurm walltime reached or scancel)"
        raise KeyboardInterrupt(aborted["why"])
    signal.signal(signal.SIGTERM, on_term)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, on_term)

    code = EXIT_OK
    env_unhealthy = None
    extra = []
    try:
        if depth == "selftest":
            code = _selftest(ctx, rep, modes)
        else:
            problems, checked = preflight.run(ctx.fx, depth, ctx.data_dir)
            rep.raw("PREFLIGHT (real federation canary):")
            for c in checked:
                rep.raw(f"  ok   {c}")
            if problems:
                env_unhealthy = "federation/fixtures"
                rep.raw("")
                rep.raw("ENVIRONMENT UNHEALTHY (federation/fixtures) -- no tests were run; this is NOT an app failure:")
                for p in problems:
                    rep.raw(f"  !!   {p}")
                code = EXIT_ENV                        # summary / cleanup happen in the common tail below
            else:
                rep.raw("PREFLIGHT passed.")
                rep.raw("")
                rep.raw("RESULTS  (PASS|FAIL|SKIP <area>::<test> (<seconds>s); detail indented)")
                code = _run_pytest(rep, only)
    except KeyboardInterrupt:
        aborted["why"] = aborted["why"] or "interrupted (KeyboardInterrupt)"
        code = EXIT_ABORTED
    finally:
        ctx.kill_all_servers()
        if ctx.wsgi_unavailable:
            extra.append(f"!! SERVER=wsgi was UNAVAILABLE in this run ({ctx.wsgi_unavailable}); wsgi-mode tests were SKIPPED, "
                         f"not passed. Install waitress into the test venv to enable them.")
        rep.finalize(env_unhealthy=env_unhealthy, aborted=aborted["why"], extra_lines=extra)

    counts = rep.counts()
    if aborted["why"]:
        code = EXIT_ABORTED
    elif code == EXIT_OK and counts["FAIL"]:
        code = EXIT_FAILED
    _preserve_evidence(ctx, run_dir, code)
    if code in (EXIT_OK, EXIT_ENV) and not os.environ.get("KEEP_DATA"):
        shutil.rmtree(work_dir, ignore_errors=True)
        rep.raw(f"cleanup: removed {work_dir}")
    else:
        rep.raw(f"cleanup: kept {work_dir} (downloaded data, temp DBs, temp homes) because the run was not fully green")
    rep.raw(f"exit code: {code}")
    rep.log.close()
    try:
        latest = log_root / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_dir.name)
    except OSError:
        pass
    return code


def _run_pytest(rep, only):
    import pytest
    args = [str(TESTS / "pvsuite"), "-p", "no:cacheprovider", "-q", "-rN", "--tb=short", "--rootdir", str(TESTS),
            "-o", "python_files=test_*.py", "-W", "ignore::DeprecationWarning"]
    if only:
        args += ["-k", only]
    rc = pytest.main(args, plugins=[rep])
    if rc == 5:
        rep.raw("No tests were selected (does ONLY match anything at this depth?).")
        return EXIT_HARNESS
    if rc == 2 and not rep.results:
        return EXIT_HARNESS
    return EXIT_OK if rc == 0 else EXIT_FAILED


def _preserve_evidence(ctx, run_dir, code):
    """Servers' last_error.log always (small); a consistent SQLite copy of each
    downloads DB whenever the run was not fully green."""
    import sqlite3
    ev = run_dir / "server-logs"
    ev.mkdir(exist_ok=True)
    for s in ctx.servers:
        try:
            if s.last_error_log.exists():
                shutil.copy2(s.last_error_log, ev / f"{s.name}.last_error.log")
            if code != EXIT_OK and s.downloads_db.exists():
                src = sqlite3.connect(f"file:{s.downloads_db}?mode=ro", uri=True)
                dst = sqlite3.connect(ev / f"{s.name}.downloads.sqlite")
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
        except Exception:       # noqa: BLE001
            pass


# ------------------------------------------------------------------ selftest
def _selftest(ctx, rep, modes):
    """Harness self-check that needs NO federation: proves the launcher isolates,
    both server modes boot, kill -9 + restart works, the writers work and
    credentials are scrubbed. Run this first on a new machine."""
    import json
    import tempfile

    import requests
    import yaml
    from pvsuite.harness import dbutil, env, run

    failures = 0

    def step(name, fn):
        nonlocal failures
        t0 = time.time()
        try:
            note = fn()
            rep.manual("PASS", f"selftest::{name}", time.time() - t0, note=note or "")
        except Exception as e:      # noqa: BLE001
            failures += 1
            rep.manual("FAIL", f"selftest::{name}", time.time() - t0, error=f"{type(e).__name__}: {str(e)[:800]}")

    def fixtures_parse():
        need = {"canary", "tiny_file", "small_file", "nested_dir", "special_name_file", "big_listing", "medium_files",
                "hour_dir", "many_files", "long_run", "missing", "protected"}
        assert need <= set(ctx.fx), f"missing keys {need - set(ctx.fx)}"
        assert len(ctx.fx["nested_dir"]["files"]) == ctx.fx["nested_dir"]["file_count"]
        assert len(ctx.fx["hour_dir"]["sizes"]) == ctx.fx["hour_dir"]["file_count"]
        assert sum(ctx.fx["hour_dir"]["sizes"].values()) == ctx.fx["hour_dir"]["total_bytes"]
        return f"{len(ctx.fx)} fixture groups"

    def env_scrubbed():
        base = env.sanitized_env(ctx.work_dir / "selftest_home", {})
        leaked = [k for k in env.CRED_ENV_VARS if k in base and k != "_CONDOR_CREDS"]
        assert not leaked, f"credential variables present: {leaked}"
        assert Path(base["_CONDOR_CREDS"]).is_dir() and not any(Path(base["_CONDOR_CREDS"]).iterdir())
        assert base["XDG_RUNTIME_DIR"].startswith(str(ctx.work_dir))
        assert not any(k in base for k in env.PROXY_ENV_VARS)

    def boot(mode):
        def go():
            s = ctx.make_server(f"selftest-{mode}", mode)
            try:
                s.start()
                r = requests.get(s.base_url + "/downloads/history", timeout=10)
                assert r.status_code == 200 and r.json() == []
                assert requests.get(s.base_url + "/", timeout=10).status_code == 200
                assert "PV-READY" in s.log_text()
                assert dbutil.integrity(s.downloads_db) == "ok"
                s.kill9()
                assert not s.alive()
                s.start()                            # restart on the same DB after kill -9
                assert requests.get(s.base_url + "/downloads/history", timeout=10).status_code == 200
                return f"mode={mode} boot, kill -9, restart OK on port {s.port}"
            finally:
                s.stop(timeout=3)
        return go


    def anomaly_reporting():
        """Synthetic event: proves a 'cannot schedule new futures after shutdown' failure would be surfaced
        prominently (mode, timing, traceback) even if every test passed. Uses a private sub-run dir."""
        from pvsuite import reporting as rp
        sub = ctx.run_dir / "selftest_anomaly"
        sub.mkdir(exist_ok=True)
        old = os.environ["PV_RUN_DIR"]
        os.environ["PV_RUN_DIR"] = str(sub)
        try:
            c2 = run.RunContext()
        finally:
            os.environ["PV_RUN_DIR"] = old
        srv = c2.make_server("selftest-anomaly", "wsgi")
        srv.log_path.write_text("\n".join([
            "INFO start",
            "Traceback (most recent call last):",
            '  File "downloads.py", line 1, in _run_download_job',
            "RuntimeError: cannot schedule new futures after shutdown",
            "INFO after", ""]), encoding="utf-8")
        c2.current_test = "selftest::synthetic"
        c2.anomaly("futures-shutdown", srv, job_id="deadbeefdeadbeef", path="/synthetic/dir",
                   error="cannot schedule new futures after shutdown", category="unknown",
                   seconds_into_job=1440.0, files_done=1, files_total=2, job_started_at="2026-01-01T00:00:00")
        r2 = rp.Reporter(c2, [], mirror=False)
        r2.finalize()
        text = (sub / "results.log").read_text(encoding="utf-8")
        for needle in ("WAS SEEN IN THIS RUN", "server mode : wsgi", "1440.0s into the job", "Traceback (most recent call last)",
                       "RuntimeError: cannot schedule new futures after shutdown", "a green suite would NOT have ruled it out"):
            assert needle in text, f"summary is missing {needle!r}"
        r2.log.close()
        return "synthetic event is reported prominently with mode, timing and traceback"

    def stall_detection():
        """A job row left in_progress with nothing driving it must become a STALL failure, not a hang."""
        import sqlite3
        from pvsuite.harness import jobs as J
        from pvsuite.harness.api import Api
        s = ctx.make_server("selftest-stall", "uvicorn")
        try:
            s.start()
            con = sqlite3.connect(s.downloads_db)
            files = json.dumps([{"path": "/pv/never", "status": "pending"}])
            con.execute("INSERT INTO download_history (id,name,destination,status,item_count,started_at) VALUES (1,'x','d','in_progress',1,'2026-01-01T00:00:00')")
            con.execute("INSERT INTO download_jobs (job_id,history_id,name,destination,status,item_count,files,started_at,updated_at) "
                        "VALUES ('stalljob',1,'x','d','in_progress',1,?,'2026-01-01T00:00:00','2026-01-01T00:00:00')", (files,))
            con.commit()
            con.close()
            api = Api(s)
            t0 = time.time()
            try:
                J.wait_terminal(api, {"job_id": "stalljob", "history_id": 1, "dest": str(ctx.work_dir), "paths": [], "t0": t0, "server": s},
                                timeout=60, stall=4, poll=0.5)
            except J.JobStall as e:
                assert "DB row" in str(e) and "server.log tail" in str(e)
                return f"STALL raised after {time.time() - t0:.1f}s with DB row and log tail attached"
            raise AssertionError("wait_terminal returned instead of raising JobStall")
        finally:
            s.stop(timeout=3)

    def timeout_watchdog():
        """The per-test hard timeout must interrupt a hang (so it becomes FAIL [timeout], never a stuck job)."""
        from pvsuite import conftest as cf
        cf._arm(1)
        t0 = time.time()
        try:
            while time.time() - t0 < 15:
                time.sleep(0.05)
        except cf.PVTimeout:
            el = time.time() - t0
            assert 0.8 <= el < 4, f"fired after {el:.1f}s instead of ~1s"
            return f"hard timeout interrupted a hang after {el:.1f}s"
        finally:
            cf._disarm()
        raise AssertionError("the timeout watchdog never fired")

    def writers():
        p = ctx.run_dir / "selftest_probe.json"
        p.write_text(json.dumps({"ok": True}))
        assert json.loads(p.read_text()) == {"ok": True}
        p.unlink()

    step("fixtures_parse", fixtures_parse)
    step("environment_is_scrubbed", env_scrubbed)
    step("results_writer", writers)
    step("anomaly_reporting", anomaly_reporting)
    step("stall_detection", stall_detection)
    step("timeout_watchdog", timeout_watchdog)
    step("boot_uvicorn", boot("uvicorn"))
    if "wsgi" in modes.split(",") or os.environ.get("SELFTEST_WSGI", "1") == "1":
        try:
            import waitress   # noqa: F401
            step("boot_wsgi", boot("wsgi"))
        except ImportError:
            rep.manual("SKIP", "selftest::boot_wsgi", reason="waitress is not installed; deep runs would SKIP wsgi mode")
    return EXIT_FAILED if failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
