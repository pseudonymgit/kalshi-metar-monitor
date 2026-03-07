import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class HydrationStallRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app._build_hydration_stall_state",
        return_value={
            "hydration_queue_depth": 1,
            "hydration_backoff_until": {"KDEN": 1000.0},
            "stations_blocked_by_hydration": ["KDEN"],
            "stations_blocked_beyond_window": ["KDEN"],
            "prolonged_hydration_stall_detected": True,
            "stall_window_seconds": 900,
        },
    )
    def test_hydration_stall_runtime_shape(self, *_mocks):
        response = self.client.get("/observability/hydration-stall-runtime?station=kden")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["hydration_queue_depth"], 1)
        self.assertEqual(payload["stations_blocked_by_hydration"], ["KDEN"])
        self.assertTrue(payload["prolonged_hydration_stall_detected"])


class HydrationRecoveryEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": True}})
    @patch("app.process_hydration_queue_worker", return_value={"status": "hydrated", "station": "KDEN"})
    @patch("app.enqueue_station_hydration", return_value=True)
    @patch(
        "app._build_hydration_stall_state",
        side_effect=[
            {
                "stations_blocked_by_hydration": ["KDEN"],
                "stations_blocked_beyond_window": ["KDEN"],
                "hydration_queue_depth": 1,
                "hydration_backoff_until": {},
                "prolonged_hydration_stall_detected": True,
                "stall_window_seconds": 900,
            },
            {
                "stations_blocked_by_hydration": [],
                "stations_blocked_beyond_window": [],
                "hydration_queue_depth": 0,
                "hydration_backoff_until": {},
                "prolonged_hydration_stall_detected": False,
                "stall_window_seconds": 900,
            },
        ],
    )
    def test_recovery_endpoint_attempts_bounded_recovery(self, *_mocks):
        response = self.client.post(
            "/ops/hydration/recover-stalled",
            json={"max_stations": 1, "max_worker_runs": 1},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target_stations"], ["KDEN"])
        self.assertEqual(payload["enqueued_stations"], ["KDEN"])
        self.assertEqual(payload["restored_stations"], ["KDEN"])
        self.assertEqual(payload["worker_results"], [{"status": "hydrated", "station": "KDEN"}])

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_last_hydration_execution_snapshot", return_value={})
    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch(
        "app._build_hydration_stall_state",
        side_effect=[
            {
                "stations_blocked_by_hydration": ["KDEN"],
                "stations_blocked_beyond_window": ["KDEN"],
                "hydration_queue_depth": 1,
                "hydration_backoff_until": {},
                "prolonged_hydration_stall_detected": True,
                "stall_window_seconds": 900,
            },
            {
                "stations_blocked_by_hydration": ["KDEN"],
                "stations_blocked_beyond_window": ["KDEN"],
                "hydration_queue_depth": 1,
                "hydration_backoff_until": {},
                "prolonged_hydration_stall_detected": True,
                "stall_window_seconds": 900,
            },
        ],
    )
    def test_recovery_endpoint_dry_run_does_not_enqueue_or_run_worker(self, _stall, mock_enqueue, mock_worker, *_mocks):
        response = self.client.post("/ops/hydration/recover-stalled", json={"dry_run": True})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["target_stations"], ["KDEN"])
        self.assertEqual(payload["enqueued_stations"], [])
        self.assertEqual(payload["worker_results"], [])
        mock_enqueue.assert_not_called()
        mock_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
