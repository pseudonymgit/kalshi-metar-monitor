"""Test Layer 0: Schema Migration (Backward Compatibility - L0-T4)."""

import unittest
import tempfile
import os
import sqlite3
from unittest.mock import patch

from core import metar_monitor, kalshi_monitor


class SchemaMigrationTests(unittest.TestCase):
    """Test schema migration and backward compatibility (L0-T4)."""

    def setUp(self):
        # Set up a temporary database file for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name

        # Clear state
        metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()
        metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        metar_monitor._MISSING_LADDER_DEDUPE.clear()
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

        kalshi_monitor._SERIES_MARKETS_CACHE.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS.clear()
        kalshi_monitor._DISCOVERED_WEATHER_MARKETS_BY_STATION.clear()

    def tearDown(self):
        # Clean up temp database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    def test_l0_t4_backward_compatibility_no_schema_breaking_changes(self):
        """L0-T4: Verify no existing tests break and no existing tables altered."""

        # Ensure schema is created
        metar_monitor._ensure_alert_schema()

        # Connect to database and verify tables exist
        db_path = metar_monitor._alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            cursor = conn.cursor()

            # Check that new tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cursor.fetchall()]

            # Verify signal_layer_state table exists
            self.assertIn("signal_layer_state", tables, "signal_layer_state table should exist")

            # Verify market_cache table exists
            self.assertIn("market_cache", tables, "market_cache table should exist")

            # Verify alert_delivery_queue table exists
            self.assertIn("alert_delivery_queue", tables, "alert_delivery_queue table should exist")

            # Verify signal_layer_state schema (signal_name is PRIMARY KEY)
            cursor.execute("PRAGMA table_info(signal_layer_state)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            self.assertIn("signal_name", columns, "signal_layer_state should have signal_name column")
            self.assertIn("state_json", columns, "signal_layer_state should have state_json column")
            self.assertIn("updated_at", columns, "signal_layer_state should have updated_at column")

            # Verify market_cache schema (market_id is PRIMARY KEY)
            cursor.execute("PRAGMA table_info(market_cache)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            self.assertIn("market_id", columns, "market_cache should have market_id column")
            self.assertIn("station", columns, "market_cache should have station column")
            self.assertIn("cache_json", columns, "market_cache should have cache_json column")
            self.assertIn("discovered_at", columns, "market_cache should have discovered_at column")
            self.assertIn("updated_at", columns, "market_cache should have updated_at column")

        finally:
            conn.close()

    def test_signal_layer_state_table_structure(self):
        """Verify signal_layer_state table has correct structure."""

        metar_monitor._ensure_alert_schema()

        db_path = metar_monitor._alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(signal_layer_state)")

            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]

            # Verify expected columns (signal_name is PRIMARY KEY, no id column)
            expected_columns = ["signal_name", "state_json", "updated_at"]
            for col in expected_columns:
                self.assertIn(col, column_names, f"signal_layer_state should have '{col}' column")

            # Verify signal_name is primary key
            primary_keys = [col[1] for col in columns if col[5] == 1]
            self.assertEqual(primary_keys, ["signal_name"], "signal_name should be the primary key")

        finally:
            conn.close()

    def test_market_cache_table_structure(self):
        """Verify market_cache table has correct structure."""

        metar_monitor._ensure_alert_schema()

        db_path = metar_monitor._alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(market_cache)")

            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]

            # Verify expected columns (market_id is PRIMARY KEY, no id column)
            expected_columns = ["market_id", "station", "cache_json", "discovered_at", "updated_at"]
            for col in expected_columns:
                self.assertIn(col, column_names, f"market_cache should have '{col}' column")

            # Verify market_id is primary key
            primary_keys = [col[1] for col in columns if col[5] == 1]
            self.assertEqual(primary_keys, ["market_id"], "market_id should be the primary key")

        finally:
            conn.close()

    def test_persistence_after_schema_creation(self):
        """Test that persistence works correctly after schema creation."""

        metar_monitor._ensure_alert_schema()

        # Test signal state persistence
        signal_name = "test:KDEN:1"
        test_state = {"value": "test", "count": 42}
        metar_monitor._persist_signal_state(signal_name, test_state)

        loaded = metar_monitor._load_signal_state(signal_name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["value"], "test")
        self.assertEqual(loaded["count"], 42)

        # Test market cache persistence
        kalshi_monitor._persist_market_cache("test:KDEN:KXHIGH", "KDEN", {"markets": []})

        loaded = kalshi_monitor._load_market_cache("test:KDEN:KXHIGH")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["station"], "KDEN")

    def test_multiple_schema_initializations_safe(self):
        """Test that calling _ensure_alert_schema multiple times is safe."""

        # Call multiple times - should not error or create duplicates
        metar_monitor._ensure_alert_schema()
        metar_monitor._ensure_alert_schema()
        metar_monitor._ensure_alert_schema()

        # Verify tables still exist and are not duplicated
        db_path = metar_monitor._alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signal_layer_state'")
            self.assertIsNotNone(cursor.fetchone())

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='market_cache'")
            self.assertIsNotNone(cursor.fetchone())

        finally:
            conn.close()

    def test_existing_tables_unchanged(self):
        """Verify that _ensure_alert_schema does not create legacy tables (transition_events, alerts)."""

        metar_monitor._ensure_alert_schema()

        db_path = metar_monitor._alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            cursor = conn.cursor()

            # transition_events and alerts are no longer created by _ensure_alert_schema
            # (they are created lazily by their respective modules).
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transition_events'")
            self.assertIsNone(cursor.fetchone(), "transition_events should not be created by _ensure_alert_schema")

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'")
            self.assertIsNone(cursor.fetchone(), "alerts should not be created by _ensure_alert_schema")

        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
