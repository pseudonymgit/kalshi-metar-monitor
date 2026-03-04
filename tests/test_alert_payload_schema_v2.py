import os
import unittest
from unittest.mock import patch

from core import kalshi_monitor


class AlertPayloadSchemaV2Tests(unittest.TestCase):
    @patch("core.kalshi_monitor.requests.post")
    @patch("core.kalshi_monitor.get_last_hydration_execution_snapshot")
    @patch("core.kalshi_monitor._load_current_epoch_context")
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache")
    def test_composed_alert_payload_includes_schema_v2_sections(
        self,
        mock_snapshot,
        mock_epoch_context,
        mock_hydration,
        mock_post,
    ):
        os.environ["ALERT_WEBHOOK_URL"] = "https://example.com/webhook"

        mock_snapshot.return_value = {
            "markets": [
                {
                    "ticker": "KXHIGHAUS-26DEC31-B82",
                    "strike": 82,
                    "strike_type": "between",
                    "floor_strike": 82,
                    "cap_strike": 83,
                    "event_ticker": "KXHIGHAUS-26DEC31",
                }
            ],
            "observed": {"current_temp_f": 82.4},
            "market_types": ["HIGH"],
        }
        mock_epoch_context.return_value = {
            "settlement_bucket": 82,
            "prior_settlement_bucket": 81,
            "settlement_jump_magnitude": 1,
            "epoch_status": "open",
            "reversion_occurred": False,
            "first_reversion_timestamp_utc": None,
            "max_excursion_above_settlement": 0.0,
            "terminal_state_reached": False,
        }
        mock_hydration.return_value = {
            "KAUS": {
                "evaluated_at_utc": "2026-01-01T12:00:00Z",
                "series_ticker": "KXHIGHAUS",
                "raw_market_count": 3,
                "filtered_market_count": 2,
                "rejection_counts": {
                    "city_token_mismatch": 1,
                    "market_type_mismatch": 0,
                    "inactive_market": 0,
                    "date_mismatch": 0,
                },
                "cache_written": True,
            }
        }

        class Response:
            status_code = 204

        mock_post.return_value = Response()

        result = kalshi_monitor.send_composed_weather_market_alert(
            station="KAUS",
            market_types={"HIGH"},
            transition_reason="crossed_up",
            prev_temp_f=81.5,
            now_temp_f=82.4,
            delta_f=0.9,
            obs_time_utc="2026-01-01T12:01:00Z",
        )

        self.assertTrue(result["ok"])
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(int(sent_payload["alert_schema_version"]), 2)
        self.assertEqual(sent_payload["alert_classification"], "MARKET_ELIGIBLE")
        self.assertIn("alert_summary", sent_payload)
        self.assertIn("transition_correlation", sent_payload)
        self.assertIn("suppression_context", sent_payload)
        self.assertIn("diagnostic_metadata", sent_payload)
        self.assertIn("summary", sent_payload)
        self.assertIn("transition_context", sent_payload)
        self.assertIn("market_evaluation", sent_payload)
        self.assertIn("alert_decision", sent_payload)
        self.assertIn("execution_context", sent_payload)
        self.assertEqual(sent_payload["alert_decision"]["decision"], "FIRED")

    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", side_effect=AssertionError("send path must not hydrate inline"))
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={"markets": [], "observed": {"current_temp_f": 80.0}, "market_types": ["HIGH"]})
    def test_cache_miss_enqueues_without_inline_hydration(self, _cache_snapshot, _hydrate, mock_enqueue):
        result = kalshi_monitor.send_composed_weather_market_alert(
            station="KAUS",
            market_types={"HIGH"},
            transition_reason="crossed_up",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_markets")
        mock_enqueue.assert_called_once_with("KAUS", reason="alert_send_cache_missing")

    @patch("core.kalshi_monitor._SERIES_MARKETS_CACHE", {
        "KXHIGHDEN": {
            "markets": [
                {
                    "ticker": "KXHIGHDEN-26DEC31-B70",
                    "strike_type": "between",
                    "floor_strike": 70,
                    "cap_strike": 71,
                    "event_ticker": "KXHIGHDEN-26DEC31",
                    "status": "active",
                }
            ],
            "hydrated_at_utc": "2026-01-01T12:00:00+00:00",
            "station_local_day": "2026-01-01",
        }
    })
    @patch("core.kalshi_monitor._SERIES_BY_STATION", {"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26DEC31")
    @patch("core.kalshi_monitor.get_metar_state", return_value={"last_obs": {"KDEN": {"temp_f": 69.7}}})
    @patch("core.kalshi_monitor.immutable_public_state_snapshot", return_value={"last_obs": {}})
    def test_cache_only_snapshot_reads_observed_temp_from_last_obs(self, *_mocks):
        snapshot = kalshi_monitor.build_structured_snapshot_from_cache("KDEN", {"HIGH"})
        self.assertEqual(snapshot["observed"]["current_temp_f"], 69.7)


if __name__ == "__main__":
    unittest.main()
