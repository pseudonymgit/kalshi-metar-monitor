import unittest
from unittest.mock import patch

from core import kalshi_monitor


class KalshiWeatherMarketDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.original_markets = list(kalshi_monitor._DISCOVERED_WEATHER_MARKETS)
        self.original_station_mapping = dict(kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION)

    def tearDown(self):
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS = list(self.original_markets)
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION = dict(self.original_station_mapping)

    def test_discover_weather_markets_prefers_structured_station_metadata(self):
        payload = {
            "markets": [
                {
                    "ticker": "CHI-TMAX",
                    "status": "OPEN",
                    "title": "Daily high temperature",
                    "settlement_rule": "Settles to KMDW and secondary KORD",
                    "settlement_metadata": {"station": "KORD"},
                    "expiration_time": "2026-03-07T23:00:00Z",
                }
            ],
            "cursor": None,
        }

        with patch("core.market_monitor._kalshi_public_get", return_value=payload):
            discovered = kalshi_monitor.discover_kalshi_weather_markets()

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["market_symbol"], "CHI-TMAX")
        self.assertEqual(discovered[0]["market_type"], "HIGH_TEMP")
        self.assertEqual(discovered[0]["station"], "KORD")
        self.assertTrue(discovered[0]["active"])

    def test_discover_weather_markets_uses_first_icao_from_settlement_rule(self):
        payload = {
            "markets": [
                {
                    "ticker": "CHI-TMIN",
                    "status": "OPEN",
                    "title": "Daily low temperature",
                    "settlement_rule": "Primary station KMDW backup station KORD",
                }
            ],
            "cursor": None,
        }

        with patch("core.market_monitor._kalshi_public_get", return_value=payload):
            discovered = kalshi_monitor.discover_kalshi_weather_markets()

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["market_type"], "LOW_TEMP")
        self.assertEqual(discovered[0]["station"], "KMDW")

    def test_station_mapping_groups_markets_by_station(self):
        payload = {
            "markets": [
                {
                    "ticker": "AUS-TMAX",
                    "status": "OPEN",
                    "title": "Daily high temperature",
                    "settlement_rule": "Settles to KAUS",
                },
                {
                    "ticker": "AUS-TMIN",
                    "status": "OPEN",
                    "title": "Daily low temperature",
                    "settlement_rule": "Settles to KAUS",
                },
                {
                    "ticker": "AUS-RAIN",
                    "status": "OPEN",
                    "title": "Daily precipitation",
                    "settlement_rule": "Settles to KAUS",
                },
                {
                    "ticker": "CHI-TMAX",
                    "status": "OPEN",
                    "title": "Daily high temperature",
                    "settlement_rule": "Settles to KMDW",
                },
            ],
            "cursor": None,
        }

        with patch("core.market_monitor._kalshi_public_get", return_value=payload):
            kalshi_monitor.discover_kalshi_weather_markets()

        station_mapping = kalshi_monitor.get_discovered_weather_market_station_mapping()
        self.assertEqual(station_mapping["KAUS"], ["AUS-RAIN", "AUS-TMAX", "AUS-TMIN"])
        self.assertEqual(station_mapping["KMDW"], ["CHI-TMAX"])


if __name__ == "__main__":
    unittest.main()