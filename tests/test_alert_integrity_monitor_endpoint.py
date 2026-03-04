import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertIntegrityMonitorEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app.get_transition_history")
    @patch("app._build_runtime_authority_hydration_snapshot")
    @patch("app._canonical_live_station_universe")
    def test_integrity_endpoint_emits_pipeline_and_hydration_findings(
        self,
        mock_station_universe,
        mock_hydration,
        mock_transitions,
        mock_evals,
        _mock_alerts,
    ):
        mock_station_universe.return_value = {
            "stations": ["KDEN"],
            "configured_stations": {"KDEN"},
            "discovered_stations": set(),
            "watchlist_stations": set(),
        }
        mock_transitions.return_value = [{"station": "KDEN", "timestamp": "2030-01-01T00:00:00+00:00"}]
        mock_evals.return_value = {
            "KDEN": {
                "latest_evaluation_timestamp_utc": "2029-12-31T23:00:00+00:00",
                "latest_evaluation_outcome": "SUPPRESSED_NO_ELIGIBLE_MARKET",
                "latest_suppression_reason": "",
            }
        }
        mock_hydration.return_value = {
            "stations": {
                "KDEN": {
                    "cache_present": True,
                    "hydration_prerequisite": {
                        "series_discovered": False,
                        "cache_valid": False,
                    },
                }
            }
        }

        response = self.client.get("/integrity/alert_pipeline")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        finding_types = {row["finding_type"] for row in payload["findings"]}
        self.assertIn("ALERT_PIPELINE_GAP", finding_types)
        self.assertIn("TRANSITION_WITHOUT_EVALUATION", finding_types)
        self.assertIn("SUPPRESSION_WITHOUT_REASON", finding_types)
        self.assertIn("HYDRATION_DRIFT", finding_types)
        self.assertIn("MARKET_DISCOVERY_REGRESSION", finding_types)
        self.assertIn("STATION_ALERT_SILENCE", finding_types)

    @patch("app.get_recent_alerts", return_value=[{"station": "KDEN", "created_utc": "2030-01-01T00:00:05+00:00"}])
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"latest_evaluation_timestamp_utc": "2030-01-01T00:00:01+00:00", "latest_evaluation_outcome": "ALERT_EMITTED", "latest_suppression_reason": ""}})
    @patch("app.get_transition_history", return_value=[{"station": "KDEN", "timestamp": "2030-01-01T00:00:00+00:00"}])
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={"stations": {"KDEN": {"cache_present": True, "hydration_prerequisite": {"series_discovered": True, "cache_valid": True}}}})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"], "configured_stations": {"KDEN"}, "discovered_stations": {"KDEN"}, "watchlist_stations": set()})
    def test_integrity_endpoint_returns_no_findings_when_healthy(self, *_mocks):
        response = self.client.get("/integrity/alert_pipeline")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["finding_count"], 0)
        self.assertEqual(payload["findings"], [])


if __name__ == "__main__":
    unittest.main()
