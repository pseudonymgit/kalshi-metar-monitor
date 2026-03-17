import unittest
from unittest.mock import patch

import app as app_module
from core import kalshi_monitor


class SeriesSurfaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.original_series_by_station = dict(kalshi_monitor._SERIES_BY_STATION)
        self.original_series_markets_cache = dict(kalshi_monitor._SERIES_MARKETS_CACHE)

    def tearDown(self):
        with kalshi_monitor._SERIES_LOCK:
            kalshi_monitor._SERIES_BY_STATION = dict(self.original_series_by_station)
            kalshi_monitor._SERIES_MARKETS_CACHE = dict(self.original_series_markets_cache)

    def test_get_series_surface_snapshot_reports_hydration_state(self):
        with kalshi_monitor._SERIES_LOCK:
            kalshi_monitor._SERIES_BY_STATION = {
                "KDEN": ["KXHIGHDEN", "KXDENHIGH"],
            }
            kalshi_monitor._SERIES_MARKETS_CACHE = {
                "KXHIGHDEN": {
                    "markets": [{"ticker": "A"}, {"ticker": "B"}],
                    "hydrated_at_utc": "2026-03-12T12:00:00+00:00",
                    "station_local_day": "2026-03-12",
                }
            }

        payload = kalshi_monitor.get_series_surface_snapshot()

        self.assertIn("generated_utc", payload)
        self.assertEqual(len(payload["stations"]), 1)
        station_row = payload["stations"][0]
        self.assertEqual(station_row["station"], "KDEN")
        self.assertEqual(station_row["series_tickers"], ["KXHIGHDEN", "KXDENHIGH"])
        self.assertEqual(station_row["total_raw_market_count"], 2)
        self.assertEqual(
            station_row["series"],
            [
                {
                    "series_ticker": "KXHIGHDEN",
                    "hydrated": True,
                    "raw_market_count": 2,
                    "hydrated_at_utc": "2026-03-12T12:00:00+00:00",
                    "station_local_day": "2026-03-12",
                },
                {
                    "series_ticker": "KXDENHIGH",
                    "hydrated": False,
                    "raw_market_count": 0,
                    "hydrated_at_utc": None,
                    "station_local_day": None,
                },
            ],
        )


class SeriesSurfaceEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    @patch(
        "app.get_series_surface_snapshot",
        return_value={
            "generated_utc": "2026-01-01T00:00:00+00:00",
            "stations": [
                {
                    "station": "KDEN",
                    "series_tickers": ["KXHIGHDEN", "KXDENHIGH"],
                    "series": [
                        {
                            "series_ticker": "KXHIGHDEN",
                            "hydrated": True,
                            "raw_market_count": 34,
                            "hydrated_at_utc": "2026-01-01T00:00:00+00:00",
                            "station_local_day": "2026-01-01",
                        }
                    ],
                    "total_raw_market_count": 34,
                }
            ],
        },
    )
    def test_endpoint_returns_snapshot(self, _snapshot_mock):
        response = self.client.get("/observability/series-surface")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "generated_utc": "2026-01-01T00:00:00+00:00",
                "stations": [
                    {
                        "station": "KDEN",
                        "series_tickers": ["KXHIGHDEN", "KXDENHIGH"],
                        "series": [
                            {
                                "series_ticker": "KXHIGHDEN",
                                "hydrated": True,
                                "raw_market_count": 34,
                                "hydrated_at_utc": "2026-01-01T00:00:00+00:00",
                                "station_local_day": "2026-01-01",
                            }
                        ],
                        "total_raw_market_count": 34,
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
