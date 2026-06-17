"""Test Layer 0: Market Cache Persistence (CRITICAL-3)."""

import unittest
import tempfile
import os
from unittest.mock import patch

from core import kalshi_monitor


class MarketCachePersistenceTests(unittest.TestCase):
    """Test market cache persistence across restarts (L0-T2)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear market cache
        kalshi_monitor._SERIES_MARKETS_CACHE.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_market_cache_persistence_roundtrip(self):
        """Test that market cache can be persisted and reloaded correctly."""

        market_id = "series:KDEN:KXHIGHDEN"
        station = "KDEN"
        cache_entry = {
            "markets": [
                {"ticker": "KXHIGHDEN-25JAN26-70", "strike": 70, "last_price": 50.0},
                {"ticker": "KXHIGHDEN-25JAN26-71", "strike": 71, "last_price": 45.0},
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
        self.assertEqual(len(loaded["markets"]), 2)
        self.assertEqual(loaded["markets"][0]["ticker"], "KXHIGHDEN-25JAN26-70")
        self.assertEqual(loaded["markets"][0]["strike"], 70)

    def test_load_missing_market_cache_returns_none(self):
        """Test that loading non-existent market cache returns None."""

        result = kalshi_monitor._load_market_cache("nonexistent:market:id")
        self.assertIsNone(result)

    def test_load_all_market_cache(self):
        """Test loading all market cache entries for startup hydration."""

        # Create multiple cache entries
        entries = [
            ("series:KDEN:KXHIGHDEN", "KDEN", {"markets": [], "hydrated_at_utc": "2025-01-01T12:00:00+00:00", "station_local_day": "25JAN26"}),
            ("series:KLAX:KXLOWLAX", "KLAX", {"markets": [], "hydrated_at_utc": "2025-01-01T12:00:00+00:00", "station_local_day": "25JAN26"}),
            ("series:KNYC:KXHIGHNY", "KNYC", {"markets": [], "hydrated_at_utc": "2025-01-01T12:00:00+00:00", "station_local_day": "25JAN26"}),
        ]

        for market_id, station, cache_entry in entries:
            kalshi_monitor._persist_market_cache(market_id, station, cache_entry)

        # Load all
        loaded = kalshi_monitor._load_all_market_cache()

        # Verify
        self.assertEqual(len(loaded), 3)
        self.assertIn("series:KDEN:KXHIGHDEN", loaded)
        self.assertIn("series:KLAX:KXLOWLAX", loaded)
        self.assertIn("series:KNYC:KXHIGHNY", loaded)

        # Verify cache data
        for market_id, entry in loaded.items():
            self.assertEqual(entry["station"], market_id.split(":")[1])

    def test_cache_hit_rate_after_restart(self):
        """Test that cache hit rate is high after restart (simulating L0-T2)."""

        # Simulate first runtime - populate cache
        station = "KDEN"
        series_ticker = "KXHIGHDEN"
        markets = [
            {"ticker": "KXHIGHDEN-25JAN26-70", "strike": 70, "last_price": 50.0},
            {"ticker": "KXHIGHDEN-25JAN26-71", "strike": 71, "last_price": 45.0},
            {"ticker": "KXHIGHDEN-25JAN26-72", "strike": 72, "last_price": 40.0},
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

        # Calculate cache hit rate
        total_requests = 20  # Simulate 20 market requests
        cache_hits = 0

        for i in range(total_requests):
            # Check if we can load the market from cache
            cached = kalshi_monitor._load_market_cache(f"series:{station}:{series_ticker}")
            if cached is not None:
                cache_hits += 1

        # Calculate hit rate
        hit_rate = cache_hits / total_requests

        # Verify hit rate ≥ 95% (L0-T2 pass criteria)
        self.assertGreaterEqual(
            hit_rate, 0.95,
            f"Cache hit rate {hit_rate*100:.1f}% should be ≥ 95%"
        )

    def test_discovered_markets_persistence(self):
        """Test that discovered markets are persisted to database."""

        # Simulate market discovery
        station = "KDEN"
        market_symbol = "KXHIGHDEN-25JAN26"

        kalshi_monitor._persist_market_cache(
            f"discovered:{station}:{market_symbol}",
            station,
            {"symbol": market_symbol, "station": station, "discovered_at": "2025-01-01T12:00:00+00:00"},
        )

        # Load back
        loaded = kalshi_monitor._load_market_cache(f"discovered:{station}:{market_symbol}")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["symbol"], market_symbol)
        self.assertEqual(loaded["station"], station)


if __name__ == "__main__":
    unittest.main()
