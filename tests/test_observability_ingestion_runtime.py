import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class IngestionRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/ingestion-runtime")
        self.assertEqual(response.status_code, 400)

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_station_ingestion_runtime",
        return_value={
            "station": "KJFK",
            "last_poll_attempt_utc": "2026-03-02T12:00:00+00:00",
            "last_fetch_status": "ok",
            "fetched_observation_count": 5,
            "ingested_observation_count": 1,
            "rejected_observation_count": 4,
            "rejection_reasons": [{"reason": "dedup_older_or_equal_timestamp", "count": 4}],
            "latest_raw_observation_timestamp": "2026-03-02T11:59:00+00:00",
            "latest_accepted_observation_timestamp": "2026-03-02T11:58:00+00:00",
        },
    )
    def test_returns_station_runtime_fields(self, *_mocks):
        response = self.client.get("/observability/ingestion-runtime?station=kjfk")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "KJFK")
        self.assertEqual(payload["execution_domain"], "production")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["last_fetch_status"], "ok")
        self.assertEqual(payload["fetched_observation_count"], 5)
        self.assertEqual(payload["ingested_observation_count"], 1)
        self.assertEqual(payload["rejected_observation_count"], 4)
        self.assertEqual(payload["latest_raw_observation_timestamp"], "2026-03-02T11:59:00+00:00")
        self.assertEqual(payload["latest_accepted_observation_timestamp"], "2026-03-02T11:58:00+00:00")


if __name__ == "__main__":
    unittest.main()
