import unittest
from unittest.mock import patch

from core import kalshi_monitor


class SettlementStationResolutionTests(unittest.TestCase):
    def test_airport_backed_market_resolves_correctly(self):
        self.assertEqual(kalshi_monitor.resolve_settlement_station("DEN"), "KDEN")

    def test_climate_station_market_resolves_correctly(self):
        self.assertEqual(kalshi_monitor.resolve_settlement_station("NYC"), "KNYC")

    def test_market_derived_station_universe_updates_when_market_removed(self):
        with patch(
            "core.kalshi_monitor.build_market_derived_station_universe",
            side_effect=[["DEN", "NYC"], ["DEN"]],
        ):
            initial = kalshi_monitor.build_market_polling_station_universe()
            updated = kalshi_monitor.build_market_polling_station_universe()

        self.assertEqual(initial, ["KDEN", "KNYC"])
        self.assertEqual(updated, ["KDEN"])

    def test_build_market_polling_station_universe_ignores_resolver_exceptions(self):
        with patch(
            "core.kalshi_monitor.build_market_derived_station_universe",
            return_value=["DEN", "NYC"],
        ), patch(
            "core.kalshi_monitor.resolve_settlement_station",
            side_effect=["KDEN", RuntimeError("bad token")],
        ):
            stations = kalshi_monitor.build_market_polling_station_universe()

        self.assertEqual(stations, ["KDEN"])

    def test_build_market_derived_station_universe_extracts_open_high_low_tokens(self):
        payload = {
            "markets": [
                {"ticker": "KXHIGHDEN-01MAR26", "status": "open"},
                {"ticker": "KXLOWNYC-01MAR26", "status": "OPEN"},
                {"ticker": "KXHIGHLAX-01MAR26", "status": "closed"},
                {"ticker": "OTHER-01MAR26", "status": "open"},
            ],
            "cursor": None,
        }

        with patch("core.kalshi_monitor._kalshi_public_get", return_value=payload):
            tokens = kalshi_monitor.build_market_derived_station_universe()

        self.assertEqual(tokens, ["DEN", "NYC"])


if __name__ == "__main__":
    unittest.main()
