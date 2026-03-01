import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from core.settlement_epoch_logger import log_transition_for_settlement_epoch

app = app_module.app


class ObservabilityStationSummaryTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"], "poll_seconds": 60})
    @patch("app.get_state")
    @patch("app.datetime")
    def test_station_summary_includes_ingestion_only_station(self, mock_datetime, mock_get_state, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 10, 12, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_get_state.return_value = {
            "stations": ["KDEN", "KLAX"],
            "last_seen_iso": {
                "KDEN": "2026-01-01T10:10:00+00:00",
                "KLAX": "2026-01-01T08:00:00+00:00",
            },
            "last_poll_utc": "2026-01-01T10:19:00+00:00",
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.1,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69, "market_type": "HIGH"},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )

            response = self.client.get("/observability/station-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)

        rows = {row["station"]: row for row in payload["rows"]}
        self.assertEqual(rows["KDEN"]["ingestion_status"], "healthy")
        self.assertEqual(rows["KDEN"]["epoch_status"], "open")
        self.assertEqual(rows["KDEN"]["settlement_bucket"], 70)

        self.assertEqual(rows["KLAX"]["ingestion_status"], "stale")
        self.assertIsNone(rows["KLAX"]["epoch_status"])
        self.assertIsNone(rows["KLAX"]["settlement_bucket"])

    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch(
        "app.get_state",
        return_value={
            "stations": ["KDEN"],
            "last_seen_iso": {"KDEN": "2026-01-01T10:10:00+00:00"},
            "last_poll_utc": "2026-01-01T10:19:00+00:00",
        },
    )
    @patch("app.datetime")
    def test_station_summary_prefers_open_epoch_for_multi_market_type(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 10, 12, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.1,
                metadata={"obs_time": "2026-01-01T09:55:00Z", "previous_settlement_bucket": 69, "market_type": "LOW"},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T09:55:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=71,
                current_temp=71.0,
                metadata={
                    "obs_time": "2026-01-01T10:05:00Z",
                    "previous_settlement_bucket": 70,
                    "market_type": "HIGH",
                    "terminal_state_reached": True,
                },
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:05:00Z",
            )

            response = self.client.get("/observability/station-summary?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["count"], 1)

        row = payload["rows"][0]
        self.assertEqual(row["station"], "KDEN")
        self.assertEqual(row["current_epoch_selection_source"], "open_epoch")
        self.assertEqual(row["epoch_status"], "open")


if __name__ == "__main__":
    unittest.main()
