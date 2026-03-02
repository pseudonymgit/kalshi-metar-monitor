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


if __name__ == "__main__":
    unittest.main()
