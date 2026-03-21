import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class IngestionHealthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"], "poll_seconds": 60})
    @patch(
        "app.get_state",
        return_value={
            "stations": ["KDEN", "KLAX"],
            "last_seen_iso": {
                "KDEN": "2025-01-01T00:00:00+00:00",
                "KLAX": "2024-12-31T23:40:00+00:00",
            },
            "last_poll_utc": "2025-01-01T00:01:00+00:00",
        },
    )
    @patch("app.datetime")
    def test_ingestion_health_deterministic_station_fields(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2025, 1, 1, 0, 2, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        response = self.client.get("/observability/ingestion-health")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stale_after_seconds"], 180)
        self.assertTrue(payload["scheduler_running"])
        self.assertIn("system_health", payload)
        self.assertIn("ingestion", payload["system_health"])

        stations = {row["station"]: row for row in payload["stations"]}

        self.assertEqual(stations["KDEN"]["status"], "healthy")
        self.assertEqual(stations["KDEN"]["freshness_lag_seconds"], 120)
        self.assertEqual(stations["KLAX"]["status"], "stale")
        self.assertEqual(stations["KLAX"]["freshness_lag_seconds"], 1320)

    @patch("app.get_series_discovery_cache_snapshot", return_value={"KDEN": "KXHIGHDEN", "KMIA": "KXHIGHMIA"})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch(
        "app.get_state",
        return_value={
            "stations": ["KDEN"],
            "last_seen_iso": {
                "KDEN": "2025-01-01T00:00:00+00:00",
            },
            "last_poll_utc": "2025-01-01T00:01:00+00:00",
        },
    )
    @patch("app.datetime")
    def test_ingestion_health_includes_supported_discovered_station_in_live_universe(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2025, 1, 1, 0, 2, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        response = self.client.get("/observability/ingestion-health")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        stations = {row["station"]: row for row in payload["stations"]}

        self.assertIn("KMIA", stations)
        self.assertEqual(stations["KMIA"]["status"], "stale")
        self.assertEqual(stations["KMIA"]["status_reason"], "no_accepted_observation")


if __name__ == "__main__":
    unittest.main()
