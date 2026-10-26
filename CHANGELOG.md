# Changelog

All notable changes to this project are documented here, most recent first. Dates are the UTC dates of the corresponding commits. Issue and pull request numbers refer to this repository on GitHub.

## 2026-10-26

### Fixed
- **Download status could permanently disagree with its history entry.** A download's job record and its entry in the download history used to be saved in two separate steps. If the application was stopped or killed in the narrow window between them, the job was recorded as finished (complete, partial or failed) while its history entry stayed "in progress" forever, and nothing ever went back to correct it. Both records are now written together in a single database transaction, so they can no longer drift apart.
- **Entries already stranded by that bug are repaired automatically.** On startup, any history entry still marked "in progress" beneath a job that has already finished is updated to match the job's final status, file list and error message. For downloads that were restarted, only the most recent attempt is used. The same single-transaction write is also used when a download is recovered after a restart in which every file had already succeeded.

## 2026-10-13

### Changed
- Upgraded the `pelicanfs` dependency from 1.3.1 to 1.5.0 (minimum version in `pyproject.toml`, pinned version and hashes in `requirements.txt` and `uv.lock`).

## 2026-10-12

### Added
- **Automatic retry for temporary server errors during downloads.** Downloads now retry when the data source answers with HTTP 500, 502, 503 or 504, or when a file arrives truncated or corrupted partway through the transfer. These retries use a longer wait than connection errors (up to three retries, after 5, 15 and 30 seconds) because server-side problems tend to last longer than a dropped connection. Each kind of failure keeps its own retry budget, so a mix of failures cannot cause unbounded retrying. Client errors such as 401, 403 and 404, and any other unrecognized failure, are still reported immediately without retrying.

### Fixed
- **Incomplete files are no longer left behind after a failed transfer.** A partially written file is deleted before each retry so the retry starts from scratch, and it is also deleted when the retries are exhausted, so a truncated file can never be mistaken for a finished download. This applies to both single-file and folder downloads.

### Tests
- Added test coverage for the retry schedule, for retrying truncated and corrupted transfers, for the cases that must not be retried, and for per-file retry inside folder downloads.

## 2026-10-06

### Fixed
- Fixed a bug in the test-suite launch script: loading the system shell profile while unset-variable checking was enabled could abort the run when loading software modules. The check is now relaxed only for that step.

## 2026-09-28

### Fixed
- **Removed a broken `/about` route (#8).** The route pointed at a page template that no longer exists, so visiting it returned a server error. It was not linked from anywhere in the application.
- **Downloaded filenames with special characters are now correct (#8).** Single-file downloads derived the saved filename from the encoded form of the path, so a file such as `Measurement+1.xml` was saved as `Measurement%2B1.xml`. The file contents were fine but the name was wrong. The destination filename is now computed explicitly from the original path, the same way folder downloads already did.
- **Large directory listings are no longer silently cut off (#8).** The data source returns at most 1,000 entries for a single directory listing and gives no indication that the list is incomplete, so a directory with 1,919 files appeared to hold exactly 1,000. The listing endpoint now sets an `X-Listing-Truncated` response header whenever a listing reaches that limit, and the dataset browser and quick-access page show a notice that only the first 1,000 items are displayed. The response body is unchanged, so existing callers are unaffected. Because the data source gives no other signal, a directory that genuinely holds exactly 1,000 items is also flagged.
- **The "reset connection" step used when retrying a failed download did nothing (#8).** The shared filesystem object was being recreated, but library-level caching handed back the same instance, with the same underlying network session, every time. Retries after a connection failure therefore kept reusing the stale session. The reset now bypasses the instance cache and clears the cached HTTP filesystem beneath it, so each retry gets a genuinely new connection. A reset in progress does not disturb requests that are already running on the old connection.
- **The test launch script now loads the `pelican` command-line tool.** The tests that compare this application against the command-line tool were silently skipped on every real run because the tool was never available on the search path. The script now loads it automatically, and still skips those tests with a clear reason if it cannot be found.

### Changed
- Moved the README from `tests/` to the repository root and added a test reference describing what each group of tests does and what a failure in it means.
- Adjusted the concurrency test so the number of overlapping directory listings it requires scales with the test depth. A fixed threshold failed under normal load from other jobs sharing the same system.

### Tests
- Added regression tests for the removed `/about` route, the truncated-listing notice, and the connection-reset behavior, including a test that documents why the two-part reset fix is needed.

## 2026-09-25

### Added
- **End-to-end test suite (#8).** A new suite starts its own instance of the application and exercises it against the real data federation. It is organized into groups for application startup, database integrity, downloads, concurrent downloads and listings, restarts, forced termination of the server mid-download, direct federation access without the application (to tell federation problems apart from application problems), long-running downloads, parity with the `pelican` command-line tool, and whole-run invariants and isolation checks. Tests run at three depths (quick, standard and deep), use a pinned fixture file, and produce a written run report. A batch-job script is provided to run the suite on a cluster.
- Test output (run logs and result files) is now excluded from version control.

## 2026-09-15

### Added
- **Version indicator on the admin page.** A small, unobtrusive label in the corner of the admin page shows the short commit hash and commit time of the code the running application actually loaded, with a `-dirty` suffix if there were uncommitted changes, or `unknown` if version information is unavailable. It is read once at startup, so it reflects the code in memory rather than whatever is currently on disk.

### Changed
- The file-format badge on dataset cards now has a tooltip explaining that the label is entered manually and shows the predominant file type, so a few incidental files (for example an auto-generated index page) may not match it (#5).

## 2026-09-10

### Added
- **Automatic retry for failed downloads (part of #6).** Downloads now retry on connection-class failures (dropped or stale connections, brief DNS or handshake problems) instead of failing the file after a single attempt and requiring the user to notice and click Restart. Up to two retries are made, after 2 and 5 seconds, and the connection is reset before each retry. Folder downloads retry the individual file that failed rather than the whole folder, and the check for whether a path is a file or a folder is retried too. Authentication, not-found, permission and unrecognized errors are reported immediately without retrying.
- **Recovery of downloads interrupted by an application restart (part of #6).** If the application stops mid-download, unfinished downloads are now detected and resumed the next time it starts, instead of staying "in progress" forever. Files that already succeeded are kept and only the remaining files are downloaded again. The job keeps its original ID, so a browser tab still watching it keeps working, and its original start time is preserved. A download whose files had all finished before the interruption is marked complete. Recovery claims each job atomically so that two application processes starting at the same time cannot both resume the same download, and a failure during recovery never prevents the application from starting.

## 2026-08-31

### Fixed
- **The background indexing worker could silently stop renewing itself.** The worker runs inside a container that has no job-scheduler client tools, but it tried to resubmit itself by calling the scheduler directly. That failed the first time a run reached its time limit, and the self-renewing chain of indexing runs quietly stopped, leaving queued datasets waiting. The worker now exits with a dedicated exit code to request the next run, and the launch script on the host, where the scheduler is available, performs the resubmission.
- **Indexing large datasets could fail with "database is locked".** The worker used the database library's default 5-second wait for write locks, which is too short when several processes write to the same database and a large dataset's final write legitimately holds the lock for a while. The wait is now 30 seconds.

### Changed
- The indexing worker's cycle length was raised from 24 to 48 hours, still well below the scheduler's maximum run time, to halve how often it has to resubmit itself while leaving room for an in-progress dataset to finish and hand off cleanly.
- The indexing worker's default request for parallel tasks was reduced from 64 to 32.
