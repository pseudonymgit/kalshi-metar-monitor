import unittest
from unittest.mock import patch

from core import kalshi_monitor, metar_monitor


class StructuralHardeningTests(unittest.TestCase):
    @patch("core.kalshi_monitor._kalshi_public_get", side_effect=AssertionError("should not be called"))
    @patch("core.kalshi_monitor.build_structured_snapshot", side_effect=AssertionError("should not be called"))
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    def test_hydration_prerequisite_is_cache_only(self, *_mocks):
        with patch.dict("core.kalshi_monitor._SERIES_MARKETS_CACHE", {}, clear=True):
            result = kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")
        self.assertEqual(result["status"], "cache_missing")

    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._station_local_previous_day", return_value="2025-03-09")
    @patch("core.kalshi_monitor.station_local_day_key", return_value="2025-03-10")
    @patch(
        "core.kalshi_monitor.get_cached_series_markets",
        return_value={
            "markets": [],
            "hydrated_at_utc": "2025-01-01T10:00:00+00:00",
            "station_local_day": "2025-03-09",
        },
    )
    def test_hydration_prerequisite_accepts_station_local_yesterday_cache(self, *_mocks):
        result = kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")
        self.assertEqual(result["status"], "cache_valid")
        self.assertTrue(result["rollover_grace"])

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3})
    @patch("core.metar_monitor._compute_window")
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_missing"})
    def test_fetch_window_skips_ingest_when_ladder_not_hydrated(
        self,
        _hydration,
        mock_ingest,
        mock_fetch,
        mock_compute,
        *_mocks,
    ):
        mock_compute.return_value = ("s", "e", None, None)

        result = metar_monitor.fetch_window("KDEN", 3, source="nws")

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["poll_skipped_reason"], "ladder_not_hydrated")
        mock_fetch.assert_not_called()
        mock_ingest.assert_not_called()

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale"})
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot")
    @patch("core.metar_monitor._save_cache")
    def test_poll_once_skips_station_when_ladder_not_hydrated(self, _save, mock_hydrate, mock_ingest, mock_fetch, *_mocks):
        metar_monitor._poll_once()

        mock_fetch.assert_not_called()
        mock_ingest.assert_not_called()
        mock_hydrate.assert_not_called()

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale"})
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot")
    @patch("core.metar_monitor._save_cache")
    def test_poll_once_records_ingestion_admission_for_non_hydrated_station(self, _save, _hydrate, _ingest, _fetch, *_mocks):
        metar_monitor._poll_once()

        state = metar_monitor.get_state()
        admission = state["ingestion_admission"]["KDEN"]
        self.assertFalse(admission["hydration_passed"])
        self.assertFalse(admission["admitted_to_fetch"])
        self.assertEqual(admission["skip_reason"], "ladder_not_hydrated")
        self.assertIsNotNone(admission["evaluated_at_utc"])

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={})
    @patch("core.metar_monitor._ingest_obs", return_value=(1, 0))
    def test_simulation_uses_ingest_path(self, mock_ingest, *_mocks):
        metar_monitor._simulate_temperature_for_testing("KDEN", 71.1)

        mock_ingest.assert_called_once()

    def test_observability_domain_blocks_live_kalshi_calls(self):
        with kalshi_monitor.kalshi_execution_domain("observability"):
            with self.assertRaises(RuntimeError):
                kalshi_monitor._kalshi_public_get("/markets?limit=1")

    @patch("core.kalshi_monitor.build_structured_snapshot", return_value={"markets": []})
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor.get_cached_series_markets", return_value={"markets": [], "hydrated_at_utc": "2025-01-01T00:00:00+00:00", "station_local_day": "2025-01-01"})
    def test_explicit_hydrator_requires_production_domain(self, *_mocks):
        with kalshi_monitor.kalshi_execution_domain("observability"):
            with self.assertRaises(RuntimeError):
                kalshi_monitor.hydrate_station_ladder_snapshot("KDEN", {"HIGH", "LOW"})

    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._kalshi_public_get", return_value={"markets": []})
    def test_cache_metadata_present(self, *_mocks):
        kalshi_monitor.build_structured_snapshot("KDEN", {"HIGH"})

        cached = kalshi_monitor.get_cached_series_markets("KXHIGHDEN")
        self.assertIsNotNone(cached)
        self.assertIn("markets", cached)
        self.assertIn("hydrated_at_utc", cached)
        self.assertIn("station_local_day", cached)

    def test_no_direct_state_mutation_for_temperature_domains(self):
        source = open("core/metar_monitor.py", "r", encoding="utf-8").read()
        forbidden_fragments = [
            '_STATE["last_obs"][',
            '_STATE["last_seen_iso"][',
            '_STATE["last_observed_integer"][',
            '_STATE["running_daily_max"][',
            '_STATE["last_settlement_bucket"][',
            '_STATE["last_instant_bucket"][',
            '_STATE["last_obs"].update(',
            '_STATE["last_seen_iso"].update(',
            '_STATE["last_observed_integer"].update(',
            '_STATE["running_daily_max"].update(',
            '_STATE["last_settlement_bucket"].update(',
            '_STATE["last_instant_bucket"].update(',
            '_STATE["last_obs"].pop(',
            '_STATE["last_seen_iso"].pop(',
            '_STATE["last_observed_integer"].pop(',
            '_STATE["running_daily_max"].pop(',
            '_STATE["last_settlement_bucket"].pop(',
            '_STATE["last_instant_bucket"].pop(',
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, source)


class SimulationEndpointHydrationTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    @patch("app.ensure_scheduler_started", return_value=True)
    @patch("app.ensure_series_discovery_loaded", return_value={})
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", return_value={"status": "hydrated"})
    @patch("app._simulate_temperature_for_testing", return_value={"ok": True})
    def test_simulate_ladder_hydrates_explicitly(self, _simulate, mock_hydrate, *_mocks):
        response = self.client.post("/metar/simulate-ladder", json={"icao": "KDEN", "temp_f": 70.0})
        self.assertEqual(response.status_code, 200)
        mock_hydrate.assert_called_once_with(station="KDEN", market_types={"HIGH", "LOW"})


if __name__ == "__main__":
    unittest.main()
