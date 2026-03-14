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
                        "outside_price_band": 3,
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
                            "outside_price_band": 1,
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

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_transitions"], 3)
        self.assertEqual(payload["summary"]["transitions_with_markets"], 2)
        self.assertEqual(payload["summary"]["transitions_without_markets"], 1)
        self.assertEqual(payload["summary"]["transitions_suppressed_price_band"], 1)
        self.assertEqual(payload["summary"]["transitions_suppressed_other_rules"], 1)
        self.assertEqual(payload["summary"]["transitions_emitted_alert"], 1)

        first = payload["transitions"][0]
        self.assertEqual(first["station"], "KDEN")
        self.assertEqual(first["markets_discovered_count"], 5)
        self.assertEqual(first["markets_inside_price_band"], 2)
        self.assertEqual(first["markets_after_settlement_filter"], 1)
        self.assertEqual(first["markets_after_all_rules"], 0)

    @patch("core.metar_monitor.get_transition_history", return_value=[])
    def test_alert_review_diagnostics_empty(self, _mock_history):
        payload = metar_monitor.get_alert_review_diagnostics(limit="bad")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["summary"]["total_transitions"], 0)
        self.assertEqual(payload["transitions"], [])


class AlertReviewDiagnosticsEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    @patch("app.get_alert_review_diagnostics")
    def test_alert_review_endpoint_returns_collector_payload(self, mock_collector):
        mock_collector.return_value = {"ok": True, "summary": {}, "transitions": []}

        response = self.client.get("/diagnostics/alert_review?limit=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ok"], True)
        mock_collector.assert_called_once_with(limit=20)


if __name__ == "__main__":
    unittest.main()
