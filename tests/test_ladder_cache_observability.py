import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app as app_module
from core.ladder_cache_observability import build_ladder_cache_snapshot


class LadderCacheObservabilityBuilderTests(unittest.TestCase):
    @patch("core.ladder_cache_observability.get_cached_series_markets", return_value=None)
    @patch("core.ladder_cache_observability.get_last_hydration_execution_snapshot", return_value={})
    @patch("core.ladder_cache_observability.get_hydration_prerequisite_state_snapshot", return_value={})
    def test_handles_no_hydration_state(self, *_mocks):
        payload = build_ladder_cache_snapshot([])
        self.assertIn("generated_utc", payload)
        self.assertEqual(payload["station_count"], 0)
        self.assertEqual(payload["stations"], [])

    @patch("core.ladder_cache_observability.get_cached_series_markets")
    @patch("core.ladder_cache_observability.get_last_hydration_execution_snapshot")
    @patch("core.ladder_cache_observability.get_hydration_prerequisite_state_snapshot")
    def test_builds_station_rows_for_partial_state(self, prereq_mock, execution_mock, cached_mock):
        now = datetime.now(timezone.utc)
        hydrated_at = (now - timedelta(seconds=45)).isoformat()

        prereq_mock.return_value = {
            "KDEN": {"cache_valid": True},
            "KPHL": {"cache_valid": False},
        }
        execution_mock.return_value = {
            "KDEN": {
                "series_ticker": "KXHIGHDEN-25MAR06",
                "evaluated_at_utc": now.isoformat(),
            }
        }

        def _cached(series_ticker):
            if series_ticker == "KXHIGHDEN-25MAR06":
                return {"markets": [{"ticker": "A"}, {"ticker": "B"}], "hydrated_at_utc": hydrated_at}
            return None

        cached_mock.side_effect = _cached

        payload = build_ladder_cache_snapshot(["kden"])
        self.assertEqual(payload["station_count"], 2)

        by_station = {row["station"]: row for row in payload["stations"]}
        self.assertEqual(by_station["KDEN"]["series_ticker"], "KXHIGHDEN-25MAR06")
        self.assertEqual(by_station["KDEN"]["market_count"], 2)
        self.assertGreaterEqual(by_station["KDEN"]["ladder_cache_age_seconds"], 0)
        self.assertTrue(by_station["KDEN"]["hydration"]["cache_present"])
        self.assertTrue(by_station["KDEN"]["hydration"]["series_discovered"])
        self.assertTrue(by_station["KDEN"]["hydration"]["cache_valid"])

        self.assertEqual(by_station["KPHL"]["series_ticker"], None)
        self.assertEqual(by_station["KPHL"]["market_count"], 0)
        self.assertEqual(by_station["KPHL"]["ladder_cache_age_seconds"], None)
        self.assertFalse(by_station["KPHL"]["hydration"]["cache_present"])
        self.assertFalse(by_station["KPHL"]["hydration"]["series_discovered"])
        self.assertFalse(by_station["KPHL"]["hydration"]["cache_valid"])


class LadderCacheObservabilityEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    @patch("app.build_ladder_cache_snapshot", return_value={"generated_utc": "2026-01-01T00:00:00+00:00", "station_count": 0, "stations": []})
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN", "KPHL"]})
    def test_endpoint_returns_snapshot(self, _station_universe_mock, _snapshot_mock):
        response = self.client.get("/observability/ladder_cache")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"generated_utc": "2026-01-01T00:00:00+00:00", "station_count": 0, "stations": []},
        )


if __name__ == "__main__":
    unittest.main()
