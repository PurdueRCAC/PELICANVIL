#!/usr/bin/env python3
"""
Standalone Slurm worker for admin-triggered dataset size indexing.

Deliberately NOT part of the FastAPI app's request/response cycle — RCAC
policy on Anvil's login nodes is job prep/submission/monitoring only, no
sustained background work, so this has to be a real Slurm job. And because
pelican-ui runs per-user via PUN, having each user's own process submit its
own Slurm job would hit the same "not every user is in the charged
allocation" problem already solved once for the project-folder picker — so
this script is submitted manually, once, by one identity, and keeps itself
alive across Anvil's 96-hour hard walltime cap by resubmitting itself near
the end of each cycle rather than a human re-running it. Since 2026-08-24
this process itself runs inside an Apptainer container (see
scripts/indexing_worker.sbatch) that deliberately carries no Slurm client
tools — `sbatch` isn't on PATH in there, so this script can't call it
directly any more. Instead it signals the need to resubmit by exiting with
RESUBMIT_EXIT_CODE (see main()); the sbatch script itself, which is still
running on the host waiting for this container to return, is what actually
calls `sbatch` for the next cycle.

Usage on Anvil: `sbatch scripts/indexing_worker.sbatch` (see that file for
the #SBATCH resource header) starts the first cycle. This script assumes
it's already running inside an allocated job, and never calls `sbatch`
itself — it only signals the need for the next cycle via its exit code (see
RESUBMIT_EXIT_CODE); the sbatch script is what actually resubmits.
"""
import asyncio
import faulthandler
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from api.core.config import DB_PATH
from api.core.indexing_queue import (
    claim_next_pending,
    mark_cancelled,
    mark_complete,
    mark_failed,
    recover_interrupted,
    update_progress,
)
# Reusing pelican.py's own filesystem-resolution rather than re-implementing
# it here. reset_default_filesystem is the connection-pressure safeguard
# from the 2026-08-04 investigation — see list_path() below for how it's
# used. _encode_path_segment is the special-character path-encoding fix
# from the 2026-08-04 investigation into folders like "Measurement+1"
# 404ing — see its docstring in pelican.py for the confirmed root cause
# (aiowebdav2 silently unescaping every .ls() path before sending it).
from api.routes.pelican import _resolve_filesystem, reset_default_filesystem, _encode_path_segment
# _is_auth_required/_is_connection_error moved here from pelican.py in the
# 2026-08-04 failure-visibility work, alongside the new classify_failure
# that wraps both — see api/core/failure_classification.py's own docstring
# for why. log_unexpected_pelican_error is the same ~/.pelican-ui/last_error.log
# diagnostic api/routes/pelican.py's web-request handlers already write to;
# extending it to this worker's failure path (see main()) gives Branden the
# same no-Anvil-shell-access-needed diagnostic surface for indexing failures
# that already existed for web-request failures.
from api.core.failure_classification import _is_auth_required, _is_connection_error, classify_failure
from api.core.pelican_auth import log_unexpected_pelican_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pelican-ui.indexing-worker")

# RUNBOOK — if the worker looks stalled (admin panel/log not moving for
# 10+ minutes) and you're about to cancel it: first run, from any Anvil
# login node, `scancel --signal=USR1 <jobid>` (find <jobid> via `squeue -u
# $USER`) — Slurm delivers the signal to whichever compute node the job
# actually landed on, no need to find/SSH there yourself. That dumps every
# thread's current stack to indexing_worker_stackdump_<pid>.txt next to the
# job's own log file (see _STACKDUMP_PATH below), showing exactly which
# call it's stuck in. *Then* cancel it if you still need to — this way the
# evidence survives the cancel instead of being destroyed by it, which is
# what happened on 2026-08-06 (job 19689186: ~60 minutes of silence, then
# cancelled, with no way to tell afterward where it had actually been
# stuck). This costs nothing if the worker is healthy — it's a signal
# handler that sits idle until triggered.
_STACKDUMP_PATH = os.path.join(os.path.dirname(DB_PATH), f"indexing_worker_stackdump_{os.getpid()}.txt")
_stackdump_file = None  # kept referenced so it isn't garbage-collected/closed for the registration's lifetime


def _register_stackdump_handler() -> None:
    """SIGUSR1 -> full thread-stack dump via the stdlib faulthandler module —
    zero overhead until triggered (a bare signal registration, no polling,
    no background thread), no external tool or install required. Preferred
    over py-spy (confirmed installable/usable on Anvil via `pip install
    py-spy` in a scratch venv, and ptrace_scope=0 here means it *can* attach
    same-user — both checked 2026-08-06) because py-spy still requires
    finding which compute node the job landed on and getting a shell there
    before you can attach; faulthandler's SIGUSR1 is reachable from any
    login node via plain `scancel --signal=USR1 <jobid>`, using tooling
    Branden already uses to manage this job. See the RUNBOOK comment above.

    Guarded the same way as api/core/indexing_queue.py's fcntl import:
    SIGUSR1 doesn't exist on Windows, and this function's only ever
    meaningfully called under Slurm/Linux, but the guard keeps this file
    importable if that ever changes."""
    global _stackdump_file
    try:
        sigusr1 = signal.SIGUSR1
    except AttributeError:
        logger.warning("SIGUSR1 not available on this platform — stack-dump-on-signal not registered")
        return
    _stackdump_file = open(_STACKDUMP_PATH, "a", encoding="utf-8")
    faulthandler.register(sigusr1, file=_stackdump_file, all_threads=True, chain=False)
    logger.info("Stack-dump-on-SIGUSR1 registered — 'scancel --signal=USR1 <jobid>' dumps to %s", _STACKDUMP_PATH)


IDLE_POLL_SECONDS = 5

# Comfortably under Anvil's 96-hour hard cap on the shared partition (see
# scripts/indexing_worker.sbatch's --time) — deliberately NOT set equal to
# that cap. This is a self-imposed cycle length, not meant to match Slurm's
# hard kill: it needs a real margin below 96h so a dataset walk still
# in-flight when the deadline hits has room to finish and hand off
# gracefully (see main()'s deadline check + RESUBMIT_EXIT_CODE) before
# Slurm force-kills the job instead — a force-kill happens before
# indexing_worker.py ever gets to run its own exit-code/resubmit logic, so
# the self-renewal chain silently stops right there (confirmed both by the
# 2026-08-24 incident and by the 2026-08-28 short-walltime tests). 48h
# keeps that margin comfortable while halving resubmission frequency
# relative to the original 24h. Not a hard constraint; adjust freely, but
# keep real distance from 96h.
WALLTIME_HOURS = 48
# How far before the walltime deadline to stop claiming new work and
# resubmit instead — must be comfortably longer than one indexing run can
# take (the prompt's own benchmark: ~20-40 min for a 500k-800k file
# dataset), so a job claimed just before the margin still has room to finish
# before Slurm kills this cycle.
RENEWAL_MARGIN_MINUTES = 60

# Distinct, deliberately unusual exit code meaning "walltime approaching,
# please resubmit" — sys.exit()s this instead of calling `sbatch` directly
# (2026-08-24 incident: this script runs inside an Apptainer container with
# no Slurm client tools on PATH, so a direct subprocess.run(["sbatch", ...])
# from in here always fails with FileNotFoundError, silently ending the
# indexing chain the moment a job first hits its walltime deadline — exactly
# what happened after noaa-goes19 completed on 2026-08-27, stalling the
# queue with 10 datasets, including a ~2M-folder noaa-goes16, still waiting).
# scripts/indexing_worker.sbatch runs on the host and is still alive after
# `apptainer exec` returns — it checks for this specific code and is the one
# that actually calls `sbatch`, since that binary only exists out there.
# Chosen from the BSD sysexits.h range (64-78, EX_TEMPFAIL=75) rather than
# 0-2 or 126-165, which shells/Python/signal handling already give other
# conventional meanings to.
RESUBMIT_EXIT_CODE = 75

# Connection-pressure safeguards (2026-08-04 investigation): pelicanfs's own
# _ls_real creates a brand-new aiohttp.ClientSession per .ls() call and
# never reuses one across calls — third-party behavior this worker can't
# change without touching pelicanfs source (out of scope, see that
# investigation's report). Sustained over tens of thousands of calls in one
# continuous multi-hour process, this is consistent with the observed
# "healthy for hours, then a wall" pattern (TCP TIME_WAIT / ephemeral-port /
# file-descriptor pressure building until new connections can't be
# established). Two safeguards mitigate this entirely from pelican-ui's own
# code, without touching pelicanfs/aiowebdav2 — see list_path() in
# _index_dataset for where both are applied:
#   - proactive (PROACTIVE_RESET_CALLS): drop and recreate the shared
#     filesystem singleton every N .ls() calls, well before pressure has a
#     chance to build toward the failure point. The real failure log's
#     first healthy dataset made it 40,200 calls over 3+ hours before any
#     sign of trouble, and the *next* dataset — starting from a process
#     that already carried that accumulated pressure — failed after only
#     ~29 minutes, nowhere near that count on its own. That gap between
#     "still fine at 40,200" and "already struggling well before that on a
#     pressured process" is exactly why this threshold is deliberately an
#     order of magnitude under the first number, not tuned tight against
#     it — the real safe ceiling isn't known, and an unnecessary reset is
#     nearly free (recreating one filesystem object), while resetting too
#     late risks hitting the wall anyway. This is a starting point meant to
#     be adjusted from the real log data Phase 4's logging below produces,
#     not a final tuned value. Counts cumulatively across dataset
#     boundaries within one continuous worker process, not per-dataset —
#     matching how the real degradation compounded across datasets 3 and 4
#     in the same process, not within either one alone.
PROACTIVE_RESET_CALLS = 2000

# Retry-loop tuning (2026-08-04, extending the original single-retry
# design): real production evidence showed a genuine network blip
# (dataset_id=17's `OSError: Connect call failed`) outlast the one retry
# the original design allowed. Two separate, deliberately different-depth
# schedules — see list_path()'s _retry_connection_class/_retry_unclassified
# for how each is used:
#
# CONNECTION_RETRY_BACKOFFS: for exceptions _is_connection_error recognizes
# (timeout, NoConnectionError, CancelledError/aiohappyeyeballs,
# ClientConnectorError/OSError, BadDirectorResponse) — well-understood as
# usually-transient, so these get the full loop. ~3x multiplier per step
# (5s, 15s, 45s), capped well under the "not exceeding a minute or two"
# guidance — worst case this adds ~65s to one dataset's walk (3 retries,
# 4 attempts total) before falling through to mark_failed. That's a real,
# bounded delay to the sequential queue behind it (this worker is
# deliberately single-threaded — see _index_dataset's own docstring on the
# mentor directive against concurrent listing calls), not an open-ended
# one; not tuned from a precisely-timed data point (the real blip's exact
# duration isn't in the log), but a reasoned middle ground between "long
# enough to plausibly ride out a real several-tens-of-seconds hiccup" and
# "short enough that a genuinely down origin doesn't stall the queue for
# many minutes." A starting point, same spirit as PROACTIVE_RESET_CALLS —
# adjustable from real retry-outcome data once this is live.
CONNECTION_RETRY_BACKOFFS = [5, 15, 45]

# UNCLASSIFIED_RETRY_ATTEMPTS/_BACKOFF_SECONDS: for anything
# _is_connection_error doesn't recognize — a genuinely new exception type,
# or an anomaly (e.g. the 2026-08-04 investigation's openalex case, which
# turned out not to be an encoding bug and remains unexplained) that
# doesn't fit a known connection-class pattern. Deliberately a much
# smaller, more conservative allowance than the connection-class loop
# above — 1 retry, not 3 — on the theory that most real-world failures are
# probably transient even when the exact cause isn't cleanly classified,
# without fully abandoning the caution that made these zero-retry in the
# original design. A flat (not exponential) backoff is enough for a single
# retry; no filesystem reset here (unlike the connection-class path) since
# resetting is specifically a connection-pressure mitigation, and an
# unclassified exception isn't confirmed to be session/connection-related
# at all — resetting on faith could mask what's actually going on.
UNCLASSIFIED_RETRY_ATTEMPTS = 1
UNCLASSIFIED_RETRY_BACKOFF_SECONDS = 10

# Circuit breaker (2026-08-05 investigation): CONNECTION_RETRY_BACKOFFS
# above is designed for, and confirmed effective against, a transient,
# recoverable connection blip on an otherwise-healthy origin (the
# 2026-08-04 dataset_id=17 OSError case). It is NOT designed for, and was
# observed actively making worse, a different failure shape: sustained
# origin/Director unreachability. Real evidence, 2026-08-05, dataset_id=3
# (noaa-goes18): every single folder across a ~20-hour span (the entire
# ABI-L1b-RadM/2023-2026 day-of-year tree, then a long run of ABI-L2-*
# product folders) failed with fsspec.exceptions.FSTimeoutError, always
# only after exhausting the full 4-attempt/~90-110s retry cycle, never
# once succeeding on a fresh post-reset attempt.
#
# Traced to source by reading the installed pelicanfs package directly and
# reproducing the exception chain: FSTimeoutError comes from pelicanfs's
# get_dirlist_url() (Director PROPFIND resolution, which runs before the
# actual listing call), which uses a *hardcoded* 5-second
# aiohttp.ClientTimeout with no try/except around it (unlike the sibling
# cache-selection loop in get_working_cache, which does catch its own
# timeout and treats it as "try the next cache") — and that 5s value isn't
# exposed through any pelicanfs/OSDFFileSystem constructor kwarg. Per this
# project's standing convention against touching third-party source, that
# timeout value itself is not something pelican-ui's own code can adjust;
# a Director that's sustained-degraded (or simply can't answer a
# particular request pattern within 5s) will keep producing this exact
# failure regardless of how many times list_path resets and retries —
# which is exactly what the real log shows.
#
# CIRCUIT_BREAKER_CONSECUTIVE_FAILURES: once this many *different* folders
# in a row (not retries on the same folder — that's CONNECTION_RETRY_BACKOFFS
# above; this counts folders that each already exhausted that whole loop)
# have failed this way with no successful or data-class response in
# between, treat that as confirmed evidence of sustained origin trouble
# rather than a run of bad luck, and abort the dataset immediately instead
# of paying the full ~90-110s retry cost on every remaining folder — at
# the real noaa-goes18 run's rate, a dataset this deep would otherwise
# take weeks to exhaust its walk while still recording a wildly wrong
# near-zero total, exactly the failure mode this circuit breaker exists to
# close. 5 is deliberately small as a *folder* count, but not as an
# *attempt* count: each folder counted here already cost 4 real requests
# against the origin over ~90-110s (the existing retry loop's own cost,
# not additional load this change introduces), so 5 consecutive failures
# is already ~20 real requests spread over roughly 7.5-9 minutes — enough
# to be confident it isn't coincidence, without waiting much longer to act
# on it. A single confirmed data-class response (a real 404/401/403) or a
# single successful listing anywhere in between resets the count to 0 —
# see walk()'s own handling — so a genuinely recovering or merely-flaky
# origin does not trip this. A reasoned starting point, same spirit as
# PROACTIVE_RESET_CALLS and CONNECTION_RETRY_BACKOFFS above — adjustable
# from real trip-outcome data once this is live.
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 5

# Root-dataset circuit breaker (2026-08-06 investigation): the breaker above
# only ever gets a chance to increment for folders *inside* an already-
# started walk — walk()'s "if path == entry['path']: raise" deliberately
# re-raises a root-listing failure immediately (a root that can't be listed
# has no partial walk worth continuing), before ever reaching that counter.
# That's correct for what it's for, but it means a systemic origin/Director
# outage — where *every* dataset's very first listing call fails — never
# trips the folder-level breaker at all: each dataset still pays the full
# CONNECTION_RETRY_BACKOFFS cost (~90-110s) before moving on to the next,
# one after another, for as long as the outage lasts. Real evidence,
# 2026-08-06: noaa-goes19/16/17 each failed exactly this way, back to back,
# with zero "Circuit breaker: N/5..." log lines between them.
#
# ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES tracks consecutive *whole-
# dataset* root-listing failures instead — module-level, spanning dataset
# boundaries like PROACTIVE_RESET_CALLS/CIRCUIT_BREAKER_CONSECUTIVE_FAILURES
# above. Once tripped, _retry_connection_class swaps its normal multi-
# attempt backoff schedule for root-path calls down to a single short probe
# (ROOT_CIRCUIT_BREAKER_PROBE_BACKOFF) — still one real request against the
# origin, never zero, so a recovered origin is still noticed promptly, just
# without paying the full retry loop on every dataset while it's already
# confirmed down. Resets to 0 on any successful listing anywhere (root or
# not — see walk()'s handling), same "any sign of life resets it" rule as
# the folder-level breaker. Deliberately a smaller count than the folder
# breaker (3, not 5): each trip here costs a full dataset's worth of retry
# time, not one folder's, so less evidence is needed before switching to
# the cheaper probe.
ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 3
ROOT_CIRCUIT_BREAKER_PROBE_BACKOFF = [10]


class _ConnectionClassExhausted(Exception):
    """Raised by _retry_connection_class once its full backoff schedule is
    exhausted for one folder — a distinct wrapper (rather than letting the
    bare underlying exception propagate) so walk()'s circuit breaker can
    recognize "this folder's connection-class retry loop ran out" by type
    alone, without re-classifying the terminal exception itself. Re-
    classifying there would be unreliable: _reraise_cancelled_as_exception
    below already converts a terminal CancelledError into a plain
    RuntimeError before it gets here, which would lose the
    isinstance-checkable "this was connection-class" signal a second
    classification pass in walk() would need.
    """


class CircuitBreakerTripped(Exception):
    """Raised by walk() when CIRCUIT_BREAKER_CONSECUTIVE_FAILURES
    consecutive *different* folders in one dataset's walk have each
    individually exhausted the full connection-class retry loop (a
    _ConnectionClassExhausted each), with no successful or confirmed
    data-class response in between. See CIRCUIT_BREAKER_CONSECUTIVE_FAILURES'
    module-level comment above for the evidence and reasoning behind this."""


class IndexingCancelled(Exception):
    """Raised by walk() when update_progress() (api/core/indexing_queue.py)
    reports that an admin has requested a cancel for this dataset via the
    admin panel's Cancel action (POST /admin/datasets/{id}/cancel-index,
    see api/routes/indexing.py). Checked once per folder, piggybacked onto
    the same per-folder update_progress() write this walk already makes —
    see that function's own docstring for why this needs no separate poll
    loop and why noticing within roughly one folder's latency (rather than
    hard-interrupting whatever single fs.ls() call might currently be
    outstanding) is the deliberate tradeoff here: this worker is strictly
    single-threaded and sequential, one call in flight at a time, so
    "wait for the current call to finish or time out via its own existing
    retry/timeout handling, then notice the flag" bounds the delay to
    however long that one call already takes — no worse than a normal
    connection-class retry cycle — without the real complexity of trying
    to cancel a live network call out from under pelicanfs/aiowebdav2.

    A distinct category from CircuitBreakerTripped above (a system
    decision, made from repeated failure evidence) and from a genuine
    data-class/connection-class failure (something going wrong) — this is
    a deliberate, healthy admin action, and main() below gives it its own
    "cancelled" status/error_category so it reads that way in the admin
    panel and logs rather than looking like any of those.
    """


# Running count of .ls() calls made on the *current* filesystem instance,
# since it was last (re)created — module-level and reset to 0 by either
# safeguard firing, not scoped to a single dataset's walk (see
# PROACTIVE_RESET_CALLS' comment on why this has to span dataset
# boundaries).
_ls_call_count = 0

# Consecutive whole-dataset root-listing failures — see
# ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES' module-level comment. Also
# module-level/cross-dataset for the same reason as _ls_call_count above.
_consecutive_root_failures = 0


# sqlite3.connect()'s default timeout=5.0 (confirmed empirically, not just
# from the docs) governs how long a connection retries against another
# connection's write lock before raising "database is locked" — 5s is far
# too short here: pelican.db is genuinely multi-writer (this worker plus
# every PUN process's own admin-CRUD writes via api/routes/database.py),
# and _finalize_folder_sizes' own single-transaction bulk INSERT SELECT +
# DELETE can itself legitimately hold the write lock for a long time on a
# multi-million-row dataset (GOES-scale) over GPFS-backed shared storage.
# Real incident, 2026-08-31: dataset_id=18 (allencell, 1.4M+ staged rows)
# hit "database is locked" mid-flush after several already-logged "queue
# file atomic write took Ns" warnings pointing at general filesystem
# slowness that morning — a transient stall, not a logic bug, that a
# too-short busy-timeout turned into a lost claim (1.4M folders' progress
# preserved via staging, but the dataset had to resume rather than finish
# cleanly). 30s rides out realistic contention/GPFS slowness while still
# surfacing a genuinely stuck lock in bounded, loud time rather than
# hanging silently.
_SQLITE_BUSY_TIMEOUT_SECONDS = 30

_STAGING_BATCH_SIZE = 200
_STAGING_TABLE_SQL = """CREATE TABLE IF NOT EXISTS dataset_folder_sizes_staging (
    dataset_id INTEGER NOT NULL,
    folder_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    unavailable INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset_id, folder_path)
)"""


def _ensure_unavailable_column(con: sqlite3.Connection, table: str) -> None:
    """`unavailable` (2026-08-05, Part 3): distinguishes a folder that's
    confirmed unrecoverable (missing-data fallback or circuit-breaker abort
    — see walk()'s own handling) from a folder that's genuinely, legitimately
    0 bytes; both used to be stored identically as size_bytes=0, which the
    dataset browser couldn't tell apart. CREATE TABLE IF NOT EXISTS above is
    a no-op against a table that already exists from before this column was
    added (the real shared pelican.db has been running since before this
    week), so this ALTER is the actual migration for any pre-existing
    deployment; "duplicate column" is the expected, harmless outcome once
    it's already been added once (in-process or by an earlier run), same
    "catch and ignore the already-applied case" shape as every other
    lazy-migration in this codebase.
    """
    try:
        con.execute(f"ALTER TABLE {table} ADD COLUMN unavailable INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


def _count_staged(dataset_id: int) -> int:
    # sqlite3.connect()'s default busy-timeout (5s) only bounds waiting on
    # another *SQLite* connection's lock — it does nothing for a raw GPFS-
    # level I/O stall on open()/read() against the shared DB file itself,
    # which would just block here with no exception and no indication of
    # where the process is stuck. Same "log loudly if it's ever slow" fix as
    # api/core/indexing_queue.py's _log_if_slow, applied here since this is
    # the other blocking call in the exact code path implicated in the
    # 2026-08-06 ~60-minute silent stall (this call runs immediately before
    # mark_failed's own queue-file write, in the same failure-handling
    # block).
    start = time.monotonic()
    con = sqlite3.connect(DB_PATH, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        con.execute(_STAGING_TABLE_SQL)
        _ensure_unavailable_column(con, "dataset_folder_sizes_staging")
        row = con.execute(
            "SELECT COUNT(*) FROM dataset_folder_sizes_staging WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        return row[0]
    finally:
        con.close()
        elapsed = time.monotonic() - start
        if elapsed >= 5.0:
            logger.warning(
                "_count_staged(dataset_id=%s) took %.1fs — unusually slow, possible DB/filesystem"
                " contention", dataset_id, elapsed,
            )
        else:
            logger.debug("_count_staged(dataset_id=%s) took %.3fs", dataset_id, elapsed)


def _index_dataset(entry: dict) -> int:
    """Sequentially, recursively lists entry['path'] via pelicanfs, summing
    per-folder byte totals as it goes. Never issues concurrent listing calls
    — per the mentor directive driving this whole design ("we should not use
    Pelican servers for this kind of task" casually), this stays the least
    aggressive option: one namespace, one worker, one listing call in
    flight at a time, always.

    A directory's own `size` from pelicanfs is a meaningless fixed value
    (observed 4096 regardless of contents — there's no filesystem to `du`
    since this is federated, not local), so it's never used here; only file
    sizes and the recursively-computed subtotal of each directory are summed.

    Each completed folder is streamed to a `dataset_folder_sizes_staging`
    table (batched every _STAGING_BATCH_SIZE rows) instead of being kept in
    a path->size dict for the whole walk — that in-memory dict was the OOM
    source on deep datasets like GOES (hundreds of thousands of folders held
    for the entire multi-hour walk). The write buffer bounds memory to a
    fixed size regardless of total folders visited; only the current
    recursion's stack (bounded by directory depth) and the small buffer are
    ever held.

    Resumability: before listing a directory, checks the staging table (not
    memory) for a size already recorded there by a prior crashed run of this
    same dataset_id, and uses it instead of re-listing. walk() only writes a
    folder after all of its children are done (post-order), so a staged
    entry for a path guarantees that path's whole subtree already finished
    before the crash — safe to trust and skip.

    Returns folders_visited_count; per-folder sizes live in the staging
    table for _finalize_folder_sizes to pick up.
    """
    fs = _resolve_filesystem(entry["path"])
    dataset_id = entry["dataset_id"]
    con = sqlite3.connect(DB_PATH, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
    con.execute(_STAGING_TABLE_SQL)
    _ensure_unavailable_column(con, "dataset_folder_sizes_staging")
    folders_visited = 0
    buffer: list[tuple[int, str, int, int]] = []
    # See CIRCUIT_BREAKER_CONSECUTIVE_FAILURES' module-level comment.
    # Counts *different* folders that each exhausted the connection-class
    # retry loop in a row; reset to 0 by any successful listing or any
    # confirmed data-class response (see walk() below).
    consecutive_connection_failures = 0

    def flush() -> None:
        if not buffer:
            return
        con.executemany(
            "INSERT OR REPLACE INTO dataset_folder_sizes_staging"
            " (dataset_id, folder_path, size_bytes, unavailable) VALUES (?, ?, ?, ?)",
            buffer,
        )
        con.commit()
        buffer.clear()

    def staged_size(path: str) -> int | None:
        row = con.execute(
            "SELECT size_bytes FROM dataset_folder_sizes_staging WHERE dataset_id = ? AND folder_path = ?",
            (dataset_id, path.rstrip("/")),
        ).fetchone()
        return row[0] if row else None

    def _reraise_cancelled_as_exception(exc: BaseException) -> None:
        # CancelledError is a BaseException, not an Exception — main()'s
        # except Exception (and this task's own spec: fall through to
        # mark_failed once retries are exhausted) needs a real Exception to
        # catch, or a CancelledError surviving to the end of a retry loop
        # would crash the whole worker process instead of just failing this
        # one dataset. Re-raised as a plain RuntimeError carrying the same
        # message so mark_failed still gets something meaningful to report.
        if isinstance(exc, asyncio.CancelledError):
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        raise exc

    def _retry_connection_class(path: str, encoded: str, first_exc: BaseException) -> list:
        """The full retry loop for exceptions _is_connection_error
        recognizes — see CONNECTION_RETRY_BACKOFFS' module-level comment
        for the reasoning behind the schedule. Resets the filesystem
        singleton before every attempt (existing behavior, kept) since
        these are specifically session/connection-pressure failures a
        fresh instance plausibly helps with.

        Root-listing calls (path == entry["path"]) swap down to
        ROOT_CIRCUIT_BREAKER_PROBE_BACKOFF once the root breaker is already
        tripped — see ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES' module-level
        comment. Non-root calls always use the full schedule; a subfolder
        failure mid-walk is exactly the case the existing folder-level
        breaker already handles.
        """
        nonlocal fs
        global _ls_call_count
        is_root = path == entry["path"]
        root_breaker_active = is_root and _consecutive_root_failures >= ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES
        schedule = ROOT_CIRCUIT_BREAKER_PROBE_BACKOFF if root_breaker_active else CONNECTION_RETRY_BACKOFFS
        if root_breaker_active:
            logger.warning(
                "Root circuit breaker active for dataset_id=%s (%d consecutive dataset root"
                " failures) — probing with a single short retry instead of the full schedule",
                dataset_id, _consecutive_root_failures,
            )
        exc = first_exc
        for attempt, backoff in enumerate(schedule, start=1):
            logger.warning(
                "Connection-class retry %d/%d for dataset_id=%s: %s (%d calls on this instance)"
                " — resetting, waiting %ds, then retrying %r",
                attempt, len(schedule), dataset_id, type(exc).__name__,
                _ls_call_count, backoff, path,
            )
            reset_default_filesystem()
            _ls_call_count = 0
            fs = _resolve_filesystem(entry["path"])
            time.sleep(backoff)
            try:
                return fs.ls(encoded)
            except BaseException as e:
                if not _is_connection_error(e):
                    # The retry surfaced a *different kind* of failure —
                    # most plausibly the reset+wait cleared whatever was
                    # transient and this is the dataset's real,
                    # non-connection problem (e.g. a genuine 404 that was
                    # masked by the original connection error). Stop
                    # spending more retries/origin requests on the
                    # connection-class schedule; deliberately doesn't hand
                    # this off to the unclassified path's own small
                    # allowance either — a failure that changed shape
                    # mid-retry is exactly the kind of surprising case
                    # worth stopping on and reporting plainly, not
                    # guessing at further.
                    logger.warning(
                        "Connection-class retry for dataset_id=%s surfaced a different"
                        " exception type (%s) — stopping retries, reporting it directly",
                        dataset_id, type(e).__name__,
                    )
                    raise
                exc = e
        logger.error(
            "Connection-class retries exhausted for dataset_id=%s after %d attempt(s): %s",
            dataset_id, len(schedule) + 1, type(exc).__name__,
        )
        try:
            _reraise_cancelled_as_exception(exc)
        except BaseException as final_exc:
            raise _ConnectionClassExhausted(f"{type(final_exc).__name__}: {final_exc}") from final_exc

    def _retry_unclassified(path: str, encoded: str, first_exc: BaseException) -> list:
        """Small, separate safety net for exceptions _is_connection_error
        doesn't recognize — see UNCLASSIFIED_RETRY_ATTEMPTS' module-level
        comment for why this is deliberately shallower than the
        connection-class loop above (fewer attempts, flat backoff, no
        filesystem reset).
        """
        exc = first_exc
        for attempt in range(1, UNCLASSIFIED_RETRY_ATTEMPTS + 1):
            logger.warning(
                "Unclassified-exception retry %d/%d for dataset_id=%s: %s"
                " — waiting %ds, then retrying %r",
                attempt, UNCLASSIFIED_RETRY_ATTEMPTS, dataset_id, type(exc).__name__,
                UNCLASSIFIED_RETRY_BACKOFF_SECONDS, path,
            )
            time.sleep(UNCLASSIFIED_RETRY_BACKOFF_SECONDS)
            try:
                return fs.ls(encoded)
            except BaseException as e:
                exc = e
        logger.error(
            "Unclassified-exception retries exhausted for dataset_id=%s after %d attempt(s): %s",
            dataset_id, UNCLASSIFIED_RETRY_ATTEMPTS + 1, type(exc).__name__,
        )
        _reraise_cancelled_as_exception(exc)

    def list_path(path: str) -> list:
        """The one place this walk calls fs.ls() — dispatches a failure into
        exactly one of three buckets:
          - connection-class (_is_connection_error): the full retry loop.
          - confirmed data-class (classify_failure's auth_required/
            not_found/permission — a genuine, well-understood non-connection
            failure, e.g. a real RemoteResourceNotFoundError): zero retries,
            propagates immediately, unchanged from before this retry-loop
            work. Reuses classify_failure rather than a fresh check here so
            this stays in sync with the same categories the admin
            panel/downloads/Quick Access already treat as "confirmed,
            don't-retry" shapes of failure — no separate classification
            scheme to keep in sync by hand.
          - genuinely unclassified (classify_failure's "unknown" catch-all):
            the small safety-net retry.
        Getting this dispatch right matters: an earlier version of this
        change routed *everything* that wasn't connection-class into the
        unclassified retry, including confirmed 404s — caught during
        verification (a simulated RemoteResourceNotFoundError was getting a
        pointless extra origin request and a 10s wait before still failing,
        exactly the "don't retry confirmed-missing data" rule this task
        explicitly requires staying zero-retry).

        The proactive-reset check below is a clean post-step, applied once
        a result is finally in hand regardless of whether it took 1 attempt
        or several retries to get there — this is deliberate: retries
        never increment _ls_call_count themselves (each connection-class
        retry already resets it to 0 via reset_default_filesystem), so
        there's no special-casing needed for a proactive threshold being
        crossed mid-retry-sequence; the counter and the check both only
        ever see "how many successful calls has the *current* instance
        served," exactly as before this change.
        """
        nonlocal fs
        global _ls_call_count

        # _encode_path_segment: see its docstring in api/routes/pelican.py
        # for the confirmed root cause this works around (aiowebdav2
        # silently unescaping every .ls() path before it reaches the
        # wire) — path itself stays the raw, human-readable string
        # everywhere else in this walk (staged_size, buffer, logging);
        # only the actual fs.ls() call gets the encoded copy.
        encoded = _encode_path_segment(path)
        try:
            result = fs.ls(encoded)
        except BaseException as e:
            if _is_connection_error(e):
                result = _retry_connection_class(path, encoded, e)
            elif classify_failure(e).code != "unknown":
                # Confirmed data-class (auth_required/not_found/permission)
                # — a reset+retry can't fix genuinely missing data or a
                # missing token, and retrying would just be an unnecessary
                # extra request against the origin, contrary to the mentor
                # directive against casual repeated listing calls.
                raise
            else:
                result = _retry_unclassified(path, encoded, e)

        _ls_call_count += 1
        if _ls_call_count >= PROACTIVE_RESET_CALLS:
            logger.info(
                "Proactive filesystem reset for dataset_id=%s after %d .ls() calls (threshold=%d)",
                dataset_id, _ls_call_count, PROACTIVE_RESET_CALLS,
            )
            reset_default_filesystem()
            _ls_call_count = 0
            fs = _resolve_filesystem(entry["path"])

        return result

    def _update_progress_and_check_cancel(folders_done: int) -> None:
        # The one place both of walk()'s update_progress() calls go through
        # — see IndexingCancelled's own docstring for why checking here,
        # once per folder, is the right granularity (piggybacked on a write
        # this walk already makes, no new poll loop).
        if update_progress(dataset_id, folders_done):
            raise IndexingCancelled(
                f"Indexing of dataset_id={dataset_id} was cancelled by an admin."
            )

    def walk(path: str) -> int:
        nonlocal folders_visited, consecutive_connection_failures
        global _consecutive_root_failures
        cached = staged_size(path)
        if cached is not None:
            folders_visited += 1
            _update_progress_and_check_cancel(folders_visited)
            return cached

        folder_unavailable = False
        try:
            entries = list_path(path)
            # A successful listing is the strongest possible signal the
            # origin/Director is currently answering normally — see
            # CIRCUIT_BREAKER_CONSECUTIVE_FAILURES' comment on why this
            # resets the streak rather than just decaying it. Also resets
            # the root breaker's streak (ROOT_CIRCUIT_BREAKER_CONSECUTIVE_
            # FAILURES) — any successful listing anywhere, not just at the
            # root, is equally strong evidence the origin isn't down.
            consecutive_connection_failures = 0
            _consecutive_root_failures = 0
        except Exception as e:
            # list_path already ran this failure through the full retry
            # treatment its classification calls for (the full
            # connection-class backoff loop, the small unclassified
            # allowance, or zero retries for a confirmed data-class
            # failure like a genuine 404 — see list_path's own docstring)
            # before ever raising here. There is deliberately no further
            # fallback attempt beyond that: Phase 1 of the 2026-08-04
            # investigation into this confirmed neither of the two
            # candidate fallbacks is worth building.
            #   - Reusing size metadata already present in the parent's
            #     own listing response for this child doesn't work: a
            #     directory entry's `size` field is a fixed filesystem-
            #     block-size placeholder (observed 4096 regardless of
            #     actual contents — see this function's own docstring
            #     above, from the original Phase 0 investigation), not a
            #     real recursive total, so it can't stand in for a
            #     genuine size even if reused for free.
            #   - A direct existence/size check on the failing path
            #     (e.g. a WebDAV HEAD-equivalent) turns out to already be
            #     exactly what pelicanfs's own _ls_real does internally
            #     before it ever raises FileNotFoundError to us — read
            #     directly in the installed library: on a
            #     RemoteResourceNotFoundError, it calls client.check(),
            #     which issues the *same* PROPFIND method as the original
            #     failed listing, against the same path, moments earlier.
            #     A second attempt from our own code would almost
            #     certainly just repeat that same request against the
            #     same origin for the same answer — real load for no
            #     realistic benefit, contrary to the mentor directive
            #     against casual repeated federation calls.
            #
            # Branden's explicit decision given that: no incomplete-total
            # flag, no "at least X" framing, no separate uncertainty
            # tracking — this folder's contribution is simply 0, logged
            # clearly enough that a human (or a future Claude Code
            # session) reading the log can tell this dataset's total may
            # be missing real data, without building anything further
            # than that.
            #
            # The dataset's own root is deliberately excluded from this —
            # if entry["path"] itself can't be listed at all, there's no
            # "rest of the walk" to continue past; unlike a subfolder deep
            # in an otherwise-working walk, that means this indexing
            # attempt accomplished nothing, and silently recording a
            # "complete, total=0" result would be genuinely misleading
            # (indistinguishable from a dataset that legitimately has
            # zero bytes). Re-raising here preserves today's existing
            # behavior for that case unchanged — mark_failed at the
            # per-dataset level, exactly as before this task.
            if path == entry["path"]:
                # Root circuit breaker (see ROOT_CIRCUIT_BREAKER_CONSECUTIVE_
                # FAILURES' module-level comment): only a
                # _ConnectionClassExhausted at the root counts — same
                # confirmed-origin-trouble reasoning as the folder-level
                # breaker below, just tracked across dataset boundaries
                # instead of within one dataset's walk, since a root
                # failure never gets far enough into this function to reach
                # that one.
                if isinstance(e, _ConnectionClassExhausted):
                    _consecutive_root_failures += 1
                    logger.warning(
                        "Root circuit breaker: %d/%d consecutive dataset root-listing"
                        " failures (dataset_id=%s)",
                        _consecutive_root_failures, ROOT_CIRCUIT_BREAKER_CONSECUTIVE_FAILURES,
                        dataset_id,
                    )
                raise

            # Circuit breaker (see CIRCUIT_BREAKER_CONSECUTIVE_FAILURES'
            # module-level comment): only a _ConnectionClassExhausted
            # counts toward the streak — a confirmed data-class response
            # (real 404/401/403, classify_failure().code != "unknown")
            # is itself evidence the origin is reachable and answering
            # normally, so it resets the streak rather than leaving it
            # alone; a genuinely unclassified exhaustion is left as-is,
            # since it's neither confirmed origin trouble nor confirmed
            # origin health.
            if isinstance(e, _ConnectionClassExhausted):
                consecutive_connection_failures += 1
                logger.warning(
                    "Circuit breaker: %d/%d consecutive connection-class folder"
                    " failures for dataset_id=%s",
                    consecutive_connection_failures, CIRCUIT_BREAKER_CONSECUTIVE_FAILURES, dataset_id,
                )
                if consecutive_connection_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILURES:
                    logger.error(
                        "Circuit breaker TRIPPED for dataset_id=%s: %d consecutive folders failed"
                        " with connection-class errors (most recent: %s) — aborting this dataset"
                        " early rather than continuing to retry against what looks like sustained"
                        " origin/Director trouble",
                        dataset_id, consecutive_connection_failures, e,
                    )
                    raise CircuitBreakerTripped(
                        f"Origin appears unreachable or sustained-degraded: "
                        f"{consecutive_connection_failures} consecutive folders failed with "
                        f"connection-class errors (most recent: {e})."
                    ) from e
            elif classify_failure(e).code != "unknown":
                consecutive_connection_failures = 0

            logger.error(
                "Folder unrecoverable for dataset_id=%s after exhausting retries: %s: %s"
                " — no viable fallback access method (see walk()'s own comment for why) —"
                " counting %r as 0 bytes and continuing the walk",
                dataset_id, type(e).__name__, e, path,
            )
            entries = []
            # Part 3 (2026-08-05): distinct from a folder that's genuinely,
            # legitimately 0 bytes — see _ensure_unavailable_column's
            # docstring. Both the missing-data-fallback path above and a
            # circuit-breaker-triggered abort (the folders that already ran
            # through this same except block on the way to tripping —
            # CircuitBreakerTripped itself is raised above, before reaching
            # here, only for the one folder whose failure crosses the
            # threshold, so that one folder's own row is never staged at
            # all; the dataset ends up "failed", not "complete", exactly
            # like a root-path failure) both flow through this one
            # "unrecoverable, log and continue/abort" branch, so marking it
            # unavailable here covers both causes with one line, matching
            # Branden's "one shared badge, not two" call.
            folder_unavailable = True

        total = 0
        for item in entries:
            if item["type"] == "directory":
                total += walk(item["name"])
            else:
                total += item.get("size") or 0
        folders_visited += 1
        buffer.append((dataset_id, path.rstrip("/"), total, int(folder_unavailable)))
        if len(buffer) >= _STAGING_BATCH_SIZE:
            flush()
        _update_progress_and_check_cancel(folders_visited)
        return total

    try:
        walk(entry["path"])
        flush()
    except (CircuitBreakerTripped, IndexingCancelled):
        # Same reasoning as a normal mid-walk crash (see main()'s own
        # comment on this): nothing staged so far should be lost just
        # because this dataset is being aborted early rather than
        # crashing outright — flush the buffer one last time before
        # letting this propagate to main()'s except block. A cancel gets
        # exactly the same resumable-staged-progress treatment as every
        # other abort here (see main()'s own comment for why that's the
        # deliberate choice, not an oversight).
        flush()
        raise
    finally:
        con.close()

    return folders_visited


def _finalize_folder_sizes(dataset_id: int) -> None:
    """The one write to the *real* catalog table for the whole job —
    deliberately a single transaction at the end, copying the dataset's
    now-complete staged rows over and clearing staging, to minimize
    contention with the catalog db's other occasional writers (the per-user
    admin CRUD panel, api/routes/database.py). Everything that happened
    during the walk itself went to the separate staging table, not this one.
    The dataset's own total is just the row for its own root path — there's
    no separate "total" column, since Phase 0 found the requirement is
    genuinely per-folder data, and the root-path row already *is* the total.
    """
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB_PATH, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS dataset_folder_sizes (
                dataset_id INTEGER NOT NULL,
                folder_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                indexed_at TEXT NOT NULL,
                unavailable INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (dataset_id, folder_path)
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_dataset_folder_sizes_folder_path"
            " ON dataset_folder_sizes(folder_path)"
        )
        _ensure_unavailable_column(con, "dataset_folder_sizes")
        con.execute(_STAGING_TABLE_SQL)
        _ensure_unavailable_column(con, "dataset_folder_sizes_staging")
        con.execute("DELETE FROM dataset_folder_sizes WHERE dataset_id = ?", (dataset_id,))
        con.execute(
            """INSERT INTO dataset_folder_sizes (dataset_id, folder_path, size_bytes, indexed_at, unavailable)
               SELECT dataset_id, folder_path, size_bytes, ?, unavailable
               FROM dataset_folder_sizes_staging WHERE dataset_id = ?""",
            (now, dataset_id),
        )
        con.execute("DELETE FROM dataset_folder_sizes_staging WHERE dataset_id = ?", (dataset_id,))
        con.commit()
    finally:
        con.close()


def main() -> None:
    _register_stackdump_handler()
    recover_interrupted()
    deadline = time.monotonic() + WALLTIME_HOURS * 3600 - RENEWAL_MARGIN_MINUTES * 60
    logger.info("Indexing worker started, renewal margin %d min", RENEWAL_MARGIN_MINUTES)

    while True:
        if time.monotonic() >= deadline:
            logger.info(
                "Approaching walltime limit — exiting %d so the host sbatch script resubmits the"
                " next cycle", RESUBMIT_EXIT_CODE,
            )
            sys.exit(RESUBMIT_EXIT_CODE)

        entry = claim_next_pending()
        if entry is None:
            time.sleep(IDLE_POLL_SECONDS)
            continue

        logger.info("Indexing dataset_id=%s path=%s", entry["dataset_id"], entry["path"])
        try:
            folders_visited = _index_dataset(entry)
            _finalize_folder_sizes(entry["dataset_id"])
            mark_complete(entry["dataset_id"], folders_visited)
            logger.info("Completed dataset_id=%s (%d folders)", entry["dataset_id"], folders_visited)
        except Exception as e:
            cancelled = isinstance(e, IndexingCancelled)
            if cancelled:
                # A deliberate admin action, not an error — see
                # IndexingCancelled's own docstring. Handled first and
                # separately from every category below: it gets its own
                # "cancelled" *status* (via mark_cancelled, not mark_failed
                # — see the end of this block), not just an error_category
                # under "failed", so the admin panel and history read it as
                # a healthy stop rather than any kind of failure.
                error_category = "cancelled"
                message = str(e)
            elif isinstance(e, CircuitBreakerTripped):
                # A confirmed, well-understood category in its own right
                # (see CIRCUIT_BREAKER_CONSECUTIVE_FAILURES' module-level
                # comment) — not the genuinely-surprising "unknown" catch-all
                # classify_failure would otherwise sort this into, so this
                # is checked first and skips log_unexpected_pelican_error
                # below, same as every other already-understood category.
                error_category = "circuit_breaker"
                message = str(e)
            elif _is_auth_required(e):
                error_category = "auth_required"
                message = f'"{entry["path"]}" requires an access token and none is stored for it.'
            else:
                # classify_failure instead of the old bare str(e)/type(e).__name__
                # fallback — same honest-categories reasoning as
                # api/routes/pelican.py's download_one_file, so the admin
                # panel gets a consistent, classified reason instead of a
                # raw exception string that happened to differ in shape
                # from what the downloads/Quick Access surfaces show.
                category = classify_failure(e)
                error_category = category.code
                message = category.message
                if category.code == "unknown":
                    # Same "only log the genuinely surprising case" split as
                    # download_one_file — gives Branden the same
                    # no-Anvil-shell-access-needed diagnostic surface for
                    # indexing failures that already existed for web-request
                    # failures via this same log file.
                    log_unexpected_pelican_error(entry["path"], e)
            if cancelled:
                # info, not exception — a full traceback dump would read as
                # a crash, which this deliberately isn't.
                logger.info("Indexing cancelled for dataset_id=%s", entry["dataset_id"])
            else:
                logger.exception("Indexing failed for dataset_id=%s", entry["dataset_id"])
            # Nothing is lost on a mid-walk crash — or a cancel, which gets
            # exactly the same treatment: every folder finished so far was
            # already streamed to dataset_folder_sizes_staging (see
            # _index_dataset), not held in memory. Surface that here instead
            # of letting it sit as a silent mystery, and leave it in place —
            # the next claim of this dataset_id resumes from it rather than
            # re-walking from scratch. Cancelling on purpose is not evidence
            # the partial data is bad; there's no reason to make a deliberate
            # stop *more* wasteful to recover from than an accidental crash.
            partial_count = _count_staged(entry["dataset_id"])
            if partial_count:
                message += (
                    f" ({partial_count} folders already indexed and saved to staging —"
                    " will resume from there on retry.)"
                )
                logger.info(
                    "Partial progress preserved for dataset_id=%s: %d folders staged",
                    entry["dataset_id"], partial_count,
                )
            if cancelled:
                mark_cancelled(entry["dataset_id"], folders_done=partial_count or None)
            else:
                mark_failed(entry["dataset_id"], message, folders_done=partial_count or None, error_category=error_category)
            # No re-raise: a failed job (or a cancel) must not take the
            # worker process down with it — the loop goes back to idling
            # and stays able to pick up the next queued entry immediately,
            # per Phase 1's failure-handling spec and this feature's own
            # "does not stop the worker" scope constraint.


if __name__ == "__main__":
    main()
