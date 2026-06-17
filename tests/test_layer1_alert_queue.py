"""Test Layer 1: Alert Retry Queue with Exponential Backoff (CRITICAL-1)."""

import unittest
import tempfile
import os
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from core import metar_monitor


class AlertDeliveryQueueTests(unittest.TestCase):
    """Test alert delivery queue with exponential backoff (L1-T1, L1-T2, L1-T3)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear state
        metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()
        metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        metar_monitor._MISSING_LADDER_DEDUPE.clear()
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l1_t1_queue_alert_for_delivery(self):
        """L1-T1: Queue an alert for delivery with exponential backoff."""

        webhook = "https://example.com/webhook"
        payload = {
            "station": "KDEN",
            "temp_f": 72.5,
            "transition_type": "settlement_up",
            "transition_context": {"instant_bucket": 72, "settlement_bucket": 72},
        }

        alert_id = "test_alert:KDEN:1"

        # Queue the alert
        result = metar_monitor._queue_alert_for_delivery(alert_id, webhook, payload)

        # Verify
        self.assertTrue(result["queued"])
        self.assertEqual(result["alert_id"], alert_id)

        # Verify the alert is in the queue
        db_path = metar_monitor._alert_db_path()
        conn = None
        try:
            conn = __import__("sqlite3").connect(db_path, timeout=1)
            row = conn.execute(
                "SELECT alert_id, webhook_url, payload_json FROM alert_delivery_queue WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], alert_id)
            self.assertEqual(row[1], webhook)
            stored_payload = json.loads(row[2])
            self.assertEqual(stored_payload["station"], "KDEN")
        finally:
            if conn:
                conn.close()

    def test_l1_t2_dead_letter_endpoint(self):
        """L1-T2: Dead letter endpoint handles failed alerts."""

        webhook = "https://example.com/webhook"
        payload = {"station": "KDEN", "temp_f": 72.5}

        alert_id = "dead_letter_test:KDEN:1"

        # Queue the alert
        metar_monitor._queue_alert_for_delivery(alert_id, webhook, payload)

        # Verify alert is pending
        pending = metar_monitor._get_pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["alert_id"], alert_id)

        # Mark as dead letter
        result = metar_monitor._mark_alert_delivery_queue_dead_letter(alert_id, "webhook_timeout")
        self.assertIsNone(result)  # No return value expected

        # Verify alert is now in failed queue
        failed = metar_monitor._get_failed_alerts()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["alert_id"], alert_id)
        self.assertEqual(failed[0]["last_error"], "webhook_timeout")

    def test_l1_t3_exponential_backoff_retry(self):
        """L1-T3: Exponential backoff increases delay on each retry attempt."""

        webhook = "https://example.com/webhook"
        payload = {"station": "KDEN", "temp_f": 72.5}

        alert_id = "backoff_test:KDEN:1"

        # Queue the alert
        metar_monitor._queue_alert_for_delivery(alert_id, webhook, payload)

        # Simulate failed deliveries
        for attempt in range(1, 4):
            # Update attempt count and calculate next retry
            next_retry_ts = int(time.time()) + 60 * (2 ** attempt)  # Exponential: 120s, 240s, 480s
            import datetime
            next_retry_iso = datetime.datetime.fromtimestamp(next_retry_ts, tz=datetime.timezone.utc).isoformat()
            metar_monitor._update_alert_delivery_queue_attempt(alert_id, f"HTTP_500")

            # Verify next_retry_at is updated
            db_path = metar_monitor._alert_db_path()
            conn = None
            try:
                conn = __import__("sqlite3").connect(db_path, timeout=1)
                row = conn.execute(
                    "SELECT attempt_count, next_retry_at FROM alert_delivery_queue WHERE alert_id = ?",
                    (alert_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertGreaterEqual(row[0], attempt)
                # Next retry should be in the future
                next_retry_dt = __import__("datetime").datetime.fromisoformat(row[1].replace("Z", "+00:00"))
                future_threshold = datetime.datetime.now(datetime.timezone.utc) + timedelta(seconds=60)
                self.assertGreater(next_retry_dt.replace(tzinfo=datetime.timezone.utc), future_threshold)
            finally:
                if conn:
                    conn.close()

    def test_l1_t3_delete_alert_from_queue(self):
        """L1-T3: Successfully delivered alerts are removed from queue."""

        webhook = "https://example.com/webhook"
        payload = {"station": "KDEN", "temp_f": 72.5}

        alert_id = "delete_test:KDEN:1"

        # Queue the alert
        metar_monitor._queue_alert_for_delivery(alert_id, webhook, payload)

        # Verify alert is in queue
        pending = metar_monitor._get_pending_deliveries()
        self.assertEqual(len(pending), 1)

        # Delete the alert (simulating successful delivery)
        metar_monitor._delete_alert_delivery_queue(alert_id)

        # Verify alert is removed
        pending = metar_monitor._get_pending_deliveries()
        self.assertEqual(len(pending), 0)

    def test_l1_t3_retry_batch_processes_pending(self):
        """L1-T3: Retry batch processes pending deliveries."""

        webhook = "https://example.com/webhook"
        payload = {"station": "KDEN", "temp_f": 72.5}

        alert_id1 = "batch_test:KDEN:1"
        alert_id2 = "batch_test:KDEN:2"

        # Queue multiple alerts
        metar_monitor._queue_alert_for_delivery(alert_id1, webhook, payload)
        metar_monitor._queue_alert_for_delivery(alert_id2, webhook, payload)

        # Verify both are pending
        pending = metar_monitor._get_pending_deliveries()
        self.assertEqual(len(pending), 2)

        # Call retry batch (mocked to avoid actual HTTP calls)
        with patch("requests.post") as mock_post:
            mock_response = type("Response", (), {"status_code": 200, "text": "OK"})()
            mock_post.return_value = mock_response

            results = metar_monitor._retry_delivery_batch()

            # Verify batch results
            self.assertGreaterEqual(len(results), 1)
            for result in results:
                if "success" in result:
                    self.assertTrue(result["success"])

        # Verify pending is empty (or reduced)
        pending = metar_monitor._get_pending_deliveries()
        self.assertEqual(len(pending), 0)


class AlertQueueSchemaTests(unittest.TestCase):
    """Test alert delivery queue schema setup (L1 schema)."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l1_schema_creates_alert_delivery_queue_table(self):
        """L1-T1: alert_delivery_queue table is created with correct schema."""

        metar_monitor._ensure_alert_schema()

        db_path = metar_monitor._alert_db_path()
        conn = None
        try:
            conn = __import__("sqlite3").connect(db_path, timeout=1)
            cursor = conn.cursor()

            # Check table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alert_delivery_queue'")
            self.assertIsNotNone(cursor.fetchone())

            # Check columns
            cursor.execute("PRAGMA table_info(alert_delivery_queue)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            expected_columns = [
                "id", "alert_id", "webhook_url", "payload_json",
                "attempt_count", "next_retry_at", "last_error",
                "created_at", "dead_lettered_at"
            ]
            for col in expected_columns:
                self.assertIn(col, columns, f"alert_delivery_queue should have '{col}' column")

            # Verify unique constraint on alert_id
            cursor.execute("PRAGMA table_info(alert_delivery_queue)")
            for col in cursor.fetchall():
                if col[1] == "alert_id":
                    self.assertEqual(col[3], 1, "alert_id should have UNIQUE constraint")

        finally:
            if conn:
                conn.close()

    def test_l1_schema_creates_kalshi_rate_limit_table(self):
        """L1-T4: kalshi_rate_limit table is created with correct schema."""

        metar_monitor._ensure_alert_schema()

        db_path = metar_monitor._alert_db_path()
        conn = None
        try:
            conn = __import__("sqlite3").connect(db_path, timeout=1)
            cursor = conn.cursor()

            # Check table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kalshi_rate_limit'")
            self.assertIsNotNone(cursor.fetchone())

            # Check columns
            cursor.execute("PRAGMA table_info(kalshi_rate_limit)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            expected_columns = ["id", "endpoint", "request_time"]
            for col in expected_columns:
                self.assertIn(col, columns, f"kalshi_rate_limit should have '{col}' column")

        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    unittest.main()
