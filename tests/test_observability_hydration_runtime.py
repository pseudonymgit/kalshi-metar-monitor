import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class HydrationPrerequisiteRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/hydration-prerequisite-runtime")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"ok": False, "error": "station query param required"},
        )

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_hydration_prerequisite_state_snapshot",
        return_value={"KDEN": {"attempted": True, "cache_valid": True}},
    )
    def test_returns_runtime_shape(self, *_mocks):
        response = self.client.get("/observability/hydration-prerequisite-runtime?station=kden")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["execution_domain"], "production")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["hydration_state"], {"attempted": True, "cache_valid": True})
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
