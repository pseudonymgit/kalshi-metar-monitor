import unittest
from unittest.mock import patch

import app as app_module
from core import metar_monitor


class AlertReviewDiagnosticsBuilderTests(unittest.TestCase):
    @patch("core.metar_monitor.get_transition_history")
    def test_alert_review_diagnostics_includes_breakdowns_and_summary(self, mock_history):
        mock_history.return_value = [
            {
                "station": "KDEN",
                "timestamp_utc": "2026-01-01T10:00:00Z",
                "transition_type": "settlement_up",
                "instant_bucket_before": 69,
                "instant_bucket_after": 70,
                "settlement_bucket": 70,
                "running_max": 70.2,
                "alerts_sent": 0,
                "evaluation_outcome": "NO_ELIGIBLE_MARKET",
                "suppression_reason": "LADDER_HYDRATION_WARMUP",
                "market_eligibility_runtime": {
                    "markets_considered_count": 5,
                    "eligible_markets_count": 0,
                    "rejection_breakdown": {
                        "directional_strike_rejected": 3,
                        "settlement_mismatch": 1,
                        "wrong_series": 1,
                        "expired_market": 0,
                        "unknown_reason": 0,
                    },
                },
            },
            {
                "station": "KSEA",
                "timestamp_utc": "2026-01-01T10:02:00Z",
                "transition_type": "instant_up",
                "instant_bucket_before": 61,
                "instant_bucket_after": 62,
                "settlement_bucket": 62,
                "running_max": 62.1,
                "alerts_sent": 1,
                "evaluation_outcome": "ALERT_SENT",
                "suppression_reason": "",
                "metadata": {
                    "market_eligibility_runtime": {
                        "markets_considered_count": 2,
                        "eligible_markets_count": 1,
                        "rejection_breakdown": {
                            "directional_strike_rejected": 1,
                            "settlement_mismatch": 0,
                            "wrong_series": 0,
                            "expired_market": 0,
                            "unknown_reason": 0,
                        },
                    }
                },
            },
            {
                "station": "KMIA",
                "timestamp_utc": "2026-01-01T10:03:00Z",
                "transition_type": "instant_down",
                "instant_bucket_before": 80,
                "instant_bucket_after": 79,
                "settlement_bucket": 81,
                "running_max": 81.0,
                "alerts_sent": 0,
                "evaluation_outcome": "SUPPRESSED_MARKET_RULE",
                "suppression_reason": "RATE_LIMIT",
                "market_eligibility_runtime": {
                    "markets_considered_count": 0,
                    "eligible_markets_count": 0,
                    "rejection_breakdown": {},
                },
            },
        ]

        payload = metar_monitor.get_alert_review_diagnostics(limit=50)

        self.assertEqual(payload["summary"]["total_transitions"], 3)
        self.assertEqual(payload["summary"]["transitions_with_markets"], 2)
        self.assertEqual(payload["summary"]["transitions_without_markets"], 1)
        self.assertEqual(payload["summary"]["suppressed_directional_strike"], 1)
        self.assertEqual(payload["summary"]["suppressed_market_rules"], 1)
        self.assertEqual(payload["summary"]["suppressed_hydration"], 0)
        self.assertEqual(payload["summary"]["alerts_emitted"], 1)
        self.assertEqual(payload["market_statistics"]["avg_markets_considered"], 7 / 3)
        self.assertEqual(payload["market_statistics"]["avg_eligible_markets"], 1 / 3)
        self.assertEqual(payload["market_statistics"]["avg_rejected_markets"], 2.0)

        first = payload["transition_samples"][0]
        self.assertEqual(first["station"], "KDEN")
        self.assertEqual(first["markets_considered_count"], 5)
        self.assertEqual(first["rejected_markets_count"], 5)
        self.assertEqual(first["market_evaluation_context"]["eligible_markets_count"], 0)
        self.assertEqual(first["rejection_breakdown"]["directional_strike_rejected"], 3)
        self.assertEqual(first["decision_outcome"]["runtime_suppression_reason"], "LADDER_HYDRATION_WARMUP")
        self.assertEqual(first["decision_outcome"]["diagnostic_suppression_reason"], "LADDER_HYDRATION_WARMUP")

    @patch("core.metar_monitor.get_transition_history", return_value=[])
    def test_alert_review_diagnostics_empty(self, _mock_history):
        payload = metar_monitor.get_alert_review_diagnostics(limit="bad")

        self.assertEqual(payload["summary"]["total_transitions"], 0)
        self.assertEqual(payload["summary"]["suppressed_hydration"], 0)
        self.assertEqual(payload["market_statistics"]["avg_markets_considered"], 0.0)
        self.assertEqual(payload["transition_samples"], [])


class AlertReviewDiagnosticsEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    @patch("app.get_alert_review_diagnostics")
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={"stations": {}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    def test_alert_review_bundle_endpoint_empty_payload(
        self,
        _mock_transitions,
        _mock_evals,
        _mock_hydration,
        _mock_alerts,
        mock_diagnostics,
    ):
        mock_diagnostics.return_value = {
            "summary": {
                "total_transitions": 0,
                "alerts_emitted": 0,
                "transitions_with_markets": 0,
                "transitions_without_markets": 0,
                "suppressed_directional_strike": 0,
                "suppressed_market_rules": 0,
                "suppressed_hydration": 0,
            },
            "market_statistics": {
                "avg_markets_considered": 0.0,
                "avg_eligible_markets": 0.0,
                "avg_rejected_markets": 0.0,
                "directional_strike_rejections": 0,
                "settlement_mismatch_rejections": 0,
                "wrong_series_rejections": 0,
                "expired_market_rejections": 0,
            },
            "transition_samples": [],
        }

        response = self.client.get("/diagnostics/alert_review_bundle")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload, mock_diagnostics.return_value)
        self.assertEqual(payload["summary"]["suppressed_hydration"], 0)

    @patch("app.get_alert_review_diagnostics")
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={"stations": {}})
    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.get_transition_history", return_value=[])
    def test_alert_review_bundle_endpoint_aggregates_from_diagnostics_rows(
        self,
        _mock_transitions,
        _mock_evals,
        _mock_hydration,
        _mock_alerts,
        mock_diagnostics,
    ):
        mock_diagnostics.return_value = {
            "summary": {
                "total_transitions": 2,
                "alerts_emitted": 1,
                "transitions_with_markets": 1,
                "transitions_without_markets": 1,
                "suppressed_directional_strike": 0,
                "suppressed_market_rules": 1,
                "suppressed_hydration": 0,
            },
            "market_statistics": {
                "avg_markets_considered": 2.0,
                "avg_eligible_markets": 0.5,
                "avg_rejected_markets": 1.5,
                "directional_strike_rejections": 1,
                "settlement_mismatch_rejections": 1,
                "wrong_series_rejections": 1,
                "expired_market_rejections": 0,
            },
            "transition_samples": [
                {
                    "station": "KDEN",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "transition_type": "instant_up",
                    "instant_bucket_before": 60,
                    "instant_bucket_after": 61,
                    "settlement_bucket": 61,
                    "markets_considered_count": 4,
                    "eligible_markets_count": 1,
                    "rejected_markets_count": 3,
                    "rejection_breakdown": {
                        "directional_strike_rejected": 1,
                        "settlement_mismatch": 1,
                        "wrong_series": 1,
                        "expired_market": 0,
                    },
                    "decision_outcome": {
                        "alerts_sent": 0,
                        "evaluation_outcome": "NO_ELIGIBLE_MARKET",
                        "runtime_suppression_reason": "",
                        "diagnostic_suppression_reason": "MARKET_RULES",
                    },
                },
                {
                    "station": "KDEN",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "transition_type": "instant_up",
                    "instant_bucket_before": 61,
                    "instant_bucket_after": 62,
                    "settlement_bucket": 62,
                    "markets_considered_count": 0,
                    "eligible_markets_count": 0,
                    "rejected_markets_count": 0,
                    "rejection_breakdown": {
                        "directional_strike_rejected": 0,
                        "settlement_mismatch": 0,
                        "wrong_series": 0,
                        "expired_market": 0,
                    },
                    "decision_outcome": {
                        "alerts_sent": 0,
                        "evaluation_outcome": "UNKNOWN",
                        "runtime_suppression_reason": "",
                        "diagnostic_suppression_reason": "HYDRATION_NOT_READY",
                    },
                },
            ],
        }

        response = self.client.get("/diagnostics/alert_review_bundle?station=KDEN&limit=2")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["suppressed_hydration"], 0)
        self.assertEqual(payload["market_statistics"]["avg_markets_considered"], 2.0)
        self.assertEqual(payload["market_statistics"]["avg_eligible_markets"], 0.5)
        self.assertEqual(payload["market_statistics"]["avg_rejected_markets"], 1.5)
        self.assertEqual(payload["market_statistics"]["directional_strike_rejections"], 1)
        self.assertEqual(payload["market_statistics"]["settlement_mismatch_rejections"], 1)
        self.assertEqual(payload["market_statistics"]["wrong_series_rejections"], 1)
        self.assertEqual(payload["market_statistics"]["expired_market_rejections"], 0)
        self.assertEqual(
            payload["transition_samples"][1]["decision_outcome"]["diagnostic_suppression_reason"],
            "HYDRATION_NOT_READY",
        )


if __name__ == "__main__":
    unittest.main()
