from fastapi import APIRouter, HTTPException, Response
from pelicanfs import OSDFFileSystem
from fsspec.implementations.http import HTTPFileSystem
import fsspec, os, json, shutil, logging, sqlite3, time
from pathlib import Path
from urllib.parse import quote
from collections import defaultdict
from api.core.config import DB_PATH
from api.core.pelican_auth import get_token_for_namespace, log_unexpected_pelican_error
from api.core.failure_classification import _is_auth_required, classify_failure
osdf = OSDFFileSystem(direct_reads=False)

logger = logging.getLogger("pelican-ui.pelican")

pelicanRouter = APIRouter()
ROOTPATH = Path.cwd()
DATASET_PATH = os.path.join(ROOTPATH, "data", "datasets.json")
USER = os.environ.get("USER")
SCRATCH_PATH = os.path.join("/anvil", "scratch", USER)


def _resolve_filesystem(namespace: str) -> OSDFFileSystem:
    """Most calls reuse the shared module-level `osdf` instance (fine — it's
    unauthenticated). When a token has been saved for this namespace (see
    api/routes/token_auth.py), build a one-off instance carrying it instead:
    pelicanfs's own token discovery is a single global value and can't
    represent different tokens for different namespaces, and the shared
    `osdf` instance can't have its headers swapped per-call safely since
    requests against other namespaces may be in flight concurrently.
    """
    token = get_token_for_namespace(namespace)
    if token:
        return OSDFFileSystem(direct_reads=False, headers={"Authorization": f"Bearer {token}"})
    return osdf


def reset_default_filesystem() -> None:
    """Drops the shared module-level `osdf` singleton and replaces it with a
    fresh OSDFFileSystem, so the next _resolve_filesystem() call for any
    unauthenticated namespace gets a brand-new instance — and, transitively,
    whatever internal aiohttp session/connector/DNS-resolver state pelicanfs
    holds on the old one is dropped instead of carried forward.

    2026-09-28 fix: this used to just do `OSDFFileSystem(direct_reads=False)`
    again, which is a no-op — confirmed directly against pelicanfs 1.3.1 /
    fsspec 2026.6.0. fsspec filesystem classes use a caching metaclass
    (fsspec.spec._Cached): construction is memoized by a token derived from
    the class, the process id, and the constructor's args/kwargs (see
    _Cached.__call__ in fsspec/spec.py), and — for an async-implemented class
    instantiated the normal synchronous way, which is what OSDFFileSystem()
    is here — that token does NOT include the calling thread, so every
    thread in this process asking for `OSDFFileSystem(direct_reads=False)`
    gets back the literal same cached instance, same aiohttp session and
    all. So the old "reset" was just looking up and returning the exact
    object it meant to replace; nothing was ever dropped.

    The fix has two parts, both needed — confirmed by direct inspection,
    not assumed from either library's docs:

    1. `skip_instance_cache=True` on the OSDFFileSystem(...) call itself, an
       fsspec-recognized kwarg that `_Cached.__call__` special-cases to skip
       the cache lookup (and skip storing the result) entirely, producing a
       genuinely new *outer* object. Not storing it in the cache is
       intentional and harmless: the only other place in this app that
       constructs `OSDFFileSystem(direct_reads=False)` is this module's own
       import-time `osdf = ...` above, which never needs to find this one
       again.

    2. That alone is NOT enough, confirmed by comparing the actual aiohttp
       session objects (not just the outer OSDFFileSystem identity) before
       and after: `OSDFFileSystem.__init__` builds its own
       `fsspec.implementations.http.HTTPFileSystem` internally
       (`self.http_file_system = fshttp.HTTPFileSystem(...)`) — and that's
       the object whose `_session` actually backs every `.get()`/`.isdir()`/
       `.open()` call (i.e., the download path; `.ls()`/listing goes through
       a separate aiowebdav2 client that pelicanfs already opens fresh per
       call, per this function's own longer-standing note below — never the
       stale-session problem to begin with). `skip_instance_cache` is popped
       off by fsspec's caching metaclass *before* `__init__` ever runs, so it
       never reaches that inner HTTPFileSystem(...) call — which is still an
       ordinary, cached fsspec construction, keyed on args that are identical
       every time OSDFFileSystem(direct_reads=False) is built. Net effect
       confirmed empirically: two "genuinely different" OSDFFileSystem
       objects, built with skip_instance_cache=True and nothing else, still
       shared the exact same `http_file_system` object and the exact same
       aiohttp session underneath — the one thing that actually needed
       resetting was untouched. `HTTPFileSystem.clear_instance_cache()`
       (a classmethod fsspec provides on every cached filesystem class,
       clearing that class's process-wide lookup table so the *next*
       construction — the one about to happen inside the OSDFFileSystem()
       call below — misses the cache and builds fresh) fixes that: confirmed
       the resulting http_file_system and its session are both genuinely new
       objects once this runs first.

    Concurrency: `clear_instance_cache()` only empties HTTPFileSystem's
    class-level lookup TABLE (a plain dict) — it doesn't reach into, close,
    or invalidate any HTTPFileSystem object that already exists, the same
    way `skip_instance_cache` only ever affects a *future* lookup. A thread
    that already captured a reference to the pre-reset `fs` (e.g. mid-
    request, before this ran) keeps using its own `http_file_system` and
    session, fully functional, for as long as it holds that reference —
    exactly the same guarantee the old (broken) implementation's docstring
    already promised, now actually true because there IS a genuinely
    distinct old object (transitively, all the way down to the real aiohttp
    session) to keep using instead of "the same object under a new coat of
    paint."

    Written for scripts/indexing_worker.py's connection-pressure safeguards
    (see its PROACTIVE_RESET_CALLS / list_path) — a single-process, 24h+
    Slurm job making tens of thousands of sequential .ls() calls is the one
    caller shape actually exposed to the degrade-over-time failure found by
    the 2026-08-04 investigation (a brand-new aiohttp.ClientSession gets
    created per .ls() call inside pelicanfs's own get_webdav_client, never
    reused — third-party behavior, not something pelican-ui's call site can
    change without touching pelicanfs source, which is out of scope). The
    per-user PUN web process wasn't found to be meaningfully exposed to the
    same failure (see that investigation's Phase 5 conclusion — a request
    makes at most a handful of calls, nowhere near the volume needed, and
    Passenger's own process recycling is already a coarser version of the
    same mitigation) so nothing here is wired into pelicanlistPath or
    download_one_file; this function just lives here, next to osdf itself,
    since resetting it is squarely pelican.py's own responsibility. It is
    also the actual safety net behind GitHub issue #6's retry logic
    (_with_connection_retry below calls this before every retried attempt),
    so making it genuinely reset something matters there too, not just for
    the indexing worker's original use case.
    """
    global osdf
    HTTPFileSystem.clear_instance_cache()
    osdf = OSDFFileSystem(direct_reads=False, skip_instance_cache=True)


def _encode_path_segment(path: str) -> str:
    """Percent-encodes a path's special characters (space, +, #, %, &, =,
    non-ASCII, etc.) while leaving '/' as a literal path separator —
    defensive pre-encoding for every fs.ls()/fs.get() call, not a
    general-purpose URL helper. Apply this right before the call, not
    earlier — everything else in this app (DB storage, staging tables,
    JSON responses, error messages, _resolve_filesystem's token lookup)
    keeps using the raw, human-readable path unchanged.

    Exists because of a real bug in aiowebdav2 (0.6.2), confirmed by
    reading its source (aiowebdav2/urn.py): Urn.__init__ correctly
    percent-encodes the path it's given (`self._path = quote(path)`), but
    Client.get_url() then calls Urn.path(), which unconditionally
    *unquotes* it again before building the actual request URL — the two
    cancel out, so every .ls() call currently sends the raw, unescaped
    path onto the wire regardless of what's passed in. Confirmed directly
    against the installed library:
        Urn('Measurement+1').path() == '/Measurement+1'   (unescaped!)
    This is the root cause of the 2026-08-04 bug report (a folder named
    "Measurement+1" 404ing while its "Measurement1" sibling worked) — the
    origin most likely received (or misinterpreted) a literal/ambiguous
    '+' instead of the folder's real name.

    Also verified directly that pre-encoding survives that exact
    round-trip intact — Urn's own quote()-then-unquote() only cancels ONE
    level, so a value already percent-encoded once when it enters Urn
    comes out the other side exactly as encoded:
        Urn(quote('Measurement+1')).path() == '/Measurement%2B1'  (correct)
    A path with no special characters is unaffected — quote()'s default
    safe set already covers every character a normal path needs.

    .get()/downloads don't go through aiowebdav2's Urn at all (a
    different pelicanfs code path — get_origin_url/get_working_cache,
    built with urllib.parse.urljoin, which never encodes OR decodes), so
    a raw special character reaches the wire unencoded there too, just
    via "never gets encoded in the first place" rather than a cancelling
    round-trip. Same fix, same call-site shape, applied at every place
    this app actually calls fs.ls()/fs.get(): here (download_one_file,
    pelicanlistPath) and scripts/indexing_worker.py's own fs.ls() call,
    which imports this function from here rather than duplicating it.

    Deliberately does not touch pelicanfs/aiowebdav2 source, matching the
    project's existing convention (see reset_default_filesystem above) —
    this encodes defensively at the boundary, in pelican-ui's own code,
    before a path ever reaches either library.
    """
    return quote(path, safe="/")


def _attach_folder_sizes(entries: list) -> list:
    """Annotates each directory entry in a .ls() result with real_size (an
    int, from the indexing worker's dataset_folder_sizes table) when known,
    else None, and with unavailable (bool) — True when the indexing worker
    confirmed this exact folder was unrecoverable (missing-data fallback or
    a circuit-breaker abort — see scripts/indexing_worker.py's walk() and
    its `unavailable` column, added 2026-08-05 Part 3) rather than
    genuinely, legitimately 0 bytes. Keyed purely by path, not dataset id —
    quick-access.js's file browser has no dataset entity at all (just a
    pasted path), so a dataset-scoped lookup wouldn't work for it; this is
    the one place both datasets.js's and quick-access.js's file browsers
    get real folder sizes (and now this status) from, since both already
    call this same route.

    Batches into a single query rather than one per directory entry. Table
    (or the unavailable column on it, for a DB that predates 2026-08-05)
    may not exist yet on a fresh deployment (the worker creates/migrates it
    lazily on its first successful run, see scripts/indexing_worker.py) —
    that's not an error, it just means nothing is indexed yet, so every
    entry gets real_size: None, unavailable: False.
    """
    dir_paths = [e["name"].rstrip("/") for e in entries if e.get("type") == "directory"]
    if not dir_paths:
        return entries

    sizes: dict[str, int] = {}
    unavailable_paths: dict[str, bool] = {}
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            placeholders = ",".join("?" * len(dir_paths))
            cur = con.cursor()
            cur.execute(
                f"SELECT folder_path, size_bytes, unavailable FROM dataset_folder_sizes"
                f" WHERE folder_path IN ({placeholders})",
                dir_paths,
            )
            for folder_path, size_bytes, unavailable in cur.fetchall():
                sizes[folder_path] = size_bytes
                unavailable_paths[folder_path] = bool(unavailable)
        finally:
            con.close()
    except sqlite3.OperationalError:
        # table (or unavailable column) doesn't exist yet — every entry
        # just stays real_size: None, unavailable: False below, not an error
        pass

    for entry in entries:
        if entry.get("type") == "directory":
            key = entry["name"].rstrip("/")
            entry["real_size"] = sizes.get(key)
            entry["unavailable"] = unavailable_paths.get(key, False)
    return entries


# GitHub issue #6, Part 1 (2026-09-10 investigation): download_one_file used
# to make exactly one attempt per pelicanfs call, so a single transient
# connection blip failed the whole file and required the user to notice and
# click Restart. This is the retry-with-backoff fix — deliberately much
# smaller than scripts/indexing_worker.py's CONNECTION_RETRY_BACKOFFS =
# [5, 15, 45]: that schedule is tuned for a 40,000-call unattended batch walk,
# where a dataset can afford to spend a couple of minutes clearing a bad
# patch. This is a single user-facing call with a progress indicator the
# researcher is actively watching — 2 retries at 2s/5s (7s worst-case added
# latency per file, 3 attempts total) is enough to ride out a dead pooled
# connection or a brief DNS/handshake hiccup without the delay itself being
# the next thing they complain about.
DOWNLOAD_RETRY_BACKOFFS = [2, 5]


def _with_connection_retry(description: str, fn):
    """Calls fn() (a zero-arg callable performing exactly one blocking
    pelicanfs isdir()/get()/get_file() call), retrying up to
    len(DOWNLOAD_RETRY_BACKOFFS) more times if it raises a connection-class
    exception (classify_failure(exc).code == "connection") — resetting the
    shared filesystem singleton before each retry so a stale/dead aiohttp
    session isn't reused on the next attempt, mirroring
    scripts/indexing_worker.py's own reset-then-retry pattern for the same
    exception classes (see api/core/failure_classification.py's
    _is_connection_error).

    Dispatches explicitly on classify_failure(exc).code rather than a
    blanket except-and-retry — this is deliberate: the indexing worker's own
    retry loop once had a real bug where an unclassified exception got
    wrongly routed into the retry meant only for connection-class failures.
    Any exception that isn't classified "connection" (auth_required,
    not_found, permission, unknown) propagates immediately on the very first
    attempt, unretried — retrying a genuine 404 or a permissions error
    wouldn't fix it, would just delay reporting it, and is exactly the
    misclassification this dispatch avoids repeating.

    fn is responsible for re-resolving the filesystem itself on every call
    (not capturing one `fs` reference from outside) — reset_default_filesystem
    only replaces the module-level shared singleton going forward; a caller
    holding a reference to the old instance from before the reset would keep
    using the now-stale one otherwise.
    """
    attempts = len(DOWNLOAD_RETRY_BACKOFFS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if classify_failure(e).code != "connection" or attempt == attempts:
                raise
            backoff = DOWNLOAD_RETRY_BACKOFFS[attempt - 1]
            logger.info(
                "Connection-class failure on %s (attempt %d/%d): %s — resetting"
                " filesystem singleton and retrying in %ds",
                description, attempt, attempts, e, backoff,
            )
            reset_default_filesystem()
            time.sleep(backoff)


class DownloadError(Exception):
    """Raised by download_one_file instead of HTTPException. This is called
    from background job threads (api/routes/downloads.py), not just request
    handlers, and HTTPException only makes sense when there's an active
    request to attach a status code to.

    category carries the FailureCategory.code classify_failure produced
    (see below) — api/routes/downloads.py's _run_download_job stores this
    alongside the message on each failed file, so the frontend gets a
    stable machine-readable reason too, not just prose to display verbatim.
    Defaults to "unknown" for DownloadAuthRequiredError, which doesn't go
    through classify_failure (it's already unambiguous by construction).
    """

    def __init__(self, message: str, category: str = "unknown"):
        self.category = category
        super().__init__(message)


class DownloadAuthRequiredError(DownloadError):
    """Same "needs a token" case as the 401 branch in pelicanlistPath below,
    just surfaced through DownloadError's channel since download_one_file
    runs on a background job thread with no request to attach a status code
    to (see downloads.py's _run_download_job, which reads .namespace off
    this to flag the failed file for the frontend's retry-with-token flow)."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        super().__init__(f'"{namespace}" requires an access token.', "auth_required")


def _walk_remote_files(fs, path: str) -> list:
    """Sequentially enumerates every real file under `path`, recursing
    through directories via this app's own already-trusted fs.ls() (the same
    call pelicanlistPath's browsing already relies on) — used only by
    _download_directory below. One fs.ls() call at a time, no concurrency,
    matching this whole call site's "never issue overlapping Pelican
    requests" shape (see reset_default_filesystem's docstring / scripts/
    indexing_worker.py's own single-threaded walk).

    Returns raw, human-readable remote paths (not percent-encoded) — same
    convention as everywhere else in this app; _encode_path_segment is
    applied right before each actual fs.ls()/fs.get_file() call, not stored.
    """
    entries = fs.ls(_encode_path_segment(path), detail=True)
    files: list = []
    for entry in entries:
        name = entry["name"].rstrip("/")
        if entry.get("type") == "directory":
            files.extend(_walk_remote_files(fs, name))
        else:
            files.append(name)
    return files


def _download_directory(fs, path: str, storage_location: str) -> None:
    """2026-08-06 (GitHub issue #3): fs.get(dir, dest, recursive=True) is
    confirmed broken for directories. Traced live against the real
    federation (aws-opendata/us-west-2/ai2-public-datasets/beliefbank):
    PelicanFileSystem._get resolves ONE cache URL for the whole call, then
    hands off to a plain, non-Pelican-aware fsspec.HTTPFileSystem for the
    actual recursive walk+write — whose own directory/file-boundary
    detection can resolve one branch against a *different* host than the
    rest of the tree. Confirmed by direct inspection: this produces one
    real file under the correct cache-host-named folder, plus a second,
    spurious host-named folder containing a raw XrdHTTP directory-listing
    HTML page mis-saved as if it were a real file — not two legitimate
    copies needing to be merged/deduped, a single bogus artifact from the
    walk logic. fsspec's other_paths()/common_prefix() (fsspec/utils.py)
    can't find one clean shared prefix across a tree split across two
    hosts, and falls back to keeping the full resolved URL — host, port,
    namespace path, all of it — as literal on-disk folder names for
    whichever branch diverged.

    This sidesteps that entirely rather than cleaning up after it: never
    calls fs.get(..., recursive=True) on a directory at all. Enumerates the
    real files with _walk_remote_files above, then downloads each one
    individually via fs.get_file() with an explicit destination path this
    function computes itself (dataset-relative, under storage_location) —
    no recursive multi-file path-construction logic ever runs, so there's
    nothing for it to get wrong. Confirmed single-file fs.get() calls (not
    directories) are unaffected by this bug and are left exactly as they
    were — see download_one_file below.

    Strictly sequential — a plain loop, no asyncio.gather/thread pool —
    matching the one-call-in-flight-at-a-time shape this whole download
    path already had for its single fs.get() call, and the same "never
    concurrent" principle scripts/indexing_worker.py's walk() uses for its
    own fs.ls() calls. Sidesteps any question of how much concurrent load
    is safe against the federation rather than needing a tuned cap.
    """
    dest_root = os.path.join(storage_location, os.path.basename(path))
    os.makedirs(dest_root, exist_ok=True)
    for remote_path in _walk_remote_files(fs, path):
        rel = remote_path[len(path):].lstrip("/")
        local_path = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        logger.info(
            "Directory download for %s: writing %s -> %s (explicit destination,"
            " bypassing fs.get()'s own recursive path construction — see"
            " _download_directory's docstring for why)",
            path, remote_path, local_path,
        )
        # Retried per-file (GitHub issue #6, Part 1), not by re-running the
        # whole directory: a connection blip on file N of a large directory
        # should only redo file N, not re-download the N-1 files that
        # already succeeded. Re-resolves the filesystem itself on each
        # attempt (via `path`, the directory's own namespace) rather than
        # reusing the `fs` this function was called with — see
        # _with_connection_retry's own docstring for why that matters after
        # a mid-loop reset_default_filesystem() call.
        _with_connection_retry(
            f"download of {remote_path}",
            lambda remote_path=remote_path, local_path=local_path: _resolve_filesystem(path).get_file(
                _encode_path_segment(remote_path), local_path
            ),
        )


def download_one_file(filepath: str, storage_location: str) -> None:
    # The actual transfer mechanism (fsspec/pelicanfs streaming 5MB chunks
    # straight to disk) is unchanged and correct. What used to be wrong was
    # invoking this synchronously inside a request handler: this app runs
    # under Passenger via a2wsgi (see passenger_wsgi.py), which pins one
    # worker for the full duration of whatever request it's handling — so a
    # large/slow transfer here held a worker (and, for the browser, a
    # connection) open for as long as the transfer took, long enough to hit
    # reverse-proxy timeouts. This function is now only ever called from a
    # background thread (api/routes/downloads.py's job worker), never from
    # directly inside a request handler.
    path = filepath.rstrip("/")
    try:
        # isdir() only swallows OSError (fsspec/spec.py) — a genuine
        # not-found on `path` falls through to the plain fs.get() branch
        # below and surfaces its own correctly-classified not-found error
        # exactly as before this change; any other kind of failure (auth,
        # connection, ...) propagates straight out of isdir() into this
        # same except block, same as it always would have. Wrapped in the
        # same connection-class retry as the transfer calls below (GitHub
        # issue #6, Part 1) — re-resolves the filesystem on each attempt
        # rather than reusing one `fs` reference, same reasoning as
        # _download_directory's own per-file retry.
        def _check_isdir():
            fs = _resolve_filesystem(path)
            return fs, fs.isdir(_encode_path_segment(path))

        fs, is_dir = _with_connection_retry(f"checking path type for {path}", _check_isdir)
        if is_dir:
            _download_directory(fs, path, storage_location)
        else:
            # 2026-09-28 fix: this used to hand fs.get() the percent-encoded
            # path (_encode_path_segment(path)) as both the thing to fetch
            # AND, implicitly, the source of the output filename — fsspec's
            # get() derives the local destination name from the string it's
            # given (see AbstractFileSystem.get()'s other_paths() call in
            # fsspec/spec.py), so a name containing '+' or another character
            # this app has to pre-encode (see _encode_path_segment's own
            # docstring) landed on disk still percent-encoded, e.g.
            # "Measurement+1.xml" saved as "Measurement%2B1.xml" — bytes
            # correct, filename wrong. _download_directory just below
            # already avoided this same trap by computing each file's local
            # destination explicitly from the clean, un-encoded remote path
            # (see its own docstring) and calling fs.get_file() with that
            # explicit destination instead of letting fs.get() infer one;
            # this does the same thing for the single-file case, and is
            # confirmed unaffected by the directory-walk bug above.
            local_path = os.path.join(storage_location, os.path.basename(path))
            _with_connection_retry(
                f"download of {path}",
                lambda: _resolve_filesystem(path).get_file(_encode_path_segment(path), local_path),
            )
    except Exception as e:
        if _is_auth_required(e):
            raise DownloadAuthRequiredError(path) from None
        category = classify_failure(e)
        if category.code == "unknown":
            # Only the genuinely unclassified case gets logged loudly and
            # written to last_error.log — auth/not_found/permission/
            # connection are all already-understood, expected shapes of
            # failure with an honest message of their own; logging those as
            # if they were surprising would just be noise.
            logger.exception("download_one_file failed for %s -> %s", filepath, storage_location)
            log_unexpected_pelican_error(path, e)
        raise DownloadError(category.message, category.code) from (e if category.code == "unknown" else None)


# 2026-09-28 investigation (GitHub-issue-suite item 3): a real directory with
# 1,919 files was observed listing as exactly 1,000 through this endpoint,
# with nothing telling the caller entries were missing. Traced by hand —
# issuing the same PROPFIND pelicanfs/aiowebdav2 issue directly and reading
# the raw response — to the origin/cache server itself (XRootD's http/WebDAV
# plugin, confirmed via its own `Server: XrootD/v5.9.2` response header): the
# response is a normal HTTP 207 with a complete, well-formed <D:multistatus>
# document and a correct Content-Length, capped at 1,000 <D:response>
# entries, with no truncation flag, no continuation cursor, and no error of
# any kind marking it partial. This is not something pelican-ui's code
# introduces, and neither aiowebdav2 nor pelicanfs adds or removes anything
# from that response — there is no client-side page size to raise, and (per
# this project's established convention — see _encode_path_segment's own
# docstring) this deliberately does not patch pelicanfs/aiowebdav2 source to
# work around origin-server behavior that's out of scope to fix directly.
#
# Since there is no signal in the response that distinguishes "this folder
# has exactly 1,000 entries" from "this folder has more and got cut off,"
# hitting the cap count is the only thing this app can go on — flagging it
# is a heuristic, not a certainty (a folder that genuinely has exactly 1,000
# entries would be flagged too), but an honest "there may be more" beats
# both silently pretending the listing is complete and guessing a precise
# total this app has no way to actually know.
LISTING_TRUNCATION_SUSPECT_COUNT = 1000


@pelicanRouter.get("/datasets/category/list-path")
def pelicanlistPath(path: str, response: Response):
    fs = _resolve_filesystem(path)
    try:
        entries = fs.ls(_encode_path_segment(path))
        if len(entries) == LISTING_TRUNCATION_SUSPECT_COUNT:
            # A header, not a body-shape change: the response stays the same
            # bare JSON array every existing caller (the JS file browser, the
            # code snippets, anything else hitting this endpoint) already
            # expects; this is additive-only for whichever caller checks it.
            response.headers["X-Listing-Truncated"] = "1"
        return _attach_folder_sizes(entries)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f'Path "{path}" was not found on the federation.')
    except Exception as e:
        if _is_auth_required(e):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "auth_required",
                    "namespace": path,
                    "message": f'"{path}" requires an access token.',
                },
            ) from None
        logger.exception("pelicanlistPath failed for %s", path)
        log_unexpected_pelican_error(path, e)
        raise HTTPException(status_code=502, detail="Couldn't reach the Pelican/OSDF federation. Try again.")


@pelicanRouter.get("datasets/")
def giveSize():
    return
