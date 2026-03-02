import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class NWSFetchRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/nws-fetch-runtime")
        self.assertEqual(response.status_code, 400)

    @patch("app._current_kalshi_execution_domain", return_value="observability")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_last_nws_fetch_diagnostic",
        return_value={
            "station": "KDEN",
            "timestamp_utc": "2026-03-03T10:11:12+00:00",
            "request_url": "https://api.weather.gov/stations/KDEN/observations?start=2026-03-03T10:00:00Z&end=2026-03-03T10:10:00Z&limit=200",
            "start_iso": "2026-03-03T10:00:00Z",
            "end_iso": "2026-03-03T10:10:00Z",
            "http_status": 200,
            "feature_count": 2,
            "response_timestamp": "Tue, 03 Mar 2026 10:11:12 GMT",
        },
    )
    def test_returns_stored_diagnostic(self, *_mocks):
        response = self.client.get("/observability/nws-fetch-runtime?station=kden")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["last_fetch_timestamp_utc"], "2026-03-03T10:11:12+00:00")
        self.assertEqual(
            payload["request_url"],
            "https://api.weather.gov/stations/KDEN/observations?start=2026-03-03T10:00:00Z&end=2026-03-03T10:10:00Z&limit=200",
        )
        self.assertEqual(payload["start_iso"], "2026-03-03T10:00:00Z")
        self.assertEqual(payload["end_iso"], "2026-03-03T10:10:00Z")
        self.assertEqual(payload["http_status"], 200)
        self.assertEqual(payload["feature_count"], 2)
        self.assertEqual(payload["response_timestamp"], "Tue, 03 Mar 2026 10:11:12 GMT")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["execution_domain"], "observability")


if __name__ == "__main__":
    unittest.main()
