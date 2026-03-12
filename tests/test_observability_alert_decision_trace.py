import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertDecisionTraceEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_signal_runtime", return_value={"KDEN": {"signal_type": "near_boundary_momentum_up", "suppression_reason": None, "cooldown_state": {"station_active": True}}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_state")
    def test_non_hydrated_station_blocks_at_ingestion_admission(
        self,
        mock_get_state,
        mock_hydration_state,
        _mock_transition_history,
        _mock_latest_eval,
        _mock_latest_signal,
        _mock_recent_alerts,
    ):
        mock_get_state.return_value = {
            "ingestion_admission": {
                "KDEN": {
                    "admitted_to_fetch": False,
                    "skip_reason": "ladder_not_hydrated",
                }
            },
            "last_seen_iso": {},
            "last_obs": {},
        }
        mock_hydration_state.return_value = {"KDEN": {"cache_valid": False}}

        response = self.client.get("/observability/alert-decision-trace?station=kden")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["execution_mode"], "observability")
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["terminal_state"], "BLOCKED_INGESTION_ADMISSION")
        self.assertEqual(payload["signal_type"], "near_boundary_momentum_up")
        self.assertIn("cooldown_state", payload)
        self.assertEqual(len(payload["decision_chain"]), 1)
        self.assertEqual(payload["decision_chain"][0]["stage"], "INGESTION_ADMISSION")
        self.assertEqual(payload["decision_chain"][0]["status"], "BLOCK")

    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_state")
    def test_no_observation_blocks_at_observation_present(
        self,
        mock_get_state,
        mock_hydration_state,
        _mock_transition_history,
        _mock_latest_eval,
        _mock_recent_alerts,
    ):
        mock_get_state.return_value = {
            "ingestion_admission": {
                "KDEN": {"admitted_to_fetch": True}
            },
            "last_seen_iso": {},
            "last_obs": {},
        }
        mock_hydration_state.return_value = {"KDEN": {"cache_valid": True}}

        response = self.client.get("/observability/alert-decision-trace?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["terminal_state"], "BLOCKED_NO_OBSERVATION")
        self.assertEqual(len(payload["decision_chain"]), 2)
        self.assertEqual(payload["decision_chain"][1]["stage"], "OBSERVATION_PRESENT")
        self.assertEqual(payload["decision_chain"][1]["status"], "BLOCK")

    @patch("app.get_recent_alerts", return_value=[])
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={"KDEN": {"latest_evaluation_outcome": "NO_ELIGIBLE_MARKET"}},
    )
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up"}])
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_state")
    def test_settlement_exists_but_no_market_blocks_at_market_match(
        self,
        mock_get_state,
        mock_hydration_state,
        _mock_transition_history,
        _mock_latest_eval,
        _mock_recent_alerts,
    ):
        mock_get_state.return_value = {
            "ingestion_admission": {
                "KDEN": {"admitted_to_fetch": True}
            },
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 70.0}},
        }
        mock_hydration_state.return_value = {"KDEN": {"cache_valid": True}}

        response = self.client.get("/observability/alert-decision-trace?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["terminal_state"], "BLOCKED_NO_MARKET_MATCH")
        self.assertEqual(payload["decision_chain"][-1]["stage"], "MARKET_MATCH")
        self.assertEqual(payload["decision_chain"][-1]["status"], "BLOCK")

    @patch("app.get_recent_alerts", return_value=[])
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={
            "KDEN": {
                "latest_evaluation_outcome": "SUPPRESSED_RATE_LIMIT",
                "latest_suppression_reason": "RATE_LIMIT",
            }
        },
    )
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up"}])
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_state")
    def test_suppressed_alert_blocks_at_suppression_gate(
        self,
        mock_get_state,
        mock_hydration_state,
        _mock_transition_history,
        _mock_latest_eval,
        _mock_recent_alerts,
    ):
        mock_get_state.return_value = {
            "ingestion_admission": {
                "KDEN": {"admitted_to_fetch": True}
            },
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 70.0}},
        }
        mock_hydration_state.return_value = {"KDEN": {"cache_valid": True}}

        response = self.client.get("/observability/alert-decision-trace?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["terminal_state"], "BLOCKED_SUPPRESSED")
        self.assertEqual(payload["decision_chain"][-1]["stage"], "SUPPRESSION_GATE")
        self.assertEqual(payload["decision_chain"][-1]["status"], "BLOCK")

    @patch("app.get_recent_alerts", return_value=[{"station": "KDEN"}])
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={"KDEN": {"latest_evaluation_outcome": "ALERT_SENT"}},
    )
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up"}])
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app.get_state")
    def test_successful_alertable_path_returns_alertable_terminal_state(
        self,
        mock_get_state,
        mock_hydration_state,
        _mock_transition_history,
        _mock_latest_eval,
        _mock_recent_alerts,
    ):
        mock_get_state.return_value = {
            "ingestion_admission": {
                "KDEN": {"admitted_to_fetch": True}
            },
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 70.0}},
        }
        mock_hydration_state.return_value = {"KDEN": {"cache_valid": True}}

        response = self.client.get("/observability/alert-decision-trace?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["terminal_state"], "ALERTABLE")
        self.assertEqual(len(payload["decision_chain"]), 6)
        self.assertTrue(all(stage["status"] == "PASS" for stage in payload["decision_chain"]))


if __name__ == "__main__":
    unittest.main()
