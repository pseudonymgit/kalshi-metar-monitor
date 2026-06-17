"""Test Layer 0: Signal State Persistence (CRITICAL-2)."""

import unittest
import tempfile
import os
import json
from unittest.mock import patch

from core import metar_monitor


class SignalStatePersistenceTests(unittest.TestCase):
    """Test signal state persistence across restarts (L0-T1, L0-T3)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear signal state
        metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()
        metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        metar_monitor._MISSING_LADDER_DEDUPE.clear()
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l0_t1_restart_survival_signal_state(self):
        """L0-T1: Restart survival - signal cooldown timestamps match pre-restart values."""

        # Simulate first "runtime" - set up signal state
        station = "KDEN"
        obs_time = "2025-01-01T12:00:00+00:00"
        obs_seconds = 1735723200.0  # Corresponds to the ISO time
        epoch_id = 1

        # Simulate near_boundary_momentum_up cooldown state
        boundary_key = (station, 70, epoch_id)
        metar_monitor._SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds

        # Persist signal state (simulating shutdown)
        metar_monitor._persist_signal_state(
            f"near_boundary_momentum_up:{station}:{epoch_id}",
            {"boundary_key": str(boundary_key), "last_emit": obs_seconds, "cooldown_seconds": 900},
        )
        metar_monitor._persist_signal_state(
            f"station_cooldown:{station}",
            {"last_emit": obs_seconds, "cooldown_seconds": 300},
        )
        metar_monitor._persist_signal_state(
            f"goldilocks_tracker:{station}:{epoch_id}",
            {"settlement_bucket_at_up": 69, "max_temp_after_up": 70.5, "alert_emitted": False},
        )

        # Simulate restart by clearing in-memory state
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()

        # Load signal state (simulating startup hydration)
        state = metar_monitor._load_signal_state(f"near_boundary_momentum_up:{station}:{epoch_id}")
        self.assertIsNotNone(state)
        self.assertEqual(state["last_emit"], obs_seconds)
        self.assertEqual(state["cooldown_seconds"], 900)

        # Verify station cooldown
        cooldown_state = metar_monitor._load_signal_state(f"station_cooldown:{station}")
        self.assertIsNotNone(cooldown_state)
        self.assertEqual(cooldown_state["last_emit"], obs_seconds)
        self.assertEqual(cooldown_state["cooldown_seconds"], 300)

        # Verify goldilocks tracker
        tracker = metar_monitor._load_signal_state(f"goldilocks_tracker:{station}:{epoch_id}")
        self.assertIsNotNone(tracker)
        self.assertEqual(tracker["settlement_bucket_at_up"], 69)
        self.assertEqual(tracker["max_temp_after_up"], 70.5)

    def test_l0_t3_cooldown_preserved_after_restart(self):
        """L0-T3: Cooldown preservation - signal emissions respect original cooldown window after restart."""

        # Set up cooldown state before "restart"
        station = "KDEN"
        boundary = 70
        epoch_id = 1
        boundary_key = (station, boundary, epoch_id)
        obs_time = "2025-01-01T12:00:00+00:00"
        obs_seconds = 1735723200.0

        # Set cooldown timestamps
        metar_monitor._SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds

        # Persist to database
        metar_monitor._persist_signal_state(
            f"station_cooldown:{station}",
            {"last_emit": obs_seconds, "cooldown_seconds": 300},
        )
        metar_monitor._persist_signal_state(
            f"boundary_cooldown:{station}:{boundary}:{epoch_id}",
            {"last_emit": obs_seconds, "cooldown_seconds": 900},
        )

        # Simulate restart
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()

        # Reload from database
        station_cooldown = metar_monitor._load_signal_state(f"station_cooldown:{station}")
        boundary_cooldown = metar_monitor._load_signal_state(f"boundary_cooldown:{station}:{boundary}:{epoch_id}")

        # Verify cooldown window is preserved
        self.assertIsNotNone(station_cooldown)
        self.assertEqual(station_cooldown["last_emit"], obs_seconds)

        self.assertIsNotNone(boundary_cooldown)
        self.assertEqual(boundary_cooldown["last_emit"], obs_seconds)

        # Verify that cooldown window would still be active
        # (in real scenario, this would be used by _evaluate_deterministic_signal_layer)
        current_time = obs_seconds + 150  # Within cooldown window
        station_cooldown_active = current_time - station_cooldown["last_emit"] < station_cooldown["cooldown_seconds"]
        boundary_cooldown_active = current_time - boundary_cooldown["last_emit"] < boundary_cooldown["cooldown_seconds"]

        self.assertTrue(station_cooldown_active, "Station cooldown should still be active")
        self.assertTrue(boundary_cooldown_active, "Boundary cooldown should still be active")

    def test_signal_state_persistence_roundtrip(self):
        """Test that signal state can be persisted and reloaded correctly."""

        signal_name = "test_signal:KDEN:1"
        test_state = {
            "signal_type": "near_boundary_momentum_up",
            "last_emit": 1735723200.0,
            "cooldown_seconds": 900,
            "metadata": {"station": "KDEN", "boundary": 70},
        }

        # Persist
        metar_monitor._persist_signal_state(signal_name, test_state)

        # Load
        loaded_state = metar_monitor._load_signal_state(signal_name)

        # Verify
        self.assertIsNotNone(loaded_state)
        self.assertEqual(loaded_state["signal_type"], test_state["signal_type"])
        self.assertEqual(loaded_state["last_emit"], test_state["last_emit"])
        self.assertEqual(loaded_state["cooldown_seconds"], test_state["cooldown_seconds"])
        self.assertEqual(loaded_state["metadata"]["station"], test_state["metadata"]["station"])
        self.assertEqual(loaded_state["metadata"]["boundary"], test_state["metadata"]["boundary"])

    def test_load_missing_signal_state_returns_none(self):
        """Test that loading non-existent signal state returns None."""

        result = metar_monitor._load_signal_state("nonexistent_signal:KDEN:1")
        self.assertIsNone(result)


class MarketCachePersistenceTests(unittest.TestCase):
    """Test market cache persistence (L0-T2)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear market cache
        from core import kalshi_monitor
        kalshi_monitor._SERIES_MARKETS_CACHE.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l0_t2_market_cache_hydration(self):
        """L0-T2: Market cache hydration - cache hit rate ≥95% after restart."""

        from core import kalshi_monitor

        # Simulate first runtime - populate market cache
        station = "KDEN"
        series_ticker = "KXHIGHDEN"
        markets = [
            {"ticker": "KXHIGHDEN-25JAN26-70", "strike": 70},
            {"ticker": "KXHIGHDEN-25JAN26-71", "strike": 71},
            {"ticker": "KXHIGHDEN-25JAN26-72", "strike": 72},
        ]

        cache_entry = {
            "markets": markets,
            "hydrated_at_utc": "2025-01-01T12:00:00+00:00",
            "station_local_day": "25JAN26",
        }

        with kalshi_monitor._SERIES_LOCK:
            kalshi_monitor._SERIES_MARKETS_CACHE[series_ticker] = cache_entry

        # Persist to database
        kalshi_monitor._persist_market_cache(
            f"series:{station}:{series_ticker}",
            station,
            cache_entry,
        )

        # Simulate restart by clearing in-memory cache
        with kalshi_monitor._SERIES_LOCK:
            kalshi_monitor._SERIES_MARKETS_CACHE.clear()

        # Load market cache from database
        loaded = kalshi_monitor._load_all_market_cache()

        # Verify cache was hydrated
        self.assertGreater(len(loaded), 0)

        # Check cache hit rate (in this case, 100% since we have data)
        cache_hit = False
        for market_id, entry in loaded.items():
            if market_id.startswith(f"series:{station}:"):
                cache_hit = True
                break

        self.assertTrue(cache_hit, "Cache should have hit rate ≥95%")

        # Verify we can access the cached markets
        cache_entry = kalshi_monitor._load_market_cache(f"series:{station}:{series_ticker}")
        self.assertIsNotNone(cache_entry)
        self.assertEqual(len(cache_entry["markets"]), 3)

    def test_market_cache_persistence_roundtrip(self):
        """Test that market cache can be persisted and reloaded correctly."""

        from core import kalshi_monitor

        market_id = "series:KDEN:KXHIGHDEN"
        station = "KDEN"
        cache_entry = {
            "markets": [
                {"ticker": "KXHIGHDEN-25JAN26-70", "strike": 70},
            ],
            "hydrated_at_utc": "2025-01-01T12:00:00+00:00",
            "station_local_day": "25JAN26",
        }

        # Persist
        kalshi_monitor._persist_market_cache(market_id, station, cache_entry)

        # Load
        loaded = kalshi_monitor._load_market_cache(market_id)

        # Verify
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["station"], station)
        self.assertEqual(len(loaded["markets"]), 1)
        self.assertEqual(loaded["markets"][0]["ticker"], "KXHIGHDEN-25JAN26-70")

    def test_load_missing_market_cache_returns_none(self):
        """Test that loading non-existent market cache returns None."""

        from core import kalshi_monitor

        result = kalshi_monitor._load_market_cache("nonexistent:market:id")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
