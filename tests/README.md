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
| **standard** | quick + small nested directory (exact expected tree, none of the stray host-named folders or HTML-as-file from issue #3); a `+`/space name; destination path with space and `+`; unwritable destination then Restart (chmod, `permission`); mixed good+bad batch (`partial`, reasons persisted); whole-record and per-file Restart; truncation flagged (not silently hidden) for a >1000-entry directory listing; an existing file requested right after a 404 in the same namespace (through the app **and** through pelicanfs alone, to attribute failures); concurrency (5 jobs at once: never more than 3 `in_progress`, the rest `pending` until a slot frees, one history row per job, no `database is locked`); directory listing while downloads run; UI-equivalent-vs-`pelican` CLI parity (SKIPs if the CLI is absent); **kill -9 mid-download** then restart (same job_id resumed, succeeded items untouched, no truncated file, no duplicate history rows); DB consistency invariants | ~10-30 min |
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
| `PELICAN_BIN` | path to the `pelican` CLI for the parity tests — overrides the automatic `module load pelican` (see below) |
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

## What the first runs found, and what was fixed (2026-09-28)

Three real Anvil runs (quick, standard, deep) against commit `0f0c48f` found the following. Three real app bugs
have since been fixed (`reset_default_filesystem`, the `+`-name filename bug, and the listing-truncation flag —
all in `api/routes/pelican.py`, plus a small, additive-only JS change for the last one); the dead `/about` route
was removed (`main.py`); two test-only findings (timing races under real load) were fixed in the tests themselves;
the pre-fix `kill` STALL is expected, already-addressed behaviour, not something this pass touched. Re-run any of
the specific tests named below to reproduce/verify.

| finding | root cause | fix |
|---|---|---|
| `reset_default_filesystem()` was a no-op | Confirmed against real pelicanfs 1.3.1/fsspec 2026.6.0, two layers deep: (1) fsspec's caching metaclass returns the *same* `OSDFFileSystem` for identical args regardless of `skip_instance_cache` propagation quirks noted below; (2) even once the *outer* object is made genuinely new (`skip_instance_cache=True`), `OSDFFileSystem.__init__` separately constructs its own `fsspec.implementations.http.HTTPFileSystem` — the thing that actually holds the aiohttp session behind every `.get()`/`.isdir()`/`.open()` call — and *that* inner construction is an ordinary, separately-cached fsspec call that never receives `skip_instance_cache` (fsspec pops it off before `__init__` runs). Two "genuinely different" `OSDFFileSystem` objects built with only `skip_instance_cache=True` were confirmed to still share the exact same `http_file_system` and aiohttp session. `.ls()`/listing was never affected — pelicanfs already opens a fresh WebDAV session per call there. | `api/routes/pelican.py`'s `reset_default_filesystem()`: `HTTPFileSystem.clear_instance_cache()` (empties that class's cache table so the imminent inner construction misses it) followed by `OSDFFileSystem(direct_reads=False, skip_instance_cache=True)`. Confirmed via `osdf.http_file_system` / `osdf.http_file_system._session` identity, not just outer-object identity — see `tests/pvsuite/test_federation.py`'s three `test_*instance_cach*`/`test_skip_instance_cache_*` tests (one deliberately demonstrates the half-fix still failing) and `test_downloads.py::test_reset_default_filesystem_produces_a_working_new_session`. |
| `downloads::plus_name_downloads_via_the_listed_path` | The `+` file downloaded with correct content but was **saved as `FFC_Profile_Measurement%2B1.xml`** — `download_one_file`'s single-file path handed `fs.get()` the percent-encoded path as both the thing to fetch *and* (fsspec derives the local filename from that same string) the source of the output filename. The directory-download path already avoided this by computing each destination filename explicitly from the clean path. | `download_one_file` (`api/routes/pelican.py`) now does the same for the single-file case: computes `local_path` from the clean, un-encoded `path` and calls `fs.get_file(encoded_path, local_path)` instead of `fs.get(encoded_path, storage_location, recursive=True)`. Re-verified against the real fixture: correct filename, size, and sha256. |
| `downloads::listing_returns_every_entry_of_a_large_directory` | A real directory with 1,919 objects (S3 ground truth) returns exactly **1,000** through `/datasets/category/list-path`. Traced by issuing the same PROPFIND directly: the origin server (XRootD's webdav plugin, confirmed via its own `Server: XrootD/v5.9.2` header) returns a complete, well-formed, non-error HTTP 207 capped at 1,000 `<D:response>` entries — no truncation signal of any kind. **This is the origin server's own behavior, not PELICANVIL's, pelicanfs's, or aiowebdav2's** — there is no page size to raise and no continuation cursor to follow. | Not fixable at this layer, so made honest instead of silent: `pelicanlistPath` sets an `X-Listing-Truncated: 1` response header when the listing hits the known 1,000-entry cap (`LISTING_TRUNCATION_SUSPECT_COUNT`); the JSON body shape is unchanged (backward compatible). `datasets.js`/`quick-access.js` check the header and toast "This folder has more than 1000 items — only the first 1000 are shown." The test (renamed `test_large_directory_listing_flags_truncation_instead_of_hiding_it`) now asserts the header is set, not that the full 1,919 come back. |
| `kill::kill_mid_download_resumes_the_same_job` on `4766e6d` | `[STALL]`: the job stays `in_progress` forever after the process is killed (the reporter's days-old orphaned jobs). | Unrelated to this fix pass — this is what commits `cdfb44f`/`900e898` already fixed. Passes on `0f0c48f` (the job resumes in ~20 s); kept failing on `4766e6d` is the suite correctly detecting the pre-fix behaviour (see "Before/after" below). |
| `boot::page_renders[...-about]` | `GET /about` was a 500: `api/templates/about.html` never existed. Investigated: `/about` was never linked from the navbar or any page (only Explore Datasets, Quick Access, Downloads, Docs are) — **a route that was never fully wired up, not a regression**. | Removed the dead route (`main.py`) rather than fabricating page content. `/about` now cleanly 404s — see `test_boot.py::test_about_route_removed`; the page-render parametrize list no longer includes it. |
| `concurrency::directory_listing_works_while_downloads_run` on deep | Only 3 of the required 5+ listings overlapped the downloads under deep tier's heavier concurrent load (29 on standard, same test) — contention from everything else deep tier runs at once, not a regression. | **Test-only fix**: the required overlap count now scales by `ctx.depth` (`MIN_LISTING_OVERLAP_BY_DEPTH` in `test_concurrency.py`: 5 at standard, 2 at deep) instead of a flat 5 regardless of load. |
| `downloads::mixed_good_and_bad_batch_is_partial` on standard | Flaked once: `wait_terminal` correctly waits for the **job** (`download_jobs` table) to reach a terminal status, but the test then read the **history** row (`download_history`, a separate, later DB write — see `_run_download_job` in `api/routes/downloads.py`) once, immediately, with no wait of its own; for a batch fast enough (2.2s for 3 tiny files) the history write can still be a beat behind. | **Test-only fix**: added `wait_history_terminal()` (`tests/pvsuite/helpers.py`) and used it before asserting on the history row, instead of a one-shot read right after `wait_terminal` returns. |
| `downloads::existing_file_after_a_404_in_the_same_namespace_still_downloads` / `federation::pelicanfs_direct_...` | **Separately documented, environment-dependent pelicanfs behavior, not in scope for this fix pass** (not one of the numbered items) and unrelated to any of the fixes above. On this Windows dev box: after a genuine 404, the next request for an *existing* file in the same namespace intermittently fails with a false `not_found` (pelicanfs's own `bad_cache()` dropping a cache after any transfer exception, including a 404). Passed 8/8 and 12/12 from a Linux box; failed 6/12 again on this Windows box during this pass's final full regression run — genuinely environment-dependent, not something this pass's fixes touch. |

Also observed (cause not verified, unrelated to the fixes above): after SIGTERM, a test server with a download in
flight lingered for minutes instead of exiting (probably the non-daemon download-executor threads), which may
matter for Passenger restarts. The harness itself always ends servers with SIGKILL after a short grace period.

Also investigated: `/documentation` was reported with a connection reset (not a clean 500) during one standard run.
`docs.html`/the `documentation_page` route are a normal, static, fully-implemented page with no server-side
computation that could crash a worker — confirmed by reading both, and by hammering `/documentation` with 300
concurrent requests against a real server while a real download job ran (all 300 returned 200, no errors). Could
not reproduce; most likely a one-off Anvil-side network blip rather than an app defect. Flagged as unverified
without a fresh Anvil run — re-run `boot::page_renders[...-documentation]` a few times if it recurs and capture
the server log tail from `failures/`.

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
objects (S3 truth) but the app's list endpoint returns exactly 1,000 (the origin server's own cap — see the fixes
table above) and flags it via the `X-Listing-Truncated` response header rather than pretending the listing is complete.

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

The sbatch script also runs `module load pelican` automatically (unless `PELICAN_BIN` is already set), so the
CLI-parity tests (`test_parity.py`) run instead of silently skipping — previously the `PELICAN_BIN` override
existed but nothing ever loaded the module providing it. If `pelican` isn't the right module name on a given
cluster, the load fails loudly (a warning, not a hard error) and the parity tests SKIP with a clear reason.

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
