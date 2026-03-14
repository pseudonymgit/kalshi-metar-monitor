import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class KalshiConnectivityRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_kalshi_connectivity_snapshot",
        return_value={
            "series_discovery_success_count": 3,
            "series_discovery_error_count": 1,
        },
    )
    def test_returns_runtime_shape(self, *_mocks):
        response = self.client.get("/observability/kalshi-connectivity-runtime")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["execution_domain"], "production")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(
            payload["connectivity_snapshot"],
            {
                "series_discovery_success_count": 3,
                "series_discovery_error_count": 1,
            },
        )
        self.assertTrue(payload["ok"])

    @patch("app._merge_discovered_stations_into_watchlist", side_effect=AssertionError("observability startup must remain read-only"))
    @patch("app.ensure_series_discovery_loaded", side_effect=AssertionError("observability startup must remain read-only"))
    @patch("app.ensure_scheduler_started", side_effect=AssertionError("observability startup must remain read-only"))
    @patch("app._current_kalshi_execution_domain", return_value="observability")
    @patch("app.is_scheduler_running", return_value=False)
    @patch("app.get_kalshi_connectivity_snapshot", return_value={})
    def test_observability_path_bypasses_autostart_hooks(self, *_mocks):
        app_module._autostart_fallback_done = False

        response = self.client.get("/observability/kalshi-connectivity-runtime")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(app_module._autostart_fallback_done)


if __name__ == "__main__":
    unittest.main()
