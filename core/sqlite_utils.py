"""
DEPRECATED — SQLite Utilities Module

This module is being replaced by core/db_connection.py.

Kept as backward-compatible wrapper. New code should use:
    from core.db_connection import get_connection, get_readonly_connection

Migration: See docs/plans/FP-DB-STRATEGY.md for file-by-file migration plan.
"""
import warnings
import os
from typing import Optional

# Re-export from new module
from core.db_connection import (
    get_connection as _get_connection,
    get_readonly_connection as _get_readonly_connection,
    close_all,
    get_config,
    get_registry_keys,
    MOST_COMMON_DBS,
)

# Module-level cache for backward-compatible path-based lookups
_PATH_TO_DB_KEY: dict = {}


def _resolve_db_key(db_path: str) -> str:
    """Try to resolve a file path back to a DB registry key."""
    # Check cache
    if db_path in _PATH_TO_DB_KEY:
        return _PATH_TO_DB_KEY[db_path]

    # Normalize the path
    abs_path = os.path.abspath(os.path.expanduser(db_path))

    # Try to match against registry paths
    from core.db_connection import DB_REGISTRY, DATA_DIR
    for key, config in DB_REGISTRY.items():
        if not config.relative_path.startswith('/'):
            resolved = os.path.join(DATA_DIR, config.relative_path)
        else:
            resolved = config.relative_path
        if os.path.abspath(resolved) == abs_path:
            _PATH_TO_DB_KEY[db_path] = key
            return key

    # Fallback: try common names
    basename = os.path.basename(abs_path)
    for shortcut, key in MOST_COMMON_DBS.items():
        cfg = DB_REGISTRY.get(key)
        if cfg and (basename == cfg.relative_path or basename == os.path.basename(cfg.relative_path)):
            _PATH_TO_DB_KEY[db_path] = key
            return key

    # No match — this will use the default fallback
    return 'metar_backfill'


def get_sqlite_connection(db_path: str, timeout: Optional[int] = 30) -> 'sqlite3.Connection':
    """
    DEPRECATED: Use get_connection(db_key) from core.db_connection instead.

    Args:
        db_path: Path to SQLite database (for backward compatibility)
        timeout: Timeout in seconds (used only if path can't be resolved)

    Returns:
        SQLite connection with PRAGMA settings applied
    """
    warnings.warn(
        "get_sqlite_connection() is deprecated. "
        "Use get_connection('db_key') from core.db_connection instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import sqlite3 as _sqlite3

    db_key = _resolve_db_key(db_path)

    try:
        # Try using the new connection manager
        conn_mgr = _get_connection(db_key)
        # Need to manually enter the context manager to get the connection object
        return conn_mgr.__enter__()
    except (KeyError, Exception):
        # Fallback: create connection directly
        conn = _sqlite3.connect(db_path, timeout=timeout if timeout else 30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn


def get_readonly_sqlite_connection(db_path: str, timeout: Optional[int] = 30) -> 'sqlite3.Connection':
    """
    DEPRECATED: Use get_readonly_connection(db_key) from core.db_connection instead.

    Args:
        db_path: Path to SQLite database (for backward compatibility)
        timeout: Timeout in seconds (used only if path can't be resolved)

    Returns:
        Read-only SQLite connection with PRAGMA settings applied
    """
    warnings.warn(
        "get_readonly_sqlite_connection() is deprecated. "
        "Use get_readonly_connection('db_key') from core.db_connection instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    import sqlite3 as _sqlite3

    db_key = _resolve_db_key(db_path)

    try:
        conn_mgr = _get_readonly_connection(db_key)
        return conn_mgr.__enter__()
    except (KeyError, Exception):
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout if timeout else 30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn


# Keep backward-compatible alias
connect = get_sqlite_connection


def close_connection(conn) -> None:
    """
    Close a connection obtained via the deprecated get_sqlite_connection path.

    For cached connections (new module), this is a no-op — the cache manages lifecycle.
    For fallback connections, this properly closes.
    """
    try:
        conn.close()
    except Exception:
        pass