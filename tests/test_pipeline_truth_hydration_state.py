import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class PipelineTruthHydrationStateTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_latest_station_signal_runtime", return_value={"KDEN": {}})
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"hydration_state": {"status": "cache_valid"}}})
    def test_pipeline_truth_uses_hydration_snapshot(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload.get("hydration_status"), "cache_valid")


if __name__ == "__main__":
    unittest.main()
