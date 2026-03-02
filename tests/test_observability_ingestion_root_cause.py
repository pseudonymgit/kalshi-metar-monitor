import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class IngestionRootCauseEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/ingestion-root-cause")
        self.assertEqual(response.status_code, 400)

    @patch("app.get_state", return_value={"ingestion_admission": {"KJFK": {"skip_reason": "not_skipped"}}})
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={
            "window_start_utc": "2026-03-02T12:00:00+00:00",
            "window_end_utc": "2026-03-02T12:05:00+00:00",
        },
    )
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "last_fetch_status": "ok",
            "fetched_observation_count": 0,
            "ingested_observation_count": 0,
            "rejected_observation_count": 0,
        },
    )
    def test_empty_fetch(self, *_mocks):
        response = self.client.get("/observability/ingestion-root-cause?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["deterministic_root_cause"], "NO_DATA_RETURNED_FROM_SOURCE")

    @patch("app.get_state", return_value={"ingestion_admission": {"KJFK": {"skip_reason": "not_skipped"}}})
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={
            "window_start_utc": "2026-03-02T12:00:00+00:00",
            "window_end_utc": "2026-03-02T12:05:00+00:00",
        },
    )
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "last_fetch_status": "ok",
            "fetched_observation_count": 2,
            "ingested_observation_count": 0,
            "rejected_observation_count": 1,
            "latest_raw_observation_timestamp": "2026-03-02T11:58:00+00:00",
        },
    )
    def test_window_ahead(self, *_mocks):
        response = self.client.get("/observability/ingestion-root-cause?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["deterministic_root_cause"], "WINDOW_AHEAD_OF_AVAILABLE_DATA")
        self.assertEqual(payload["seconds_window_vs_latest_raw"], -120)

    @patch("app.get_state", return_value={"ingestion_admission": {"KJFK": {"skip_reason": "not_skipped"}}})
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "last_fetch_status": "ok",
            "fetched_observation_count": 3,
            "ingested_observation_count": 0,
            "rejected_observation_count": 3,
            "rejection_reasons": [{"reason": "dedup_older_or_equal_timestamp", "count": 3}],
        },
    )
    def test_dedup_rejection(self, *_mocks):
        response = self.client.get("/observability/ingestion-root-cause?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["deterministic_root_cause"], "ALL_REJECTED_DEDUP")
        self.assertEqual(payload["dominant_rejection_reason"], "dedup_older_or_equal_timestamp")

    @patch("app.get_state", return_value={"ingestion_admission": {"KJFK": {"skip_reason": "not_skipped"}}})
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={
            "window_start_utc": "2026-03-02T12:00:00+00:00",
            "window_end_utc": "2026-03-02T12:05:00+00:00",
        },
    )
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "last_fetch_status": "ok",
            "fetched_observation_count": 3,
            "ingested_observation_count": 1,
            "rejected_observation_count": 2,
            "latest_accepted_observation_timestamp": "2026-03-02T12:03:00+00:00",
        },
    )
    def test_healthy_ingestion(self, *_mocks):
        response = self.client.get("/observability/ingestion-root-cause?station=kjfk")
        payload = response.get_json()
        self.assertEqual(payload["deterministic_root_cause"], "UNKNOWN_INGESTION_FAILURE")
        self.assertEqual(payload["seconds_window_vs_latest_accepted"], 0)


if __name__ == "__main__":
    unittest.main()
