import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from core.settlement_epoch_logger import log_transition_for_settlement_epoch


app = app_module.app


class ObservabilityDayStructureTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()
        self._discovery_patcher = patch("app.ensure_series_discovery_loaded", return_value={})
        self._watchlist_patcher = patch("app.get_watchlist", return_value={"watchlist": [], "count": 0})
        self._discovery_patcher.start()
        self._watchlist_patcher.start()
        self.addCleanup(self._discovery_patcher.stop)
        self.addCleanup(self._watchlist_patcher.stop)

    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"]})
    @patch("app.get_state", return_value={"stations": ["KDEN", "KLAX"]})
    @patch("app.datetime")
    def test_day_structure_includes_configured_stations_without_epochs(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="instant_up",
                settlement_bucket=70,
                current_temp=71.0,
                metadata={"obs_time": "2026-01-01T10:10:00Z", "terminal_state_reached": True},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:10:00Z",
            )

            response = self.client.get("/observability/day-structure")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)

        by_station = {row["station"]: row for row in payload["rows"]}

        self.assertEqual(by_station["KDEN"]["epoch_count_today"], 1)
        self.assertTrue(by_station["KDEN"]["open_epoch_present"])
        self.assertEqual(by_station["KDEN"]["current_or_latest_settlement_bucket"], 70)
        self.assertEqual(by_station["KDEN"]["current_or_latest_epoch_status"], "open")
        self.assertEqual(by_station["KDEN"]["latest_selection_source"], "open_epoch")

        self.assertEqual(by_station["KLAX"]["epoch_count_today"], 0)
        self.assertFalse(by_station["KLAX"]["open_epoch_present"])
        self.assertIsNone(by_station["KLAX"]["current_or_latest_settlement_bucket"])
        self.assertIsNone(by_station["KLAX"]["current_or_latest_epoch_status"])

    @patch("app.get_default_config", return_value={"stations": ["KDEN"]})
    @patch("app.get_state", return_value={"stations": ["KDEN"]})
    @patch("app.datetime")
    def test_day_structure_prefers_open_else_latest_for_today(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.0,
                metadata={"obs_time": "2026-01-01T09:00:00Z", "previous_settlement_bucket": 69},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T09:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=71,
                current_temp=71.0,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 70, "terminal_state_reached": True},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )

            response = self.client.get("/observability/day-structure?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["count"], 1)

        row = payload["rows"][0]
        self.assertEqual(row["epoch_count_today"], 2)
        self.assertEqual(row["closed_epoch_count_today"], 2)
        self.assertEqual(row["terminal_epoch_count_today"], 1)
        self.assertEqual(row["current_or_latest_settlement_bucket"], 71)
        self.assertEqual(row["current_or_latest_epoch_status"], "closed")
        self.assertEqual(row["latest_selection_source"], "latest_epoch_fallback")


if __name__ == "__main__":
    unittest.main()
