import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class IngestionWindowRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/ingestion-window-runtime")
        self.assertEqual(response.status_code, 400)

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_window_runtime",
        return_value={
            "station": "KJFK",
            "window_start_utc": "2026-03-02T11:55:00+00:00",
            "window_end_utc": "2026-03-02T12:00:00+00:00",
            "last_seen_iso": "2026-03-02T11:58:00+00:00",
            "latest_raw_observation_timestamp": "2026-03-02T11:59:00+00:00",
            "latest_accepted_observation_timestamp": "2026-03-02T11:58:00+00:00",
            "sample_rejected_observations": [
                {
                    "obs_time": "2026-03-02T11:54:00+00:00",
                    "rejection_reason": "outside_window_before_grace_start",
                    "delta_seconds_from_window_start": -60.0,
                    "delta_seconds_from_window_end": -360.0,
                }
            ],
        },
    )
    def test_returns_station_runtime_fields(self, *_mocks):
        response = self.client.get("/observability/ingestion-window-runtime?station=kjfk")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "KJFK")
        self.assertEqual(payload["execution_domain"], "production")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["window_start_utc"], "2026-03-02T11:55:00+00:00")
        self.assertEqual(payload["window_end_utc"], "2026-03-02T12:00:00+00:00")
        self.assertEqual(payload["last_seen_iso"], "2026-03-02T11:58:00+00:00")
        self.assertEqual(payload["latest_raw_observation_timestamp"], "2026-03-02T11:59:00+00:00")
        self.assertEqual(payload["latest_accepted_observation_timestamp"], "2026-03-02T11:58:00+00:00")
        self.assertEqual(payload["sample_rejected_observations"][0]["rejection_reason"], "outside_window_before_grace_start")


if __name__ == "__main__":
    unittest.main()
