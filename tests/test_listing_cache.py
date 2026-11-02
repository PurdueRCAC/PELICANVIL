"""Unit tests for pelicanlistPath's app-level listing cache and timeout
safety net (api/routes/pelican.py).

Mocks _resolve_filesystem so no real federation access is needed.
Run with:

    python -m unittest tests/test_listing_cache.py -v
"""
import threading
import time
import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

from starlette.responses import Response

from api.routes import pelican as pelican_module
from api.routes.pelican import (
    LISTING_CACHE_TTL_SECONDS,
    LISTING_TIMEOUT_SECONDS,
    LISTING_TRUNCATION_SUSPECT_COUNT,
    _listing_cache,
    _listing_cache_lock,
    pelicanlistPath,
)


def _fake_entries(n=5):
    return [{"name": f"/ns/dir/file{i}", "size": 100 * i, "type": "file"} for i in range(n)]


class ListingCacheTests(unittest.TestCase):
    """Cache-hit / cache-miss behaviour of pelicanlistPath."""

    def setUp(self):
        self.mock_fs = MagicMock()
        self.resolve_patcher = patch.object(
            pelican_module, "_resolve_filesystem", return_value=self.mock_fs
        )
        self.resolve_patcher.start()
        self.addCleanup(self.resolve_patcher.stop)

        self.attach_patcher = patch.object(
            pelican_module, "_attach_folder_sizes", side_effect=lambda e: e
        )
        self.attach_patcher.start()
        self.addCleanup(self.attach_patcher.stop)

        with _listing_cache_lock:
            _listing_cache.clear()

    def test_first_call_hits_federation(self):
        entries = _fake_entries()
        self.mock_fs.ls.return_value = entries
        resp = Response()

        result = pelicanlistPath("/ns/dir", resp)

        self.mock_fs.ls.assert_called_once()
        self.assertEqual(result, entries)

    def test_second_call_within_ttl_uses_cache(self):
        entries = _fake_entries()
        self.mock_fs.ls.return_value = entries
        resp = Response()

        pelicanlistPath("/ns/dir", resp)
        self.mock_fs.ls.reset_mock()

        result = pelicanlistPath("/ns/dir", resp)

        self.mock_fs.ls.assert_not_called()
        self.assertEqual(result, entries)

    def test_different_paths_are_cached_independently(self):
        entries_a = _fake_entries(3)
        entries_b = _fake_entries(7)
        self.mock_fs.ls.side_effect = [entries_a, entries_b]
        resp = Response()

        result_a = pelicanlistPath("/ns/a", resp)
        result_b = pelicanlistPath("/ns/b", resp)

        self.assertEqual(self.mock_fs.ls.call_count, 2)
        self.assertEqual(result_a, entries_a)
        self.assertEqual(result_b, entries_b)

    def test_truncation_header_set_when_count_hits_cap(self):
        entries = _fake_entries(LISTING_TRUNCATION_SUSPECT_COUNT)
        self.mock_fs.ls.return_value = entries
        resp = Response()

        pelicanlistPath("/ns/big", resp)

        self.assertEqual(resp.headers.get("x-listing-truncated"), "1")

    def test_truncation_header_preserved_on_cache_hit(self):
        entries = _fake_entries(LISTING_TRUNCATION_SUSPECT_COUNT)
        self.mock_fs.ls.return_value = entries

        resp1 = Response()
        pelicanlistPath("/ns/big", resp1)

        resp2 = Response()
        pelicanlistPath("/ns/big", resp2)

        self.assertEqual(resp2.headers.get("x-listing-truncated"), "1")
        self.assertEqual(self.mock_fs.ls.call_count, 1)


class ListingTimeoutTests(unittest.TestCase):
    """_listing_from_federation's timeout safety net."""

    def setUp(self):
        self.mock_fs = MagicMock()
        self.resolve_patcher = patch.object(
            pelican_module, "_resolve_filesystem", return_value=self.mock_fs
        )
        self.resolve_patcher.start()
        self.addCleanup(self.resolve_patcher.stop)

        self.attach_patcher = patch.object(
            pelican_module, "_attach_folder_sizes", side_effect=lambda e: e
        )
        self.attach_patcher.start()
        self.addCleanup(self.attach_patcher.stop)

        with _listing_cache_lock:
            _listing_cache.clear()

    def test_timeout_returns_504(self):
        def slow_ls(*args, **kwargs):
            time.sleep(LISTING_TIMEOUT_SECONDS + 5)
            return []

        self.mock_fs.ls.side_effect = slow_ls

        with patch.object(pelican_module, "LISTING_TIMEOUT_SECONDS", 0.1):
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                pelicanlistPath("/ns/slow", Response())
            self.assertEqual(ctx.exception.status_code, 504)

    def test_file_not_found_returns_404(self):
        self.mock_fs.ls.side_effect = FileNotFoundError("gone")

        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            pelicanlistPath("/ns/missing", Response())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
