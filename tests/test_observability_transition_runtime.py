import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app as app_module

app = app_module.app


class TransitionRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/transition-runtime")
        self.assertEqual(response.status_code, 400)

    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_state",
        return_value={
            "last_obs": {"KJFK": {"temp_f": 45.2, "obs_time": "2026-03-02T11:58:00+00:00"}},
            "last_seen_iso": {"KJFK": "2026-03-02T11:58:00+00:00"},
            "last_observed_integer": {"KJFK": 45},
            "last_settlement_bucket": {"KJFK": 45},
        },
    )
    @patch("app.datetime")
    @patch("app.station_local_day_key", side_effect=lambda station, ts: "2026-03-02" if "2026-03-02" in ts else "2026-03-01")
    def test_returns_runtime_transition_observability_fields(self, _day_key_mock, datetime_mock, *_mocks):
        fixed_now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        datetime_mock.now.return_value = fixed_now

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "alerts.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE transition_events (
                    id INTEGER PRIMARY KEY,
                    created_utc TEXT,
                    station TEXT,
                    transition_type TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO transition_events (created_utc, station, transition_type) VALUES (?, ?, ?)",
                ("2026-03-01T23:55:00+00:00", "KJFK", "settlement_up"),
            )
            conn.execute(
                "INSERT INTO transition_events (created_utc, station, transition_type) VALUES (?, ?, ?)",
                ("2026-03-02T11:00:00+00:00", "KJFK", "instant_up"),
            )
            conn.execute(
                "INSERT INTO transition_events (created_utc, station, transition_type) VALUES (?, ?, ?)",
                ("2026-03-02T11:59:00+00:00", "KJFK", "settlement_up"),
            )
            conn.commit()
            conn.close()

            with patch.dict(os.environ, {"ALERT_DB_PATH": db_path}, clear=False):
                response = self.client.get("/observability/transition-runtime?station=kjfk")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["latest_observation_temp_f"], 45.2)
        self.assertEqual(payload["latest_observation_timestamp"], "2026-03-02T11:58:00+00:00")
        self.assertEqual(payload["last_observed_integer"], 45)
        self.assertEqual(payload["current_settlement_integer"], 45)
        self.assertEqual(payload["transitions_seen_today"], 1)
        self.assertEqual(payload["last_transition_type"], "settlement_up")
        self.assertEqual(payload["last_transition_timestamp"], "2026-03-02T11:59:00+00:00")


if __name__ == "__main__":
    unittest.main()
