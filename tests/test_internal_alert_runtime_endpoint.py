import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class InternalAlertRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=False)
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_signal_runtime", return_value={"KDEN": {"signal_type": None, "suppression_reason": "NO_SIGNAL_CONDITION_MATCH", "cooldown_state": {}}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}})
    @patch(
        "app.get_state",
        return_value={
            "ingestion_admission": {"KDEN": {"admitted_to_fetch": True}},
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 55.0}},
        },
    )
    def test_scheduler_stopped_reported(self, *_mocks):
        response = self.client.get("/observability/internal-alert-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["scheduler_running"])
        self.assertIn("signal_type", payload)
        self.assertIn("suppression_reason", payload)
        self.assertIn("cooldown_state", payload)

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": False}})
    @patch(
        "app.get_state",
        return_value={
            "ingestion_admission": {"KDEN": {"admitted_to_fetch": False, "skip_reason": "ladder_not_hydrated"}},
            "last_seen_iso": {},
            "last_obs": {},
        },
    )
    def test_hydration_missing_classified_as_hydration(self, *_mocks):
        response = self.client.get("/observability/internal-alert-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["first_blocking_stage"], "INGESTION_ADMISSION")
        self.assertEqual(payload["diagnostic_class"], "HYDRATION")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}})
    @patch(
        "app.get_state",
        return_value={
            "ingestion_admission": {"KDEN": {"admitted_to_fetch": True}},
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 55.0}},
        },
    )
    def test_no_transition_classified_as_transition(self, *_mocks):
        response = self.client.get("/observability/internal-alert-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["first_blocking_stage"], "SETTLEMENT_TRANSITION")
        self.assertEqual(payload["diagnostic_class"], "TRANSITION")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_recent_alerts", return_value=[])
    @patch(
        "app.get_latest_station_market_evaluation_context",
        return_value={"KDEN": {"latest_evaluation_outcome": "SUPPRESSED_RATE_LIMIT", "latest_suppression_reason": "RATE_LIMIT"}},
    )
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up"}])
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}})
    @patch(
        "app.get_state",
        return_value={
            "ingestion_admission": {"KDEN": {"admitted_to_fetch": True}},
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 55.0}},
        },
    )
    def test_suppressed_outcome_classified_as_suppression(self, *_mocks):
        response = self.client.get("/observability/internal-alert-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["first_blocking_stage"], "SUPPRESSION_GATE")
        self.assertEqual(payload["diagnostic_class"], "SUPPRESSION")

    @patch("app.kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_recent_alerts", return_value=[{"station": "KDEN", "sent_utc": "2026-01-01T12:00:00Z"}])
    @patch("app.get_latest_station_market_evaluation_context", return_value={"KDEN": {"latest_evaluation_outcome": "ALERT_SENT"}})
    @patch("app.get_transition_history", return_value=[{"transition_type": "settlement_up"}])
    @patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}})
    @patch(
        "app.get_state",
        return_value={
            "ingestion_admission": {"KDEN": {"admitted_to_fetch": True}},
            "last_seen_iso": {"KDEN": "2026-01-01T10:00:00Z"},
            "last_obs": {"KDEN": {"temp_f": 55.0}},
        },
    )
    def test_alert_emitted_has_no_diagnostic_block(self, *_mocks):
        response = self.client.get("/observability/internal-alert-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["first_blocking_stage"], "NONE")
        self.assertEqual(payload["diagnostic_class"], "NONE")


if __name__ == "__main__":
    unittest.main()
