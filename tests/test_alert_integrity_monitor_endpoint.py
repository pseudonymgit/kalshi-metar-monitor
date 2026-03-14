import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class AlertIntegrityMonitorEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.hydration_queue_snapshot", return_value={"queue": ["KDEN"], "queue_depth": 1, "queued_stations": ["KDEN"], "backoff_until": {"KDEN": 120.0}, "backoff_stations": ["KDEN"], "stations_in_backoff": 1, "next_backoff_expiry": 120.0, "last_hydration_request_ts": 10.0})
    @patch("app.compute_system_health_snapshot", return_value={"hydration": {"reason": "hydration_cache_not_written"}})
    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": False}})
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
        _mock_hydration_execution,
        _mock_transition_runtime,
        _mock_fire_audit,
        _mock_system_health,
        _mock_hydration_queue,
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

        response = self.client.get("/integrity/alert_pipeline?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        finding_types = {row["finding_type"] for row in payload["findings"]}
        self.assertIn("ALERT_PIPELINE_GAP", finding_types)
        self.assertIn("TRANSITION_WITHOUT_EVALUATION", finding_types)
        self.assertIn("SUPPRESSION_WITHOUT_REASON", finding_types)
        self.assertIn("HYDRATION_DRIFT", finding_types)
        self.assertIn("MARKET_DISCOVERY_REGRESSION", finding_types)
        self.assertIn("STATION_ALERT_SILENCE", finding_types)
        self.assertIn("HYDRATION_STALL_CONDITION", finding_types)
        self.assertTrue(payload["hydration_stall_signal"]["hydration_stall_condition"])
        self.assertEqual(payload["hydration_queue"]["stations_in_backoff"], 1)


    @patch("app.hydration_queue_snapshot", return_value={"queue": [], "queue_depth": 0, "queued_stations": [], "backoff_until": {}, "backoff_stations": [], "stations_in_backoff": 0, "next_backoff_expiry": None, "last_hydration_request_ts": 0.0})
    @patch("app.compute_system_health_snapshot", return_value={"hydration": {"reason": "hydration_ready"}})
    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": True}})
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app.get_transition_history")
    @patch("app._build_runtime_authority_hydration_snapshot")
    @patch("app._canonical_live_station_universe")
    def test_integrity_endpoint_treats_market_rule_suppression_as_reasoned(
        self,
        mock_station_universe,
        mock_hydration,
        mock_transitions,
        mock_evals,
        _mock_alerts,
        _mock_hydration_execution,
        _mock_transition_runtime,
        _mock_fire_audit,
        _mock_system_health,
        _mock_hydration_queue,
    ):
        mock_station_universe.return_value = {
            "stations": ["KDEN"],
            "configured_stations": {"KDEN"},
            "discovered_stations": {"KDEN"},
            "watchlist_stations": set(),
        }
        mock_transitions.return_value = [{"station": "KDEN", "timestamp": "2030-01-01T00:00:00+00:00"}]
        mock_evals.return_value = {
            "KDEN": {
                "latest_evaluation_timestamp_utc": "2030-01-01T00:00:01+00:00",
                "latest_evaluation_outcome": "SUPPRESSED_MARKET_RULE",
                "latest_suppression_reason": "",
            }
        }
        mock_hydration.return_value = {
            "stations": {
                "KDEN": {
                    "cache_present": True,
                    "hydration_prerequisite": {
                        "series_discovered": True,
                        "cache_valid": True,
                    },
                }
            }
        }

        response = self.client.get("/integrity/alert_pipeline?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        finding_types = {row["finding_type"] for row in payload["findings"]}
        self.assertNotIn("SUPPRESSION_WITHOUT_REASON", finding_types)

    @patch("app.hydration_queue_snapshot", return_value={"queue": [], "queue_depth": 0, "queued_stations": [], "backoff_until": {}, "backoff_stations": [], "stations_in_backoff": 0, "next_backoff_expiry": None, "last_hydration_request_ts": 0.0})

    @patch("app.hydration_queue_snapshot", return_value={"queue": [], "queue_depth": 0, "queued_stations": [], "backoff_until": {}, "backoff_stations": [], "stations_in_backoff": 0, "next_backoff_expiry": None, "last_hydration_request_ts": 0.0})
    @patch("app.compute_system_health_snapshot", return_value={"hydration": {"reason": "hydration_ready"}})
    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}, {"station": "KSEA", "alerts_sent_today": 4}]})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 2})
    @patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": True}})
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"latest_evaluation_timestamp_utc": "2030-01-01T00:00:01+00:00", "latest_evaluation_outcome": "SUPPRESSED_MARKET_RULE", "latest_suppression_reason": ""}})
    @patch("app.get_transition_history", return_value=[{"station": "KDEN", "timestamp": "2030-01-01T00:00:00+00:00"}])
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={"stations": {"KDEN": {"cache_present": True, "hydration_prerequisite": {"series_discovered": True, "cache_valid": True}}}})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"], "configured_stations": {"KDEN"}, "discovered_stations": {"KDEN"}, "watchlist_stations": set()})
    def test_integrity_endpoint_station_filter_does_not_double_count_alerts_sent_today(self, *_mocks):
        response = self.client.get("/integrity/alert_pipeline?station=KDEN")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["hydration_stall_signal"]["alerts_sent_today"], 1)

    @patch("app.compute_system_health_snapshot", return_value={"hydration": {"reason": "hydration_ready"}})
    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}]})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": True}})
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
        self.assertFalse(payload["hydration_stall_signal"]["hydration_stall_condition"])
        self.assertEqual(payload["hydration_queue"]["stations_in_backoff"], 0)

    @patch("app.compute_system_health_snapshot", return_value={"hydration": {"reason": "hydration_ready"}})
    @patch("app._build_alert_fire_audit_rows", return_value={"stations": [{"station": "KDEN", "alerts_sent_today": 1}]})
    @patch("app._get_transition_runtime_summary", return_value={"transitions_seen_today": 1})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"], "configured_stations": {"KDEN"}, "discovered_stations": {"KDEN"}, "watchlist_stations": set()})
    def test_integrity_endpoint_is_read_only_for_runtime_snapshots(self, *_mocks):
        transitions = [{"station": "KDEN", "timestamp": "2030-01-01T00:00:00+00:00"}]
        recent_alerts = [{"station": "KDEN", "created_utc": "2030-01-01T00:00:05+00:00"}]
        hydration_snapshot = {"stations": {"KDEN": {"cache_present": True, "hydration_prerequisite": {"series_discovered": True, "cache_valid": True}}}}
        hydration_queue = {
            "queue": ["KDEN"],
            "queue_depth": 1,
            "queued_stations": ["KDEN"],
            "backoff_until": {"KDEN": 200.0},
            "backoff_stations": ["KDEN"],
            "stations_in_backoff": 1,
            "next_backoff_expiry": 200.0,
            "last_hydration_request_ts": 5.0,
        }

        with (
            patch("app.get_transition_history", return_value=transitions),
            patch("app.get_recent_alerts", return_value=recent_alerts),
            patch("app._build_runtime_authority_hydration_snapshot", return_value=hydration_snapshot),
            patch(
                "app.get_latest_station_market_evaluation_context",
                return_value={
                    "KDEN": {
                        "latest_evaluation_timestamp_utc": "2030-01-01T00:00:01+00:00",
                        "latest_evaluation_outcome": "ALERT_EMITTED",
                        "latest_suppression_reason": "",
                    }
                },
            ),
            patch("app.hydration_queue_snapshot", return_value=hydration_queue),
            patch("app.get_last_hydration_execution_snapshot", return_value={"KDEN": {"cache_written": True}}),
            patch("app._build_ingestion_health_rows", return_value={"stations": []}),
        ):
            before = {
                "transitions": [dict(row) for row in transitions],
                "recent_alerts": [dict(row) for row in recent_alerts],
                "hydration_snapshot": {"stations": {k: dict(v) for k, v in hydration_snapshot["stations"].items()}},
                "hydration_queue": {
                    **hydration_queue,
                    "queue": list(hydration_queue["queue"]),
                    "queued_stations": list(hydration_queue["queued_stations"]),
                    "backoff_until": dict(hydration_queue["backoff_until"]),
                    "backoff_stations": list(hydration_queue["backoff_stations"]),
                },
            }

            response = self.client.get("/integrity/alert_pipeline?station=KDEN")
            self.assertEqual(response.status_code, 200)

            self.assertEqual(transitions, before["transitions"])
            self.assertEqual(recent_alerts, before["recent_alerts"])
            self.assertEqual(hydration_snapshot, before["hydration_snapshot"])
            self.assertEqual(hydration_queue, before["hydration_queue"])



if __name__ == "__main__":
    unittest.main()
