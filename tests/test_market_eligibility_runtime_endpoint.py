import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class MarketEligibilityRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_station_required(self):
        response = self.client.get("/observability/market-eligibility-runtime")
        payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "station query param required")

    @patch("app.get_transition_history", return_value=[{"settlement_bucket": 61}])
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_returns_latest_market_eligibility_runtime_details(
        self,
        _mock_scheduler,
        _mock_execution_domain,
        mock_latest_eval,
        _mock_transition_history,
    ):
        mock_latest_eval.return_value = {
            "KDEN": {
                "latest_evaluation_outcome": "NO_ELIGIBLE_MARKET",
                "latest_suppression_reason": None,
                "market_eligibility_runtime": {
                    "markets_considered_count": 12,
                    "eligible_markets_count": 3,
                    "rejected_markets_count": 9,
                    "rejection_breakdown": {
                        "outside_price_band": 2,
                        "wrong_series": 1,
                        "expired_market": 3,
                        "settlement_mismatch": 2,
                        "unknown_reason": 1,
                    },
                },
            }
        }

        response = self.client.get("/observability/market-eligibility-runtime?station=kden")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["execution_domain"], "production")
        self.assertEqual(payload["latest_settlement_integer"], 61)
        self.assertEqual(payload["markets_considered_count"], 12)
        self.assertEqual(payload["eligible_markets_count"], 3)
        self.assertEqual(payload["rejected_markets_count"], 9)
        self.assertEqual(
            payload["rejection_breakdown"],
            {
                "outside_price_band": 2,
                "wrong_series": 1,
                "expired_market": 3,
                "settlement_mismatch": 2,
                "unknown_reason": 1,
            },
        )
        self.assertEqual(payload["latest_evaluation_outcome"], "NO_ELIGIBLE_MARKET")
        self.assertIsNone(payload["latest_suppression_reason"])


if __name__ == "__main__":
    unittest.main()
