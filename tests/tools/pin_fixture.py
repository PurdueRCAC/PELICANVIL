#!/usr/bin/env python
"""Helper for (re)pinning entries in tests/fixtures.yaml. Read-only against the
federation; downloads only what it is told to hash, and refuses big files.

  python tests/tools/pin_fixture.py file  /ns/path/to/file.txt          # size + sha256
  python tests/tools/pin_fixture.py file  /ns/path/big.bin --size-only  # size only
  python tests/tools/pin_fixture.py dir   /ns/path/to/dir [--sha-all] [--sample 5]
  python tests/tools/pin_fixture.py ls    /ns/path/to/dir               # what the app's listing sees

Prints YAML fragments to paste into tests/fixtures.yaml. Run it with the same
Python environment as the suite (pelicanfs installed). It scrubs credentials
from its own environment exactly like the suite does.
"""
import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pvsuite.harness import env, fed   # noqa: E402


def cmd_file(a):
    size = fed.file_size(a.path)
    print(f"path: {a.path}")
    print(f"size: {size}")
    if not a.size_only:
        data = fed.fetch_bytes(a.path, limit=a.limit)
        print(f"sha256: {hashlib.sha256(data).hexdigest()}")
        print(f"md5: {hashlib.md5(data).hexdigest()}")
        assert len(data) == size, f"size mismatch {len(data)} != {size}"


def cmd_ls(a):
    for e in fed.ls(a.path):
        print(f"{e['type'][0]} {e.get('size')!s:>12} {e['name']}")


def cmd_dir(a):
    files = fed.walk_files(a.path)
    total = sum(files.values())
    print(f"path: {a.path}")
    print(f"file_count: {len(files)}")
    print(f"total_bytes: {total}")
    names = sorted(files)
    sample = names if a.sha_all else names[:: max(1, len(names) // max(1, a.sample))][: a.sample]
    print("files:" if a.sha_all else "sample:")
    for rel in sample:
        data = fed.fetch_bytes(a.path.rstrip("/") + "/" + rel, limit=a.limit)
        print(f"  - {{path: \"{rel}\", size: {files[rel]}, sha256: {hashlib.sha256(data).hexdigest()}}}")
    if not a.sha_all:
        print("# all per-file sizes (for kill/recovery integrity checks):")
        print("sizes:")
        for rel in names:
            print(f"  \"{rel}\": {files[rel]}")


def main():
    env.apply_sanitized_env_to_this_process(Path(tempfile.mkdtemp(prefix="pv-pin-")))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("file"); f.add_argument("path"); f.add_argument("--size-only", action="store_true")
    f.add_argument("--limit", type=int, default=64 * 1024 * 1024); f.set_defaults(fn=cmd_file)
    d = sub.add_parser("dir"); d.add_argument("path"); d.add_argument("--sha-all", action="store_true")
    d.add_argument("--sample", type=int, default=5); d.add_argument("--limit", type=int, default=64 * 1024 * 1024)
    d.set_defaults(fn=cmd_dir)
    l = sub.add_parser("ls"); l.add_argument("path"); l.set_defaults(fn=cmd_ls)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
