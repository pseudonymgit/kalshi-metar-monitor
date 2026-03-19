import copy
import unittest
from unittest.mock import patch

import app as app_module
from core import metar_monitor


app = app_module.app


class AlertPathTruthPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._transition_history = copy.deepcopy(metar_monitor._TRANSITION_HISTORY)
        app_module._autostart_fallback_done = True

    def tearDown(self):
        with metar_monitor._TRANSITION_LOCK:
            metar_monitor._TRANSITION_HISTORY.clear()
            metar_monitor._TRANSITION_HISTORY.extend(copy.deepcopy(self._transition_history))

    def test_send_alert_records_config_block_on_correlated_transition(self):
        with metar_monitor._TRANSITION_LOCK:
            metar_monitor._TRANSITION_HISTORY.clear()
            metar_monitor._TRANSITION_HISTORY.append(
                {
                    "station": "KDEN",
                    "transition_type": "settlement_up",
                    "instant_bucket_before": 69,
                    "instant_bucket_after": 70,
                    "settlement_bucket": 70,
                    "running_max": 70.2,
                    "current_temp": 70.2,
                    "timestamp_utc": "2026-03-19T10:00:00Z",
                    "transition_event_id": 321,
                    "metadata": {"obs_time": "2026-03-19T09:59:00Z"},
                }
            )

        payload = {
            "station": "KDEN",
            "summary": {"temp_f": 70.2},
            "legacy": {
                "temp_f": 70.2,
                "obs_time": "2026-03-19T09:59:00Z",
                "transition_correlation": {
                    "transition_event_id": 321,
                    "timestamp_utc": "2026-03-19T10:00:00Z",
                },
            },
        }

        result = metar_monitor._send_alert("", payload)
        latest_transition = metar_monitor.get_transition_history(station="KDEN", limit=1)[0]
        alert_path_truth = latest_transition.get("alert_path_truth") or {}

        self.assertEqual(result["delivery_blocking_stage"], "config")
        self.assertEqual(result["delivery_blocking_reason"], "WEBHOOK_MISSING")
        self.assertTrue(alert_path_truth["send_alert_entered"])
        self.assertEqual(alert_path_truth["blocking_stage"], "config")
        self.assertEqual(alert_path_truth["suppression_or_non_emission_reason"], "WEBHOOK_MISSING")
        self.assertFalse(alert_path_truth["webhook_send_attempted"])
        self.assertFalse(alert_path_truth["webhook_send_succeeded"])
        self.assertFalse(alert_path_truth["webhook_send_failed"])


class AlertPathTruthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_transition_history")
    @patch("app.get_state")
    def test_endpoint_returns_latest_correlated_transition_truth(self, mock_get_state, mock_get_transition_history):
        mock_get_state.return_value = {
            "last_seen_iso": {"KDEN": "2026-03-19T10:05:00Z"},
            "last_obs": {"KDEN": {"temp_f": 70.2, "raw_text": "KDEN 191005Z AUTO ..."}},
        }
        mock_get_transition_history.return_value = [
            {
                "station": "KDEN",
                "transition_type": "settlement_up",
                "timestamp_utc": "2026-03-19T10:06:00Z",
                "transition_event_id": 999,
                "settlement_bucket": 70,
                "metadata": {"obs_time": "2026-03-19T09:59:00Z"},
                "alert_path_truth": {
                    "send_alert_entered": True,
                    "blocking_stage": "suppression_gate",
                    "suppression_or_non_emission_reason": "LADDER_HYDRATION_WARMUP",
                    "webhook_send_attempted": False,
                    "webhook_send_succeeded": False,
                    "webhook_send_failed": False,
                },
            },
            {
                "station": "KDEN",
                "transition_type": "settlement_down",
                "timestamp_utc": "2026-03-19T10:00:00Z",
                "transition_event_id": 321,
                "metadata": {},
            },
        ]

        response = self.client.get("/observability/alert-path-truth?station=kden")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["execution_mode"], "observability")
        self.assertEqual(payload["truth_source"], "persisted_transition_record")
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["latest_accepted_observation"]["observed_at_utc"], "2026-03-19T10:05:00Z")
        self.assertEqual(payload["latest_accepted_observation"]["observation"]["temp_f"], 70.2)
        self.assertEqual(payload["latest_emitted_transition"]["transition_event_id"], 999)
        self.assertEqual(payload["latest_emitted_transition"]["transition_type"], "settlement_up")
        self.assertTrue(payload["send_alert_entered"])
        self.assertEqual(payload["blocking_stage"], "suppression_gate")
        self.assertEqual(payload["suppression_or_non_emission_reason"], "LADDER_HYDRATION_WARMUP")
        self.assertFalse(payload["webhook_send_attempted"])
        self.assertFalse(payload["webhook_send_succeeded"])
        self.assertFalse(payload["webhook_send_failed"])

    @patch("app.get_transition_history")
    @patch("app.get_state")
    def test_endpoint_falls_back_to_latest_transition_runtime_truth(self, mock_get_state, mock_get_transition_history):
        mock_get_state.return_value = {
            "last_seen_iso": {"KDEN": "2026-03-19T10:07:00Z"},
            "last_obs": {"KDEN": {"temp_f": 70.8}},
        }
        mock_get_transition_history.return_value = [
            {
                "station": "KDEN",
                "transition_type": "settlement_up",
                "timestamp_utc": "2026-03-19T10:06:00Z",
                "transition_event_id": 999,
                "metadata": {
                    "evaluation_outcome": "SUPPRESSED_RATE_LIMIT",
                    "suppression_reason": "RATE_LIMIT",
                    "alerts_sent": 0,
                },
            },
            {
                "station": "KDEN",
                "transition_type": "settlement_down",
                "timestamp_utc": "2026-03-19T10:00:00Z",
                "transition_event_id": 321,
                "metadata": {"obs_time": "2026-03-19T09:59:00Z"},
                "alert_path_truth": {
                    "send_alert_entered": True,
                    "blocking_stage": "suppression_gate",
                    "suppression_or_non_emission_reason": "LADDER_HYDRATION_WARMUP",
                    "webhook_send_attempted": False,
                    "webhook_send_succeeded": False,
                    "webhook_send_failed": False,
                },
            },
        ]

        response = self.client.get("/observability/alert-path-truth?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["truth_source"], "runtime_fallback")
        self.assertEqual(payload["latest_emitted_transition"]["transition_event_id"], 999)
        self.assertEqual(payload["latest_emitted_transition"]["transition_type"], "settlement_up")
        self.assertTrue(payload["send_alert_entered"])
        self.assertEqual(payload["blocking_stage"], "suppression_gate")
        self.assertEqual(payload["suppression_or_non_emission_reason"], "RATE_LIMIT")
        self.assertFalse(payload["webhook_send_attempted"])
        self.assertFalse(payload["webhook_send_succeeded"])
        self.assertFalse(payload["webhook_send_failed"])


if __name__ == "__main__":
    unittest.main()
