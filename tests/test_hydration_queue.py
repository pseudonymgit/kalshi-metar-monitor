import unittest
from unittest.mock import patch

from core import kalshi_monitor


class HydrationQueueTests(unittest.TestCase):
    def setUp(self):
        self.orig_queue = list(kalshi_monitor._hydration_queue)
        self.orig_backoff = dict(kalshi_monitor._hydration_backoff_until)
        self.orig_last_ts = kalshi_monitor._last_hydration_request_ts

        kalshi_monitor._hydration_queue = []
        kalshi_monitor._hydration_backoff_until = {}
        kalshi_monitor._last_hydration_request_ts = 0.0

    def tearDown(self):
        kalshi_monitor._hydration_queue = self.orig_queue
        kalshi_monitor._hydration_backoff_until = self.orig_backoff
        kalshi_monitor._last_hydration_request_ts = self.orig_last_ts

    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="production")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", return_value={"status": "hydrated"})
    @patch("core.kalshi_monitor.time.time", return_value=1000.0)
    def test_enqueue_order_is_deterministic(self, _time, _hydrate, _domain):
        kalshi_monitor.enqueue_station_hydration("kphl", reason="cache_missing")
        kalshi_monitor.enqueue_station_hydration("kden", reason="cache_missing")
        kalshi_monitor.enqueue_station_hydration("kphl", reason="cache_missing")

        snapshot = kalshi_monitor.hydration_queue_snapshot()
        self.assertEqual(snapshot["queue"], ["KDEN", "KPHL"])

    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="production")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", return_value={"status": "hydrated"})
    @patch("core.kalshi_monitor.time.time", side_effect=[1000.0, 1000.4, 1001.2])
    def test_worker_processes_one_request_per_interval(self, _time, mock_hydrate, _domain):
        kalshi_monitor.enqueue_station_hydration("KDEN")
        kalshi_monitor.enqueue_station_hydration("KPHL")

        first = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})
        second = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})
        third = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})

        self.assertEqual(first["status"], "hydrated")
        self.assertEqual(second["status"], "rate_limited")
        self.assertEqual(third["status"], "hydrated")
        self.assertEqual(mock_hydrate.call_count, 2)

    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="production")
    @patch("core.kalshi_monitor.time.time", side_effect=[2000.0, 2001.0, 2002.0])
    def test_429_sets_backoff_and_prevents_immediate_retry(self, _time, _domain):
        response = type("Response", (), {"status_code": 429})()
        error = kalshi_monitor.requests.HTTPError("rate limited")
        error.response = response

        with patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", side_effect=error):
            kalshi_monitor.enqueue_station_hydration("KDEN")
            first = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})
            second = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})

        self.assertEqual(first["status"], "backoff")
        self.assertEqual(second["status"], "backoff")
        self.assertEqual(second["station"], "KDEN")
        self.assertGreater(second["retry_after"], 0)


if __name__ == "__main__":
    unittest.main()
