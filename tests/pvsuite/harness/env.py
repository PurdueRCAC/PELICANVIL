"""Environment sanitising and small shared helpers for the PELICANVIL suite.

Everything the suite spawns (the app server, the pelican CLI, the harness's
own direct federation calls) runs with an environment built by sanitized_env()
so that *no real credential can be discovered* and none can leak into logs.

pelicanfs (1.3.1, read from source) discovers credentials from, in order:
BEARER_TOKEN, BEARER_TOKEN_FILE, default_bearer_token_file() =
$XDG_RUNTIME_DIR/bt_u<uid> (else /tmp/bt_u<uid>), TOKEN (a file path),
HTCondor creds ($_CONDOR_CREDS, else ./.condor_creds relative to the CWD),
and finally by shelling out to the `pelican` binary if it is on PATH (an OIDC
device flow that can block for minutes). Each is neutralised below.
"""
import getpass
import hashlib
import os
import re
import shutil
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[2]
SUITE_DIR = TESTS_DIR / "pvsuite"
DEFAULT_REPO = TESTS_DIR.parent

CRED_ENV_VARS = (
    "BEARER_TOKEN", "BEARER_TOKEN_FILE", "TOKEN", "_CONDOR_CREDS",
    "_CONDOR_SCRATCH_DIR", "SCITOKENS_FILE", "PELICAN_TOKEN", "OSDF_TOKEN",
)
# Proxies would silently reroute (or on some sites break) federation traffic;
# the suite wants the same direct path the real app uses.
PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")

IS_WINDOWS = os.name == "nt"


def current_user() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or getpass.getuser()


def path_without_pelican(path_value: str) -> str:
    """PATH with every directory that contains a `pelican` executable removed,
    so pelicanfs can never shell out to it (OIDC device flow) during the
    'no token' tests. Only used for the app/harness processes; the CLI-parity
    test runs the CLI itself via an absolute path."""
    keep = []
    for d in path_value.split(os.pathsep):
        if not d:
            continue
        if shutil.which("pelican", path=d) or shutil.which("pelican.exe", path=d):
            continue
        keep.append(d)
    return os.pathsep.join(keep)


def find_pelican_cli() -> str | None:
    explicit = os.environ.get("PELICAN_BIN")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    return shutil.which("pelican")


def sanitized_env(home: Path, extra: dict | None = None) -> dict:
    """A clean environment rooted at `home` (a per-server / per-run temp dir)."""
    env = {k: v for k, v in os.environ.items()
           if k not in CRED_ENV_VARS and k not in PROXY_ENV_VARS}
    home = Path(home)
    (home / "xdg_runtime").mkdir(parents=True, exist_ok=True)
    (home / "empty_condor_creds").mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)          # os.path.expanduser on Windows
    env["XDG_RUNTIME_DIR"] = str(home / "xdg_runtime")
    env["_CONDOR_CREDS"] = str(home / "empty_condor_creds")
    env["USER"] = current_user()
    env["PATH"] = path_without_pelican(env.get("PATH", ""))
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def apply_sanitized_env_to_this_process(home: Path) -> None:
    """The harness process itself talks to the federation (preflight, fixture
    verification), so it gets the same clean environment."""
    env = sanitized_env(home)
    for k in list(os.environ):
        if k in CRED_ENV_VARS or k in PROXY_ENV_VARS:
            del os.environ[k]
    for k in ("HOME", "USERPROFILE", "XDG_RUNTIME_DIR", "_CONDOR_CREDS", "USER", "PATH"):
        os.environ[k] = env[k]


def sha256_file(path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def md5_file(path, chunk=1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dir_tree(root) -> dict:
    """{relative posix path: size} for every regular file under root."""
    root = Path(root)
    out = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        try:
            if p.is_file():
                out[p.relative_to(root).as_posix()] = p.stat().st_size
        except OSError:
            pass
    return out


def dir_bytes(root) -> int:
    total = 0
    stack = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def redact(text: str) -> str:
    """Belt and braces: never let something token-shaped reach a log."""
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]{20,}", r"\1<redacted>", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<redacted-jwt>", text)
    return text
