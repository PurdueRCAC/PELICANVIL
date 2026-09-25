# PELICANVIL test suite

One command, on Anvil, tests the **real app against the real Pelican/OSDF federation** and writes
pass/fail results to a log file. No browser, no mocks, no emulated federation. The indexing worker is out
of scope entirely.

```bash
bash   tests/pelicanvil_tests.sbatch setup       # ONCE, on a login node: builds the test venv (see "Python environment")
sbatch tests/pelicanvil_tests.sbatch selftest    # ~1 min  harness self-check, needs NO federation; run this first
sbatch tests/pelicanvil_tests.sbatch quick       # ~5 min
sbatch tests/pelicanvil_tests.sbatch standard    # ~30 min
sbatch tests/pelicanvil_tests.sbatch deep        # hours
```

`#SBATCH --time` is static (8 h, for `deep`). Shorten it for smaller runs:
`sbatch --time=00:20:00 tests/pelicanvil_tests.sbatch quick`, `--time=01:30:00` for `standard`.
Submit from your checkout (`cd ~/ondemand/dev/pelicanvil` first): Slurm runs a copy of the script, so the
checkout is taken from the submit directory (or `SUITE_DIR=`).

## What each depth runs

| depth | contents | typical time |
|---|---|---|
| **quick** | preflight canary; app boots and every page renders; DB basics (history/job rows created, updated, listed, deleted through the API; schema init idempotent across restarts); one small file verified by sha256; a real 404 (`not_found` with a reason); a real token-required namespace with no token (`auth_required`, 401 shape); real `~/.pelican-ui` untouched | ~5 min (about 15 s of tests once the venv exists) |
| **standard** | quick + small nested directory (exact expected tree, none of the stray host-named folders or HTML-as-file from issue #3); a `+`/space name; destination path with space and `+`; unwritable destination then Restart (chmod, `permission`); mixed good+bad batch (`partial`, reasons persisted); whole-record and per-file Restart; listing completeness for a >1000-entry directory; an existing file requested right after a 404 in the same namespace (through the app **and** through pelicanfs alone, to attribute failures); concurrency (5 jobs at once: never more than 3 `in_progress`, the rest `pending` until a slot frees, one history row per job, no `database is locked`); directory listing while downloads run; UI-equivalent-vs-`pelican` CLI parity (SKIPs if the CLI is absent); **kill -9 mid-download** then restart (same job_id resumed, succeeded items untouched, no truncated file, no duplicate history rows); DB consistency invariants | ~10-30 min |
| **deep** | standard under **both** server modes (plain `uvicorn` and `wsgi` = `passenger_wsgi:application` via a2wsgi under waitress) + a 1,200-file directory job (~10 min); a **long job** over a whole GOES day (24 directories, 2,833 files, ~24 min, capped by `DURATION`); the concurrency test x `REPS`; a 24-job queue; repeated kill/recover cycles (at job start, between items, near the end, twice in a row) x `REPS`; three larger files concurrently; the same file requested twice into one destination (behaviour documented); deleting a history record while its job runs; a duration-based soak; CLI parity on a 64 MiB file | hours (defaults: roughly 2.5-3.5 h; measured 32 min with `REPS=1 DURATION=240 SOAK_DURATION=90`) |

No test is automatically retried: a flaky pass is signal. Repeated tests report pass rates in the summary.

## Environment overrides

Export them, or prefix the command (`ONLY=kill sbatch tests/pelicanvil_tests.sbatch standard`).

| variable | meaning |
|---|---|
| `ONLY` | pytest `-k` expression, e.g. `ONLY="kill or restart"`, `ONLY=nested_directory` |
| `REPS` | repetitions for the repeated deep tests (default 3) |
| `DURATION` | cap in seconds for the long job (default 3600; the job normally finishes in ~25 min) |
| `SOAK_DURATION` | length of the soak loop in seconds (default 1800) |
| `SERVER` | `uvicorn`, `wsgi` or `uvicorn,wsgi` (default `uvicorn`; deep defaults to both) |
| `LONG_MODES` | server mode(s) for the long job (default: same as `SERVER`) |
| `APP_DIR` | app checkout under test (default: this checkout). Use a worktree for before/after, see below |
| `STALL_SECONDS`, `LONG_STALL_SECONDS` | a job with no change to per-file status *or* bytes on disk for this long is a `STALL` failure (defaults 600 / 1200) |
| `LONG_MAX_GB` | scratch cap for the long job (default 20) |
| `PV_PYTHON`, `TEST_VENV` | interpreter / venv (default venv: `/anvil/scratch/$USER/pelicanvil-tests/venv`) |
| `PELICAN_BIN` | path to the `pelican` CLI for the parity tests |
| `PV_MODULES` | modules to `module load` first, e.g. `python/3.11` |
| `KEEP_DATA=1` | keep downloaded data even on a fully passing run |

## Reading the results

Everything for a run is in `logs/test-runs/<jobid>-<depth>/`:

* `results.log`: **read this first**. Written incrementally, so a walltime kill still leaves what finished.
  ```
  PASS downloads::small_file_downloads_with_correct_sha256[uvicorn] (2.0s)
  FAIL downloads::plus_name_downloads_via_the_listed_path[uvicorn] (2.3s)
      AssertionError: ... the download is on disk but under the WRONG NAME ...
      at helpers.py:31
      evidence: failures/downloads__plus_name_downloads_via_the_listed_path_uvicorn_/
  SKIP restart::unwritable_destination_then_restart_succeeds[uvicorn] (0.0s)
      reason: cannot make a directory unwritable here (Windows, or running as root)
  ...
  27 passed, 4 failed, 4 skipped, env-unhealthy: no
  ```
  A header records host, commit, features present, library versions and the known limits. After the counts, the
  summary lists failed tests, **pass rates across repetitions**, the sections below, and the limits again.
  Failure kinds: `[timeout]` (hard per-test timeout), `[STALL]` (no progress), `[server-died]`, `[setup error]`,
  `[ENV-SUSPECT]` (the federation canary failed right after the test: possibly environmental).
* `results.json`, `results.jsonl`, `junit.xml`: machine-readable copies.
* `failures/<test>/`: for each failed test: the exception, server log tail, the temp `last_error.log`, and a
  read-only dump of the relevant `download_jobs` / `download_history` rows.
* `anomalies/`, listed in the summary: evidence for events a green run must still surface, captured the moment
  they happen because they cannot be reproduced on demand:
  * **`cannot schedule new futures after shutdown`**, shown prominently with the full traceback, the server
    mode, and how many seconds into the job it happened. It was seen in real use and cannot be triggered on
    demand: **a green suite does NOT rule it out.**
  * **unknown-category** download failures (their tracebacks are in `server-logs/`, and `last_error.log`).
  * **false `not_found`**: the app said `not_found` for a path the fixtures guarantee exists. The harness
    immediately asks the federation directly and records whether it agrees.
* `server-logs/<server>.log`: every app server's stdout/stderr (pelicanfs debug logging is on), plus
  `.last_error.log` and, when the run was not green, a copy of the downloads DB.
* `slurm.out`, a copy of the job's Slurm output.

**Exit codes:** `0` all passed, `1` test failures, `2` harness error (bad usage, isolation failure, no tests
selected), **`3` ENVIRONMENT UNHEALTHY** (federation unreachable / a pinned fixture drifted / scratch full: no tests
ran, and it is *not* an app failure), `4` aborted by Slurm walltime/scancel (results are partial).

## Environment health vs app failure

Before any test, a **preflight canary** checks (with the same real pelicanfs the app uses) that the director is
reachable and every fixture needed for this depth still exists with its pinned size, that the protected namespace
still refuses a token-less listing, and that scratch has room. Any problem aborts the run with
`ENVIRONMENT UNHEALTHY (federation/fixtures)` and exit code 3. During the run, after each failed test the
canary is repeated; a failure marks it `[ENV-SUSPECT]`.

## Isolation and credentials (no production-code changes)

`tests/pvsuite/harness/launcher.py` starts the real app in a subprocess. It imports `api.core.config` **first**,
overwrites `DB_PATH`, `DOWNLOADS_DB_PATH`, `INDEXING_QUEUE_PATH` and `BLIND_MODE_PATH`, and only then imports
`main` (every other module does `from api.core.config import ...`, so they bind the patched values; import-time
`_init_db()` / `_recover_interrupted_jobs()` run against the temp DB). After import it verifies every module
holds a temp path (else exit 97 and the suite aborts). The catalog DB is created by running the repo's own
`scripts/init_db.py` against the temp path. Adding a dataset enqueues indexing, which only appends to a scratch
JSON file that nothing ever reads. Each server gets a temp `HOME` under `/anvil/scratch/$USER/pelicanvil-tests/<jobid>-<depth>/`.

pelicanfs discovers credentials from `BEARER_TOKEN`, `BEARER_TOKEN_FILE`, `$XDG_RUNTIME_DIR/bt_u<uid>`, `TOKEN`,
`_CONDOR_CREDS` / `./.condor_creds`, and by running the `pelican` binary (an interactive OIDC flow that could
block a "no token" test for minutes). All of it is neutralised: those variables are unset, `XDG_RUNTIME_DIR` and
`_CONDOR_CREDS` point at empty temp dirs, and every directory containing a `pelican` binary is removed from the
app's `PATH`. Proxy variables are removed. Log output is redacted for bearer tokens / JWTs. The last test of a
run verifies the real `~/.pelican-ui` did not change.

## What the first runs found (commit `0f0c48f`, 2026-09-24, from a Windows dev box and a Linux box)

These are the suite doing its job, not harness problems; expect these FAILs until they are addressed:

| test | finding |
|---|---|
| `boot::page_renders[...-about]` | `GET /about` is a 500: `api/templates/about.html` does not exist (`main.py`, the `/about` route). |
| `downloads::plus_name_downloads_via_the_listed_path` | The `+` file downloads with correct content but is **saved as `FFC_Profile_Measurement%2B1.xml`** (percent-encoded on disk). |
| `downloads::listing_returns_every_entry_of_a_large_directory` | The list endpoint returns exactly **1000** entries for a directory with 1,919 objects (S3 truth), so directory downloads of such folders would silently omit files. |
| `downloads::existing_file_after_a_404_in_the_same_namespace_still_downloads` and `federation::pelicanfs_direct_...` | **Environment-dependent, so it may or may not fail on Anvil.** From a Windows dev box: after a genuine 404 for one path, the next request for an *existing* file in the same namespace failed with a false `not_found` in 5/8 app rounds and 4/12 pelicanfs-only rounds (controls: 25/25 and 6/6 ok; a 404 in a *different* namespace never caused it; a fresh **process** never showed it). From a WSL/Linux box the same tests passed 8/8 and 12/12. Mechanism (read from pelicanfs 1.3.1 source, supported by pelicanfs debug logs): any transfer exception, including a 404 for a nonexistent object, makes pelicanfs `bad_cache()` drop that cache from the namespace's list, and the state lives in the process for the life of the filesystem instance (15 min TTL); whether the next cache in the list serves a cold object depends on which caches the director returns for the client's location. `not_found` is never retried by the app. Look for `FALSE not_found` in the summary and `Marking cache at ... as bad` in `server-logs/*.log`. |
| `federation::reset_shaped_construction_yields_a_fresh_filesystem` | `OSDFFileSystem(direct_reads=False)` returns the **identical cached object** every time (fsspec instance cache; only `skip_instance_cache=True` differs), so `reset_default_filesystem()` in `api/routes/pelican.py` (which constructs it again) does not actually reset the session or the namespace/bad-cache state. |
| `kill::kill_mid_download_resumes_the_same_job` on `4766e6d` | `[STALL]`: the job stays `in_progress` forever after the process is killed (the reporter's days-old orphaned jobs). Passes on `0f0c48f` (the job resumes in ~20 s). |

Also observed (cause not verified): after SIGTERM, a test server with a download in flight lingered for minutes
instead of exiting (probably the non-daemon download-executor threads), which may matter for Passenger restarts.
The harness itself always ends servers with SIGKILL after a short grace period.

## Known limits (also printed in every log)

A passing run does **not** clear: the `cannot schedule new futures after shutdown` error (only captured if it
happens naturally); multi-hour or multi-hundred-GB transfers; real **Passenger** process recycling / idle-timeout
termination (`SERVER=wsgi` exercises a2wsgi's event loop and threading under waitress, which is the closest proxy
available, not Passenger itself); the **front-end JavaScript** (progress-label text, the ~20 minute toast poll
cap), since there is no browser layer, only the endpoints the JS calls.

## Fixtures (`tests/fixtures.yaml`)

Real, public, archival paths, pinned by size and sha256 (or the origin-published md5): a tiny and a small file,
a 23-file 4-level nested directory (`cellpainting-gallery` metadata), a `+`-named file, an 117-file GOES-16 hour, ten
GOES hours for the many-file job, a whole GOES day for the long job, medium files (64 MiB `testfile-64M`,
78 MB, 210 MB), nonexistent paths (deliberately inside *real* namespaces: a path in a non-existent namespace
yields `BadDirectorResponse`, which the app treats as a connection failure), and the token-required namespace
`/chtc/PROTECTED` (only ever used **without** a token).

Notes discovered while pinning: the `+`/space fixture's S3 key contains a real **space** but the federation lists
and serves it as `Measurement+1.xml` (a literal space or `%20` is a 404); the `big_listing` directory holds 1,919
objects (S3 truth) but the app's list endpoint returned 1,000.

To re-pin an entry (paths are federation paths, e.g. `/aws-opendata/...`):
```bash
python tests/tools/pin_fixture.py file /ns/path/file.txt              # size + sha256 + md5
python tests/tools/pin_fixture.py file /ns/path/big.bin --size-only
python tests/tools/pin_fixture.py dir  /ns/path/dir --sha-all         # every file's size + sha256
python tests/tools/pin_fixture.py ls   /ns/path/dir                   # what the listing shows
```
then edit `tests/fixtures.yaml`, and run `sbatch tests/pelicanvil_tests.sbatch quick` to confirm the preflight
accepts it (`standard`/`deep` preflights check the larger entries).

## Python environment

The suite needs Python >= 3.11, the app's dependencies, and `pytest pyyaml requests waitress`
(`tests/requirements.txt`). `setup` (run on a login node, needs network) builds a venv at
`/anvil/scratch/$USER/pelicanvil-tests/venv` from `uv.lock` (with `uv`) or pip. The job builds it on first use if
compute nodes have network access; otherwise run `setup` once. To use an existing interpreter, `PV_PYTHON=...`.
Without `waitress`, `SERVER=wsgi` tests are **SKIPPED and reported loudly**, not silently dropped.

## Before/after: prove the suite detects the pre-fix behaviour

```bash
git worktree add ~/pelicanvil-prefix 4766e6d          # before cdfb44f / 900e898 / 0f0c48f
APP_DIR=~/pelicanvil-prefix sbatch tests/pelicanvil_tests.sbatch standard
```
The tests come from your current checkout; only the app under test changes. The header records which fixes the
checkout under test contains (`connection_retry`, `interrupted_job_recovery`, ...). The harness uses only HTTP
endpoints and files that exist on both commits, so nothing needs to be skipped: on the old commit the kill test
is expected to FAIL with a `STALL` (the orphaned job never leaves `in_progress`).

## Layout

```
tests/pelicanvil_tests.sbatch    the single entry point       tests/run_suite.py     orchestrator (preflight, pytest, summary, exit codes)
tests/fixtures.yaml              pinned fixtures              tests/tools/pin_fixture.py   fixture pinning helper
tests/pvsuite/harness/           launcher, server lifecycle, API client, federation helper, job waiting/stall detection
tests/pvsuite/test_*.py          the tests (each carries @pytest.mark.tier / timeout)
```
`tests/test_download_retry.py` (the older mocked unit tests) is not part of this suite and is untouched.
