import unittest
from unittest.mock import patch

import app as app_module
from core import kalshi_monitor, market_monitor


class KalshiConnectivitySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.original_series_discovered = market_monitor._SERIES_DISCOVERED
        self.original_series_by_station = dict(market_monitor._SERIES_BY_STATION)
        self.original_attempt_count = market_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT
        self.original_last_success = market_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC
        self.original_last_error = market_monitor._LAST_SERIES_DISCOVERY_ERROR
        self.original_last_hydration_execution = dict(market_monitor._LAST_HYDRATION_EXECUTION)

    def tearDown(self):
        market_monitor._SERIES_DISCOVERED = self.original_series_discovered
        market_monitor._SERIES_BY_STATION = dict(self.original_series_by_station)
        market_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = self.original_attempt_count
        market_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = self.original_last_success
        market_monitor._LAST_SERIES_DISCOVERY_ERROR = self.original_last_error
        market_monitor._LAST_HYDRATION_EXECUTION = dict(self.original_last_hydration_execution)

    def test_connectivity_snapshot_records_series_discovery_success(self):
        market_monitor._SERIES_DISCOVERED = False
        market_monitor._SERIES_BY_STATION = {}
        market_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = 0
        market_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
        market_monitor._LAST_SERIES_DISCOVERY_ERROR = "stale-error"

        with patch("core.market_monitor._discover_series_for_stations", return_value={"KDEN": "KXHIGHDEN"}):
            discovered = kalshi_monitor.ensure_series_discovery_loaded()

        snapshot = kalshi_monitor.get_kalshi_connectivity_snapshot()
        self.assertEqual(discovered, {"KDEN": ["KXHIGHDEN"]})
        self.assertTrue(snapshot["series_discovery_attempted"])
        self.assertIsNotNone(snapshot["last_series_discovery_success_utc"])
        self.assertIsNone(snapshot["last_series_discovery_error"])

    def test_connectivity_snapshot_records_last_discovery_failure(self):
        market_monitor._SERIES_DISCOVERED = False
        market_monitor._SERIES_BY_STATION = {}
        market_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = 0
        market_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
        market_monitor._LAST_SERIES_DISCOVERY_ERROR = None

        with patch("core.market_monitor._discover_series_for_stations", side_effect=RuntimeError("series-api-down")):
            with self.assertRaises(RuntimeError):
                kalshi_monitor.ensure_series_discovery_loaded()

        snapshot = kalshi_monitor.get_kalshi_connectivity_snapshot()
        self.assertTrue(snapshot["series_discovery_attempted"])
        self.assertEqual(snapshot["last_series_discovery_error"], "series-api-down")
        self.assertIsNone(snapshot["last_series_discovery_success_utc"])

    def test_observability_context_does_not_mutate_connectivity_counters(self):
        market_monitor._SERIES_DISCOVERED = False
        market_monitor._SERIES_BY_STATION = {}
        market_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = 0
        market_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
        market_monitor._LAST_SERIES_DISCOVERY_ERROR = None

        with app_module.app.test_request_context("/observability/runtime-authority-snapshot"):
            with patch("core.market_monitor._discover_series_for_stations", side_effect=RuntimeError("blocked")):
                with self.assertRaises(RuntimeError):
                    kalshi_monitor.ensure_series_discovery_loaded()

        snapshot = kalshi_monitor.get_kalshi_connectivity_snapshot()
        self.assertFalse(snapshot["series_discovery_attempted"])
        self.assertIsNone(snapshot["last_series_discovery_success_utc"])
        self.assertIsNone(snapshot["last_series_discovery_error"])


    def test_get_last_hydration_execution_snapshot_returns_deep_copy(self):
        market_monitor._LAST_HYDRATION_EXECUTION = {
            "KDEN": {
                "rejection_counts": {"date_mismatch": 2},
                "cache_written": False,
            }
        }

        snapshot = kalshi_monitor.get_last_hydration_execution_snapshot()
        snapshot["KDEN"]["rejection_counts"]["date_mismatch"] = 999

        self.assertEqual(
            market_monitor._LAST_HYDRATION_EXECUTION["KDEN"]["rejection_counts"]["date_mismatch"],
            2,
        )

if __name__ == "__main__":
    unittest.main()