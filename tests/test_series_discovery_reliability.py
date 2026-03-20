import unittest
from unittest.mock import patch

from core import kalshi_monitor


class SeriesDiscoveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.original_series_discovered = kalshi_monitor._SERIES_DISCOVERED
        self.original_series_by_station = dict(kalshi_monitor._SERIES_BY_STATION)
        self.original_attempt_count = kalshi_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT
        self.original_last_success = kalshi_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC
        self.original_last_error = kalshi_monitor._LAST_SERIES_DISCOVERY_ERROR

    def tearDown(self):
        kalshi_monitor._SERIES_DISCOVERED = self.original_series_discovered
        kalshi_monitor._SERIES_BY_STATION = dict(self.original_series_by_station)
        kalshi_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = self.original_attempt_count
        kalshi_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = self.original_last_success
        kalshi_monitor._LAST_SERIES_DISCOVERY_ERROR = self.original_last_error

    def test_discovery_prefers_deterministic_primary_ticker(self):
        series_items = {
            "series": [
                {"frequency": "daily", "title": "Denver highest temperature", "ticker": "KXDENHIGH"},
                {"frequency": "daily", "title": "Denver highest temperature", "ticker": "KXHIGHDEN"},
            ]
        }
        with patch("core.kalshi_monitor._kalshi_public_get", return_value=series_items):
            discovered = kalshi_monitor._discover_series_for_stations()

        self.assertEqual(discovered.get("KDEN"), ["KXHIGHDEN", "KXDENHIGH"])

    def test_discovery_includes_low_only_series(self):
        series_items = {
            "series": [
                {"frequency": "daily", "title": "Denver lowest temperature", "ticker": "KXLOWDEN"},
            ]
        }
        with patch("core.kalshi_monitor._kalshi_public_get", return_value=series_items):
            discovered = kalshi_monitor._discover_series_for_stations()

        self.assertEqual(discovered.get("KDEN"), ["KXLOWDEN"])

    def test_discovery_keeps_high_and_low_series_in_deterministic_order(self):
        series_items = {
            "series": [
                {"frequency": "daily", "title": "Denver lowest temperature", "ticker": "KXLOWDEN"},
                {"frequency": "daily", "title": "Denver highest temperature", "ticker": "KXDENHIGH"},
                {"frequency": "daily", "title": "Denver highest temperature", "ticker": "KXHIGHDEN"},
            ]
        }
        with patch("core.kalshi_monitor._kalshi_public_get", return_value=series_items):
            discovered = kalshi_monitor._discover_series_for_stations()

        self.assertEqual(discovered.get("KDEN"), ["KXHIGHDEN", "KXLOWDEN", "KXDENHIGH"])

    def test_discovery_uses_market_derived_station_codes_outside_static_mapping(self):
        series_items = {
            "series": [
                {"frequency": "daily", "title": "Seattle highest temperature", "ticker": "KXHIGHSEA"},
            ]
        }
        with patch("core.kalshi_monitor._kalshi_public_get", return_value=series_items), \
             patch("core.kalshi_monitor.discover_market_derived_station_codes", return_value=["KSEA"]):
            discovered = kalshi_monitor._discover_series_for_stations()

        self.assertEqual(discovered.get("KSEA"), ["KXHIGHSEA"])

    def test_ensure_discovery_raises_and_does_not_mark_discovered_on_empty_mapping(self):
        kalshi_monitor._SERIES_DISCOVERED = False
        kalshi_monitor._SERIES_BY_STATION = {}
        kalshi_monitor._SERIES_DISCOVERY_ATTEMPT_COUNT = 0
        kalshi_monitor._LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
        kalshi_monitor._LAST_SERIES_DISCOVERY_ERROR = None

        with patch("core.kalshi_monitor._discover_series_for_stations", return_value={}):
            with self.assertRaises(RuntimeError):
                kalshi_monitor.ensure_series_discovery_loaded()

        self.assertFalse(kalshi_monitor._SERIES_DISCOVERED)
        self.assertEqual(kalshi_monitor._LAST_SERIES_DISCOVERY_ERROR, "series discovery returned 0 station mappings")


if __name__ == "__main__":
    unittest.main()
