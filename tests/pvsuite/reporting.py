"""pytest plugin that turns a run into a diagnosable set of files.

  results.log    one line per test, written (and fsync'd) the moment it finishes,
                 so a walltime kill still leaves everything that completed
  results.jsonl  same, machine readable, also incremental
  results.json   full structured results + summary   (written at the end / on abort)
  junit.xml      JUnit report                        (written at the end / on abort)
  failures/      per failed test: exception, server.log tail, last_error.log, DB rows
  anomalies/     evidence for unknown-category / 'cannot schedule new futures' events
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from . import preflight
from .harness import dbutil, env, run
from .harness.api import Api
from .harness.run import FUTURES_NEEDLE
from .harness.server import AppServer

KNOWN_LIMITS = [
    "A passing run does NOT clear the 'cannot schedule new futures after shutdown' error: it was seen in real use, "
    "cannot be triggered on demand, and is only captured if it happens naturally during this run.",
    "It does not clear multi-hour or multi-hundred-GB transfers (the long-running job is capped by DURATION).",
    "It does not clear real Passenger process recycling / idle-timeout termination. SERVER=wsgi runs "
    "passenger_wsgi:application (a2wsgi ASGIMiddleware) under waitress: it exercises a2wsgi's own event loop and "
    "threading, but not Passenger itself.",
    "There is no browser layer: front-end JavaScript (progress-label text, the ~20 minute toast poll cap) is not "
    "exercised; the endpoints it depends on are.",
]


def _area_and_name(nodeid: str):
    m = re.search(r"test_([A-Za-z0-9_]+)\.py::(.+)$", nodeid)
    if not m:
        return "misc", nodeid
    mod, rest = m.group(1), m.group(2)
    mod = re.sub(r"^zz_", "", mod)
    rest = re.sub(r"^test_", "", rest)
    return mod, rest


def _describe(excinfo):
    et = excinfo.type.__name__
    try:
        last = excinfo.traceback[-1]
        where = f"{Path(str(last.path)).name}:{last.lineno + 1}"
    except Exception:       # noqa: BLE001
        where = "?"
    return {"type": et, "text": env.redact(str(excinfo.value)), "where": where}


def _kind(exc) -> str:
    if not exc:
        return ""
    return {"PVTimeout": "timeout", "JobTimeout": "timeout", "JobStall": "STALL",
            "ServerDied": "server-died", "AssertionError": ""}.get(exc["type"], f"error: {exc['type']}")


class Reporter:
    def __init__(self, ctx, header: list, mirror: bool = True):
        self.ctx = ctx
        self.mirror = mirror
        self.dir = ctx.run_dir
        self.log = open(self.dir / "results.log", "a", encoding="utf-8")
        self.jsonl = open(self.dir / "results.jsonl", "a", encoding="utf-8")
        self.results = []
        self._phases = {}
        self._exc = {}
        self.aborted = None
        self.env_suspects = 0
        self.t_start = time.time()
        for line in header:
            self._w(line)

    # ------------------------------------------------------------------ output
    def _w(self, line=""):
        if self.mirror:
            try:                               # mirror to the job stdout so `tail -f pelicanvil-tests-<id>.out` shows progress
                print(line, file=sys.__stdout__, flush=True)
            except Exception:       # noqa: BLE001
                pass
        self.log.write(line + "\n")
        self.log.flush()
        try:
            os.fsync(self.log.fileno())
        except OSError:
            pass

    def raw(self, line=""):
        self._w(line)

    def manual(self, status, label, secs=0.0, note="", error="", reason=""):
        """Record a result that did not come from pytest (selftest steps)."""
        self._w(f"{status} {label} ({secs:.1f}s)")
        for ln in str(error).splitlines():
            self._w("    " + ln[:600])
        if reason:
            self._w(f"    reason: {reason}")
        if note:
            self._w(f"    note: {note}")
        rec = {"test": label, "nodeid": label, "status": status, "seconds": round(secs, 2), "kind": "",
               "env_suspect": False, "notes": [note] if note else [], "reason": reason, "error": str(error),
               "error_type": "", "where": ""}
        self.results.append(rec)
        self.jsonl.write(json.dumps(rec) + "\n")
        self.jsonl.flush()

    # ------------------------------------------------------------------- hooks
    def pytest_collectreport(self, report):
        if report.failed:                       # an import error in a test module must never be silent
            self._w(f"HARNESS ERROR: could not collect {report.nodeid}")
            for ln in str(report.longreprtext).splitlines()[-25:]:
                self._w("    " + ln[:400])

    def pytest_runtest_setup(self, item):
        self.ctx.current_test = item.nodeid
        self._running = item.nodeid
        self.ctx.drain_notes()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        rep = outcome.get_result()
        if call.excinfo is not None and not call.excinfo.errisinstance(pytest.skip.Exception):
            exc = _describe(call.excinfo)
            self._exc[(item.nodeid, rep.when)] = exc
            try:
                self._capture(item, rep, exc)
            except Exception as e:      # noqa: BLE001 - capture must never break the run
                self._w(f"    (failure capture itself failed: {type(e).__name__}: {e})")

    def pytest_runtest_logreport(self, report):
        self._phases.setdefault(report.nodeid, {})[report.when] = report
        if report.when == "teardown":
            self._finish(report.nodeid)

    # ---------------------------------------------------------------- finishing
    def _finish(self, nodeid):
        ph = self._phases.pop(nodeid)
        self._running = None
        setup, call, teardown = ph.get("setup"), ph.get("call"), ph.get("teardown")
        dur = sum(getattr(r, "duration", 0.0) for r in ph.values())
        area, name = _area_and_name(nodeid)
        exc, reason, kind = None, "", ""
        if setup is not None and setup.failed:
            status, kind, exc = "FAIL", "setup error", self._exc.get((nodeid, "setup"))
        elif (setup is not None and setup.skipped) or (call is not None and call.skipped):
            status = "SKIP"
            r = setup if (setup is not None and setup.skipped) else call
            reason = str(r.longrepr[2]) if isinstance(r.longrepr, tuple) else str(r.longrepr)
            reason = re.sub(r"^Skipped:\s*", "", reason)
        elif call is not None and call.failed:
            status, exc = "FAIL", self._exc.get((nodeid, "call"))
            kind = _kind(exc)
        elif teardown is not None and teardown.failed:
            status, kind, exc = "FAIL", "teardown error", self._exc.get((nodeid, "teardown"))
        else:
            status = "PASS"
        notes = self.ctx.drain_notes()
        suspect = False
        if status == "FAIL" and kind != "setup error":
            err = preflight.canary(self.ctx.fx)
            if err:
                suspect = True
                self.env_suspects += 1
                notes.append(f"ENV-SUSPECT: federation canary failed right after this test ({err}); "
                             f"treat this failure as possibly environmental")
        label = f"{area}::{name}"
        suffix = (f" [{kind}]" if kind else "") + (" [ENV-SUSPECT]" if suspect else "")
        self._w(f"{status} {label} ({dur:.1f}s){suffix}")
        if status == "FAIL":
            if exc:
                lines = exc["text"].splitlines() or [exc["type"]]
                lines = [f"{exc['type']}: {lines[0]}"] + lines[1:]
                for ln in lines[:40]:
                    self._w("    " + ln[:600])
                if len(lines) > 40:
                    self._w(f"    ... ({len(lines) - 40} more lines in failures/)")
                self._w(f"    at {exc['where']}")
            fdir = self._fail_dir(nodeid)
            if fdir.exists():
                self._w(f"    evidence: {fdir.relative_to(self.dir).as_posix()}/")
        elif status == "SKIP":
            self._w(f"    reason: {reason}")
        for n in notes:
            for i, ln in enumerate(str(n).splitlines() or [""]):
                self._w(("    note: " if i == 0 else "          ") + ln[:600])
        rec = {"test": label, "nodeid": nodeid, "status": status, "seconds": round(dur, 2), "kind": kind,
               "env_suspect": suspect, "notes": notes, "reason": reason,
               "error": (exc or {}).get("text", ""), "error_type": (exc or {}).get("type", ""),
               "where": (exc or {}).get("where", "")}
        self.results.append(rec)
        self.jsonl.write(json.dumps(rec) + "\n")
        self.jsonl.flush()

    # ---------------------------------------------------------- failure capture
    def _fail_dir(self, nodeid) -> Path:
        return self.dir / "failures" / re.sub(r"[^A-Za-z0-9._-]+", "_", _area_and_name(nodeid)[0] + "__" + _area_and_name(nodeid)[1])[:120]

    def _capture(self, item, rep, exc):
        """Copy everything needed to diagnose this failure later into failures/<test>/."""
        d = self._fail_dir(item.nodeid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"exception-{rep.when}.txt").write_text(env.redact(str(rep.longreprtext)), encoding="utf-8")
        notes = self.ctx.drain_notes()
        self.ctx._notes = list(notes)       # keep them for the result line
        if notes:
            (d / "notes.txt").write_text("\n".join(map(str, notes)), encoding="utf-8")
        servers = {}
        for v in item.funcargs.values():
            if isinstance(v, AppServer):
                servers[v.name] = v
            elif isinstance(v, Api):
                servers[v.server.name] = v.server
        for s in self.ctx.tested_servers(item.nodeid):
            servers[s.name] = s
        for s in servers.values():
            tag = s.name
            (d / f"server_{tag}.log.tail").write_text("\n".join(s.log_tail(300)), encoding="utf-8")
            if s.last_error_log.exists():
                (d / f"last_error_{tag}.log").write_text(env.redact(s.last_error_log.read_text(errors="replace")), encoding="utf-8")
            tracked = [(j, h) for (srv, j, h) in self.ctx.tracked_for(item.nodeid) if srv is s]
            dump = dbutil.dump_for_capture(s, job_ids=[j for j, _ in tracked], history_ids=[h for _, h in tracked if h])
            (d / f"db_{tag}.json").write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")

    # --------------------------------------------------------------- finalising
    def counts(self):
        c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for r in self.results:
            c[r["status"]] += 1
        return c

    def finalize(self, env_unhealthy=None, aborted=None, extra_lines=None):
        c = self.counts()
        self._w("")
        self._w("=" * 78)
        self._w("SUMMARY")
        self._w("=" * 78)
        unhealthy = "YES - " + env_unhealthy if env_unhealthy else "no"
        self._w(f"{c['PASS']} passed, {c['FAIL']} failed, {c['SKIP']} skipped, env-unhealthy: {unhealthy}")
        if self.env_suspects:
            self._w(f"env-suspect failures (federation canary failed right after the test): {self.env_suspects}")
        if aborted and getattr(self, "_running", None):
            a, n = _area_and_name(self._running)
            self._w(f"ABORTED {a}::{n} (was running when the run was aborted; no result)")
        if aborted:
            self._w(f"RUN ABORTED: {aborted} -- results above are partial")
        self._w(f"elapsed: {time.time() - self.t_start:.0f}s")

        fails = [r for r in self.results if r["status"] == "FAIL"]
        if fails:
            self._w("")
            self._w("FAILED TESTS:")
            for r in fails:
                self._w(f"  FAIL {r['test']}" + (f" [{r['kind']}]" if r["kind"] else "") + (f" -- {r['error'].splitlines()[0][:160]}" if r["error"] else ""))

        rates = {}
        for r in self.results:
            key = re.sub(r"[-_]?rep\d+", "", r["test"])
            key = re.sub(r"\[-?\]", "", key)
            if re.search(r"repd+", r["test"]):
                rates.setdefault(key, []).append(r["status"] == "PASS")
        if rates:
            self._w("")
            self._w("PASS RATES ACROSS REPETITIONS (no automatic retries; a flaky pass is signal):")
            for k, v in sorted(rates.items()):
                self._w(f"  {sum(v)}/{len(v)} ({100 * sum(v) / len(v):.0f}%)  {k}")

        self._anomaly_section()
        self._w("")
        self._w("KNOWN LIMITS OF A GREEN RUN:")
        for k in KNOWN_LIMITS:
            self._w("  - " + k)
        for ln in extra_lines or []:
            self._w(ln)
        self._write_json(env_unhealthy, aborted)
        self._write_junit()
        self.log.flush()

    def _anomaly_section(self):
        ctx = self.ctx
        anoms = ctx.read_anomalies()
        fut = [a for a in anoms if a["kind"] == "futures-shutdown"]
        sightings = self._scan_sightings()
        self._w("")
        if fut or sightings:
            self._w("!" * 78)
            self._w("!!! 'cannot schedule new futures after shutdown' WAS SEEN IN THIS RUN")
            self._w("!!! (seen in real use, cannot be triggered on demand; a green suite would NOT have ruled it out)")
            self._w("!" * 78)
            for a in fut:
                self._w(f"  server mode : {a['mode']}   (server {a['server']}, test {a.get('test')})")
                self._w(f"  job / file  : {a.get('job_id')}  {a.get('path')}")
                self._w(f"  timing      : {a.get('seconds_into_job')}s into the job; {a.get('files_done')}/{a.get('files_total')} items done; "
                        f"job started_at={a.get('job_started_at')}; observed {a['time']}")
                self._w(f"  error text  : {a.get('error')[:300]}")
                tb = a.get("traceback_file")
                if tb and (self.dir / tb).exists():
                    self._w(f"  traceback   : {tb} (full traceback below)")
                    for ln in (self.dir / tb).read_text(errors="replace").splitlines()[:80]:
                        self._w("    | " + ln[:400])
                self._w(f"  evidence    : {a['evidence_dir']}/")
            for s in sightings:
                self._w(f"  also found in {s}")
        else:
            self._w("cannot schedule new futures after shutdown: NOT SEEN this run (this does NOT rule it out).")
        fnf = [a for a in anoms if a["kind"] == "false-not-found"]
        if fnf:
            self._w("")
            self._w(f"FALSE not_found ({len(fnf)}): the app reported `not_found` for a path the fixtures guarantee exists")
            for a in fnf:
                self._w(f"  [{a['mode']}] test={a.get('test')} job={a.get('job_id')} path={a.get('path')} "
                        f"({a.get('seconds_into_job')}s into job)")
                self._w(f"      direct federation check: {a.get('federation_direct_check')}")
                self._w(f"      evidence: {a['evidence_dir']}/")
        unk = [a for a in anoms if a["kind"] == "unknown-category"]
        if unk:
            self._w("")
            self._w(f"UNKNOWN-CATEGORY DOWNLOAD FAILURES ({len(unk)}) -- inspect the tracebacks:")
            for a in unk:
                self._w(f"  [{a['mode']}] test={a.get('test')} job={a.get('job_id')} path={a.get('path')}")
                self._w(f"      error={str(a.get('error'))[:240]}   ({a.get('seconds_into_job')}s into job)")
                self._w(f"      evidence: {a['evidence_dir']}/")

    def _scan_sightings(self):
        out = []
        for s in self.ctx.servers:
            try:
                if FUTURES_NEEDLE in s.log_text():
                    out.append(f"{s.name}/server.log")
                if s.last_error_log.exists() and FUTURES_NEEDLE in s.last_error_log.read_text(errors="replace"):
                    out.append(f"{s.name} last_error.log")
                if s.downloads_db.exists():
                    for j in dbutil.jobs(s):
                        for f in j["files"]:
                            if FUTURES_NEEDLE in (f.get("error") or ""):
                                out.append(f"{s.name} download_jobs row {j['job_id']} file {f['path']}")
            except Exception:       # noqa: BLE001
                pass
        # drop ones already reported live
        live = {(a["server"], a.get("job_id")) for a in self.ctx.read_anomalies() if a["kind"] == "futures-shutdown"}
        return [x for x in out if not any(x.startswith(sv) and jid and jid in x for sv, jid in live)] if live else out

    def _write_json(self, env_unhealthy, aborted):
        c = self.counts()
        doc = {"depth": self.ctx.depth, "modes": self.ctx.modes, "app_dir": str(self.ctx.app_dir),
               "features": self.ctx.features, "counts": {k.lower(): v for k, v in c.items()},
               "env_unhealthy": env_unhealthy, "aborted": aborted, "env_suspect_failures": self.env_suspects,
               "elapsed_seconds": round(time.time() - self.t_start, 1), "anomalies": self.ctx.read_anomalies(),
               "results": self.results}
        (self.dir / "results.json").write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    def _write_junit(self):
        c = self.counts()
        total = sum(r["seconds"] for r in self.results)
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               f'<testsuite name="pelicanvil" tests="{len(self.results)}" failures="{c["FAIL"]}" errors="0" '
               f'skipped="{c["SKIP"]}" time="{total:.2f}">']
        for r in self.results:
            area, _, name = r["test"].partition("::")
            out.append(f'  <testcase classname={quoteattr(area)} name={quoteattr(name)} time="{r["seconds"]}">')
            if r["status"] == "FAIL":
                msg = (r["kind"] + ": " if r["kind"] else "") + (r["error"].splitlines()[0] if r["error"] else r["error_type"])
                out.append(f'    <failure message={quoteattr(msg[:300])}>{escape(r["error"][:6000])}</failure>')
            elif r["status"] == "SKIP":
                out.append(f'    <skipped message={quoteattr(r["reason"][:300])}/>')
            if r["notes"]:
                out.append(f'    <system-out>{escape(chr(10).join(map(str, r["notes"])))}</system-out>')
            out.append("  </testcase>")
        out.append("</testsuite>")
        (self.dir / "junit.xml").write_text("\n".join(out) + "\n", encoding="utf-8")
