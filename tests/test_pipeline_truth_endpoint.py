import unittest

import app as app_module

app = app_module.app


class PipelineTruthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_pipeline_truth_missing_station(self):
        response = self.client.get("/observability/pipeline-truth")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "station query parameter required"},
        )

    def test_pipeline_truth_unknown_station(self):
        response = self.client.get("/observability/pipeline-truth?station=XXXX")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload.get("station"), "XXXX")
        self.assertEqual(payload.get("blocking_stage"), "HYDRATION")
        self.assertEqual(payload.get("pipeline_status"), "ok")
        self.assertIsNone(payload.get("signal_type"))
        self.assertIsNone(payload.get("suppression_reason"))
        self.assertEqual(payload.get("cooldown_state"), {})


if __name__ == "__main__":
    unittest.main()
