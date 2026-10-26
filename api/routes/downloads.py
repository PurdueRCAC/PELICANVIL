from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import sqlite3, os, json, threading, uuid, logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from api.core.config import DOWNLOADS_DB_PATH
from api.routes.pelican import download_one_file, DownloadError, DownloadAuthRequiredError

downloadsRouter = APIRouter()

logger = logging.getLogger("pelican-ui.downloads")

# Serializes all writes to downloads_history.db within this process. SQLite
# only ever allows one writer at a time regardless (readers can proceed
# concurrently under WAL, below) — this lock just avoids threads in this
# process fighting each other for that single writer slot and surfacing
# "database is locked" errors under normal load. It does NOT protect against
# a second OS process (e.g. if Passenger ever runs >1 worker process) writing
# at the same moment; cross-process safety instead relies on the busy_timeout
# set on every connection (see _get_connection), which makes SQLite retry for
# up to 10s instead of failing immediately if the file is locked elsewhere.
_db_write_lock = threading.Lock()

# Caps how many downloads run in the background at once. This is a single-
# user OOD sandbox app, each job already downloads its files sequentially
# internally, and Anvil's shared filesystem/network doesn't benefit from
# many large transfers hammering it in parallel — 3 lets a couple of batches
# overlap without thrashing, while still bounding worst-case resource use.
_job_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="download-job")

VALID_FINISH_STATUSES = ("complete", "partial", "failed")

# sentinel so _update_job can tell "leave error_message alone" apart from
# "set error_message to NULL" (a legitimate value on success)
_UNSET = object()


def _get_connection():
    os.makedirs(os.path.dirname(DOWNLOADS_DB_PATH), exist_ok=True)
    # timeout=10 makes sqlite3 retry for up to 10s (its busy_timeout) instead
    # of immediately raising "database is locked" if another connection —
    # in this process or, in theory, another one — is mid-write.
    con = sqlite3.connect(DOWNLOADS_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    # WAL lets reads (e.g. the status-polling endpoint, the /downloads page)
    # proceed without blocking on an in-progress write from a job thread.
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _init_db():
    con = _get_connection()
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            destination TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress',
            item_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error_message TEXT
        )"""
    )
    # files wasn't in the original schema — add it for rows created before
    # this column existed rather than requiring a fresh DB
    existing_columns = {row["name"] for row in cur.execute("PRAGMA table_info(download_history)")}
    if "files" not in existing_columns:
        cur.execute("ALTER TABLE download_history ADD COLUMN files TEXT")

    # download_jobs is the live/in-progress counterpart to download_history:
    # download_history is the finished/historical record (one row written on
    # start, updated once on completion); download_jobs is the row a running
    # background thread updates repeatedly as it works through a batch, and
    # what the status-polling endpoint reads from. A job's terminal state is
    # what gets copied into download_history's finish update.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS download_jobs (
            job_id TEXT PRIMARY KEY,
            history_id INTEGER,
            name TEXT NOT NULL,
            destination TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            item_count INTEGER NOT NULL DEFAULT 0,
            files TEXT,
            error_message TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    con.commit()
    con.close()


_init_db()


class DownloadJobStart(BaseModel):
    name: str
    destination: str
    paths: list[str]
    # path -> size in bytes, for whichever selected paths had a known real
    # size at selection time (see summarizeSelectionSizes in
    # datasets.js/quick-access.js — files always, folders only if indexed).
    # Persisted onto each file's own job-status entry below so a
    # byte-accurate progress percentage can be computed from the *same*
    # polled status by any page watching this job (the toast that started
    # it, or a completely separate /downloads/history page load) — the
    # frontend that started the download is the only place this was ever
    # known, so it has to be sent here to be a shared, not toast-private, fact.
    sizes: dict[str, int] | None = None


def _create_history_record(name: str, destination: str, item_count: int) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO download_history (name, destination, status, item_count, started_at) VALUES (?, ?, 'in_progress', ?, ?)",
            (name, destination, item_count, started_at),
        )
        con.commit()
        new_id = cur.lastrowid
        con.close()
    return new_id


def _finish_history_record(record_id: int, status: str, error_message: str | None, files: list[dict] | None) -> None:
    finished_at = datetime.now(timezone.utc).isoformat()
    files_json = json.dumps(files) if files is not None else None
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute(
            "UPDATE download_history SET status = ?, finished_at = ?, error_message = ?, files = ? WHERE id = ?",
            (status, finished_at, error_message, files_json, record_id),
        )
        con.commit()
        con.close()


def _finalize_job_and_history(job_id: str, history_id: int, status: str, error_message: str | None, job_files: list[dict], history_files: list[dict]) -> None:
    """Writes the terminal state to download_jobs and download_history in ONE
    transaction. They used to be two separate commits (_update_job, then
    _finish_history_record); a kill landing between them left the job
    'complete'/'partial'/'failed' but its history row stuck at 'in_progress',
    and since recovery only scans non-terminal *jobs*, nothing ever repaired
    the history row."""
    now = datetime.now(timezone.utc).isoformat()
    with _db_write_lock:
        con = _get_connection()
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE download_jobs SET status = ?, files = ?, error_message = ?, updated_at = ? WHERE job_id = ?",
                (status, json.dumps(job_files), error_message, now, job_id),
            )
            cur.execute(
                "UPDATE download_history SET status = ?, finished_at = ?, error_message = ?, files = ? WHERE id = ?",
                (status, now, error_message, json.dumps(history_files), history_id),
            )
            con.commit()
        finally:
            con.close()


def _reconcile_history_with_jobs() -> None:
    """Repairs history rows left 'in_progress' beneath a job that already
    reached a terminal status (from the pre-atomic-finalize race, or any
    other partial write). The job row is the source of truth."""
    with _db_write_lock:
        con = _get_connection()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT job_id, history_id, status, files, error_message FROM download_jobs "
                "WHERE status IN ('complete', 'partial', 'failed') AND history_id IS NOT NULL "
                # only the newest job per history row — restarts reuse the history id
                "AND rowid = (SELECT MAX(j2.rowid) FROM download_jobs j2 WHERE j2.history_id = download_jobs.history_id) "
                "AND history_id IN (SELECT id FROM download_history WHERE status != download_jobs.status)"
            )
            for job in cur.fetchall():
                files = json.loads(job["files"]) if job["files"] else []
                cur.execute(
                    "UPDATE download_history SET status = ?, finished_at = COALESCE(finished_at, ?), error_message = ?, files = ? WHERE id = ?",
                    (job["status"], datetime.now(timezone.utc).isoformat(), job["error_message"],
                     json.dumps([_history_file_entry(f) for f in files]), job["history_id"]),
                )
                logger.info("Reconciled history_id=%s to job status %s", job["history_id"], job["status"])
            con.commit()
        finally:
            con.close()


def _update_job(job_id: str, status: str | None = None, files: list[dict] | None = None, error_message=_UNSET) -> None:
    updates = ["updated_at = ?"]
    params = [datetime.now(timezone.utc).isoformat()]
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if files is not None:
        updates.append("files = ?")
        params.append(json.dumps(files))
    if error_message is not _UNSET:
        updates.append("error_message = ?")
        params.append(error_message)
    params.append(job_id)
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute(f"UPDATE download_jobs SET {', '.join(updates)} WHERE job_id = ?", params)
        con.commit()
        con.close()


def _files_with_sizes(paths: list[str], sizes: dict[str, int] | None, initial_status: str = "pending") -> list[dict]:
    """Shared by _enqueue_job's initial DB row and _run_download_job's own
    working copy — both build a fresh files list from `paths` independently
    (the background thread doesn't read back what _enqueue_job wrote), so
    both need to attach `size` the same way or the field would silently
    disappear the first time _run_download_job's own list overwrites it.

    initial_status defaults to "pending" (never attempted yet) for a fresh
    download. Restart callers pass "retrying" instead — a status distinct
    from both "pending" (never touched) and the eventual terminal
    "succeeded"/"failed", so a file actively being retried is visually
    distinguishable in the UI from one that simply hasn't started yet (the
    per-file restart work added 2026-08-04)."""
    result = []
    for p in paths:
        entry = {"path": p, "status": initial_status}
        if sizes and sizes.get(p) is not None:
            entry["size"] = sizes[p]
        result.append(entry)
    return result


def _history_file_entry(f: dict) -> dict:
    """path+status is all download_history.files has ever carried — kept
    that way (no size) so a finished record stays small. The classified
    .error/.error_category *are* added here for failed files (2026-08-04
    failure-visibility work) — without them, the Downloads page would only
    ever show a failure reason during live polling (download_jobs.files has
    them) and lose it again the moment a finished job's terminal state gets
    persisted into download_history and the page is reloaded."""
    entry = {"path": f["path"], "status": "succeeded" if f["status"] == "succeeded" else "failed"}
    if entry["status"] == "failed" and f.get("error"):
        entry["error"] = f["error"]
        entry["error_category"] = f.get("error_category", "unknown")
    return entry


def _run_download_job(job_id: str, history_id: int, destination: str, paths: list[str], preserved_files: list[dict] | None = None, sizes: dict[str, int] | None = None, is_restart: bool = False) -> None:
    # Runs on a background thread from _job_executor — this function is what
    # actually replaces the old request-held-open behavior. It reuses
    # download_one_file (api/routes/pelican.py) unchanged; only where it's
    # invoked from has changed.
    _update_job(job_id, status="in_progress")

    # files always reflects the *complete* set for this download — preserved
    # entries from a prior attempt (see restartDownloadRecord/
    # restartDownloadFile) come first, kept exactly as they already were
    # (succeeded stays succeeded; for a per-file restart, any *other*
    # still-failed file not being retried this round stays failed too, not
    # silently cleared or retried), followed by this run's own files
    # (retry_files, index-aligned with `paths` so the loop below can keep
    # mutating them by position). Built as one list, up front, so every
    # _update_job(job_id, files=files) call below — including the very
    # first one, before any file has even started — already contains the
    # preserved entries.
    #
    # This is the fix for the restart bug found 2026-08-04: download_jobs.files
    # used to be built from `paths` (the retry subset) alone, so anything
    # polling this job's live status mid-restart — and the Downloads page's
    # terminal-state render, which reads straight from this same job status,
    # not download_history — saw only the retried files; the previously-
    # succeeded ones appeared to vanish until a manual page refresh finally
    # read the *separately* correct download_history record.
    # download_history's own merge was always correct; this was purely a
    # download_jobs gap. preserved/retry_files are the same dict objects
    # `files` holds (list concatenation copies references, not the dicts
    # themselves), so mutating retry_files[i] below is mutating files too —
    # no separate merge step needed at the end like the old code had.
    preserved = list(preserved_files or [])
    retry_files = _files_with_sizes(paths, sizes, initial_status="retrying" if is_restart else "pending")
    files = preserved + retry_files
    _update_job(job_id, files=files)

    succeeded = [f["path"] for f in preserved if f.get("status") == "succeeded"]
    failed = [f["path"] for f in preserved if f.get("status") == "failed"]

    for i, path in enumerate(paths):
        try:
            download_one_file(path, destination)
            retry_files[i]["status"] = "succeeded"
            succeeded.append(path)
        except DownloadAuthRequiredError as e:
            # Flagged distinctly from a plain DownloadError so the frontend's
            # poll loop (downloadFromPath in quick-access.js/datasets.js) can
            # tell "needs a token" apart from "actually broke" and pop the
            # same token modal browsing uses, instead of just reporting a
            # generic failure.
            retry_files[i]["status"] = "failed"
            retry_files[i]["error"] = str(e)
            retry_files[i]["error_category"] = e.category
            retry_files[i]["auth_required"] = True
            retry_files[i]["namespace"] = e.namespace
            failed.append(path)
        except DownloadError as e:
            retry_files[i]["status"] = "failed"
            retry_files[i]["error"] = str(e)
            retry_files[i]["error_category"] = e.category
            failed.append(path)
        except Exception:
            # Belt-and-suspenders: download_one_file already wraps unexpected
            # exceptions in DownloadError, but a job thread that dies here
            # with an unhandled exception would leave this job (and its
            # history row) stuck in "in_progress" forever with nothing to
            # report why — worse than recording a generic failure and moving on.
            logger.exception("Unexpected error downloading %s for job %s", path, job_id)
            retry_files[i]["status"] = "failed"
            retry_files[i]["error"] = "Unexpected error. Check server logs."
            retry_files[i]["error_category"] = "unknown"
            failed.append(path)
        _update_job(job_id, files=files)

    total_count = len(files)
    if not failed:
        final_status = "complete"
        error_message = None
    elif not succeeded:
        final_status = "failed"
        error_message = f"{len(failed)} of {total_count} item(s) failed to download."
    else:
        final_status = "partial"
        error_message = f"{len(failed)} of {total_count} item(s) failed to download."

    # files already IS the full preserved+retry set (see above), so no
    # separate combined_files merge is needed — final_status/error_message
    # reflect the whole picture since `succeeded` was seeded with the
    # preserved paths from the start. Job + history are written atomically.
    history_files = [_history_file_entry(f) for f in files]
    _finalize_job_and_history(job_id, history_id, final_status, error_message, files, history_files)


def _enqueue_job(history_id: int, name: str, destination: str, paths: list[str], preserved_files: list[dict] | None = None, sizes: dict[str, int] | None = None, is_restart: bool = False) -> str:
    # Shared by startDownloadJob and both restart routes below — all boil
    # down to "create a download_jobs row and hand the actual transfer off to
    # a background thread", they just differ in how the history_id/paths they
    # pass in were obtained. Submitting to the pool just enqueues the call and
    # returns immediately — the actual transfer work happens on whichever
    # worker thread picks it up, entirely off the request that called this.
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    # Includes preserved_files here too, kept exactly as they already were
    # (see _run_download_job's own comment on why nothing forces their
    # status), not just the retry subset in `paths` — otherwise there'd be
    # a brief window between this insert and _run_download_job's own first
    # _update_job call where the row already exists but still shows only
    # the retried files, the same gap the 2026-08-04 restart-bug fix closed
    # inside _run_download_job itself.
    preserved = list(preserved_files or [])
    files = preserved + _files_with_sizes(paths, sizes, initial_status="retrying" if is_restart else "pending")
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO download_jobs (job_id, history_id, name, destination, status, item_count, files, started_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (job_id, history_id, name, destination, len(files), json.dumps(files), now, now),
        )
        con.commit()
        con.close()
    _job_executor.submit(_run_download_job, job_id, history_id, destination, paths, preserved_files, sizes, is_restart)
    return job_id


def _recover_interrupted_jobs() -> None:
    """Called once at process start, right after _init_db() — same
    "best-effort work done once per PUN process import" convention as
    main.py's migrate_category_icons() (wrapped in try/except there for the
    same reason this call site is below).

    GitHub issue #6, Part 3: if the PUN process dies mid-download (Passenger
    recycling, an app restart, a node/session issue), the background thread
    running _run_download_job dies with it, without ever reaching its own
    terminal _update_job/_finish_history_record calls — so that job's
    download_jobs row is left stuck at whatever status ('pending' if the
    executor hadn't even picked it up yet, 'in_progress' otherwise) it last
    had, and its download_history counterpart stays 'in_progress' forever
    too. There was previously no code anywhere that detected this on the
    next process start — confirmed directly by reading
    api/core/indexing_queue.py's own recover_interrupted(), whose docstring
    states this gap explicitly (that function exists for the separate
    indexing worker's own, structurally different, interrupted-job problem).

    Detection needs no new state: a live process's _run_download_job always
    drives a row through to a terminal status (complete/partial/failed)
    before returning, exceptions included (see its own try/except Exception
    fallback) — so a row parked at 'pending' or 'in_progress' at the moment
    this function runs, before this fresh process has submitted anything of
    its own yet, can only be left over from a *previous* process's death.

    Resume, not just retry-from-scratch: download_jobs.files is written
    after every single file (_run_download_job's _update_job(job_id,
    files=files) inside its per-file loop, not just at the end), so the
    last commit before death already has real per-file status. Files
    already "succeeded" are preserved untouched; everything else — "failed",
    "pending" (never attempted), and "retrying" (a manual restart that was
    itself interrupted this time) — all get re-enqueued, since none of
    those represent completed, verified work. Checking status != "succeeded"
    rather than listing every non-terminal status by name is deliberate:
    "retrying" is swept into the same retry bucket as "failed"/"pending"
    here, not silently dropped or treated as some fourth case.

    Keeps the same job_id rather than minting a new one (unlike a manual
    restart via _restart_files/_enqueue_job) — datasets.js's pollDownloadJob
    treats a non-ok response (e.g. a 404 from a deleted-and-recreated job
    row) as a transient hiccup and keeps polling the *same* job_id up to
    DOWNLOAD_POLL_MAX_ATTEMPTS; it has no way to learn about a replacement
    job_id. Minting a new one would silently orphan any browser tab that
    happened to still have an open toast polling the old id across the
    restart. The Downloads history page doesn't care either way, since it
    re-queries job_id fresh via listDownloadHistory's LEFT JOIN on every
    load.

    Multi-process guard: claims each row with an atomic
    UPDATE ... WHERE status IN ('pending', 'in_progress') before doing
    anything else with it, checking rowcount. _db_write_lock's own docstring
    already notes it doesn't protect against a second OS process (only
    against this process's own threads racing each other); if Passenger
    ever runs more than one worker process for the same user, two processes
    starting at once could otherwise both read the same stale row and both
    re-enqueue the same download. SQLite only ever allows one writer at a
    time regardless, so only one process's claim UPDATE can actually change
    the row; the loser's rowcount comes back 0 and it skips that row rather
    than double-enqueuing it.

    No toast/push notification here — nobody's browser is guaranteed
    connected at process cold-start, so there's no channel to push to
    anyway. The resumed row simply shows 'in_progress' again, identical to
    any other in-progress download, next time anything reads it — the
    Downloads page's existing polling/rendering already handles that state
    with no changes needed (confirmed by reading downloads.js/datasets.js:
    both key off job_id + status alone, neither cares how a row got into
    'in_progress').
    """
    _reconcile_history_with_jobs()

    con = _get_connection()
    cur = con.cursor()
    cur.execute("SELECT * FROM download_jobs WHERE status IN ('pending', 'in_progress')")
    stale_jobs = cur.fetchall()
    con.close()

    recovered_history_ids = []
    for job in stale_jobs:
        job_id = job["job_id"]
        history_id = job["history_id"]

        # Atomic claim — see docstring's multi-process guard. A concurrent
        # second process racing this same scan would lose this UPDATE (0
        # rows matched, already claimed) and skip the row below.
        with _db_write_lock:
            con = _get_connection()
            cur = con.cursor()
            cur.execute(
                "UPDATE download_jobs SET status = 'in_progress' WHERE job_id = ? AND status IN ('pending', 'in_progress')",
                (job_id,),
            )
            claimed = cur.rowcount > 0
            con.commit()
            con.close()
        if not claimed:
            continue

        all_files = json.loads(job["files"]) if job["files"] else []
        preserved_files = [f for f in all_files if f.get("status") == "succeeded"]
        retry_paths = [f["path"] for f in all_files if f.get("status") != "succeeded"]
        sizes = {f["path"]: f["size"] for f in all_files if f.get("size") is not None}

        if not retry_paths:
            # Every file had already succeeded by the time the process
            # died — this can happen if death landed between the last
            # file's success and the job's own final _update_job/
            # _finish_history_record calls. Finish it out now rather than
            # leaving a fully-succeeded job sitting at 'in_progress' with
            # nothing left to run.
            history_files = [_history_file_entry(f) for f in all_files]
            _finalize_job_and_history(job_id, history_id, "complete", None, all_files, history_files)
            recovered_history_ids.append(history_id)
            continue

        # started_at is deliberately left untouched on both rows — this is
        # a continuation of the same attempt, not a new user-initiated one
        # (unlike _restart_files, which does reset it), so the displayed
        # elapsed time stays honest.
        with _db_write_lock:
            con = _get_connection()
            cur = con.cursor()
            cur.execute(
                "UPDATE download_history SET status = 'in_progress', finished_at = NULL, error_message = NULL WHERE id = ?",
                (history_id,),
            )
            con.commit()
            con.close()

        # _run_download_job's own first _update_job(job_id, files=files)
        # call (preserved + fresh "retrying" entries for retry_paths)
        # overwrites download_jobs.files with the correct merged set before
        # anything else runs — no need to pre-write it here, same as a
        # normal _enqueue_job call doesn't need to either.
        _job_executor.submit(
            _run_download_job, job_id, history_id, job["destination"], retry_paths, preserved_files, sizes, True
        )
        recovered_history_ids.append(history_id)

    if recovered_history_ids:
        logger.info(
            "Recovered %d interrupted download job(s) left over from a previous process: history_id=%s",
            len(recovered_history_ids), recovered_history_ids,
        )


try:
    _recover_interrupted_jobs()
except Exception:
    # Best-effort, same as main.py's migrate_category_icons() — a bug here
    # must not prevent the app from serving. Worst case, an interrupted job
    # from a previous process stays stuck at 'in_progress' exactly as it
    # would have without this function existing at all; not worse than the
    # pre-existing behavior.
    logger.exception("Recovering interrupted download jobs failed at startup")


@downloadsRouter.post("/datasets/download/start")
async def startDownloadJob(payload: DownloadJobStart):
    if not payload.paths:
        raise HTTPException(status_code=400, detail="Select at least one file or folder first.")

    history_id = _create_history_record(payload.name, payload.destination, len(payload.paths))
    job_id = _enqueue_job(history_id, payload.name, payload.destination, payload.paths, sizes=payload.sizes)

    return {"job_id": job_id, "history_id": history_id, "status": "pending"}


class RestartFileRequest(BaseModel):
    path: str


def _get_restartable_record(record_id: int) -> sqlite3.Row:
    """Shared fetch+validate for both restart routes below."""
    con = _get_connection()
    cur = con.cursor()
    cur.execute("SELECT * FROM download_history WHERE id = ?", (record_id,))
    row = cur.fetchone()
    con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Download record not found.")
    if row["status"] not in ("failed", "partial"):
        raise HTTPException(status_code=400, detail="Only failed or partially-failed downloads can be restarted.")
    return row


def _restart_files(record_id: int, row: sqlite3.Row, retry_paths: list[str], preserved_files: list[dict], all_files: list[dict]) -> str:
    """Shared by restartDownloadRecord (retries every currently-failed file)
    and restartDownloadFile (retries one specific file) — both reduce to
    "reset this history record in place and re-enqueue retry_paths,
    carrying preserved_files forward exactly as they already are." The two
    callers differ only in which files end up in each bucket:
    restartDownloadRecord retries every failed file and preserves the
    succeeded ones; restartDownloadFile retries just the one requested path
    and preserves everything else untouched — including any *other* still-
    failed files, which must stay failed, not be silently retried or
    cleared, since only one path was asked for (see _run_download_job's own
    comment on why nothing here forces preserved entries to "succeeded").
    sizes comes from all_files (the full original list, both callers have
    it) rather than preserved_files alone, since preserved_files never
    includes the retried path(s) and their sizes still need to survive the
    restart the same way preserved files' do.

    What's genuinely different from startDownloadJob, and why this isn't
    just a call to it: restarting is expected to update the *same* history
    entry in place (status flips failed/partial -> in_progress on the entry
    the user clicked Restart on), not create a second entry while the old
    one lingers. startDownloadJob always inserts a fresh history row, so it
    can't produce that in-place behavior — resetting the existing row
    instead of inserting a new one is what this helper does that
    startDownloadJob's own path doesn't need to.
    """
    sizes = {f["path"]: f["size"] for f in all_files if f.get("size") is not None}

    name = row["name"]
    destination = row["destination"]
    total_item_count = len(preserved_files) + len(retry_paths)
    started_at = datetime.now(timezone.utc).isoformat()
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute(
            "UPDATE download_history SET status = 'in_progress', item_count = ?, started_at = ?, finished_at = NULL, error_message = NULL, files = NULL WHERE id = ?",
            (total_item_count, started_at, record_id),
        )
        # the finished job row from the original attempt is still sitting
        # there pointed at this history_id — drop it before inserting the new
        # one so /downloads/history's LEFT JOIN doesn't pick up both and
        # duplicate this entry in the list
        cur.execute("DELETE FROM download_jobs WHERE history_id = ?", (record_id,))
        con.commit()
        con.close()

    return _enqueue_job(record_id, name, destination, retry_paths, preserved_files=preserved_files, sizes=sizes, is_restart=True)


@downloadsRouter.post("/downloads/history/{record_id}/restart")
async def restartDownloadRecord(record_id: int):
    # Retries every file that actually failed last time; already-succeeded
    # files are carried through as preserved_files rather than re-downloaded
    # or dropped from history — see _restart_files/_run_download_job.
    row = _get_restartable_record(record_id)
    all_files = json.loads(row["files"]) if row["files"] else []
    retry_paths = [f["path"] for f in all_files if f.get("status") == "failed"]
    preserved_files = [f for f in all_files if f.get("status") == "succeeded"]
    if not retry_paths:
        raise HTTPException(status_code=400, detail="No failed files recorded for this download.")

    job_id = _restart_files(record_id, row, retry_paths, preserved_files, all_files)
    return {"job_id": job_id, "history_id": record_id, "status": "in_progress"}


@downloadsRouter.post("/downloads/history/{record_id}/restart-file")
async def restartDownloadFile(record_id: int, payload: RestartFileRequest):
    # Retries exactly one file — added 2026-08-04 alongside the "restarting"
    # per-file status so a single flaky file doesn't require re-running
    # every other already-failed file in the same batch just to retry it.
    # Everything else in the record (succeeded *and* any other still-failed
    # files) is preserved untouched — see _restart_files' own docstring.
    row = _get_restartable_record(record_id)
    all_files = json.loads(row["files"]) if row["files"] else []
    target = next((f for f in all_files if f["path"] == payload.path), None)
    if target is None or target.get("status") != "failed":
        raise HTTPException(status_code=400, detail="That file isn't recorded as failed for this download.")

    preserved_files = [f for f in all_files if f["path"] != payload.path]
    job_id = _restart_files(record_id, row, [payload.path], preserved_files, all_files)
    return {"job_id": job_id, "history_id": record_id, "status": "in_progress"}


@downloadsRouter.get("/datasets/download/status/{job_id}")
async def getDownloadJobStatus(job_id: str):
    con = _get_connection()
    cur = con.cursor()
    cur.execute("SELECT * FROM download_jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    entry = dict(row)
    entry["files"] = json.loads(entry["files"]) if entry.get("files") else []
    return entry


@downloadsRouter.get("/downloads/history")
async def listDownloadHistory():
    con = _get_connection()
    cur = con.cursor()
    # LEFT JOIN in job_id so the Downloads page can poll
    # /datasets/download/status/{job_id} for any row still in_progress to get
    # live per-file state (download_jobs tracks that; download_history.files
    # is only populated once a job reaches a terminal state).
    cur.execute(
        """SELECT h.*, j.job_id AS job_id
           FROM download_history h
           LEFT JOIN download_jobs j ON j.history_id = h.id
           ORDER BY h.started_at DESC"""
    )
    rows = cur.fetchall()
    con.close()
    result = []
    for row in rows:
        entry = dict(row)
        entry["files"] = json.loads(entry["files"]) if entry.get("files") else []
        result.append(entry)
    return result


@downloadsRouter.delete("/downloads/history/{record_id}")
async def deleteDownloadRecord(record_id: int):
    # Deleting an in_progress entry is allowed, not just terminal ones. The
    # background thread (_run_download_job) has no safe way to be cancelled
    # mid-transfer — Python doesn't support killing a thread from outside it
    # — so it keeps running to completion regardless. What matters is that it
    # stops being able to do anything visible once its row is gone: its job
    # row is deleted here too (not just the history row), so its remaining
    # _update_job/_finish_history_record calls become no-op UPDATEs (0 rows
    # matched, no error) instead of resurrecting a deleted entry, and the
    # status endpoint starts 404ing for that job_id — which the Downloads
    # page's poll loop already treats as "stop polling this card".
    with _db_write_lock:
        con = _get_connection()
        cur = con.cursor()
        cur.execute("DELETE FROM download_jobs WHERE history_id = ?", (record_id,))
        cur.execute("DELETE FROM download_history WHERE id = ?", (record_id,))
        deleted = cur.rowcount
        con.commit()
        con.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Download record not found.")
    return {"status": "success"}
