"""GitHub issue #6, Part 1: unit-level coverage for download_one_file's
connection-class retry loop (api/routes/pelican.py's _with_connection_retry).

No existing tests/ directory existed before this file — added specifically
to guard against the exact misclassification bug the indexing worker's own
retry loop once had (an unclassified exception wrongly routed into a retry
meant only for connection-class failures). Run with:

    python -m unittest tests/test_download_retry.py -v

Mocks _resolve_filesystem and time.sleep so this runs instantly and offline
— it exercises the retry/backoff/reset control flow itself, not a real
Pelican/OSDF connection.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import aiohttp
from aiohttp import http_exceptions

from api.routes import pelican as pelican_module
from api.routes.pelican import DOWNLOAD_RETRY_BACKOFFS, DownloadError, download_one_file


def _connection_error():
    # Same exception type _is_connection_error/classify_failure classify as
    # "connection" — see api/core/failure_classification.py.
    return aiohttp.ClientConnectionError("simulated dead pooled connection")


class WithConnectionRetryTests(unittest.TestCase):
    def setUp(self):
        # Never actually sleep in tests, and count resets without touching
        # the real module-level `osdf` singleton.
        self.sleep_patcher = patch.object(time, "sleep")
        self.mock_sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

        self.reset_patcher = patch.object(pelican_module, "reset_default_filesystem")
        self.mock_reset = self.reset_patcher.start()
        self.addCleanup(self.reset_patcher.stop)

    def test_connection_class_failure_retries_then_succeeds(self):
        # Fails with a connection-class exception on the first two attempts,
        # succeeds on the third (the last attempt this schedule allows).
        fn = MagicMock(side_effect=[_connection_error(), _connection_error(), "ok"])

        result = pelican_module._with_connection_retry("test call", fn)

        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 3)
        self.assertEqual(self.mock_reset.call_count, 2)
        self.mock_sleep.assert_any_call(DOWNLOAD_RETRY_BACKOFFS[0])
        self.mock_sleep.assert_any_call(DOWNLOAD_RETRY_BACKOFFS[1])

    def test_connection_class_failure_exhausts_retries_and_raises(self):
        # Fails with a connection-class exception on every attempt —
        # confirms it gives up after exactly len(DOWNLOAD_RETRY_BACKOFFS) + 1
        # tries rather than retrying forever.
        fn = MagicMock(side_effect=[_connection_error() for _ in range(10)])

        with self.assertRaises(aiohttp.ClientConnectionError):
            pelican_module._with_connection_retry("test call", fn)

        self.assertEqual(fn.call_count, len(DOWNLOAD_RETRY_BACKOFFS) + 1)

    def test_data_class_failure_is_not_retried(self):
        # The misclassification guard: a data-class exception (here,
        # not_found) must propagate on the very first attempt, unretried —
        # retrying a genuine 404 wouldn't fix it, just delay reporting it.
        fn = MagicMock(side_effect=FileNotFoundError("genuinely does not exist"))

        with self.assertRaises(FileNotFoundError):
            pelican_module._with_connection_retry("test call", fn)

        self.assertEqual(fn.call_count, 1)
        self.mock_reset.assert_not_called()
        self.mock_sleep.assert_not_called()


def _http_error(status):
    info = MagicMock()
    info.real_url = "https://origin.example/file"
    return aiohttp.ClientResponseError(info, (), status=status, message="simulated")


def _truncation():
    return http_exceptions.ContentLengthError("Not enough data for satisfy content length header.")


class TransientOriginRetryTests(unittest.TestCase):
    """2026-10-06 deep run (job 21141988): origin 500s and a mid-stream
    ContentLengthError were classified "unknown" and failed with no retry."""

    def setUp(self):
        for target in (patch.object(time, "sleep"), patch.object(pelican_module, "reset_default_filesystem")):
            m = target.start()
            self.addCleanup(target.stop)
            if target.attribute == "sleep":
                self.mock_sleep = m
            else:
                self.mock_reset = m

    def test_origin_5xx_retries_with_the_longer_backoff_then_succeeds(self):
        for status in (500, 502, 503, 504):
            self.mock_sleep.reset_mock()
            fn = MagicMock(side_effect=[_http_error(status), _http_error(status), "ok"])
            self.assertEqual(pelican_module._with_connection_retry("t", fn), "ok")
            self.assertEqual(fn.call_count, 3, status)
            self.assertEqual([c.args[0] for c in self.mock_sleep.call_args_list],
                             pelican_module.TRANSIENT_ORIGIN_RETRY_BACKOFFS[:2], status)

    def test_persistent_500_gives_up_after_the_full_schedule(self):
        fn = MagicMock(side_effect=[_http_error(500) for _ in range(20)])
        with self.assertRaises(aiohttp.ClientResponseError):
            pelican_module._with_connection_retry("t", fn)
        self.assertEqual(fn.call_count, len(pelican_module.TRANSIENT_ORIGIN_RETRY_BACKOFFS) + 1)
        self.assertEqual([c.args[0] for c in self.mock_sleep.call_args_list],
                         pelican_module.TRANSIENT_ORIGIN_RETRY_BACKOFFS)

    def test_payload_errors_retry(self):
        for make in (_truncation, lambda: http_exceptions.PayloadEncodingError("bad"),
                     lambda: aiohttp.ClientPayloadError("closed")):
            fn = MagicMock(side_effect=[make(), "ok"])
            self.assertEqual(pelican_module._with_connection_retry("t", fn), "ok")
            self.assertEqual(fn.call_count, 2)

    def test_connection_class_backoff_is_unchanged(self):
        fn = MagicMock(side_effect=[_connection_error(), _connection_error(), _connection_error()])
        with self.assertRaises(aiohttp.ClientConnectionError):
            pelican_module._with_connection_retry("t", fn)
        self.assertEqual(fn.call_count, len(DOWNLOAD_RETRY_BACKOFFS) + 1)
        self.assertEqual([c.args[0] for c in self.mock_sleep.call_args_list], DOWNLOAD_RETRY_BACKOFFS)

    def test_4xx_and_other_unknown_failures_are_not_retried(self):
        for exc in (_http_error(400), _http_error(401), _http_error(403), _http_error(404),
                    _http_error(429), _http_error(501), ValueError("boom"), RuntimeError("boom")):
            self.mock_sleep.reset_mock()
            self.mock_reset.reset_mock()
            fn = MagicMock(side_effect=exc)
            with self.assertRaises(type(exc)):
                pelican_module._with_connection_retry("t", fn)
            self.assertEqual(fn.call_count, 1, repr(exc))
            self.mock_sleep.assert_not_called()
            self.mock_reset.assert_not_called()

    def test_truncated_retry_rewrites_the_file_from_scratch(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "f.nc")
            full = b"complete-content"
            seen_at_start = []

            def get_file(_remote, local):
                seen_at_start.append(os.path.exists(local))
                with open(local, "ab") as fh:  # append: would corrupt if the partial survived
                    if len(seen_at_start) == 1:
                        fh.write(b"trunc")
                        raise _truncation()
                    fh.write(full)

            fake_fs = MagicMock()
            fake_fs.get_file.side_effect = get_file
            with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs):
                pelican_module._with_connection_retry(
                    "t", lambda: pelican_module._resolve_filesystem("/ns").get_file("r", target), partial_path=target)
            self.assertEqual(seen_at_start, [False, False])
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), full)

    def test_exhausted_truncation_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "f.nc")

            def get_file(_remote, local):
                with open(local, "wb") as fh:
                    fh.write(b"trunc")
                raise _truncation()

            with self.assertRaises(http_exceptions.ContentLengthError):
                pelican_module._with_connection_retry("t", lambda: get_file("r", target), partial_path=target)
            self.assertFalse(os.path.exists(target))

    def test_single_file_download_retries_a_500_and_a_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            fake_fs = MagicMock()
            fake_fs.isdir.return_value = False
            fake_fs.get_file.side_effect = [_http_error(500), _truncation(), None]
            with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs):
                download_one_file("/ns/a/file.nc", d)
            self.assertEqual(fake_fs.get_file.call_count, 3)

    def test_single_file_download_does_not_retry_a_403_or_404(self):
        for status in (403, 404):
            fake_fs = MagicMock()
            fake_fs.isdir.return_value = False
            fake_fs.get_file.side_effect = _http_error(status)
            with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs):
                with self.assertRaises(DownloadError):
                    download_one_file("/ns/a/file.nc", "/tmp/dest")
            self.assertEqual(fake_fs.get_file.call_count, 1, status)

    def test_directory_download_retries_only_the_failing_file(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def get_file(remote, local):
                calls.append(os.path.basename(local))
                if calls.count("b.nc") == 1 and os.path.basename(local) == "b.nc":
                    raise _http_error(500)

            fake_fs = MagicMock()
            fake_fs.isdir.return_value = True
            fake_fs.get_file.side_effect = get_file
            with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs), \
                 patch.object(pelican_module, "_walk_remote_files", return_value=["/ns/dir/a.nc", "/ns/dir/b.nc", "/ns/dir/c.nc"]):
                download_one_file("/ns/dir", d)
            self.assertEqual(calls, ["a.nc", "b.nc", "b.nc", "c.nc"])


class DownloadOneFileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sleep_patcher = patch.object(time, "sleep")
        self.addCleanup(self.sleep_patcher.stop)
        self.sleep_patcher.start()

        self.reset_patcher = patch.object(pelican_module, "reset_default_filesystem")
        self.addCleanup(self.reset_patcher.stop)
        self.reset_patcher.start()

    def test_download_one_file_recovers_from_transient_connection_blip(self):
        # isdir() fails once with a connection-class error, then succeeds;
        # the eventual fs.get() succeeds outright. Whole call should
        # transparently succeed with no DownloadError raised.
        fake_fs = MagicMock()
        fake_fs.isdir.side_effect = [_connection_error(), False]
        fake_fs.get_file.return_value = None

        with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs) as mock_resolve:
            download_one_file("/namespace/some/file.txt", "/tmp/dest")

        self.assertEqual(fake_fs.isdir.call_count, 2)
        fake_fs.get_file.assert_called_once()
        self.assertGreaterEqual(mock_resolve.call_count, 2)

    def test_download_one_file_does_not_retry_a_real_404(self):
        # isdir() itself succeeds (returns False -> not a directory), then
        # fs.get() raises FileNotFoundError — a genuine not_found, not a
        # connection blip. Must fail immediately as DownloadError(code=
        # "not_found"), with fs.get() attempted exactly once.
        fake_fs = MagicMock()
        fake_fs.isdir.return_value = False
        fake_fs.get_file.side_effect = FileNotFoundError("no such path on the federation")

        with patch.object(pelican_module, "_resolve_filesystem", return_value=fake_fs):
            with self.assertRaises(DownloadError) as ctx:
                download_one_file("/namespace/missing/file.txt", "/tmp/dest")

        self.assertEqual(ctx.exception.category, "not_found")
        self.assertEqual(fake_fs.get_file.call_count, 1)


if __name__ == "__main__":
    unittest.main()
