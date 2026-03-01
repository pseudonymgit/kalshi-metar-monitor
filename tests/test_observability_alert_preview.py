import json
import os
import sqlite3
import tempfile
import unittest

import app as app_module

app = app_module.app


class ObservabilityAlertPreviewTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_alert_preview_returns_compact_recent_alert_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        market_type TEXT,
                        event_ticker TEXT,
                        alert_type TEXT,
                        direction TEXT,
                        temp_f REAL,
                        bucket_index INTEGER,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        id,
                        created_utc,
                        station,
                        market_type,
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "2026-01-01T10:00:00Z",
                        "KDEN",
                        "HIGH",
                        "KXHIGH-TICKER",
                        "ladder_cross",
                        "up",
                        70.2,
                        70,
                        json.dumps({"reason": "crossed_up", "source": "deterministic", "attention_phrase": "OPEN EPOCH / ALERTABLE", "alert_context": {"attention_phrase": "OPEN EPOCH / ALERTABLE", "station_local_timestamp": "2026-01-01T03:00:00-07:00", "previous_relevant_bucket": 69, "current_relevant_bucket": 70, "settlement_bucket": 70, "prior_settlement_bucket": 69, "settlement_jump_magnitude": 1, "epoch_status": "open", "reversion_occurred": False, "first_reversion_timestamp_utc": None, "max_excursion_above_settlement": 0.2, "terminal_state_reached": False}}),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        id,
                        created_utc,
                        station,
                        market_type,
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        "2026-01-01T10:05:00Z",
                        "KLAX",
                        "LOW",
                        "KXLOW-TICKER",
                        "ladder_missing",
                        None,
                        58.0,
                        58,
                        json.dumps({"suppression_reason": "market_missing"}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            response = self.client.get("/observability/alert-preview?station=KDEN&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["count"], 1)

        self.assertEqual(
            payload["schema_fields"],
            [
                "station",
                "created_utc",
                "alert_type",
                "market_type",
                "direction",
                "event_ticker",
                "bucket_index",
                "metadata",
                "alert_context",
                "attention_phrase",
                "station_local_timestamp",
                "previous_relevant_bucket",
                "current_relevant_bucket",
                "settlement_bucket",
                "prior_settlement_bucket",
                "settlement_jump_magnitude",
                "epoch_status",
                "reversion_occurred",
                "first_reversion_timestamp_utc",
                "max_excursion_above_settlement",
                "terminal_state_reached",
            ],
        )

        row = payload["rows"][0]
        self.assertEqual(row["station"], "KDEN")
        self.assertEqual(row["created_utc"], "2026-01-01T10:00:00Z")
        self.assertEqual(row["alert_type"], "ladder_cross")
        self.assertEqual(row["market_type"], "HIGH")
        self.assertEqual(row["direction"], "up")
        self.assertEqual(row["event_ticker"], "KXHIGH-TICKER")
        self.assertEqual(row["bucket_index"], 70)
        self.assertEqual(row["reason"], "crossed_up")
        self.assertEqual(row["attention_phrase"], "OPEN EPOCH / ALERTABLE")
        self.assertEqual(row["station_local_timestamp"], "2026-01-01T03:00:00-07:00")
        self.assertEqual(row["previous_relevant_bucket"], 69)
        self.assertEqual(row["current_relevant_bucket"], 70)
        self.assertEqual(row["settlement_bucket"], 70)
        self.assertEqual(row["prior_settlement_bucket"], 69)
        self.assertEqual(row["settlement_jump_magnitude"], 1)
        self.assertEqual(row["epoch_status"], "open")
        self.assertFalse(row["reversion_occurred"])
        self.assertIsNone(row["first_reversion_timestamp_utc"])
        self.assertEqual(row["max_excursion_above_settlement"], 0.2)
        self.assertFalse(row["terminal_state_reached"])
        self.assertTrue(row["is_recent_alert_example"])
        self.assertEqual(row["payload_preview"]["temp_f"], 70.2)
        self.assertEqual(row["payload_preview"]["attention_phrase"], "OPEN EPOCH / ALERTABLE")



    def test_alert_preview_station_filter_applies_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        market_type TEXT,
                        event_ticker TEXT,
                        alert_type TEXT,
                        direction TEXT,
                        temp_f REAL,
                        bucket_index INTEGER,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        id, created_utc, station, market_type, event_ticker,
                        alert_type, direction, temp_f, bucket_index, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "2026-01-01T10:00:00Z",
                        "KDEN",
                        "HIGH",
                        "KDEN-OLDER",
                        "ladder_cross",
                        "up",
                        70.0,
                        70,
                        json.dumps({"reason": "older_station_alert"}),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        id, created_utc, station, market_type, event_ticker,
                        alert_type, direction, temp_f, bucket_index, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        "2026-01-01T10:01:00Z",
                        "KLAX",
                        "HIGH",
                        "KLAX-NEWEST",
                        "ladder_cross",
                        "up",
                        60.0,
                        60,
                        json.dumps({"reason": "newest_global"}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            station_response = self.client.get("/observability/alert-preview?station=KDEN&limit=1")
            global_response = self.client.get("/observability/alert-preview?limit=1")

        self.assertEqual(station_response.status_code, 200)
        station_payload = station_response.get_json()
        self.assertEqual(station_payload["count"], 1)
        self.assertEqual(station_payload["rows"][0]["station"], "KDEN")
        self.assertEqual(station_payload["rows"][0]["event_ticker"], "KDEN-OLDER")

        self.assertEqual(global_response.status_code, 200)
        global_payload = global_response.get_json()
        self.assertEqual(global_payload["count"], 1)
        self.assertEqual(global_payload["rows"][0]["station"], "KLAX")
        self.assertEqual(global_payload["rows"][0]["event_ticker"], "KLAX-NEWEST")
        self.assertIsNone(global_payload["rows"][0]["attention_phrase"])
        self.assertIsNone(global_payload["rows"][0]["alert_context"])

    def test_alert_preview_normalizes_invalid_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALERT_DB_PATH"] = os.path.join(tmp, "alerts.db")
            response = self.client.get("/observability/alert-preview?limit=abc")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["limit"], 25)
        self.assertEqual(payload["count"], 0)


if __name__ == "__main__":
    unittest.main()
