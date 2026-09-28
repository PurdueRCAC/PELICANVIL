"""Direct, real federation access for the harness itself (preflight, fixture
verification, expected-tree computation). This is the real pelicanfs against
the real federation, the same library and call shapes the app uses; it exists
so the suite can independently learn what is actually there and compare it to
what the app downloaded. No mocking anywhere."""
import time

from pelicanfs import OSDFFileSystem


class FedError(Exception):
    pass


_fs = None


def fs():
    global _fs
    if _fs is None:
        _fs = OSDFFileSystem(direct_reads=False)
    return _fs


def reset():
    global _fs
    _fs = None


def _retry(fn, attempts=3, delay=3.0):
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except FileNotFoundError:
            raise
        except Exception as e:      # noqa: BLE001 - any transient federation failure
            last = e
            reset()
            time.sleep(delay)
    raise FedError(f"{type(last).__name__}: {last}") from last


def ls(path: str):
    return _retry(lambda: fs().ls(path, detail=True))


def file_size(path: str) -> int:
    """Size of a single file via its parent listing (the call the UI uses)."""
    parent, _, name = path.rstrip("/").rpartition("/")
    for e in ls(parent):
        if e["name"].rstrip("/").rsplit("/", 1)[-1] == name and e["type"] != "directory":
            return int(e["size"])
    raise FileNotFoundError(path)


def walk_files(path: str) -> dict:
    """{path relative to `path`: size} for every file under a directory, using
    the same sequential ls-recursion shape as the app's _walk_remote_files."""
    base = path.rstrip("/")
    out = {}

    def rec(p):
        for e in ls(p):
            name = e["name"].rstrip("/")
            if e["type"] == "directory":
                rec(name)
            else:
                out[name[len(base):].lstrip("/")] = int(e["size"])
    rec(base)
    return out


def fetch_bytes(path: str, limit: int = 8 * 1024 * 1024) -> bytes:
    """Fetch a small file fully into memory (pinning / verification only)."""
    def go():
        with fs().open(path, "rb") as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise FedError(f"{path} is larger than {limit} bytes; refusing to pull it whole")
        return data
    return _retry(go)


def auth_probe(path: str) -> str:
    """'auth_required' | 'listed' | 'not_found' | 'other:<ExceptionType>' for a
    token-less ls(). Deliberately independent of the app's own classifier."""
    from aiowebdav2.exceptions import AccessDeniedError, RemoteResourceNotFoundError, UnauthorizedError
    from pelicanfs.exceptions import NoCredentialsException
    try:
        fs().ls(path, detail=True)
        return "listed"
    except (UnauthorizedError, AccessDeniedError, NoCredentialsException):
        return "auth_required"
    except (FileNotFoundError, RemoteResourceNotFoundError):
        return "not_found"
    except Exception as e:      # noqa: BLE001
        status = getattr(e, "status", None)
        if status in (401, 403):
            return "auth_required"
        return f"other:{type(e).__name__}"
    finally:
        reset()
