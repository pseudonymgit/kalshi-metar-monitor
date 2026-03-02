import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertCausalityClassEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app._get_alert_runtime_snapshot", return_value={"latest_outcome": "UNKNOWN", "station_alerts": []})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app._build_ingestion_diagnostic_classification", return_value=("FETCH_EMPTY", "no ingestion"))
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch("app.get_station_ingestion_runtime", return_value={})
    def test_no_ingestion_precedence(self, *_mocks):
        response = self.client.get("/observability/alert-causality-class?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["alert_causality_class"], "NO_INGESTION")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app._get_alert_runtime_snapshot", return_value={"latest_outcome": "UNKNOWN", "station_alerts": []})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 0})
    @patch("app._build_ingestion_diagnostic_classification", return_value=("INGESTION_HEALTHY", "healthy"))
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch("app.get_station_ingestion_runtime", return_value={})
    def test_no_transitions(self, *_mocks):
        response = self.client.get("/observability/alert-causality-class?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["alert_causality_class"], "NO_TRANSITIONS")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app._get_alert_runtime_snapshot", return_value={"latest_outcome": "SUPPRESSED_RATE_LIMIT", "station_alerts": []})
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={
            "KDEN": {
                "latest_evaluation_outcome": "SUPPRESSED_RATE_LIMIT",
                "latest_suppression_reason": "RATE_LIMIT",
                "market_eligibility_runtime": {"eligible_markets_count": 1},
            }
        },
    )
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app._build_ingestion_diagnostic_classification", return_value=("INGESTION_HEALTHY", "healthy"))
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch("app.get_station_ingestion_runtime", return_value={})
    def test_suppressed_transition(self, *_mocks):
        response = self.client.get("/observability/alert-causality-class?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["alert_causality_class"], "SUPPRESSED_TRANSITION")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app._get_alert_runtime_snapshot", return_value={"latest_outcome": "UNKNOWN", "station_alerts": []})
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={"KDEN": {"latest_evaluation_outcome": "ALERTABLE", "market_eligibility_runtime": {"eligible_markets_count": 1}}},
    )
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app._build_ingestion_diagnostic_classification", return_value=("INGESTION_HEALTHY", "healthy"))
    @patch("app.get_station_ingestion_window_runtime", return_value={})
    @patch("app.get_station_ingestion_runtime", return_value={})
    def test_alert_path_healthy(self, *_mocks):
        response = self.client.get("/observability/alert-causality-class?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["alert_causality_class"], "ALERT_PATH_HEALTHY")


if __name__ == "__main__":
    unittest.main()
