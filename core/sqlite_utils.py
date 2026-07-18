"""
SQLite Utilities Module

Provides standardized database connections with proper concurrency settings.
"""
import sqlite3
from typing import Optional, Any, Union


def get_sqlite_connection(db_path: str, timeout: Optional[int] = 30) -> sqlite3.Connection:
    """
    Get SQLite connection with proper concurrency settings.
    
    Args:
        db_path: Path to SQLite database
        timeout: Timeout in seconds (default 30)
        
    Returns:
        SQLite connection with PRAGMA settings applied
    """
    conn = sqlite3.connect(db_path, timeout=timeout if timeout else 30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
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
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


# Backward compatibility function signature to allow quick fixes
connect = get_sqlite_connection