import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class IngestionDiagnosticClassEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/ingestion-diagnostic-class")
        self.assertEqual(response.status_code, 400)

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={"last_fetch_status": "not_attempted", "last_poll_attempt_utc": None},
    )
    def test_no_fetch_attempt(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "NO_FETCH_ATTEMPT")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={"last_fetch_status": "ok", "fetched_observation_count": 0},
    )
    def test_fetch_empty(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "FETCH_EMPTY")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 3,
            "ingested_observation_count": 0,
            "rejected_observation_count": 3,
            "rejection_reasons": [{"reason": "outside_window_before_grace_start", "count": 3}],
        },
    )
    def test_all_rejected_outside_window(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "ALL_REJECTED_OUTSIDE_WINDOW")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 2,
            "ingested_observation_count": 0,
            "rejected_observation_count": 2,
            "rejection_reasons": [{"reason": "dedup_older_or_equal_timestamp", "count": 2}],
        },
    )
    def test_all_rejected_dedup(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "ALL_REJECTED_DEDUP")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 2,
            "ingested_observation_count": 0,
            "rejected_observation_count": 2,
            "rejection_reasons": [{"reason": "outside_station_local_trading_day", "count": 2}],
        },
    )
    def test_station_day_mismatch(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "STATION_DAY_MISMATCH")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={"window_start_utc": "2026-03-02T12:00:00+00:00", "window_end_utc": "2026-03-02T12:05:00+00:00"},
    )
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 1,
            "ingested_observation_count": 0,
            "rejected_observation_count": 0,
            "latest_raw_observation_timestamp": "2026-03-02T11:59:00+00:00",
        },
    )
    def test_window_ahead_of_data(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "WINDOW_AHEAD_OF_DATA")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={"window_start_utc": "2026-03-02T11:55:00+00:00", "window_end_utc": "2026-03-02T12:00:00+00:00"},
    )
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 1,
            "ingested_observation_count": 0,
            "rejected_observation_count": 0,
            "latest_raw_observation_timestamp": "2026-03-02T12:01:00+00:00",
        },
    )
    def test_window_behind_data(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "WINDOW_BEHIND_DATA")

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "fetched_observation_count": 3,
            "ingested_observation_count": 1,
            "latest_accepted_observation_timestamp": "2026-03-02T11:58:00+00:00",
        },
    )
    def test_ingestion_healthy(self, *_mocks):
        response = self.client.get("/observability/ingestion-diagnostic-class?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["diagnostic_class"], "INGESTION_HEALTHY")


if __name__ == "__main__":
    unittest.main()
