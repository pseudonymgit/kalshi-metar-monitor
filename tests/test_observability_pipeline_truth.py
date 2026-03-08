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
    def test_read_only_does_not_mutate_transition_or_hydration_state(
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

    @patch("app._count_composed_alerts_for_station_local_day")
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True, "status": "cache_valid"}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 1}}})
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up", "timestamp": "2030-01-01T12:05:00+00:00"}])
    @patch("app.get_state", return_value={"last_seen_iso": {"KDEN": "2030-01-01T12:00:00+00:00"}})
    @patch("app.hydration_queue_snapshot")
    @patch("app.get_recent_alerts")
    def test_read_only_does_not_mutate_hydration_queue_or_alert_history(
        self,
        mock_get_recent_alerts,
        mock_hydration_queue_snapshot,
        _mock_get_state,
        _mock_get_transition_history,
        _mock_get_latest_market_eval,
        _mock_get_hydration_snapshot,
        mock_count_alerts,
    ):
        hydration_queue = {
            "queue": ["KDEN"],
            "queue_depth": 1,
            "queued_stations": ["KDEN"],
            "backoff_until": {"KDEN": 120.0},
            "backoff_stations": ["KDEN"],
            "stations_in_backoff": 1,
            "next_backoff_expiry": 120.0,
            "last_hydration_request_ts": 10.0,
        }
        alert_history = [{"station": "KDEN", "created_utc": "2030-01-01T12:10:00+00:00", "alert_type": "composed_alert_sent"}]
        mock_hydration_queue_snapshot.return_value = hydration_queue
        mock_get_recent_alerts.return_value = alert_history
        mock_count_alerts.return_value = 1

        queue_before = {
            **hydration_queue,
            "queue": list(hydration_queue["queue"]),
            "queued_stations": list(hydration_queue["queued_stations"]),
            "backoff_until": dict(hydration_queue["backoff_until"]),
            "backoff_stations": list(hydration_queue["backoff_stations"]),
        }
        alerts_before = [dict(row) for row in alert_history]

        response = self.client.get("/observability/pipeline-truth?station=KDEN")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(hydration_queue, queue_before)
        self.assertEqual(alert_history, alerts_before)
        mock_hydration_queue_snapshot.assert_not_called()
        mock_get_recent_alerts.assert_not_called()

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


    def test_missing_station_returns_400(self):
        response = self.client.get("/observability/pipeline-truth")
        self.assertEqual(response.status_code, 400)

    @patch("app._count_composed_alerts_for_station_local_day", return_value=0)
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_state", return_value={"last_seen_iso": {}})
    def test_unknown_station_returns_deterministic_structure(self, *_mocks):
        response = self.client.get("/observability/pipeline-truth?station=ZZZZ")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "ZZZZ")
        self.assertEqual(payload["pipeline_status"], "BLOCKED")
        self.assertEqual(payload["blocking_stage"], "INGESTION")
        self.assertEqual(payload["reason"], "no_accepted_observation")
        self.assertEqual(payload["transitions_seen_today"], 0)
        self.assertEqual(payload["eligible_markets_count"], 0)
        self.assertEqual(payload["alerts_sent_today"], 0)
        self.assertEqual(payload["hydration_status"], "cache_invalid")
        self.assertIsNone(payload["last_transition_timestamp"])


if __name__ == "__main__":
    unittest.main()
