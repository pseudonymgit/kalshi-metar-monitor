import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertDiagnosticsEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app._build_market_coverage_rows")
    @patch("app.get_current_day_structure_summaries")
    @patch("app._station_today_day_keys")
    @patch("app._canonical_live_station_universe")
    @patch("app.get_state")
    def test_alert_diagnostics_classification_contract(
        self,
        mock_get_state,
        mock_station_universe,
        mock_station_day_keys,
        mock_day_structure,
        mock_market_coverage,
        mock_latest_eval,
        _mock_transition_history,
    ):
        stations = ["KDEN", "KSEA", "KLAX", "KPHX", "KORD", "KJFK", "KATL"]

        mock_station_universe.return_value = {
            "stations": stations,
            "configured_stations": {"KDEN", "KSEA", "KLAX", "KORD", "KJFK", "KATL"},
            "discovered_stations": {"KPHX", "KJFK"},
            "watchlist_stations": set(),
        }
        mock_station_day_keys.return_value = {station: "2026-01-01" for station in stations}

        mock_get_state.return_value = {
            "last_seen_iso": {
                "KDEN": "2026-01-01T10:00:00Z",
                "KLAX": "2026-01-01T10:00:00Z",
                "KPHX": "2026-01-01T10:00:00Z",
                "KORD": "2026-01-01T10:00:00Z",
                "KJFK": "2026-01-01T10:00:00Z",
                "KATL": "2026-01-01T10:00:00Z",
            },
            "last_obs": {
                "KDEN": {"temp_f": 71.2},
                "KLAX": {"temp_f": 74.2},
                "KPHX": {"temp_f": 75.1},
                "KORD": {"temp_f": 76.8},
                "KJFK": {"temp_f": 70.1},
                "KATL": {"temp_f": 71.1},
            },
        }

        mock_day_structure.return_value = {
            "rows": [
                {
                    "station": "KDEN",
                    "epoch_count_today": 0,
                    "current_or_latest_settlement_bucket": 71,
                    "current_or_latest_terminal_state_reached": False,
                },
                {
                    "station": "KLAX",
                    "epoch_count_today": 1,
                    "current_or_latest_settlement_bucket": 70,
                    "current_or_latest_terminal_state_reached": True,
                },
                {
                    "station": "KPHX",
                    "epoch_count_today": 1,
                    "current_or_latest_settlement_bucket": 74,
                    "current_or_latest_terminal_state_reached": False,
                },
                {
                    "station": "KORD",
                    "epoch_count_today": 1,
                    "current_or_latest_settlement_bucket": 75,
                    "current_or_latest_terminal_state_reached": False,
                },
                {
                    "station": "KJFK",
                    "epoch_count_today": 1,
                    "current_or_latest_settlement_bucket": 70,
                    "current_or_latest_terminal_state_reached": True,
                },
                {
                    "station": "KATL",
                    "epoch_count_today": 0,
                    "current_or_latest_settlement_bucket": 71,
                    "current_or_latest_terminal_state_reached": False,
                },
            ]
        }

        mock_market_coverage.return_value = {
            "rows": [
                {"station": "KLAX", "eligible_market_count_after_filters": 1},
                {"station": "KPHX", "eligible_market_count_after_filters": 1},
                {"station": "KORD", "eligible_market_count_after_filters": 1},
                {"station": "KATL", "eligible_market_count_after_filters": 1},
            ]
        }

        mock_latest_eval.return_value = {
            "KLAX": {"latest_evaluation_outcome": "ALERT_SENT"},
            "KPHX": {"latest_evaluation_outcome": "SUPPRESSED_RATE_LIMIT"},
            "KORD": {"latest_evaluation_outcome": "ALERT_SENT"},
            "KATL": {"latest_evaluation_outcome": "ALERT_SENT"},
        }

        response = self.client.get("/observability/alert-diagnostics")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        rows = {row["station"]: row for row in payload["stations"]}

        self.assertIn("KPHX", rows)
        self.assertEqual(rows["KSEA"]["diagnostic_class"], "NO_OBSERVATION")
        self.assertEqual(rows["KLAX"]["diagnostic_class"], "TERMINAL_STATE")
        self.assertEqual(rows["KPHX"]["diagnostic_class"], "SUPPRESSED")
        self.assertEqual(rows["KORD"]["diagnostic_class"], "ALERTABLE")
        self.assertEqual(rows["KJFK"]["diagnostic_class"], "TERMINAL_STATE")
        self.assertEqual(rows["KATL"]["diagnostic_class"], "ALERTABLE")


if __name__ == "__main__":
    unittest.main()
