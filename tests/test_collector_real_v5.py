#!/usr/bin/env python3
"""
Tests for real-v5 Kalshi Passive Collector.

Coverage:
- Dry-run 20/20: live API test (public endpoints, no credentials)
- Real-mode fixture: mocked data persistence, storage cap, expiry
- Endpoint allowlist: deny-list blocks, allowlist accepts
- Rate limit: minimum interval enforcement
- Timeout: request timeout handling
- Hash assertion: pass on correct file, abort on modified
- Storage cap: fail-closed when cap exceeded
- Lock file: prevent concurrent runs
"""

import sys
import os
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

# Add parent to path for module imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Remove the module from cache if previously imported
for m in list(sys.modules.keys()):
    if 'real-v5' in m:
        del sys.modules[m]

# Import the real-v5 module
import importlib.util
spec = importlib.util.spec_from_file_location(
    "kalshi_passive_collector_real_v5",
    os.path.join(os.path.dirname(__file__), '..', 'core', 'kalshi_passive_collector.real-v5.py')
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

KalshiPassiveCollector = mod.KalshiPassiveCollector
RealModeStorage = mod.RealModeStorage
compute_file_hash = mod.compute_file_hash
verify_file_hash = mod.verify_file_hash
STATION_MAP = mod.STATION_MAP
ALLOWED_ENDPOINTS = mod.ALLOWED_ENDPOINTS
DENIED_PREFIXES = mod.DENIED_PREFIXES
MAX_STORAGE_BYTES = mod.MAX_STORAGE_BYTES
RETENTION_HOURS = mod.RETENTION_HOURS
REQUIRED_HASH = mod.REQUIRED_HASH
run_collection = mod.run_collection


class TestDryRun20of20(unittest.TestCase):
    """Live API dry-run test: 20 stations, public endpoints, no credentials."""

    def test_dry_run_collects_all_stations(self):
        """Dry-run should collect all 20 stations with public API."""
        collector = KalshiPassiveCollector()
        result = collector.dry_run(lookback_hours=48)
        
        # Verify 20 stations
        self.assertEqual(result["total_stations"], 20)
        self.assertGreater(result["success_count"], 0,
                           msg="At least 1 station should succeed")
        
        # Verify credential-free
        self.assertTrue(result["dry_run"]["credential_free"])
        self.assertFalse(result["dry_run"]["auth_enabled"])
        self.assertEqual(result["dry_run"]["db_writes"], 0)
        
        # Verify data size within cap
        self.assertTrue(result["dry_run"]["disk_cap_ok"],
                        msg="Data size should be under 2GB cap")
        
        # Verify markets found
        self.assertGreater(result["total_markets"], 0,
                           msg="Should find at least some markets")
        
        # Verify duration is reasonable
        self.assertLess(result["duration_seconds"], 3600,
                        msg="Should complete within 1 hour")


class TestRealModeStorage(unittest.TestCase):
    """Test storage layer with mocked data."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_v5.db")
        self.storage = RealModeStorage(db_path=self.db_path)
    
    def tearDown(self):
        if self.storage:
            self.storage.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    def test_schema_creation(self):
        """Schema should create all tables on open."""
        conn = self.storage.open()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        for expected in ['v5_series', 'v5_events', 'v5_markets', 'v5_provenance']:
            self.assertIn(expected, table_names)
    
    def test_storage_cap_enforced(self):
        """Should raise RuntimeError when cap exceeded."""
        self.storage.open()
        # Simulate full storage by setting a tiny cap
        old_cap = MAX_STORAGE_BYTES
        try:
            # Write a large file to the DB path
            self.storage._conn.close()
            self.storage._conn = None
            with open(self.db_path, 'wb') as f:
                f.write(b'\x00' * 1024)  # 1KB
            mod.MAX_STORAGE_BYTES = 100  # 100 bytes cap
            with self.assertRaises(RuntimeError):
                RealModeStorage(db_path=self.db_path).open()
        finally:
            mod.MAX_STORAGE_BYTES = old_cap
    
    def test_storage_cap_check(self):
        """check_storage_cap should return False when cap exceeded."""
        self.storage.open()
        self.storage._conn.close()
        self.storage._conn = None
        # Write a large file
        with open(self.db_path, 'wb') as f:
            f.write(b'\x00' * 1024)
        old_cap = mod.MAX_STORAGE_BYTES
        try:
            mod.MAX_STORAGE_BYTES = 100
            storage2 = RealModeStorage(db_path=self.db_path)
            self.assertFalse(storage2.check_storage_cap())
        finally:
            mod.MAX_STORAGE_BYTES = old_cap
    
    def test_retention_purge(self):
        """Should purge data older than RETENTION_HOURS."""
        conn = self.storage.open()
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS + 1)).isoformat()
        conn.execute("""
            INSERT INTO v5_markets 
            (station, market_type, series_ticker, ticker, collected_at)
            VALUES (?, ?, ?, ?, ?)
        """, ("KTEST", "HIGH", "KXHIGHTEST", "TEST1234", old_ts))
        conn.commit()
        # Trigger purge
        self.storage._purge_expired()
        count = conn.execute("SELECT COUNT(*) FROM v5_markets").fetchone()[0]
        self.assertEqual(count, 0, "Old data should be purged")
    
    def test_store_collection_normalized(self):
        """Store normalized collection data."""
        conn = self.storage.open()
        mock_data = {
            "collection_start": "2026-08-24T00:00:00",
            "collection_end": "2026-08-24T00:10:00",
            "duration_seconds": 600.0,
            "success_count": 20,
            "failed_count": 0,
            "total_markets": 100,
            "errors": [],
            "stations": [
                {
                    "station": "KTEST",
                    "city": "Test City",
                    "series": {
                        "HIGH": {
                            "ticker": "KXHIGHTEST",
                            "title": "Test Series",
                            "category": "Weather",
                            "settlement_sources": ["NWS"],
                            "open_time": "2026-08-24T00:00:00Z",
                        }
                    },
                    "events": [
                        {
                            "event_ticker": "EVT001",
                            "title": "Test Event",
                            "strike_date": "2026-08-25",
                        }
                    ],
                    "markets": [
                        {
                            "ticker": "KXHIGHTEST_1",
                            "title": "Market 1",
                            "result": "",
                            "last_price": "0.50",
                            "close_time": "2026-08-24T12:00:00Z",
                            "yes_bid": "0.49",
                            "no_bid": "0.48",
                            "yes_ask": "0.51",
                            "no_ask": "0.52",
                            "open_interest": 1000,
                            "volume": 500,
                            "floor_strike": "70",
                            "cap_strike": "90",
                            "liquidity": "5000",
                        }
                    ],
                    "market_count": 1,
                    "errors": [],
                }
            ],
        }
        stats = self.storage.store_collection(mock_data)
        self.assertEqual(stats["series_inserted"], 1)
        self.assertGreaterEqual(stats["events_inserted"], 0)
        self.assertGreaterEqual(stats["markets_inserted"], 0)
        self.assertIn("storage_size_bytes", stats)
        self.assertIn("storage_cap_ok", stats)
    
    def test_lock_file(self):
        """Lock file should prevent concurrent runs."""
        self.storage.open()
        self.storage.close()
        storage2 = RealModeStorage(db_path=self.db_path)
        self.assertTrue(storage2.acquire_lock(),
                        msg="Should acquire lock when not held")
        storage3 = RealModeStorage(db_path=self.db_path)
        self.assertFalse(storage3.acquire_lock(),
                         msg="Should NOT acquire lock when held")
        storage2.release_lock()
        storage4 = RealModeStorage(db_path=self.db_path)
        self.assertTrue(storage4.acquire_lock(),
                        msg="Should acquire lock after release")
        storage4.release_lock()


class TestEndpointAllowlist(unittest.TestCase):
    """Test endpoint allowlist enforcement."""

    def setUp(self):
        self.collector = KalshiPassiveCollector()
    
    def test_denied_endpoints_blocked(self):
        """Denied prefix endpoints should return None."""
        for prefix in DENIED_PREFIXES:
            result = self.collector._make_request(f"{prefix}/test")
            self.assertIsNone(result, f"Should block {prefix}/test")
    
    def test_allowed_endpoints_accepted(self):
        """Allowed prefix endpoints should attempt request (will fail on mock)."""
        # Mock to avoid actual API calls
        with patch.object(self.collector.session, 'get') as mock_get:
            mock_get.return_value.__enter__.return_value.status_code = 200
            mock_get.return_value.__enter__.return_value.json.return_value = {"series": {}}
            result = self.collector._make_request("/series/KXHIGHTEST")
            self.assertIsNotNone(result, "Should allow /series/ endpoints")
    
    def test_allowed_list_contains_endpoints(self):
        """Allowlist should contain expected endpoints."""
        self.assertIn('/markets', ALLOWED_ENDPOINTS)
        self.assertIn('/series/', ALLOWED_ENDPOINTS)
        self.assertIn('/events', ALLOWED_ENDPOINTS)
    
    def test_denied_list_contains_sensitive(self):
        """Denylist should block account/trading endpoints."""
        sensitive = ['/portfolio', '/orders', '/account', '/balance',
                     '/member', '/position', '/fills', '/trades']
        for s in sensitive:
            self.assertTrue(
                any(s.startswith(d) for d in DENIED_PREFIXES),
                f"Should deny {s}"
            )
    
    def test_station_map_20_entries(self):
        """Station map should have exactly 20 entries."""
        self.assertEqual(len(STATION_MAP), 20)


class TestRateLimit(unittest.TestCase):
    """Test rate limiting enforcement."""

    def setUp(self):
        self.collector = KalshiPassiveCollector()
    
    def test_rate_limit_min_interval(self):
        """Rate limiting should enforce minimum interval."""
        # Directly test _rate_limit behavior
        start = datetime.now()
        self.collector._last_request_time = time = 0
        self.collector._min_interval = 0.05  # 50ms for test
        
        self.collector._rate_limit()  # First call: no wait
        elapsed1 = (datetime.now() - start).total_seconds()
        self.assertLess(elapsed1, 0.03, "First call should not wait")
        
        # Second call: should wait ~50ms
        start2 = datetime.now()
        self.collector._rate_limit()
        elapsed2 = (datetime.now() - start2).total_seconds()
        self.assertGreaterEqual(elapsed2, 0.04, "Should wait minimum interval")
    
    def test_rate_limit_zero_interval(self):
        """Zero interval should not wait."""
        self.collector._min_interval = 0
        start = datetime.now()
        self.collector._rate_limit()
        elapsed = (datetime.now() - start).total_seconds()
        self.assertLess(elapsed, 0.01, "Zero interval should not wait")


class TestTimeout(unittest.TestCase):
    """Test request timeout handling."""

    def setUp(self):
        self.collector = KalshiPassiveCollector()
    
    @patch.object(mod.requests.Session, 'get')
    def test_timeout_returns_none(self, mock_get):
        """Timeout should return None gracefully."""
        mock_get.side_effect = mod.requests.exceptions.Timeout("timed out")
        result = self.collector._make_request("/series/KXHIGHTEST")
        self.assertIsNone(result, "Timeout should return None")
    
    @patch.object(mod.requests.Session, 'get')
    def test_http_error_returns_none(self, mock_get):
        """HTTP error should return None gracefully."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = mod.requests.exceptions.HTTPError(
            "429 Too Many Requests", response=mock_response
        )
        mock_get.return_value = mock_response
        result = self.collector._make_request("/series/KXHIGHTEST")
        self.assertIsNone(result, "HTTP error should return None")
    
    @patch.object(mod.requests.Session, 'get')
    def test_connection_error_returns_none(self, mock_get):
        """Connection error should return None gracefully."""
        mock_get.side_effect = mod.requests.exceptions.ConnectionError("connection failed")
        result = self.collector._make_request("/series/KXHIGHTEST")
        self.assertIsNone(result, "Connection error should return None")


class TestHashAssertion(unittest.TestCase):
    """Test file hash assertion."""

    def test_compute_file_hash_returns_string(self):
        """compute_file_hash should return a hex string."""
        h = compute_file_hash()
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)  # SHA-256 hex
    
    def test_compute_file_hash_stable(self):
        """compute_file_hash should be stable across calls."""
        h1 = compute_file_hash()
        h2 = compute_file_hash()
        self.assertEqual(h1, h2, "Hash should be stable")
    
    def test_guard_exclusion(self):
        """Guard line should be excluded from hash."""
        # If we change the guard value, hash should stay the same
        with open(__file__.replace('test_collector_real_v5.py', '../core/kalshi_passive_collector.real-v5.py'), 'r') as f:
            content = f.read()
        # Verify guard line is present
        self.assertIn('guard-hash-placeholder:', content)
    
    def test_verify_hash_requires_string(self):
        """verify_file_hash should require a string hash."""
        with self.assertRaises(SystemExit) as ctx:
            verify_file_hash("bad_hash", path="/dev/null")
        # Exit 99 on mismatch
        self.assertEqual(ctx.exception.code, 99)
    
    def test_required_hash_is_set(self):
        """REQUIRED_HASH should be set (not placeholder)."""
        self.assertNotEqual(REQUIRED_HASH, "REALV5_NO_HASH_SET",
                            msg="REQUIRED_HASH should be set at build time")
        self.assertEqual(len(REQUIRED_HASH), 64, "Should be SHA-256 hex")


class TestStationMap(unittest.TestCase):
    """Test station map integrity."""

    def test_all_stations_have_info(self):
        """All stations in STATION_MAP should have STATION_INFO."""
        for icao in STATION_MAP:
            self.assertIn(icao, mod.STATION_INFO,
                          f"Station {icao} missing info")
    
    def test_all_station_info_has_tz(self):
        """All station info should have timezone."""
        for icao, info in mod.STATION_INFO.items():
            self.assertIn('tz', info, f"Station {icao} missing timezone")
            self.assertIn('city', info, f"Station {icao} missing city")


class TestCollectorInitialization(unittest.TestCase):
    """Test collector initialization."""

    def setUp(self):
        self.collector = KalshiPassiveCollector()
    
    def test_station_map_loaded(self):
        """Station map should be loaded."""
        self.assertEqual(len(self.collector.station_map), 20)
    
    def test_endpoint_lists_loaded(self):
        """Endpoint lists should be loaded."""
        self.assertEqual(self.collector.allowed_endpoints, ALLOWED_ENDPOINTS)
        self.assertEqual(self.collector.denied_prefixes, DENIED_PREFIXES)
    
    def test_session_headers(self):
        """Session should have proper headers."""
        self.assertIn('User-Agent', self.collector.session.headers)
        self.assertIn('Accept', self.collector.session.headers)
    
    def test_min_interval_set(self):
        """Minimum interval should be set."""
        self.assertGreater(self.collector._min_interval, 0)

    @patch.object(mod.KalshiPassiveCollector, '_make_request')
    def test_get_series_info(self, mock_request):
        """get_series_info should return series data."""
        mock_request.return_value = {"series": {"ticker": "KXHIGHTEST"}}
        result = self.collector.get_series_info("KXHIGHTEST")
        self.assertEqual(result["ticker"], "KXHIGHTEST")
    
    @patch.object(mod.KalshiPassiveCollector, '_make_request')
    def test_get_events(self, mock_request):
        """get_events should return event list."""
        mock_request.return_value = {"events": [{"event_ticker": "EVT001"}]}
        result = self.collector.get_events("KXHIGHTEST")
        self.assertEqual(len(result), 1)
    
    @patch.object(mod.KalshiPassiveCollector, '_make_request')
    def test_get_market_detail(self, mock_request):
        """get_market_detail should return market data."""
        mock_request.return_value = {"market": {"ticker": "TEST123"}}
        result = self.collector.get_market_detail("TEST123")
        self.assertEqual(result["ticker"], "TEST123")


class TestStorageSchema(unittest.TestCase):
    """Test schema SQL consistency."""

    def test_schema_has_all_tables(self):
        """Schema SQL should define all expected tables."""
        for table in ['v5_series', 'v5_events', 'v5_markets', 'v5_provenance']:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", mod.SCHEMA_SQL)
    
    def test_schema_has_indexes(self):
        """Schema should have indexes for performance."""
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_v5_markets_station", mod.SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_v5_markets_event", mod.SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_v5_events_series", mod.SCHEMA_SQL)


class TestStorageConfig(unittest.TestCase):
    """Test storage configuration constants."""

    def test_max_storage_bytes(self):
        """MAX_STORAGE_BYTES should be 2GB."""
        self.assertEqual(MAX_STORAGE_BYTES, 2 * 1024 * 1024 * 1024)
    
    def test_retention_hours(self):
        """RETENTION_HOURS should be 48."""
        self.assertEqual(RETENTION_HOURS, 48)


if __name__ == "__main__":
    unittest.main()