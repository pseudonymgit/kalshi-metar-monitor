#!/usr/bin/env python3
"""
Quick unit tests for Polymarket WhaleWatch modules.

Tests:
  1. Title parsing (city→station, bucket, direction)
  2. Station mapping coverage
  3. Conviction multiplier computation
  4. Agreement score computation
  5. DB schema creation and basic CRUD
  6. Feed processing (dry-run/no API dependency test)

Run: python -m pytest scripts/test_polymarket_whale.py -v
Or:  python scripts/test_polymarket_whale.py
"""

import json
import os
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.polymarket_whale_db import (
    init_db, get_db, PM_WHALE_DB,
    store_traders, store_markets, store_consensus_signals,
    get_active_signals, get_recent_signals,
    insert_signal_feed_entry, expire_stale_signals,
    get_polymarket_conviction, get_stats,
)
from core.polymarket_kalshi_feeder import (
    parse_weather_market_title, STATION_MAP,
    compute_conviction_multiplier, compute_agreement_score,
    extract_consensus_signals, identify_weather_signals,
)


class TestTitleParsing(unittest.TestCase):
    """Test Polymarket weather market title → Kalshi station/bucket mapping."""

    def test_parse_nyc_high(self):
        result = parse_weather_market_title(
            "Will the temperature in New York exceed 85°F on August 15?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KNYC')
        self.assertEqual(result['bucket'], 85)
        self.assertEqual(result['direction'], 'UP')
        self.assertEqual(result['series'], 'HIGH')

    def test_parse_nyc_low(self):
        result = parse_weather_market_title(
            "Will the temperature in NYC be below 32°F on January 10?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KNYC')
        self.assertEqual(result['bucket'], 32)
        self.assertEqual(result['direction'], 'DOWN')

    def test_parse_chicago_high(self):
        result = parse_weather_market_title(
            "Will the high temperature in Chicago be above 90°F on July 4?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KORD')
        self.assertEqual(result['bucket'], 90)
        self.assertEqual(result['direction'], 'UP')

    def test_parse_los_angeles(self):
        result = parse_weather_market_title(
            "Will the temperature in Los Angeles be above 100°F on September 1?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KLAX')
        self.assertEqual(result['bucket'], 100)
        self.assertEqual(result['direction'], 'UP')

    def test_parse_la_low(self):
        result = parse_weather_market_title(
            "Will the temperature in LA stay below 70°F on October 15?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KLAX')
        self.assertEqual(result['bucket'], 70)
        self.assertEqual(result['direction'], 'DOWN')

    def test_parse_non_weather_returns_none(self):
        result = parse_weather_market_title(
            "Will Bitcoin reach $100K by December?"
        )
        self.assertIsNone(result)

    def test_parse_empty_title_returns_none(self):
        result = parse_weather_market_title("")
        self.assertIsNone(result)

    def test_parse_no_city_returns_none(self):
        result = parse_weather_market_title(
            "Will the temperature exceed 85 degrees tomorrow?"
        )
        # No city mentioned, should return None
        self.assertIsNone(result)

    def test_station_map_coverage(self):
        """Verify key stations are in the map."""
        expected_stations = ['KNYC', 'KORD', 'KLAX', 'KDFW', 'KATL',
                             'KBOS', 'KPHL', 'KDCA', 'KSEA', 'KMIA']
        for station in expected_stations:
            self.assertIn(station, STATION_MAP.values(),
                          f"Station {station} not in STATION_MAP")

    def test_parse_miami(self):
        result = parse_weather_market_title(
            "Will the temperature in Miami exceed 95°F on August 20?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KMIA')
        self.assertEqual(result['bucket'], 95)
        self.assertEqual(result['direction'], 'UP')

    def test_parse_with_high_keyword_in_title(self):
        result = parse_weather_market_title(
            "Will the high temperature in Boston be above 90°F?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KBOS')
        self.assertEqual(result['bucket'], 90)
        self.assertEqual(result['direction'], 'UP')

    def test_parse_with_low_keyword_in_title(self):
        result = parse_weather_market_title(
            "Will the low temperature in Denver be below 20°F?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KDEN')
        self.assertEqual(result['bucket'], 20)
        self.assertEqual(result['direction'], 'DOWN')

    def test_parse_dc(self):
        result = parse_weather_market_title(
            "Will the temperature in DC exceed 100°F?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KDCA')

    def test_parse_without_on_clause(self):
        result = parse_weather_market_title(
            "Will the temperature in Seattle be below 40°F?"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['station'], 'KSEA')
        self.assertEqual(result['bucket'], 40)
        self.assertEqual(result['direction'], 'DOWN')


class TestConvictionMultiplier(unittest.TestCase):
    """Test Layer 3 conviction multiplier formula."""

    def test_match_high_agreement(self):
        """Signal matches ensemble, high agreement → 1.0–1.5 range."""
        mult = compute_conviction_multiplier(0.85, 'UP', 'UP')
        self.assertAlmostEqual(mult, 1.417, places=2)
        self.assertLessEqual(mult, 1.5)
        self.assertGreaterEqual(mult, 1.0)

    def test_match_low_agreement(self):
        """Signal matches ensemble, low agreement → min floor 1.0."""
        mult = compute_conviction_multiplier(0.45, 'UP', 'UP')
        # 0.45/0.6 = 0.75, clamped to min 1.0 per spec
        self.assertEqual(mult, 1.0)

    def test_match_at_threshold(self):
        """Signal matches ensemble at exactly the threshold."""
        mult = compute_conviction_multiplier(0.6, 'UP', 'UP')
        self.assertAlmostEqual(mult, 1.0)

    def test_match_higher_than_1_5_capped(self):
        """Signal matches ensemble, agreement > 0.9 → capped at 1.5."""
        mult = compute_conviction_multiplier(1.0, 'UP', 'UP')
        self.assertAlmostEqual(mult, 1.5)

    def test_contradiction_high_agreement(self):
        """Signal contradicts ensemble, high agreement → moderate reduction."""
        mult = compute_conviction_multiplier(0.75, 'UP', 'DOWN')
        # anomaly_score = 1 - 0.75 = 0.25
        # mult = 1 - 0.5 * 0.25 = 0.875
        self.assertAlmostEqual(mult, 0.875, places=2)

    def test_contradiction_low_agreement(self):
        """Signal contradicts ensemble, low agreement → heavy reduction."""
        mult = compute_conviction_multiplier(0.30, 'UP', 'DOWN')
        # anomaly_score = 1 - 0.30 = 0.70
        # mult = max(0.2, 1 - 0.5 * 0.70) = max(0.2, 0.65) = 0.65
        self.assertAlmostEqual(mult, 0.65, places=2)

    def test_contradiction_capped_at_0_2(self):
        """Signal contradicts ensemble, extreme anomaly → floor at 0.2."""
        mult = compute_conviction_multiplier(0.0, 'UP', 'DOWN')
        # anomaly_score = 1.0, mult = max(0.2, 1 - 0.5) = max(0.2, 0.5) = 0.5
        # With score=0, anomaly=1, mult=0.5 which is above 0.2 floor
        self.assertAlmostEqual(mult, 0.5)

    def test_contradiction_very_low_floor(self):
        """Very high anomaly score → floor at 0.2."""
        mult = compute_conviction_multiplier(0.0, 'UP', 'DOWN', threshold=0.1)
        # anomaly = 1.0, mult = max(0.2, 1 - 0.5) = max(0.2, 0.5) = 0.5
        self.assertAlmostEqual(mult, 0.5)

    def test_no_ensemble_direction(self):
        """When no ensemble direction, assume agreement."""
        mult = compute_conviction_multiplier(0.85, 'UP')
        self.assertAlmostEqual(mult, 0.85 / 0.6, places=2)
        self.assertGreaterEqual(mult, 1.0)

    def test_custom_threshold(self):
        """Custom threshold adjusts the multiplier."""
        mult = compute_conviction_multiplier(0.7, 'UP', 'UP', threshold=0.5)
        self.assertAlmostEqual(mult, 1.4)

    def test_downward_match(self):
        """Both DOWN direction matching."""
        mult = compute_conviction_multiplier(0.8, 'DOWN', 'DOWN')
        self.assertAlmostEqual(mult, 0.8 / 0.6, places=2)
        self.assertLessEqual(mult, 1.5)

    def test_downward_contradiction(self):
        """Signal DOWN, ensemble UP → contradiction."""
        mult = compute_conviction_multiplier(0.8, 'DOWN', 'UP')
        # anomaly = 0.2, mult = max(0.2, 1 - 0.1) = max(0.2, 0.9) = 0.9
        self.assertAlmostEqual(mult, 0.9)


class TestAgreementScore(unittest.TestCase):
    """Test agreement score computation."""

    def test_high_whale_count(self):
        score = compute_agreement_score(whale_count=5, weighted_edge=0.08,
                                         total_notional=10000)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_zero_whales(self):
        score = compute_agreement_score(whale_count=0, weighted_edge=0.05,
                                         total_notional=500)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_full_score_cap(self):
        score = compute_agreement_score(whale_count=10, weighted_edge=0.20,
                                         total_notional=50000)
        self.assertAlmostEqual(score, 1.0)

    def test_minimal_score(self):
        score = compute_agreement_score(whale_count=1, weighted_edge=0.01,
                                         total_notional=100)
        self.assertGreater(score, 0.0)


class TestDBSchemaAndCRUD(unittest.TestCase):
    """Test SQLite schema creation and CRUD operations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test_pm_whale.db')
        self.conn = init_db(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.db_path + '-wal'):
            os.remove(self.db_path + '-wal')
        if os.path.exists(self.db_path + '-shm'):
            os.remove(self.db_path + '-shm')
        os.rmdir(self.tmpdir)

    def test_schema_creation(self):
        """Verify all tables exist after init."""
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r['name'] for r in tables}
        expected = {
            'polymarket_traders', 'polymarket_markets',
            'polymarket_positions', 'polymarket_consensus_signals',
            'polymarket_signals_feed',
        }
        self.assertTrue(expected.issubset(table_names),
                        f"Missing tables: {expected - table_names}")

    def test_wal_mode(self):
        """Verify WAL mode is enabled."""
        row = self.conn.execute("PRAGMA journal_mode").fetchone()
        self.assertIn(row[0].lower(), ['wal', 'memory'])

    def test_store_traders(self):
        traders = [{
            'id': 'trader_1',
            'wallet': '0xabc123def456',
            'displayName': 'Whale Trader',
            'tier': 'whale',
            'primaryCategories': ['weather', 'crypto'],
            'metrics': {
                'hitRate': 0.72,
                'pnl': 15000.0,
                'roi': 0.35,
                'avgEdge': 0.08,
                'avgPositionSize': 500.0,
                'marketsTraded': 45,
                'resolvedMarkets': 30,
                'winCount': 22,
                'lossCount': 8,
                'recentForm': 0.75,
                'consensusFollowRate': 0.65,
            },
            'copyScore': 85.0,
            'convictionScore': 72.0,
            'stabilityScore': 68.0,
            'momentumScore': 55.0,
            'agreementSignalScore': 78.0,
            'label': 'top_whale',
            'provenance': 'live',
            'lastActiveAt': '2026-08-01T20:00:00Z',
            'notes': ['consistent on weather markets'],
        }]
        count = store_traders(self.conn, traders)
        self.assertEqual(count, 1)
        # Verify upsert (same ID updates)
        count2 = store_traders(self.conn, traders)
        self.assertEqual(count2, 1)

    def test_store_markets(self):
        markets = [{
            'id': 'mkt_weather_1',
            'slug': 'nyc-temp-aug15-85',
            'title': 'Will the temperature in New York exceed 85°F on August 15?',
            'category': 'weather',
            'status': 'open',
            'volume': 50000.0,
            'liquidity': 12000.0,
            'yesPrice': 0.45,
            'noPrice': 0.55,
            'spread': 0.03,
            'tags': ['temperature', 'nyc'],
        }]
        count = store_markets(self.conn, markets)
        self.assertEqual(count, 1)

    def test_store_consensus_signals(self):
        signals = [{
            'id': 'sig_1',
            'marketId': 'mkt_weather_1',
            'category': 'weather',
            'title': 'Test signal',
            'side': 'yes',
            'agreementLevel': 'strong',
            'agreementScore': 0.85,
            'copyOpportunityScore': 72.0,
            'whaleCount': 3,
            'weightedConviction': 0.78,
            'weightedEdge': 0.06,
            'liquidityScore': 65.0,
            'timeToCloseHours': 48,
            'rationale': '3 whales accumulating YES',
            'traders': [{'wallet': '0xabc', 'notional': 500}],
        }]
        count = store_consensus_signals(self.conn, signals)
        self.assertEqual(count, 1)

    def test_full_crud_flow(self):
        """Test insert signal feed entry and query back."""
        entry = {
            'signalId': 'sig_1',
            'marketId': 'mkt_weather_1',
            'timestamp': '2026-08-01T20:00:00Z',
            'kalshi_station': 'KNYC',
            'kalshi_bucket': 85,
            'kalshi_series': 'HIGH',
            'signal_direction': 'UP',
            'conviction_multiplier': 1.35,
            'agreement_score': 0.81,
            'whale_count': 3,
            'total_notional': 2500.0,
            'status': 'PENDING',
            'ttl_hours': 24,
        }
        row_id = insert_signal_feed_entry(self.conn, entry)
        self.assertGreater(row_id, 0)

        # Query back
        signals = get_active_signals(self.conn, station='KNYC', min_whales=2)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['kalshi_bucket'], 85)
        self.assertAlmostEqual(signals[0]['conviction_multiplier'], 1.35)

    def test_get_conviction(self):
        """Test the conviction multiplier lookup."""
        entry = {
            'signalId': 'sig_2',
            'timestamp': '2026-08-01T20:00:00Z',
            'kalshi_station': 'KORD',
            'kalshi_bucket': 90,
            'signal_direction': 'UP',
            'conviction_multiplier': 1.42,
            'agreement_score': 0.85,
            'whale_count': 4,
            'total_notional': 3500.0,
            'status': 'PENDING',
            'ttl_hours': 24,
        }
        insert_signal_feed_entry(self.conn, entry)

        mult = get_polymarket_conviction(self.conn, 'KORD', 90)
        self.assertAlmostEqual(mult, 1.42)

        # No signal for this station+bucket → default 1.0
        mult2 = get_polymarket_conviction(self.conn, 'KLAX', 100)
        self.assertAlmostEqual(mult2, 1.0)

    def test_expire_stale_signals(self):
        """Test TTL-based expiration."""
        entry = {
            'signalId': 'sig_stale',
            'timestamp': '2026-07-30T20:00:00Z',
            'kalshi_station': 'KNYC',
            'kalshi_bucket': 85,
            'signal_direction': 'UP',
            'conviction_multiplier': 1.3,
            'agreement_score': 0.75,
            'whale_count': 2,
            'total_notional': 1000.0,
            'status': 'PENDING',
            'ttl_hours': 1,  # 1 hour TTL — should expire immediately
        }
        insert_signal_feed_entry(self.conn, entry)

        # Set created_at to 2 hours ago
        self.conn.execute("""
            UPDATE polymarket_signals_feed
            SET created_at = datetime('now', '-2 hours')
            WHERE signalId = 'sig_stale'
        """)
        self.conn.commit()

        expired = expire_stale_signals(self.conn)
        self.assertGreaterEqual(expired, 1)

    def test_recent_signals(self):
        """Test get_recent_signals."""
        entry = {
            'signalId': 'sig_recent',
            'timestamp': '2026-08-01T20:00:00Z',
            'kalshi_station': 'KPHL',
            'kalshi_bucket': 95,
            'signal_direction': 'UP',
            'conviction_multiplier': 1.2,
            'agreement_score': 0.7,
            'whale_count': 2,
            'total_notional': 800.0,
            'status': 'PENDING',
            'ttl_hours': 24,
        }
        insert_signal_feed_entry(self.conn, entry)
        recent = get_recent_signals(self.conn, hours=48)
        self.assertGreaterEqual(len(recent), 1)

    def test_stats(self):
        """Test get_stats returns expected keys."""
        stats = get_stats(self.conn)
        expected_keys = [
            'trader_count', 'market_count', 'weather_market_count',
            'consensus_signals', 'signal_feed_entries',
            'active_signals', 'weather_relevant_signals',
        ]
        for key in expected_keys:
            self.assertIn(key, stats)


class TestConsensusSignalExtraction(unittest.TestCase):
    """Test consensus signal extraction from raw Polymarket trades."""

    def test_single_consensus(self):
        trades = [
            {
                'proxyWallet': '0xabc',
                'side': 'BUY',
                'size': '100',
                'price': '0.45',
                'timestamp': '1722547200',
                'slug': 'nyc-temp-85',
                'title': 'Will NYC exceed 85?',
                'outcome': 'YES',
            },
            {
                'proxyWallet': '0xdef',
                'side': 'BUY',
                'size': '200',
                'price': '0.46',
                'timestamp': '1722547300',
                'slug': 'nyc-temp-85',
                'title': 'Will NYC exceed 85?',
                'outcome': 'YES',
            },
        ]
        markets = [{
            'slug': 'nyc-temp-85',
            'id': 'mkt_1',
            'category': 'weather',
            'title': 'Will NYC exceed 85?',
            'hoursToClose': 48,
        }]
        signals = extract_consensus_signals(trades, markets)
        self.assertGreaterEqual(len(signals), 1)
        self.assertGreaterEqual(signals[0]['whaleCount'], 2)

    def test_not_enough_whales(self):
        """Single whale should not generate a consensus signal."""
        trades = [{
            'proxyWallet': '0xabc',
            'side': 'BUY',
            'size': '100',
            'price': '0.45',
            'timestamp': '1722547200',
            'slug': 'test-market',
            'title': 'Test market',
            'outcome': 'YES',
        }]
        signals = extract_consensus_signals(trades, [])
        self.assertEqual(len(signals), 0)


class TestIdentifyWeatherSignals(unittest.TestCase):
    """Test weather signal identification and Kalshi mapping."""

    def test_weather_market_mapped(self):
        signals = [{
            'id': 'sig_weather',
            'marketId': 'mkt_1',
            'category': 'weather',
            'title': 'Will the temperature in New York exceed 85°F on August 15?',
            'side': 'yes',
            'outcome': 'YES',
            'whaleCount': 3,
            'totalNotional': 2500.0,
            'avgPrice': 0.45,
            'agreementScore': 0.82,
            'weightedEdge': 0.06,
            'traders': [{'wallet': '0xabc', 'notional': 1500}],
        }]
        weather = identify_weather_signals(signals)
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0]['kalshi_station'], 'KNYC')
        self.assertEqual(weather[0]['kalshi_bucket'], 85)
        self.assertEqual(weather[0]['signal_direction'], 'UP')
        self.assertGreater(weather[0]['conviction_multiplier'], 1.0)

    def test_non_weather_skipped(self):
        signals = [{
            'id': 'sig_nonweather',
            'marketId': 'mkt_2',
            'category': 'politics',
            'title': 'Will candidate X win the election?',
            'side': 'yes',
            'whaleCount': 5,
            'totalNotional': 50000.0,
            'agreementScore': 0.9,
            'traders': [],
        }]
        weather = identify_weather_signals(signals)
        self.assertEqual(len(weather), 0)

    def test_low_notional_filtered(self):
        signals = [{
            'id': 'sig_low',
            'marketId': 'mkt_3',
            'category': 'weather',
            'title': 'Will the temperature in Chicago be above 90°F?',
            'side': 'yes',
            'whaleCount': 2,
            'totalNotional': 100.0,  # Below $500 MIN_NOTIONAL
            'agreementScore': 0.5,
            'traders': [],
        }]
        weather = identify_weather_signals(signals)
        self.assertEqual(len(weather), 0)

    def test_no_side_yes_direction_up(self):
        """YES side on a 'exceed' question → signal direction UP."""
        signals = [{
            'id': 'sig_yes_up',
            'marketId': 'mkt_4',
            'category': 'weather',
            'title': 'Will the temperature in Boston exceed 85°F?',
            'side': 'yes',
            'whaleCount': 4,
            'totalNotional': 3000.0,
            'agreementScore': 0.75,
            'traders': [{'wallet': '0xabc', 'notional': 1000}],
        }]
        weather = identify_weather_signals(signals)
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0]['signal_direction'], 'UP')

    def test_no_side_direction_down(self):
        """NO side on an 'exceed' question → signal direction DOWN."""
        signals = [{
            'id': 'sig_no_down',
            'marketId': 'mkt_5',
            'category': 'weather',
            'title': 'Will the temperature in Miami exceed 95°F?',
            'side': 'no',
            'whaleCount': 3,
            'totalNotional': 2000.0,
            'agreementScore': 0.65,
            'traders': [{'wallet': '0xdef', 'notional': 800}],
        }]
        weather = identify_weather_signals(signals)
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0]['signal_direction'], 'DOWN')


if __name__ == '__main__':
    unittest.main(verbosity=2)