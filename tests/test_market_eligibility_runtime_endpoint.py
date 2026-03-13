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
    @patch("app.build_structured_snapshot_from_cache")
    @patch("app.get_latest_station_market_evaluation_context")
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_returns_latest_market_eligibility_runtime_details(
        self,
        _mock_scheduler,
        _mock_execution_domain,
        mock_latest_eval,
        mock_cache_snapshot,
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
        mock_cache_snapshot.return_value = {
            "series_ticker": "KXHIGHDEN",
            "raw_market_count": 5,
            "filtered_market_count": 2,
            "pre_directional_market_count": 2,
            "post_directional_market_count": 1,
            "empty_reason": None,
            "rejection_counts": {
                "outside_price_band": 1,
                "wrong_series": 1,
                "expired_market": 1,
                "settlement_mismatch": 0,
                "unknown_reason": 0,
            },
        }

        response = self.client.get("/observability/market-eligibility-runtime?station=kden")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["execution_domain"], "production")
        self.assertEqual(payload["latest_settlement_integer"], 61)
        self.assertEqual(payload["markets_considered_count"], 5)
        self.assertEqual(payload["eligible_markets_count"], 2)
        self.assertEqual(payload["rejected_markets_count"], 3)
        self.assertEqual(
            payload["rejection_breakdown"],
            {
                "outside_price_band": 1,
                "wrong_series": 1,
                "expired_market": 1,
                "settlement_mismatch": 0,
                "unknown_reason": 0,
            },
        )
        self.assertEqual(payload["current_cache_probe"]["station"], "KDEN")
        self.assertEqual(payload["current_cache_probe"]["series_ticker"], "KXHIGHDEN")
        self.assertEqual(payload["current_cache_probe"]["raw_market_count"], 5)
        self.assertEqual(payload["current_cache_probe"]["filtered_market_count"], 2)
        self.assertEqual(payload["current_cache_probe"]["pre_directional_market_count"], 2)
        self.assertEqual(payload["current_cache_probe"]["post_directional_market_count"], 1)
        self.assertIsNone(payload["current_cache_probe"]["empty_reason"])
        self.assertEqual(payload["latest_persisted_evaluation"]["market_eligibility_runtime"]["markets_considered_count"], 12)
        self.assertEqual(payload["latest_persisted_evaluation"]["market_eligibility_runtime"]["eligible_markets_count"], 3)
        self.assertEqual(payload["latest_evaluation_outcome"], "ELIGIBLE_MARKET_PRESENT")
        self.assertIsNone(payload["latest_suppression_reason"])


    @patch("app.get_transition_history", return_value=[{"settlement_bucket": 61}])
    @patch("app.build_structured_snapshot_from_cache", return_value={
        "series_ticker": "KXHIGHDEN",
        "raw_market_count": 7,
        "filtered_market_count": 4,
        "pre_directional_market_count": 4,
        "post_directional_market_count": 1,
        "empty_reason": None,
        "rejection_counts": {},
    })
    @patch("app.get_latest_station_market_evaluation_context", return_value={
        "KDEN": {
            "latest_evaluation_outcome": "NO_ELIGIBLE_MARKET",
            "latest_suppression_reason": None,
            "market_eligibility_runtime": {
                "markets_considered_count": 0,
                "eligible_markets_count": 0,
                "rejected_markets_count": 0,
                "rejection_breakdown": {},
            },
        }
    })
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_prefers_current_cache_probe_over_stale_persisted_counts(self, *_mocks):
        response = self.client.get("/observability/market-eligibility-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["markets_considered_count"], 7)
        self.assertEqual(payload["eligible_markets_count"], 4)
        self.assertEqual(payload["latest_evaluation_outcome"], "ELIGIBLE_MARKET_PRESENT")
        self.assertIsNone(payload["latest_suppression_reason"])
        self.assertEqual(payload["latest_persisted_evaluation"]["market_eligibility_runtime"]["markets_considered_count"], 0)
        self.assertEqual(payload["latest_persisted_evaluation"]["market_eligibility_runtime"]["eligible_markets_count"], 0)

    @patch("app.get_transition_history", return_value=[{"settlement_bucket": 61}])
    @patch("app.build_structured_snapshot_from_cache", return_value={
        "series_ticker": "KXHIGHDEN",
        "raw_market_count": 0,
        "filtered_market_count": 0,
        "pre_directional_market_count": 0,
        "post_directional_market_count": 0,
        "empty_reason": "cache_missing_or_empty",
        "rejection_counts": {},
    })
    @patch("app.get_latest_station_market_evaluation_context", return_value={
        "KDEN": {
            "latest_evaluation_outcome": "ELIGIBLE_MARKET_PRESENT",
            "latest_suppression_reason": None,
            "market_eligibility_runtime": {
                "markets_considered_count": 5,
                "eligible_markets_count": 2,
                "rejected_markets_count": 3,
                "rejection_breakdown": {},
            },
        }
    })
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_live_probe_empty_cache_sets_no_markets_cached(self, *_mocks):
        response = self.client.get("/observability/market-eligibility-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["latest_evaluation_outcome"], "NO_MARKETS_CACHED")
        self.assertEqual(payload["latest_suppression_reason"], "cache_empty")
        self.assertEqual(payload["latest_persisted_evaluation"]["latest_evaluation_outcome"], "ELIGIBLE_MARKET_PRESENT")

    @patch("app.get_transition_history", return_value=[{"settlement_bucket": 61}])
    @patch("app.build_structured_snapshot_from_cache", return_value={
        "series_ticker": "KXHIGHDEN",
        "raw_market_count": 4,
        "filtered_market_count": 0,
        "pre_directional_market_count": 0,
        "post_directional_market_count": 0,
        "empty_reason": "filtered_to_zero",
        "rejection_counts": {},
    })
    @patch("app.get_latest_station_market_evaluation_context", return_value={
        "KDEN": {
            "latest_evaluation_outcome": "ELIGIBLE_MARKET_PRESENT",
            "latest_suppression_reason": None,
            "market_eligibility_runtime": {
                "markets_considered_count": 5,
                "eligible_markets_count": 2,
                "rejected_markets_count": 3,
                "rejection_breakdown": {},
            },
        }
    })
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_live_probe_filtered_zero_sets_no_eligible_market(self, *_mocks):
        response = self.client.get("/observability/market-eligibility-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["latest_evaluation_outcome"], "NO_ELIGIBLE_MARKET")
        self.assertEqual(payload["latest_suppression_reason"], "filtered_to_zero")
        self.assertEqual(payload["latest_persisted_evaluation"]["latest_evaluation_outcome"], "ELIGIBLE_MARKET_PRESENT")


    @patch("app.get_transition_history", return_value=[{"settlement_bucket": 61}])
    @patch("app.build_structured_snapshot_from_cache", return_value={
        "series_ticker": "KXHIGHDEN",
        "raw_market_count": 4,
        "filtered_market_count": 2,
        "pre_directional_market_count": 2,
        "post_directional_market_count": 0,
        "empty_reason": "no_directional_ladder_match",
        "rejection_counts": {},
    })
    @patch("app.get_latest_station_market_evaluation_context", return_value={
        "KDEN": {
            "latest_evaluation_outcome": "ELIGIBLE_MARKET_PRESENT",
            "latest_suppression_reason": None,
            "market_eligibility_runtime": {
                "markets_considered_count": 5,
                "eligible_markets_count": 2,
                "rejected_markets_count": 3,
                "rejection_breakdown": {},
            },
        }
    })
    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    def test_live_probe_directional_collapse_reports_non_ambiguous_reason(self, *_mocks):
        response = self.client.get("/observability/market-eligibility-runtime?station=KDEN")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["eligible_markets_count"], 2)
        self.assertEqual(payload["current_cache_probe"]["post_directional_market_count"], 0)
        self.assertEqual(payload["current_cache_probe"]["empty_reason"], "no_directional_ladder_match")
        self.assertEqual(payload["latest_evaluation_outcome"], "NO_DIRECTIONAL_LADDER_MATCH")
        self.assertEqual(payload["latest_suppression_reason"], "no_directional_ladder_match")



if __name__ == "__main__":
    unittest.main()
