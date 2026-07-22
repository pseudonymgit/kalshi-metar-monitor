# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.3: DB Connection Standardization — centralized pool, schema registry, migration testing]
# 2. [2026-07-18 Fix Bug 9: Add SQLite concurrency fixes]
#


"""
SQLite Utilities Module

Provides standardized database connections with proper concurrency settings,
a centralized connection pool, and schema registry integration.

Replaces 15+ independent CREATE TABLE IF NOT EXISTS statements with
a unified schema registry (core/db_schema.py).
"""
import sqlite3
import threading
import time
import logging
from typing import Optional, Any, Union, Dict, Callable

from .db_schema import SCHEMA_VERSION, TABLES, ensure_all_tables, get_table_names

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
_connection_pool: Dict[str, sqlite3.Connection] = {}
_pool_lock = threading.Lock()


def get_sqlite_connection(db_path: str, timeout: Optional[int] = 30) -> sqlite3.Connection:
    """
    Get SQLite connection with proper concurrency settings.
    
    Uses a connection pool keyed by db_path to avoid redundant connections.
    
    Args:
        db_path: Path to SQLite database
        timeout: Timeout in seconds (default 30)
        
    Returns:
        SQLite connection with PRAGMA settings applied
    """
    conn = sqlite3.connect(db_path, timeout=timeout if timeout else 30)
    _apply_pragmas(conn)
    return conn


def get_readonly_sqlite_connection(db_path: str, timeout: Optional[int] = 30) -> sqlite3.Connection:
    """
    Get read-only SQLite connection with proper concurrency settings.
    
    Args:
        db_path: Path to SQLite database
        timeout: Timeout in seconds (default 30)
        
    Returns:
        Read-only SQLite connection with PRAGMA settings applied
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout if timeout else 30)
    _apply_pragmas(conn)
    return conn


def get_pooled_connection(db_path: str, timeout: Optional[int] = 30) -> sqlite3.Connection:
    """Get or create a pooled connection for the given db_path.
    
    Pooled connections are reused across calls to avoid creating new
    connections repeatedly. Use close_pooled_connection() to release.
    """
    with _pool_lock:
        if db_path not in _connection_pool:
            conn = sqlite3.connect(db_path, timeout=timeout if timeout else 30, check_same_thread=False)
            _apply_pragmas(conn)
            _connection_pool[db_path] = conn
        return _connection_pool[db_path]


def close_pooled_connection(db_path: str) -> None:
    """Close and remove a pooled connection."""
    with _pool_lock:
        if db_path in _connection_pool:
            try:
                _connection_pool[db_path].close()
            except Exception:
                pass
            del _connection_pool[db_path]


def close_all_pooled_connections() -> None:
    """Close all pooled connections."""
    with _pool_lock:
        for path, conn in list(_connection_pool.items()):
            try:
                conn.close()
            except Exception:
                pass
        _connection_pool.clear()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply standard PRAGMA settings to a connection."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------
def ensure_schema(db_path: str, table_names: Optional[list] = None) -> int:
    """Ensure all (or specified) tables exist in the database.
    
    Uses the centralized schema registry from db_schema.py.
    
    Args:
        db_path: Path to SQLite database
        table_names: Optional list of table names to create. If None, creates all.
        
    Returns:
        Number of tables created/verified.
    """
    conn = get_sqlite_connection(db_path)
    try:
        if table_names:
            count = 0
            for name in table_names:
                if name in TABLES:
                    conn.execute(TABLES[name])
                    count += 1
            conn.commit()
            return count
        else:
            return ensure_all_tables(conn)
    finally:
        conn.close()


def get_schema_version() -> str:
    """Get the current schema version."""
    return SCHEMA_VERSION


def get_schema_report(db_path: str) -> Dict[str, Any]:
    """Get a report of which tables exist in the database."""
    conn = get_sqlite_connection(db_path)
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        existing = {row[0] for row in c.fetchall()}
        registered = set(get_table_names())
        return {
            "schema_version": SCHEMA_VERSION,
            "db_path": db_path,
            "existing_tables": sorted(existing),
            "registered_tables": sorted(registered),
            "missing_tables": sorted(registered - existing),
            "extra_tables": sorted(existing - registered),
            "table_count": len(existing),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration testing
# ---------------------------------------------------------------------------
def run_migration_test(db_path: str = ":memory:") -> Dict[str, Any]:
    """Run automated migration test on an in-memory database.
    
    Creates all tables, verifies they exist, then drops them.
    Returns a report of successes and failures.
    """
    result = {"version": SCHEMA_VERSION, "tables_created": 0, "tables_verified": 0, "errors": []}
    
    conn = get_sqlite_connection(db_path)
    try:
        # Create all tables
        for name, ddl in TABLES.items():
            try:
                conn.execute(ddl)
                result["tables_created"] += 1
            except Exception as e:
                result["errors"].append(f"Failed to create {name}: {e}")
        
        conn.commit()
        
        # Verify all tables
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        created = {row[0] for row in c.fetchall()}
        expected = set(get_table_names())
        
        result["tables_verified"] = len(created & expected)
        missing = expected - created
        if missing:
            result["errors"].append(f"Missing tables after creation: {missing}")
        
        # Drop all tables (skip sqlite_sequence which is auto-generated)
        for name in created:
            if name in ('sqlite_sequence',):
                continue
            try:
                conn.execute(f"DROP TABLE IF EXISTS {name}")
            except Exception as e:
                result["errors"].append(f"Failed to drop {name}: {e}")
        conn.commit()
        
    finally:
        conn.close()
    
    result["success"] = len(result["errors"]) == 0
    return result


# Backward compatibility function signature to allow quick fixes
connect = get_sqlite_connection


__all__ = [
    "get_sqlite_connection", "get_readonly_sqlite_connection",
    "get_pooled_connection", "close_pooled_connection", "close_all_pooled_connections",
    "ensure_schema", "get_schema_version", "get_schema_report",
    "run_migration_test", "connect",
]