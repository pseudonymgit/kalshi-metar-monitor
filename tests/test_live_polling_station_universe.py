import unittest
from unittest.mock import patch

import app as app_module
from core import metar_monitor


class LivePollingStationUniverseTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        metar_monitor.set_live_station_universe_resolver(None)
        metar_monitor.set_live_station_universe_resolver(app_module._canonical_live_polling_stations)

    def test_poll_once_refreshes_runtime_polling_stations_from_canonical_universe(self):
        seen = []
        metar_monitor._STATE["stations"] = ["KDEN"]

        with patch("core.metar_monitor.ensure_state_loaded"), \
             patch(
                 "core.metar_monitor.get_default_config",
                 return_value={"stations": ["KDEN"], "default_source": "nws", "lookback_min": 3, "cache_file": "/tmp/cache.json"},
             ), \
             patch(
                 "core.metar_monitor._compute_window",
                 return_value=("2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z", None, None),
             ), \
             patch("core.metar_monitor._fetch_range_strict", side_effect=lambda icao, *args, **kwargs: (seen.append(icao) or [])), \
             patch("core.metar_monitor._ingest_obs", return_value=(0, 0)), \
             patch("core.metar_monitor._save_cache"):
            with patch("app.get_default_config", return_value={"stations": ["KDEN"]}), \
                 patch("app.get_state", return_value={"stations": ["KDEN"]}), \
                 patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1}), \
                 patch("app.ensure_series_discovery_loaded", return_value={"KMIA": "KXHIGHMIA"}):
                metar_monitor._poll_once()

        self.assertEqual(seen, ["KDEN", "KMIA"])
        self.assertEqual(metar_monitor._STATE["stations"], ["KDEN", "KMIA"])

    @patch("app.get_default_config", return_value={"stations": ["KDEN"]})
    @patch("app.get_state", return_value={"stations": ["KDEN"]})
    @patch("app.get_watchlist", return_value={"watchlist": ["KDEN"], "count": 1})
    @patch("app.ensure_series_discovery_loaded", return_value={"KMIA": "KXHIGHMIA"})
    def test_polling_resolver_matches_observability_canonical_universe(self, *_mocks):
        observability_stations = app_module._canonical_live_station_universe().get("stations")
        polling_stations = metar_monitor._resolve_live_polling_stations({"stations": ["KDEN"]})

        self.assertEqual(polling_stations, observability_stations)


if __name__ == "__main__":
    unittest.main()
