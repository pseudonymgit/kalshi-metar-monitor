# CHANGELOG (last 10 broad changes):
# 1. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Database Migration

Creates the composite index required for Phase 3 prediction layer queries.

Index: settlement_epochs(station, market_type, local_trading_date)

This index enables efficient point-lookup queries for settlement epochs by
station and market_type with date-range filters, which is the core query
pattern for Phase 3 prediction.

No data migration or transformation is performed. This is schema-only.
"""

import os

from .sqlite_utils import get_sqlite_connection
from core.settlement_epoch_logger import _alert_db_path as get_db_path


def _resolve_db_path():
    """
    Resolve the actual database path, handling different deployment environments.
    Prioritizes:
    1. ALERT_DB_PATH environment variable
    2. Local path for development/testing
    3. Fallback to /home/node/.openclaw/workspace/prototypes/weather-engine-source/
    """
    # Try the configured path first
    configured = get_db_path()
    
    # If it starts with /var/data and doesn't exist, try local fallback
    if configured.startswith("/var/data"):
        # Check if /var/data exists and is writable
        if not os.path.exists("/var/data") or not os.access("/var/data", os.W_OK):
            # Fall back to project directory
            project_root = os.path.dirname(os.path.abspath(__file__))
            fallback = os.path.join(project_root, "alerts.db")
            return fallback
    
    return configured


def ensure_phase3_index():
    """
    Create the composite index on settlement_epochs if it doesn't exist.
    
    This is idempotent - running multiple times is safe.
    Also creates the table if it doesn't exist (for fresh deployments).
    """
    db_path = _resolve_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    conn = get_sqlite_connection(db_path, timeout=1)
    try:
        cursor = conn.cursor()
        
        # Create table if it doesn't exist (for fresh deployments)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS settlement_epochs (
                id INTEGER PRIMARY KEY,
                station TEXT NOT NULL,
                market_type TEXT,
                local_trading_date TEXT NOT NULL,
                settlement_bucket INTEGER NOT NULL,
                prior_settlement_bucket INTEGER,
                settlement_timestamp_utc TEXT NOT NULL,
                settlement_jump_magnitude INTEGER,
                epoch_status TEXT NOT NULL,
                epoch_close_reason TEXT,
                epoch_close_timestamp_utc TEXT,
                reversion_occurred INTEGER NOT NULL DEFAULT 0,
                first_reversion_timestamp_utc TEXT,
                max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
                duration_at_or_above_settlement_seconds REAL NOT NULL DEFAULT 0,
                duration_strictly_above_settlement_seconds REAL NOT NULL DEFAULT 0,
                terminal_state_reached INTEGER NOT NULL DEFAULT 0,
                settlement_transition_event_id INTEGER,
                last_transition_event_id INTEGER,
                last_transition_timestamp_utc TEXT,
                last_transition_temp_f REAL
            )
            """
        )
        conn.commit()
        
        # Check if index already exists
        cursor.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_settlement_epochs_station_date'
            """
        )
        exists = cursor.fetchone()
        
        if not exists:
            cursor.execute(
                """
                CREATE INDEX idx_settlement_epochs_station_date
                ON settlement_epochs(station, market_type, local_trading_date)
                """
            )
            conn.commit()
            print(f"Created index idx_settlement_epochs_station_date on {db_path}")
        else:
            print(f"Index idx_settlement_epochs_station_date already exists on {db_path}")
    finally:
        conn.close()


def verify_index():
    """
    Verify the index exists and works correctly.
    """
    db_path = _resolve_db_path()
    
    conn = get_sqlite_connection(db_path, timeout=1)
    try:
        cursor = conn.cursor()
        
        # Verify index exists
        cursor.execute(
            """
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type='index' AND name='idx_settlement_epochs_station_date'
            """
        )
        result = cursor.fetchone()
        
        if result:
            print(f"Index verification passed:")
            print(f"  Name: {result[0]}")
            print(f"  Table: {result[1]}")
            print(f"  SQL: {result[2]}")
            return True
        else:
            print("Index verification FAILED - index not found")
            return False
    finally:
        conn.close()


def drop_index():
    """
    Drop the index (for testing/recovery only). Not part of normal operation.
    """
    db_path = _resolve_db_path()
    
    conn = get_sqlite_connection(db_path, timeout=1)
    try:
        cursor = conn.cursor()
        cursor.execute("DROP INDEX IF EXISTS idx_settlement_epochs_station_date")
        conn.commit()
        print(f"Dropped index on {db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "verify":
            verify_index()
        elif cmd == "drop":
            drop_index()
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: p3_db_migration.py [verify|drop]")
    else:
        ensure_phase3_index()
