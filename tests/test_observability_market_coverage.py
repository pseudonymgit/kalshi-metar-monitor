import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class ObservabilityMarketCoverageTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch.dict(
        "os.environ",
        {
            "KALSHI_TARGET_MARKET_TYPE": "HIGH",
            "ALERT_WEBHOOK_URL": "",
        },
        clear=False,
    )
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26JAN01")
    @patch("app.get_cached_series_markets")
    def test_market_coverage_shows_disabled_market_type_and_webhook_missing(
        self,
        mock_cached_series_markets,
        *_mocks,
    ):
        mock_cached_series_markets.return_value = {
            "markets": [
                {
                    "ticker": "KXHIGHDEN-26JAN01-T70",
                    "status": "active",
                    "strike_type": "between",
                    "floor_strike": 70,
                    "cap_strike": 71,
                }
            ]
        }

        response = self.client.get("/observability/market-coverage?station=KDEN")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)

        rows = {row["market_type"]: row for row in payload["rows"]}

        high_row = rows["HIGH"]
        self.assertTrue(high_row["evaluation_possible"])
        self.assertFalse(high_row["alerting_possible"])
        self.assertEqual(high_row["coverage_status"], "evaluation_only")
        self.assertEqual(high_row["coverage_reason"], "webhook_missing")

        low_row = rows["LOW"]
        self.assertFalse(low_row["evaluation_possible"])
        self.assertEqual(low_row["coverage_status"], "not_covered")
        self.assertEqual(low_row["coverage_reason"], "market_type_disabled_by_config")
        mock_cached_series_markets.assert_called_with("KXHIGHDEN")

    @patch.dict(
        "os.environ",
        {
            "KALSHI_TARGET_MARKET_TYPE": "HIGH,LOW",
            "ALERT_WEBHOOK_URL": "https://example.com/webhook",
        },
        clear=False,
    )
    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN", "KLAX"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN", "KLAX"], "count": 2})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("app.get_cached_series_markets", return_value=None)
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KLAX"})
    def test_market_coverage_prioritizes_station_not_active_reason(self, *_mocks):
        response = self.client.get("/observability/market-coverage")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["ok"])

        rows = {
            (row["station"], row["market_type"]): row
            for row in payload["rows"]
        }
        self.assertEqual(rows[("KDEN", "HIGH")]["coverage_reason"], "station_not_active")
        self.assertEqual(rows[("KLAX", "HIGH")]["coverage_reason"], "no_discovered_series")

    @patch.dict(
        "os.environ",
        {
            "KALSHI_TARGET_MARKET_TYPE": "HIGH,LOW",
            "ALERT_WEBHOOK_URL": "https://example.com/webhook",
        },
        clear=False,
    )
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("app.get_cached_series_markets", return_value=None)
    @patch("core.kalshi_monitor._get_active_stations", return_value=None)
    def test_market_coverage_handles_missing_cached_markets_gracefully(self, *_mocks):
        response = self.client.get("/observability/market-coverage?station=KDEN")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        rows = {row["market_type"]: row for row in payload["rows"]}
        self.assertEqual(rows["HIGH"]["coverage_status"], "market_data_unknown")
        self.assertEqual(rows["HIGH"]["coverage_reason"], "cache_not_yet_populated")
        self.assertEqual(rows["HIGH"]["eligible_market_count_after_filters"], 0)

    @patch.dict(
        "os.environ",
        {
            "KALSHI_TARGET_MARKET_TYPE": "HIGH,LOW",
            "ALERT_WEBHOOK_URL": "https://example.com/webhook",
        },
        clear=False,
    )
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN", "KMIA"], "count": 2})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN", "KMIA": "KXHIGHMIA"})
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26JAN01")
    @patch("app.get_cached_series_markets", return_value={"markets": []})
    @patch("core.kalshi_monitor._get_active_stations", return_value=None)
    def test_market_coverage_includes_discovered_station_in_live_universe(self, *_mocks):
        response = self.client.get("/observability/market-coverage")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        rows = {
            (row["station"], row["market_type"]): row
            for row in payload["rows"]
        }

        self.assertTrue(rows[("KMIA", "HIGH")]["station_in_live_ingestion_universe"])
        self.assertTrue(rows[("KMIA", "LOW")]["station_in_live_ingestion_universe"])
        self.assertEqual(rows[("KMIA", "HIGH")]["coverage_reason"], "no_eligible_markets_after_filters")
        self.assertEqual(rows[("KMIA", "LOW")]["coverage_reason"], "no_eligible_markets_after_filters")
        self.assertFalse(rows[("KMIA", "HIGH")]["evaluation_possible"])

    @patch.dict(
        "os.environ",
        {
            "KALSHI_TARGET_MARKET_TYPE": "HIGH,LOW",
            "ALERT_WEBHOOK_URL": "https://example.com/webhook",
        },
        clear=False,
    )
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN", "KMIA": "KXHIGHMIA"})
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26JAN01")
    @patch("app.get_cached_series_markets", return_value={"markets": []})
    @patch("core.kalshi_monitor._get_active_stations", return_value=None)
    def test_market_coverage_keeps_discovered_station_live_even_when_missing_from_watchlist(self, *_mocks):
        response = self.client.get("/observability/market-coverage")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        rows = {
            (row["station"], row["market_type"]): row
            for row in payload["rows"]
        }

        self.assertTrue(rows[("KMIA", "HIGH")]["station_in_live_ingestion_universe"])
        self.assertTrue(rows[("KMIA", "LOW")]["station_in_live_ingestion_universe"])
        self.assertFalse(rows[("KMIA", "HIGH")]["evaluation_possible"])
        self.assertEqual(rows[("KMIA", "HIGH")]["coverage_reason"], "no_eligible_markets_after_filters")

    @patch("core.kalshi_monitor._kalshi_public_get", side_effect=AssertionError("should not be called"))
    @patch("app.get_default_config", return_value={"stations": ["KDEN"], "poll_seconds": 60})
    @patch("app.get_state", return_value={"stations": ["KDEN"], "last_seen_iso": {}, "last_poll_utc": None})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("app.get_cached_series_markets", return_value={"markets": []})
    def test_market_coverage_never_calls_live_kalshi_api(self, *_mocks):
        response = self.client.get("/observability/market-coverage?station=KDEN")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
