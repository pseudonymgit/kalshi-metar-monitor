import os
import tempfile
import unittest

import app as app_module
from core.settlement_epoch_logger import log_transition_for_settlement_epoch


app = app_module.app


class ObservabilityCurrentEpochsTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_prefers_open_epoch_else_latest_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69, "market_type": "HIGH"},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="instant_up",
                settlement_bucket=70,
                current_temp=70.8,
                metadata={"obs_time": "2026-01-01T10:05:00Z", "market_type": "HIGH"},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:05:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KJFK",
                transition_type="settlement_up",
                settlement_bucket=60,
                current_temp=60.1,
                metadata={"obs_time": "2026-01-01T12:00:00Z", "previous_settlement_bucket": 59, "market_type": "LOW"},
                transition_event_id=3,
                event_timestamp_utc="2026-01-01T12:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KJFK",
                transition_type="settlement_up",
                settlement_bucket=61,
                current_temp=61.0,
                metadata={"obs_time": "2026-01-01T12:10:00Z", "previous_settlement_bucket": 60, "market_type": "LOW", "terminal_state_reached": True},
                transition_event_id=4,
                event_timestamp_utc="2026-01-01T12:10:00Z",
            )

            response = self.client.get("/observability/current-epochs")
            self.assertEqual(response.status_code, 200)

            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["count"], 2)

            by_station = {row["station"]: row for row in payload["epochs"]}

            self.assertEqual(by_station["KDEN"]["selection_source"], "open_epoch")
            self.assertEqual(by_station["KDEN"]["epoch_status"], "open")
            self.assertEqual(by_station["KDEN"]["market_type"], "HIGH")

            self.assertEqual(by_station["KJFK"]["selection_source"], "latest_epoch_fallback")
            self.assertEqual(by_station["KJFK"]["epoch_status"], "closed")
            self.assertEqual(by_station["KJFK"]["settlement_bucket"], 61)

            expected_fields = {
                "station",
                "market_type",
                "local_trading_date",
                "settlement_bucket",
                "prior_settlement_bucket",
                "settlement_timestamp_utc",
                "settlement_jump_magnitude",
                "epoch_status",
                "epoch_close_reason",
                "epoch_close_timestamp_utc",
                "reversion_occurred",
                "first_reversion_timestamp_utc",
                "max_excursion_above_settlement",
                "duration_at_or_above_settlement_seconds",
                "duration_strictly_above_settlement_seconds",
                "terminal_state_reached",
                "settlement_transition_event_id",
                "last_transition_event_id",
                "last_transition_timestamp_utc",
                "last_transition_temp_f",
                "selection_source",
            }
            self.assertEqual(set(by_station["KDEN"].keys()), expected_fields)

    def test_station_filter_is_deterministic(self):
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
                station="KLAX",
                transition_type="settlement_up",
                settlement_bucket=75,
                current_temp=75.0,
                metadata={"obs_time": "2026-01-01T11:00:00Z", "previous_settlement_bucket": 74},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T11:00:00Z",
            )

            response = self.client.get("/observability/current-epochs?station=KDEN")
            self.assertEqual(response.status_code, 200)

            payload = response.get_json()
            self.assertEqual(payload["station"], "KDEN")
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["epochs"][0]["station"], "KDEN")

    def test_returns_empty_list_when_no_epochs_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            response = self.client.get("/observability/current-epochs")
            self.assertEqual(response.status_code, 200)

            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["count"], 0)
            self.assertEqual(payload["epochs"], [])


if __name__ == "__main__":
    unittest.main()
