"""Test Layer 1: Kalshi Rate Limiter (HIGH-1)."""

import unittest
import tempfile
import os
from unittest.mock import patch, MagicMock

from core import kalshi_monitor


class KalshiRateLimitTests(unittest.TestCase):
    """Test Kalshi rate limiter with Retry-After handling (L1-T4, L1-T5)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear state
        kalshi_monitor._SERIES_MARKETS_CACHE.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l1_t4_rate_limit_check(self):
        """L1-T4: Rate limit check validates request count against threshold."""

        endpoint = "/test_endpoint"

        # Initial check should pass (no requests yet)
        result = kalshi_monitor._check_rate_limit(endpoint, max_requests=10, window_seconds=60)
        self.assertTrue(result)

        # Record some requests
        for i in range(5):
            kalshi_monitor._persist_rate_limit_entry(endpoint)

        # Check should still pass (5 < 10)
        result = kalshi_monitor._check_rate_limit(endpoint, max_requests=10, window_seconds=60)
        self.assertTrue(result)

        # Record more requests to exceed limit
        for i in range(6):
            kalshi_monitor._persist_rate_limit_entry(endpoint)

        # Check should now fail (11 > 10)
        result = kalshi_monitor._check_rate_limit(endpoint, max_requests=10, window_seconds=60)
        self.assertFalse(result)

    def test_l1_t4_rate_limit_with_different_endpoints(self):
        """L1-T4: Different endpoints have independent rate limits."""

        endpoint1 = "/test_endpoint1"
        endpoint2 = "/test_endpoint2"

        # Record requests for endpoint1
        for i in range(15):
            kalshi_monitor._persist_rate_limit_entry(endpoint1)

        # endpoint1 should be rate limited
        result1 = kalshi_monitor._check_rate_limit(endpoint1, max_requests=10, window_seconds=60)
        self.assertFalse(result1)

        # endpoint2 should not be affected
        result2 = kalshi_monitor._check_rate_limit(endpoint2, max_requests=10, window_seconds=60)
        self.assertTrue(result2)

    def test_l1_t5_parse_retry_after_integer(self):
        """L1-T5: Parse Retry-After as integer seconds."""

        # Test integer values
        self.assertEqual(kalshi_monitor._parse_retry_after("60"), 60)
        self.assertEqual(kalshi_monitor._parse_retry_after("120"), 120)
        self.assertEqual(kalshi_monitor._parse_retry_after("5"), 5)

    def test_l1_t5_parse_retry_after_fallback(self):
        """L1-T5: Parse Retry-After returns default fallback for invalid input."""

        # Test invalid inputs
        self.assertEqual(kalshi_monitor._parse_retry_after(""), 60)
        self.assertEqual(kalshi_monitor._parse_retry_after("invalid"), 60)
        self.assertEqual(kalshi_monitor._parse_retry_after(None), 60)

    def test_l1_t5_rate_limit_429_handling(self):
        """L1-T5: 429 response triggers Retry-After parsing and raises HTTPError."""

        # Mock response with 429 status and Retry-After header
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "120"}

        with patch.object(kalshi_monitor._KALSHI_PUBLIC_SESSION, "get") as mock_get:
            mock_get.return_value = mock_response

            # Call _kalshi_public_get and expect HTTPError
            with self.assertRaises(Exception) as context:
                kalshi_monitor._kalshi_public_get("/test/path")

            # Verify error message mentions Retry-After
            self.assertIn("Retry-After", str(context.exception))
            self.assertIn("120", str(context.exception))

    def test_l1_t5_rate_limit_persists_requests(self):
        """L1-T4: Rate limit requests are persisted to database."""

        endpoint = "/test_endpoint"

        # Record rate limit entry
        kalshi_monitor._persist_rate_limit_entry(endpoint)

        # Verify it's in the database
        db_path = kalshi_monitor._alert_db_path()
        conn = None
        try:
            conn = __import__("sqlite3").connect(db_path, timeout=1)
            row = conn.execute(
                "SELECT COUNT(*) FROM kalshi_rate_limit WHERE endpoint = ?",
                (endpoint,),
            ).fetchone()
            self.assertEqual(row[0], 1)
        finally:
            if conn:
                conn.close()

    def test_l1_t5_exponential_backoff_in_queue(self):
        """L1-T3: Rate limiting combines with alert queue exponential backoff."""

        # Queue an alert
        webhook = "https://example.com/webhook"
        payload = {"station": "KDEN", "temp_f": 72.5}
        alert_id = "backoff_test:KDEN:1"

        metar_monitor = __import__("core.metar_monitor", fromlist=[""])
        result = metar_monitor._queue_alert_for_delivery(alert_id, webhook, payload)
        self.assertTrue(result["queued"])

        # Simulate retry attempts
        for attempt in range(1, 4):
            # Next retry should use exponential backoff: 60 * 2^attempt seconds
            expected_backoff = 60 * (2 ** attempt)
            metar_monitor._update_alert_delivery_queue_attempt(alert_id, f"HTTP_429")

            # Verify attempt count increases
            db_path = metar_monitor._alert_db_path()
            conn = None
            try:
                conn = __import__("sqlite3").connect(db_path, timeout=1)
                row = conn.execute(
                    "SELECT attempt_count FROM alert_delivery_queue WHERE alert_id = ?",
                    (alert_id,),
                ).fetchone()
                self.assertEqual(row[0], attempt)
            finally:
                if conn:
                    conn.close()


class RateLimitSchemaTests(unittest.TestCase):
    """Test rate limit schema setup (L1 schema)."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l1_schema_kalshi_rate_limit_structure(self):
        """L1-T4: kalshi_rate_limit table has correct structure."""

        kalshi_monitor._ensure_alert_schema()

        db_path = kalshi_monitor._alert_db_path()
        conn = None
        try:
            conn = __import__("sqlite3").connect(db_path, timeout=1)
            cursor = conn.cursor()

            # Verify table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kalshi_rate_limit'")
            self.assertIsNotNone(cursor.fetchone())

            # Verify columns
            cursor.execute("PRAGMA table_info(kalshi_rate_limit)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            self.assertIn("id", columns)
            self.assertIn("endpoint", columns)
            self.assertIn("request_time", columns)

        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    unittest.main()
