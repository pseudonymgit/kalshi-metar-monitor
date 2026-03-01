import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertFireAuditEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app._count_settlement_up_epochs_for_station_local_day", return_value=2)
    @patch("app._build_market_coverage_rows")
    @patch("core.kalshi_monitor._kalshi_public_get", side_effect=AssertionError("should not be called"))
    @patch("app._station_today_day_keys", return_value={"KDEN": "2026-01-01"})
    @patch("app._canonical_live_station_universe")
    def test_transition_with_eligible_markets_and_zero_alerts_flags_integrity(
        self,
        mock_station_universe,
        _mock_day_keys,
        _mock_public_get,
        mock_coverage,
        _mock_settlement_count,
        _mock_alert_count,
    ):
        mock_station_universe.return_value = {
            "stations": ["KDEN"],
            "configured_stations": {"KDEN"},
            "discovered_stations": set(),
            "watchlist_stations": set(),
        }
        mock_coverage.return_value = {
            "rows": [
                {"station": "KDEN", "market_type": "HIGH", "eligible_market_count_after_filters": 2},
                {"station": "KDEN", "market_type": "LOW", "eligible_market_count_after_filters": 1},
            ]
        }

        response = self.client.get("/observability/alert-fire-audit")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["stations"]), 1)
        row = payload["stations"][0]
        self.assertEqual(row["station"], "KDEN")
        self.assertEqual(row["settlement_up_count_today"], 2)
        self.assertEqual(row["eligible_market_count_today"], 3)
        self.assertEqual(row["alerts_sent_today"], 0)
        self.assertEqual(row["fire_integrity"], "TRANSITION_WITHOUT_ALERT")

    @patch("app._count_composed_alerts_for_station_local_day", return_value=1)
    @patch("app._count_settlement_up_epochs_for_station_local_day", return_value=1)
    @patch("app._build_market_coverage_rows", return_value={"rows": [{"station": "KDEN", "eligible_market_count_after_filters": 1}]})
    @patch("app._station_today_day_keys", return_value={"KDEN": "2026-01-01"})
    @patch("app._canonical_live_station_universe")
    def test_alerts_present_returns_ok(
        self,
        mock_station_universe,
        _mock_day_keys,
        _mock_coverage,
        _mock_settlement_count,
        _mock_alert_count,
    ):
        mock_station_universe.return_value = {
            "stations": ["KDEN"],
            "configured_stations": {"KDEN"},
            "discovered_stations": set(),
            "watchlist_stations": set(),
        }

        response = self.client.get("/observability/alert-fire-audit")
        payload = response.get_json()
        row = payload["stations"][0]

        self.assertEqual(row["fire_integrity"], "OK")

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app._count_settlement_up_epochs_for_station_local_day", return_value=0)
    @patch("app._build_market_coverage_rows", return_value={"rows": [{"station": "KDEN", "eligible_market_count_after_filters": 1}]})
    @patch("app._station_today_day_keys", return_value={"KDEN": "2026-01-01"})
    @patch("app._canonical_live_station_universe")
    def test_no_transitions_returns_ok(
        self,
        mock_station_universe,
        _mock_day_keys,
        _mock_coverage,
        _mock_settlement_count,
        _mock_alert_count,
    ):
        mock_station_universe.return_value = {
            "stations": ["KDEN"],
            "configured_stations": {"KDEN"},
            "discovered_stations": set(),
            "watchlist_stations": set(),
        }

        response = self.client.get("/observability/alert-fire-audit")
        payload = response.get_json()
        row = payload["stations"][0]

        self.assertEqual(row["fire_integrity"], "OK")

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app._count_settlement_up_epochs_for_station_local_day", return_value=0)
    @patch("app._build_market_coverage_rows", return_value={"rows": [{"station": "KMIA", "eligible_market_count_after_filters": 1}]})
    @patch("app._station_today_day_keys", return_value={"KMIA": "2026-01-01"})
    @patch("app._canonical_live_station_universe")
    def test_market_discovered_station_appears_automatically(
        self,
        mock_station_universe,
        _mock_day_keys,
        _mock_coverage,
        _mock_settlement_count,
        _mock_alert_count,
    ):
        mock_station_universe.return_value = {
            "stations": ["KMIA"],
            "configured_stations": set(),
            "discovered_stations": {"KMIA"},
            "watchlist_stations": set(),
        }

        response = self.client.get("/observability/alert-fire-audit")
        payload = response.get_json()

        stations = [row["station"] for row in payload["stations"]]
        self.assertIn("KMIA", stations)


if __name__ == "__main__":
    unittest.main()
