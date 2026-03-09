import unittest
from unittest.mock import patch

import app as app_module


class PipelineTruthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    def test_missing_station_returns_http_400(self):
        response = self.client.get("/observability/pipeline-truth")
        self.assertEqual(response.status_code, 400)

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 2})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_valid"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 0, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_lowercase_station_normalized_to_uppercase(self, _stations, mock_transition, mock_hydration, mock_market, mock_audit):
        response = self.client.get("/observability/pipeline-truth?station=kden")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["station"], "KDEN")
        mock_transition.assert_called_once_with("KDEN")
        mock_hydration.assert_called_once_with("KDEN")
        mock_market.assert_called_once_with("KDEN")
        mock_audit.assert_called_once()

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_unknown_station_returns_deterministic_json_with_required_fields(self, _stations):
        response = self.client.get("/observability/pipeline-truth?station=xxxx")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        required_fields = {
            "station",
            "pipeline_status",
            "blocking_stage",
            "reason",
            "transitions_seen_today",
            "eligible_markets_count",
            "alerts_sent_today",
            "hydration_status",
            "last_transition_timestamp",
        }
        self.assertEqual(set(payload.keys()), required_fields)
        self.assertEqual(payload["station"], "XXXX")
        self.assertEqual(payload["pipeline_status"], "unknown_station")
        self.assertEqual(payload["blocking_stage"], "INGESTION")
        self.assertEqual(payload["reason"], "unknown_station")
        self.assertEqual(payload["transitions_seen_today"], 0)
        self.assertEqual(payload["eligible_markets_count"], 0)
        self.assertEqual(payload["alerts_sent_today"], 0)
        self.assertEqual(payload["hydration_status"], "unknown")
        self.assertIsNone(payload["last_transition_timestamp"])

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 1})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_stale"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_hydration_classification(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        payload = response.get_json()
        self.assertEqual(payload["blocking_stage"], "HYDRATION")

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 0})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_valid"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_market_evaluation_classification(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        payload = response.get_json()
        self.assertEqual(payload["blocking_stage"], "MARKET_EVALUATION")

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 2})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_valid"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_alert_emission_classification(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        payload = response.get_json()
        self.assertEqual(payload["blocking_stage"], "ALERT_EMISSION")

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 2})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_valid"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_identical_input_produces_identical_output(self, *_mocks):
        first = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        second = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        self.assertEqual(first, second)

    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}]})
    @patch("app._build_market_eligibility_runtime_payload", return_value={"eligible_markets_count": 2})
    @patch("app._build_hydration_prerequisite_runtime_payload", return_value={"hydration_state": {"status": "cache_valid"}})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1, "last_transition_timestamp": "2026-01-01T00:00:00Z"})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.hydration_queue_snapshot", side_effect=AssertionError("must not mutate hydration queue"))
    @patch("app.enqueue_station_hydration", side_effect=AssertionError("must not trigger hydration worker"))
    @patch("app.process_hydration_queue_worker", side_effect=AssertionError("must not run hydration worker"))
    @patch("app._send_alert", side_effect=AssertionError("must not evaluate alerts"))
    @patch("app._kalshi_public_get", side_effect=AssertionError("must not call Kalshi APIs"))
    @patch("app.start_scheduler", side_effect=AssertionError("must not mutate scheduler state"))
    @patch("app.stop_scheduler", side_effect=AssertionError("must not mutate scheduler state"))
    def test_endpoint_does_not_mutate_runtime_state(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
