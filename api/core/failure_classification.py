"""Shared failure classification for every surface that talks to the
Pelican/OSDF federation: dataset-page downloads and Quick Access (both going
through api/routes/pelican.py's download_one_file, on the per-user PUN web
process) and the standalone Slurm indexing worker (scripts/indexing_worker.py,
a structurally separate long-lived process). One classify_failure() here is
the single place that turns a raised exception — from either aiohttp's
hierarchy (.get()/downloads) or aiowebdav2's (.ls()/listing, per the
2026-08-01 investigation that found it doesn't share aiohttp's exception
types) — into one honest, user-facing reason, so a researcher gets the same
kind of specific answer regardless of which of the three UI surfaces they
hit a failure on.

_is_auth_required and _is_connection_error used to live in
api/routes/pelican.py (the 2026-08-01 and 2026-08-04 investigations that
built them). Moved here, with pelican.py now importing them back, because
this module needs both to build classify_failure and pelican.py's own
download_one_file needs classify_failure — keeping them in pelican.py would
make this module import pelican.py while pelican.py imports this module,
a circular import. Neither function's body changed in the move.
"""
from dataclasses import dataclass

import aiohttp
import asyncio
from aiohttp import http_exceptions

from aiowebdav2.exceptions import (
    UnauthorizedError,
    AccessDeniedError,
    RemoteResourceNotFoundError,
    NoConnectionError,
    ConnectionExceptionError,
)
from pelicanfs.exceptions import NoCredentialsException, BadDirectorResponse


def _is_auth_required(exc: Exception) -> bool:
    """True if exc means "this namespace needs a token" rather than some
    other failure (network issue, genuine 404/500, etc).

    pelicanfs doesn't use one HTTP layer consistently, so this has to cover
    both: `.get()` (download_one_file) goes through fsspec's HTTPFileSystem,
    a real aiohttp.ClientResponseError from raise_for_status(). `.ls()`
    (pelicanlistPath) goes through a *different* library entirely —
    aiowebdav2's WebDAV client — which maps 401/403 to its own
    UnauthorizedError/AccessDeniedError (see aiowebdav2/client.py's request
    method), never touching aiohttp.ClientResponseError at all. Confirmed by
    reading both installed packages' source, not assumed from docs — see
    2026-08-01 investigation notes. NoCredentialsException covers the third
    case: pelicanfs's own token-generation step finding no credential at all
    before any HTTP request goes out.
    """
    if isinstance(exc, NoCredentialsException):
        return True
    if isinstance(exc, (UnauthorizedError, AccessDeniedError)):
        return True
    return isinstance(exc, aiohttp.ClientResponseError) and exc.status in (401, 403)


# Genuine network/session failures — as opposed to a data-class exception
# like RemoteResourceNotFoundError, where the origin is reachable and
# correctly reporting the path doesn't exist. See _is_connection_error's
# own docstring for why each of these is included.
_CONNECTION_ERROR_TYPES = (
    NoConnectionError,
    ConnectionExceptionError,
    aiohttp.ClientConnectionError,
    TimeoutError,
    asyncio.CancelledError,
    BadDirectorResponse,
)


def _is_connection_error(exc: BaseException) -> bool:
    """True if exc represents a genuine connection/session-level failure —
    the kind scripts/indexing_worker.py's reactive safeguard should reset
    the filesystem singleton and retry for — rather than a data-class
    failure (a real 404, an auth requirement, a genuine server error
    response) that a reset can't fix and shouldn't delay handling.

    NoConnectionError: aiowebdav2's own wrapper around aiohttp's
    ClientConnectionError, raised by execute_request() when the underlying
    .request() call itself fails (see aiowebdav2/client.py) — covers
    ClientConnectorError, ServerTimeoutError, ConnectionTimeoutError,
    ServerDisconnectedError, etc. by inheritance, so catching the aiohttp
    base class below is partly redundant with this, but .ls() calls go
    through aiowebdav2, which re-wraps these before pelican-ui's code ever
    sees them — keeping both covers whichever layer a given pelicanfs call
    path happens to surface the exception from.
    ConnectionExceptionError: aiowebdav2's wrapper around an unexpected
    aiohttp.ClientResponseError raised directly from the request call
    itself (not a normal status-code response) — same network-failure
    shape as NoConnectionError, just a different aiowebdav2 wrapper.
    aiohttp.ClientConnectionError: the direct aiohttp exception, for any
    call path that isn't routed through aiowebdav2's wrapping (e.g. a
    download's .get() call, which goes through aiohttp directly). This
    already covers aiohttp.ClientConnectorError (the "Connect call failed"/
    OSError wrapper seen in the 2026-08-04 production run's dataset_id=17
    failure) by inheritance — ClientConnectorError -> ClientOSError ->
    ClientConnectionError, confirmed directly against the installed
    aiohttp version, not assumed. No separate entry needed for it.
    TimeoutError: covers both Python's builtin TimeoutError (which
    asyncio.TimeoutError is an alias for since 3.11, and which fsspec's own
    FSTimeoutError subclasses) and aiohttp's ServerTimeoutError/
    ConnectionTimeoutError (both already covered above via
    ClientConnectionError, included again here for the case where a
    timeout surfaces as a bare TimeoutError instead).
    asyncio.CancelledError: seen directly in the real failure logs' later,
    near-instant failures (aiohappyeyeballs' DNS/connect racing hitting
    CancelledError) — a BaseException, not an Exception, so it needs its
    own explicit inclusion; a caller checking this function needs to catch
    BaseException, not just Exception, to ever see one to classify.
    pelicanfs.exceptions.BadDirectorResponse: added 2026-08-04. Structurally
    different from the others above — read directly from pelicanfs/core.py's
    get_dirlist_url(): this is only raised *after* the Director's PROPFIND
    request already completed successfully (inside the `async with
    session.request(...) as resp:` block), when the response is missing an
    expected "Link" header. It's a malformed/unexpected response from a
    Director that *was* reached, not a network-reachability failure the
    way every other entry here is — a genuine connection failure to the
    Director would raise ClientConnectorError/ServerTimeoutError before
    ever reaching that check, both already covered above.
    Included anyway, based on real evidence: in the 2026-08-04 run, three
    datasets (15, 14, 13) failed with BadDirectorResponse within ~30ms of
    dataset 17's hard ClientConnectorError connection refusal to a
    different host — too tightly clustered to be coincidence, and
    consistent with a brief window of broader federation instability
    (a director backend returning a degraded/error response, not
    necessarily the exact same host or fault as dataset 17's). A malformed
    one-off response during that kind of transient instability is exactly
    the shape of thing retrying resolves; reusing the existing reset+retry
    path (rather than a special longer backoff) is a deliberate, modest
    choice — nothing in the evidence points to needing a different backoff
    than the other connection-class cases already use, and this can be
    revisited from real retry-outcome data once it's live.
    """
    return isinstance(exc, _CONNECTION_ERROR_TYPES)


_TRANSIENT_ORIGIN_STATUSES = (500, 502, 503, 504)

# Payload-class errors: the response started but the body didn't arrive
# intact. None of these subclass aiohttp.ClientConnectionError (checked
# against the installed aiohttp): ClientPayloadError is a plain ClientError,
# and http_exceptions.ContentLengthError/PayloadEncodingError sit under
# HttpProcessingError, so _is_connection_error misses all three.
_PAYLOAD_ERROR_TYPES = (
    aiohttp.ClientPayloadError,
    http_exceptions.ContentLengthError,
    http_exceptions.PayloadEncodingError,
)


def _is_payload_error(exc: BaseException) -> bool:
    """True for a truncated/corrupt mid-stream body, which leaves a partial
    file on disk that a retry must discard before rewriting."""
    return isinstance(exc, _PAYLOAD_ERROR_TYPES)


def _is_transient_origin_error(exc: BaseException) -> bool:
    """True if exc is an origin-side fault worth retrying that
    _is_connection_error doesn't cover: a 500/502/503/504 response or a
    truncated/corrupt payload. Deliberately NOT folded into
    _is_connection_error/classify_failure: scripts/indexing_worker.py treats
    any non-"unknown" category as a settled data-class result, and the
    download path wants a longer backoff for these than for a dead pooled
    connection. 4xx (including 401/403/404) is never transient here."""
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in _TRANSIENT_ORIGIN_STATUSES
    return _is_payload_error(exc)


@dataclass(frozen=True)
class FailureCategory:
    """code is the machine-readable category (stable — safe to store, log,
    or branch UI behavior on); message is the honest, user-facing sentence.
    No fabricated specificity: `unknown`'s message is exactly what the
    exception itself says, same "don't invent precision the code doesn't
    have" principle already used for the indexing admin badge's real
    folders-scanned count rather than a guessed percentage."""

    code: str
    message: str


def classify_failure(exc: BaseException) -> FailureCategory:
    """The one place a raised exception becomes a user-facing reason,
    reused by dataset downloads, Quick Access, and the indexing worker
    alike. Order matters — checked most-specific-first:

    1. auth_required — reuses _is_auth_required above.
    2. not_found — the origin is reachable and correctly reporting the
       path doesn't exist (FileNotFoundError from the .get()/download
       path, RemoteResourceNotFoundError from the .ls()/listing path).
    3. permission — a local write failure (PermissionError), not a
       federation-side problem at all.
    4. connection — reuses _is_connection_error above; safe to suggest
       "try again" for, unlike the other categories.
    5. unknown — the honest catch-all. No guessed cause is better than a
       wrong one (see api/routes/pelican.py's prior "Check permissions on
       the destination" toast copy, which was shown even when the real
       cause wasn't permissions at all — exactly what this replaces).
    """
    if _is_auth_required(exc):
        return FailureCategory("auth_required", "This path requires an access token.")
    if isinstance(exc, (FileNotFoundError, RemoteResourceNotFoundError)):
        return FailureCategory("not_found", "This file or folder wasn't found on the federation.")
    if isinstance(exc, PermissionError):
        return FailureCategory("permission", "Permission denied writing to the destination.")
    if _is_connection_error(exc):
        return FailureCategory("connection", "Couldn't reach the federation — a temporary network issue. Try again.")
    message = str(exc) or type(exc).__name__
    return FailureCategory("unknown", message)
