"""UI-equivalent (the app) vs the `pelican` CLI: same path, same bytes.
SKIPPED with a clear reason when the CLI is not on PATH (set PELICAN_BIN to point at it)."""
import os
import subprocess

import pytest

from .harness import env, jobs
from .helpers import assert_single_file, base, run_job


def _cli():
    p = env.find_pelican_cli()
    if not p:
        pytest.skip("`pelican` CLI not found on PATH (set PELICAN_BIN=/path/to/pelican to enable the parity tests)")
    return p


def _cli_get(cli, path, out, ctx, recursive=False, timeout=600):
    """`pelican object get "osdf://<path>" <out>` -- the exact command the UI's own snippet shows."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = [cli, "object", "get"] + (["-r"] if recursive else []) + [f"osdf://{path}", str(out)]
    r = subprocess.run(cmd, env=env.sanitized_env(ctx.work_dir / "cli_home", {"PATH": os.environ.get("PATH", "")}),
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"{' '.join(cmd)} -> exit {r.returncode}\n{env.redact(r.stdout[-800:])}\n{env.redact(r.stderr[-800:])}"
    return r


@pytest.mark.tier("standard")
@pytest.mark.timeout(900)
def test_cli_and_app_agree_on_a_small_file(api, dest, fx, ctx):
    cli = _cli()
    entry = fx["small_file"]
    job, st = run_job(api, dest, [entry["path"]])
    assert st["status"] == "complete", st
    app_file = assert_single_file(dest, entry, "app copy")
    cli_dir = ctx.dest("cli-small")
    _cli_get(cli, entry["path"], cli_dir, ctx)
    cli_file = jobs.find_file(cli_dir, base(entry["path"]), entry["size"])
    assert cli_file is not None, f"CLI produced no {base(entry['path'])}: {sorted(env.dir_tree(cli_dir))}"
    assert env.sha256_file(cli_file) == env.sha256_file(app_file) == entry["sha256"], "CLI and app copies differ"


@pytest.mark.tier("standard")
@pytest.mark.timeout(1200)
def test_cli_and_app_agree_on_a_nested_directory(api, dest, fx, ctx):
    cli = _cli()
    nd = fx["nested_dir"]
    job, st = run_job(api, dest, [nd["path"]], timeout=480)
    assert st["status"] == "complete", st
    app_root = dest / base(nd["path"])
    app_map = {k: env.sha256_file(app_root / k) for k in env.dir_tree(app_root)}
    cli_dir = ctx.dest("cli-nested")
    _cli_get(cli, nd["path"], cli_dir, ctx, recursive=True)
    candidates = [cli_dir / base(nd["path"]), cli_dir]
    cli_map = None
    for root in candidates:
        m = {k: env.sha256_file(root / k) for k in env.dir_tree(root)}
        if len(m) == len(app_map):
            cli_map = m
            break
    assert cli_map is not None, f"CLI tree does not match the app tree in file count ({len(app_map)}): {sorted(env.dir_tree(cli_dir))[:8]}"
    assert cli_map == app_map, f"differences: {sorted(set(cli_map.items()) ^ set(app_map.items()))[:6]}"


@pytest.mark.tier("deep")
@pytest.mark.timeout(1800)
def test_cli_and_app_agree_on_the_64mib_file(api, dest, fx, ctx):
    cli = _cli()
    f = fx["medium_files"][0]
    job, st = run_job(api, dest, [f["path"]], timeout=900)
    assert st["status"] == "complete", st
    app_file = jobs.find_file(dest, base(f["path"]), f["size"])
    assert app_file is not None
    cli_dir = ctx.dest("cli-64m")
    _cli_get(cli, f["path"], cli_dir, ctx)
    cli_file = jobs.find_file(cli_dir, base(f["path"]), f["size"])
    assert cli_file is not None
    assert env.md5_file(app_file) == env.md5_file(cli_file) == f["md5"]
