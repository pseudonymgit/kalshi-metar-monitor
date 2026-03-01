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
    @patch("app._kalshi_public_get")
    def test_market_coverage_shows_disabled_market_type_and_webhook_missing(
        self,
        mock_public_get,
        *_mocks,
    ):
        mock_public_get.return_value = {
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
    @patch("app._kalshi_public_get", return_value={"markets": []})
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
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN", "KMIA"], "count": 2})
    @patch("app.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN", "KMIA": "KXHIGHMIA"})
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26JAN01")
    @patch("app._kalshi_public_get", return_value={"markets": []})
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
    @patch("app._kalshi_public_get", return_value={"markets": []})
    @patch("core.kalshi_monitor._get_active_stations", return_value=None)
    def test_market_coverage_marks_discovered_station_not_live_when_missing_from_watchlist(self, *_mocks):
        response = self.client.get("/observability/market-coverage")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        rows = {
            (row["station"], row["market_type"]): row
            for row in payload["rows"]
        }

        self.assertFalse(rows[("KMIA", "HIGH")]["station_in_live_ingestion_universe"])
        self.assertFalse(rows[("KMIA", "LOW")]["station_in_live_ingestion_universe"])
        self.assertFalse(rows[("KMIA", "HIGH")]["evaluation_possible"])
        self.assertEqual(
            rows[("KMIA", "HIGH")]["coverage_reason"],
            "station_not_in_live_ingestion_universe",
        )


if __name__ == "__main__":
    unittest.main()
