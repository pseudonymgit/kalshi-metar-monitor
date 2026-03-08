import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class ObservabilityPipelineTruthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._count_composed_alerts_for_station_local_day")
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app.get_transition_history")
    @patch("app.get_state")
    def test_deterministic_for_identical_inputs(
        self,
        mock_get_state,
        mock_get_transition_history,
        mock_get_latest_market_eval,
        mock_count_alerts,
        mock_get_hydration_snapshot,
    ):
        mock_get_state.return_value = {"last_seen_iso": {"KDEN": "2030-01-01T12:00:00+00:00"}}
        mock_get_transition_history.return_value = [
            {
                "transition_type": "settlement_up",
                "timestamp": "2030-01-01T12:05:00+00:00",
            }
        ]
        mock_get_latest_market_eval.return_value = {
            "KDEN": {
                "market_eligibility_runtime": {"eligible_markets_count": 2},
            }
        }
        mock_count_alerts.return_value = 1
        mock_get_hydration_snapshot.return_value = {
            "KDEN": {"cache_valid": True, "status": "cache_valid"}
        }

        response_one = self.client.get("/observability/pipeline-truth?station=KDEN")
        response_two = self.client.get("/observability/pipeline-truth?station=KDEN")

        self.assertEqual(response_one.status_code, 200)
        self.assertEqual(response_two.status_code, 200)
        self.assertEqual(response_one.get_json(), response_two.get_json())

    @patch("app._count_composed_alerts_for_station_local_day")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app.get_transition_history")
    @patch("app.get_state")
    @patch("app.enqueue_station_hydration", side_effect=AssertionError("must not enqueue hydration"))
    @patch("app.process_hydration_queue_worker", side_effect=AssertionError("must not process hydration queue"))
    @patch("app._kalshi_public_get", side_effect=AssertionError("must not call kalshi api"))
    def test_read_only_does_not_mutate_runtime_state(
        self,
        _mock_kalshi_public_get,
        _mock_process_hydration,
        _mock_enqueue_hydration,
        mock_get_state,
        mock_get_transition_history,
        mock_get_latest_market_eval,
        mock_get_hydration_snapshot,
        mock_count_alerts,
    ):
        transitions = [{"transition_type": "settlement_up", "timestamp": "2030-01-01T12:05:00+00:00"}]
        hydration_snapshot = {"KDEN": {"cache_valid": True, "status": "cache_valid"}}

        mock_get_state.return_value = {"last_seen_iso": {"KDEN": "2030-01-01T12:00:00+00:00"}}
        mock_get_transition_history.return_value = transitions
        mock_get_latest_market_eval.return_value = {"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 1}}}
        mock_get_hydration_snapshot.return_value = hydration_snapshot
        mock_count_alerts.return_value = 1

        transitions_before = [dict(row) for row in transitions]
        hydration_before = {k: dict(v) for k, v in hydration_snapshot.items()}

        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(transitions, transitions_before)
        self.assertEqual(hydration_snapshot, hydration_before)

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": False, "status": "cache_stale", "reason": "cache_missing"}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}})
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up", "timestamp": "2030-01-01T12:05:00+00:00"}])
    @patch("app.get_state", return_value={"last_seen_iso": {"KDEN": "2030-01-01T12:00:00+00:00"}})
    def test_classifies_hydration_blocking(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["blocking_stage"], "HYDRATION")
        self.assertEqual(payload["pipeline_status"], "BLOCKED")
        self.assertEqual(payload["hydration_status"], "cache_stale")

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True, "status": "cache_valid"}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}})
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up", "timestamp": "2030-01-01T12:05:00+00:00"}])
    @patch("app.get_state", return_value={"last_seen_iso": {"KDEN": "2030-01-01T12:00:00+00:00"}})
    def test_classifies_transition_without_alert_as_alert_emission(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["transitions_seen_today"], 1)
        self.assertEqual(payload["alerts_sent_today"], 0)
        self.assertEqual(payload["blocking_stage"], "ALERT_EMISSION")
        self.assertEqual(payload["reason"], "transitions_detected_without_alert_emission")


if __name__ == "__main__":
    unittest.main()
