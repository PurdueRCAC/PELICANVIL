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
| **deep** | standard under **both** server modes (plain `uvicorn` and `wsgi` = `passenger_wsgi:application` via a2wsgi under waitress) + a 1,200-file directory job (~10 min); a **long job** over a whole GOES day (24 directories, 2,833 files, ~24 min, capped by `DURATION`); the concurrency test x `REPS`; a 24-job queue; repeated kill/recover cycles (at job start, between items, near the end, twice in a row) x `REPS`; three larger files concurrently; the same file requested twice into one destination (behaviour documented); deleting a history record while its job runs; a duration-based soak; CLI parity on a 64 MiB file | hours (defaults: roughly 2.5-3.5 h) |

No test is automatically retried: a flaky pass is signal. Repeated tests report pass rates in the summary.

## Test reference: what each job does, and what a failure means

Look a failing job up here by its `area::name` from `results.log` (strip any `[uvicorn]`/`[wsgi]`/`[repN]`/
parametrize suffix first). "Circumstances" is what real-world condition the job creates before asserting;
"a failure means" is how to read a FAIL for that specific job — not a generic "something broke."

### `boot` (quick)

| job | circumstances | a failure means |
|---|---|---|
| `page_renders[mode-home\|categories\|dataset_search\|quick_access\|downloads\|documentation\|admin]` | GETs each real page once, parametrized per page so one broken page can't mask the others. | That specific page returned non-200 or non-HTML — a template/route error on that page. `admin` needs the temp catalog's `authorizedUsers` to include the harness user (seeded automatically); if only `admin` fails, check that seeding, not the page itself. |
| `about_route_removed` | GETs `/about`, which was removed from `main.py` (it was never linked from anywhere, its template never existed). | If this is not a 404, the route came back (a merge, a revert) without being re-wired — check `main.py` for a re-added `/about` handler. |
| `static_assets_and_json_endpoints` | Fetches the three main JS bundles and the catalog/history JSON endpoints against a freshly seeded, empty temp DB. | Either a static file 404'd/came back tiny (a real asset regression) or a "fresh DB" endpoint returned non-empty data (the temp DB wasn't actually empty — an isolation bug, not an app bug). |
| `server_uses_only_temp_paths` | Checks the launcher's own startup log line and that every DB/queue/flag path the running server holds is under the run's scratch dir. | `PV-ISOLATION-FAILURE` in the log or a path outside scratch means the isolation launcher failed to patch `api.core.config` before `main` imported it — **treat as serious**: a real run could otherwise touch the shared production DB. |

### `db` (quick / standard)

| job | circumstances | a failure means |
|---|---|---|
| `schema_init_is_idempotent` | Restarts the same temp DB twice via `_init_db()` (import-time) and checks the schema, WAL mode, and that history/jobs stay empty. | The schema changed across a restart, or WAL mode isn't set — a migration/idempotency bug in `downloads.py::_init_db`. |
| `history_and_job_rows_lifecycle` | Downloads one small file through the real API and checks the history/job rows at each stage: created, in-progress, terminal, then deleted (with 404s afterward). | The DB rows don't match what the API/UI would show at some stage of a real download's life — check which stage (created/updated/deleted) failed in the assertion message. |
| `catalog_crud_through_admin_routes` (standard) | Full CRUD (add/modify/remove, with duplicate-rejection checks) on datasets/categories/users through the real `/admin/*` routes against the temp shared-catalog DB. | An admin route returned the wrong status code or didn't persist/reject as expected — a real admin-panel bug, not specific to downloads. |

### `downloads` (quick / standard) — real federation calls throughout

| job | circumstances | a failure means |
|---|---|---|
| `small_file_downloads_with_correct_sha256` (quick) | Downloads one small real file end to end. | The most basic download path is broken — check this first if many other `downloads::*` jobs also fail. |
| `real_404_reports_not_found_with_reason` (quick) | Downloads a path that genuinely does not exist. | The app didn't classify a real 404 as `not_found`, or gave no reason, or left partial files on disk. |
| `token_required_namespace_without_a_token` (quick) | Lists and downloads a real token-gated namespace with **no token available** (and confirms the `pelican` binary is off PATH, so pelicanfs can't launch an interactive OIDC flow). | Either the 401/`auth_required` shape is wrong, detection took suspiciously long (an interactive auth flow may have started), or a token got saved that was never given. |
| `two_files_in_one_job` | Two small real files in one job. | A multi-item batch doesn't correctly report both items succeeding — check if this fails but single-file jobs pass (points at batch handling, not transfer). |
| `nested_directory_produces_exactly_the_expected_tree` | Downloads a real 4-level nested directory; requires an **exact** file tree match. Regression guard for issue #3. | Extra files (stray host-named folders / an HTML listing saved as a file — the #3 bug returning) or missing files in the downloaded tree. |
| `plus_name_appears_in_listing` | Lists a real directory containing a `+`-named file. | The listing endpoint doesn't return the `+` name correctly — an aiowebdav2/encoding-layer issue, not the download path. |
| `plus_name_downloads_via_the_listed_path` | Downloads the exact path string the listing above returned (a `+`-named file). | The file lands under the **wrong local filename** (e.g. percent-encoded) even if content is correct — this is exactly the bug fixed in `download_one_file`'s single-file branch; a regression here means that fix broke. |
| `reset_default_filesystem_produces_a_working_new_session` | Runs the real `api.routes.pelican.reset_default_filesystem()` in a subprocess against the checkout under test, then a real download through it. | `reset_default_filesystem()` stopped genuinely resetting the aiohttp session (see the three `federation::*` jobs below for the underlying mechanism) — a regression in the fsspec-caching workaround. |
| `destination_path_with_space_and_plus` | Downloads a small file into a **local destination directory** whose name contains a space and `+`. | A local path (not a remote path) containing special characters breaks the download or its DB record — different bug class from the plus-name federation tests above. |
| `mixed_good_and_bad_batch_is_partial` | One batch: two real files + one real 404, so the job must end `partial`. Polls the history row to terminal separately (not just the job row) before asserting. | The `partial` status, per-file reasons, or DB persistence of a mixed-outcome batch is wrong. If this flakes intermittently, suspect a timing race reappearing (see `wait_history_terminal` in `helpers.py`) before suspecting the app. |
| `missing_directory_and_missing_nested_file_are_not_found` | Two different shapes of "doesn't exist" (a missing directory, a missing file inside a real dataset) in one batch. | Either shape isn't classified `not_found`, or files were left on disk despite total failure. |
| `nonexistent_namespace_is_reported_not_hung` | Downloads a path in a namespace that **does not exist at all** (`BadDirectorResponse` from the director) — behavior is recorded, not dictated to a specific category. | The job never reaches a terminal state (hangs) or reports no reason at all — a genuinely unhandled failure shape, worth reading the noted category/error in the log even on PASS. |
| `large_directory_listing_flags_truncation_instead_of_hiding_it` | Lists a real directory known to hold 1,919 objects, which the origin server caps at 1,000 per PROPFIND response. | Either the count isn't the expected origin cap (origin behavior changed — re-run `pin_fixture.py` to check), or the count matches the cap but `X-Listing-Truncated` isn't set (a regression in the truncation-flagging fix). |
| `existing_file_after_a_404_in_the_same_namespace_still_downloads` | One good file requested right after a 404 for a different path in the **same namespace**, repeated 8 times. | **Known environment-dependent pelicanfs behavior** (`bad_cache()` dropping a cache after any transfer exception) — not necessarily an app regression. Cross-check against `federation::pelicanfs_direct_existing_file_after_a_404` (same sequence, no app in the loop): if that one also fails, it's pelicanfs/the federation, not PELICANVIL. |

### `concurrency` (standard / deep) — real federation calls throughout

| job | circumstances | a failure means |
|---|---|---|
| `executor_cap_of_three_with_five_jobs` (standard, 5 jobs) / `executor_cap_of_three_repeated` (deep, 4 jobs, x `REPS`) | Starts N jobs simultaneously against one server; samples `download_jobs` directly (not by polling status endpoints, which would be inconsistent) to find the peak concurrent `in_progress` count. | Peak concurrency exceeded 3 (the executor cap in `downloads.py::_job_executor`), or a job never reached `complete`, or a later job started before an earlier slot actually freed — a real concurrency-control regression. |
| `directory_listing_works_while_downloads_run` | Two real directory downloads running, while repeatedly listing an unrelated directory; requires the listing to overlap the downloads at least `MIN_LISTING_OVERLAP_BY_DEPTH[ctx.depth]` times (5 standard, 2 deep — scaled down for deep tier's heavier contention). | Either a listing call failed/changed content mid-download (listing and downloading interfering with each other), or too few overlaps happened even accounting for tier — check `worst list-path latency` in the note for a slow-listing-under-load signal. |
| `queue_of_24_jobs_drains_correctly` (deep) | 24 small jobs queued against one server, well past the executor cap of 3. | Same concurrency-control checks as above, at higher queue depth — a regression here but not in the 5-job test suggests a scaling issue (e.g. queue bookkeeping) rather than the cap itself. |
| `three_larger_files_download_concurrently` (deep) | Three real files of tens/hundreds of MB downloading at once. | A concurrency issue specific to larger, longer-running transfers (vs. the small-file concurrency tests above) — e.g. a race that only shows up once transfers take long enough to overlap meaningfully. |
| `same_file_requested_twice_to_one_destination` (deep) | The exact same real file requested by two jobs into the same destination at the same time — **behavior is documented, not asserted to be any one outcome**. | Both jobs must still reach a terminal state and the server must stay healthy; if either job claims `complete`, the on-disk file must be byte-correct. A failure here means the app crashed, hung, or corrupted the file under this race — not "which job should win." |
| `delete_record_while_job_is_running` (deep) | Deletes a download's history record while its background thread is still writing files. | The orphaned thread's DB writes resurrected the deleted record, new ERROR/Traceback lines appeared in the server log, or the files it was still writing ended up incomplete/wrong. |

### `restart` (standard)

| job | circumstances | a failure means |
|---|---|---|
| `unwritable_destination_then_restart_succeeds` | A real download into a `chmod`'d-unwritable destination (must classify `permission`), then the permissions are fixed and Restart is used to recover the *same* record. **Skips on Windows / as root**, where making a directory genuinely unwritable isn't possible. | Either the local-permission failure isn't classified `permission`, or the Restart endpoint doesn't recover cleanly (wrong status, duplicate job row, file not re-downloaded). |
| `restart_file_retries_only_that_file_then_restart_all` | A 3-item batch fails entirely (unwritable dest), then per-file Restart is used on just one item, then whole-record Restart on the rest — checks that untouched items stay untouched and already-succeeded files are never re-downloaded (mtime check). | Per-file or whole-record Restart retried/cleared files it wasn't asked to, or re-downloaded a file that had already succeeded (wasted transfer, or worse, a race). |
| `restart_of_partial_keeps_succeeded_file_untouched` | A batch with one real success and one real permanent failure (no chmod needed) ends `partial`; Restart is used and the already-succeeded file's mtime must not change. | The succeeded file was re-downloaded on restart, or the record's status/item-count is wrong after restart. |

### `kill` (standard / deep) — real `kill -9` of the harness's own spawned server

| job | circumstances | a failure means |
|---|---|---|
| `kill_mid_download_resumes_the_same_job` (standard) | Kills the app process mid-download (after some real items have already succeeded, mid-directory-write), restarts it, and requires the **same job_id** to resume and reach `complete` with succeeded items untouched. This is the direct regression test for GitHub issue #6 ("jobs stuck in progress for days"). | The job never resumes (stays `in_progress`/`STALL` forever — the original issue #6 symptom), a different job_id was minted, an already-succeeded file was re-downloaded, or a truncated file is left masquerading as complete. **This is the highest-value single test in the suite** — treat any failure here as high priority. |
| `kill_recover_cycle[point=at_start\|between_files\|near_end\|double_kill, repN]` (deep) | Same as above, but kills at four different points in the job's lifecycle (including twice in a row), repeated `REPS` times. **SKIPs** a given `point`/`rep` if the job finished before the intended kill point could be reached (too fast — not a failure). | Same meaning as the standard-tier job above, but pinpoints *when* in the download lifecycle recovery breaks (e.g. only `between_files` failing suggests a mid-batch bookkeeping bug specifically, not the recovery mechanism in general). |

### `federation` (standard) — real federation, deliberately WITHOUT the app, to attribute failures correctly

| job | circumstances | a failure means |
|---|---|---|
| `pelicanfs_direct_existing_file_after_a_404` | Same 404-then-good-file sequence as `downloads::existing_file_after_a_404_in_the_same_namespace_still_downloads`, but calling pelicanfs directly with no app in the loop at all. Controls (good file only, no 404) run first and must be perfect. | If this fails **and** the `downloads::` version also fails: it's pelicanfs/the federation, not the app — a known, environment-dependent, unfixed-at-this-layer issue. If this passes but the `downloads::` version fails: the app is doing something extra to cause it — investigate the app, not pelicanfs. |
| `osdf_filesystem_instance_caching_mechanism` | Documents a fsspec library fact: constructing `OSDFFileSystem(direct_reads=False)` twice returns the *same* cached instance. Asserts `a is b` (the caching exists). | If this now fails (`a is not b`), fsspec/pelicanfs's caching behavior has changed upstream — `reset_default_filesystem`'s workaround may no longer be necessary (harmless either way, but worth knowing and possibly simplifying). |
| `skip_instance_cache_alone_leaves_the_real_session_shared` | Demonstrates the half-fix that looked sufficient but wasn't: `skip_instance_cache=True` alone still leaves the real aiohttp session (`http_file_system._session`) shared between "different" instances. Asserts the sharing still exists. | If this now fails (session no longer shared with just `skip_instance_cache`), pelicanfs's `__init__` has changed how it builds `http_file_system` — `reset_default_filesystem`'s extra `HTTPFileSystem.clear_instance_cache()` step may no longer be needed. |
| `skip_instance_cache_plus_clearing_http_cache_gives_a_real_new_session` | The actual two-part fix mechanism, confirmed to produce a genuinely new `http_file_system` and session, and that the new instance still works. | The two-part fix (`clear_instance_cache()` + `skip_instance_cache=True`) stopped producing a genuinely new session — the core mechanism behind the `reset_default_filesystem` fix is broken. |

### `long` (deep) — long-running, real, costly in wall time

| job | circumstances | a failure means |
|---|---|---|
| `many_file_directory_job` | Ten real GOES-16 hour directories (1,200 files) submitted as one job — exactly what ticking ten folders in the UI would send. | A large real multi-directory batch doesn't complete correctly, or the resulting tree doesn't match what the federation reports *right now* (re-checked live, not just against the pin) — could be a real regression or federation drift; the note distinguishes the two. |
| `long_running_directory_job` | One real ~24-minute, 2,833-file directory job (the same duration/shape the original issue #6 report showed failing), capped by `DURATION`/`LONG_MAX_GB`. A job that's still healthily progressing when the cap hits **passes** (capped, not failed) as long as no failures/stalls occurred and finished files are byte-correct. | A real failure or STALL occurred before the cap, or a "finished" file (other than the one still actively being written) has the wrong size — a long-duration-specific regression that short tests wouldn't catch. |
| `soak_repeated_download_and_verify` | Repeats download→verify→delete against one long-lived server for `SOAK_DURATION` seconds, logging every iteration to `soak-<mode>.jsonl`; fails if **any** iteration failed. | Look at `soak-<mode>.jsonl` for which iteration(s) failed and their error — a failure partway through a long-lived server's life (vs. failing immediately) suggests degradation over time (e.g. connection/resource exhaustion), not a cold-start bug. |

### `parity` (standard / deep) — SKIPs if the `pelican` CLI isn't found

| job | circumstances | a failure means |
|---|---|---|
| `cli_and_app_agree_on_a_small_file` / `_a_nested_directory` / `_the_64mib_file` (deep) | Downloads the same real path via the app and via the real `pelican` CLI (the exact command the UI's snippet shows), and requires byte-identical results. | The app and CLI disagree on the same real path — since the CLI is the reporter's own "it works with `pelican get`" comparison point in issue #6, this is a direct app-vs-CLI regression check. **SKIP** (not FAIL) means the CLI wasn't found — check `PELICAN_BIN` / `module load pelican`, not the app. |

### `invariants` / `isolation` (standard) — run last, over the whole run

| job | circumstances | a failure means |
|---|---|---|
| `download_db_consistency_invariants` | After a settle period, checks every server's DB from this run: no job stuck non-terminal with no live thread, every history row's status agrees with its job row and file list, every failed file has a real reason and known category. | A DB consistency rule was violated *by any test in this run* — this often points at a bug a specific test's own assertions didn't check for; read which server/job/history id is named in the violation and cross-reference which test created it. |
| `real_state_untouched` | Compares a snapshot of the real `~/.pelican-ui` (and shared production paths) taken before the run against one taken after. | The real `~/.pelican-ui` changed — an isolation failure (the suite touched real user state). **Treat as serious.** A shared production-path change is only noted, not failed, since another real user could cause that independently of this run. |

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

Two fixtures have real-world quirks worth knowing before you touch them: the `+`/space fixture's S3 key contains a
real **space**, but the federation lists and serves it as `Measurement+1.xml` (a literal space or `%20` is a 404);
the `big_listing` directory holds 1,919 objects (S3 truth) but the app's list endpoint returns exactly 1,000 (an
origin-server cap) and flags that via the `X-Listing-Truncated` response header rather than pretending the listing
is complete.

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
CLI-parity tests (`test_parity.py`) run instead of silently skipping. If `pelican` isn't the right module name on
a given cluster, the load fails loudly (a warning, not a hard error) and the parity tests SKIP with a clear reason.

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
