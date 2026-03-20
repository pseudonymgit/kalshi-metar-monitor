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


class AlertPathTruthClassificationTests(unittest.TestCase):
    def setUp(self):
        self._transition_history = copy.deepcopy(metar_monitor._TRANSITION_HISTORY)
        self._kalshi_last_call_ts = copy.deepcopy(metar_monitor._KALSHI_LAST_CALL_TS)
        app_module._autostart_fallback_done = True

    def tearDown(self):
        with metar_monitor._TRANSITION_LOCK:
            metar_monitor._TRANSITION_HISTORY.clear()
            metar_monitor._TRANSITION_HISTORY.extend(copy.deepcopy(self._transition_history))
        with metar_monitor._KALSHI_RATE_LIMIT_LOCK:
            metar_monitor._KALSHI_LAST_CALL_TS.clear()
            metar_monitor._KALSHI_LAST_CALL_TS.update(copy.deepcopy(self._kalshi_last_call_ts))

    @patch("core.kalshi_monitor.send_composed_weather_market_alert")
    @patch("core.kalshi_monitor.process_ladder_transition")
    @patch("core.kalshi_monitor.hydration_queue_snapshot")
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot")
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.kalshi_monitor.classify_proximity")
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache")
    @patch("core.kalshi_monitor._parse_target_market_types")
    @patch("core.kalshi_monitor._get_active_stations")
    def test_send_alert_records_specific_reason_for_eligible_non_alertable_market(
        self,
        mock_get_active_stations,
        mock_parse_target_market_types,
        mock_build_structured_snapshot_from_cache,
        mock_classify_proximity,
        mock_enqueue_station_hydration,
        mock_get_hydration_prerequisite_state_snapshot,
        mock_hydration_queue_snapshot,
        mock_process_ladder_transition,
        mock_send_composed_weather_market_alert,
    ):
        del mock_enqueue_station_hydration
        del mock_hydration_queue_snapshot
        mock_get_active_stations.return_value = {"KDEN"}
        mock_parse_target_market_types.return_value = {"HIGH"}
        mock_build_structured_snapshot_from_cache.return_value = {
            "markets": [
                {
                    "event_ticker": "KXHIGHDEN-26MAR19",
                    "series_ticker": "KXHIGHDEN",
                    "strike": 70,
                }
            ],
            "raw_market_count": 1,
            "filtered_market_count": 1,
            "rejection_counts": {},
            "cache_written": True,
        }
        mock_classify_proximity.return_value = "FAR"
        mock_get_hydration_prerequisite_state_snapshot.return_value = {
            "KDEN": {"cache_valid": True}
        }
        mock_process_ladder_transition.return_value = {
            "should_alert": False,
            "reason": None,
            "bucket_index": 0,
            "direction": None,
            "terminal_state_blocked": False,
            "outcome_hint": "ELIGIBLE_NOT_ALERTABLE",
        }
        mock_send_composed_weather_market_alert.return_value = None

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
                    "transition_event_id": 654,
                    "metadata": {"obs_time": "2026-03-19T09:59:00Z"},
                }
            )

        payload = {
            "station": "KDEN",
            "summary": {"temp_f": 70.2},
            "legacy": {
                "temp_f": 70.2,
                "obs_time": "2026-03-19T09:59:00Z",
                "instant_bucket_changed": True,
                "transition_correlation": {
                    "transition_event_id": 654,
                    "timestamp_utc": "2026-03-19T10:00:00Z",
                },
            },
        }

        result = metar_monitor._send_alert("https://example.test/webhook", payload)
        latest_transition = metar_monitor.get_transition_history(station="KDEN", limit=1)[0]
        alert_path_truth = latest_transition.get("alert_path_truth") or {}

        self.assertFalse(result["delivery_attempted"])
        self.assertEqual(latest_transition.get("evaluation_outcome"), "SUPPRESSED_ELIGIBLE_NOT_ALERTABLE")
        self.assertEqual(latest_transition.get("suppression_reason"), "ELIGIBLE_NOT_ALERTABLE")
        self.assertEqual(alert_path_truth["blocking_stage"], "suppression_gate")
        self.assertEqual(
            alert_path_truth["suppression_or_non_emission_reason"],
            "ELIGIBLE_NOT_ALERTABLE",
        )
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
