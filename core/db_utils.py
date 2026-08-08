"""
db_utils.py — Context-manager-based SQLite connection utilities.

Provides lightweight, composable wrappers around sqlite3.connect() that
auto-manage connections, transactions, and cleanup.

All functions accept a path argument, avoiding any global registry or
singleton state. Use these as drop-in replacements for raw sqlite3.connect().

Functions:
    get_db(path)                     — open connection with standard PRAGMAs
    with_db(path)                    — context manager (auto-commit + close)
    query_db(path, sql, params)      — one-shot SELECT, returns list of rows
    execute_db(path, sql, params)    — one-shot INSERT/UPDATE/DELETE
"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union


def get_db(path: str, *, timeout: int = 5, row_factory: bool = True,
           wal: bool = True, busy_timeout: int = 5000) -> sqlite3.Connection:
    """
    Open a connection to a SQLite database with standard PRAGMAs.

    Args:
        path: Path to the SQLite database file.
        timeout: Connection timeout in seconds (default 5).
        row_factory: If True (default), set conn.row_factory = sqlite3.Row.
        wal: Enable WAL journal mode (default True).
        busy_timeout: Busy timeout in ms (default 5000).

    Returns:
        sqlite3.Connection with PRAGMAs applied.
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)

    if wal:
        conn.execute("PRAGMA journal_mode=WAL;")
    if busy_timeout:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout};")

    if row_factory:
        conn.row_factory = sqlite3.Row

    return conn


@contextmanager
def with_db(path: str, *, timeout: int = 5, row_factory: bool = True,
            wal: bool = True, busy_timeout: int = 5000,
            commit: bool = True) -> Iterator[sqlite3.Connection]:
    """
    Context manager that opens a connection, yields it, then commits and closes.

    Usage::

        with with_db("data/my.db") as conn:
            conn.execute("INSERT INTO t (x) VALUES (?)", (42,))
        # auto-committed and closed

    Args:
        path: Path to the SQLite database file.
        timeout: Connection timeout in seconds.
        row_factory: If True, set conn.row_factory = sqlite3.Row.
        wal: Enable WAL journal mode.
        busy_timeout: Busy timeout in ms.
        commit: If True, commit on success before closing (default True).
                On exception the transaction is rolled back.
    """
    conn = get_db(path, timeout=timeout, row_factory=row_factory,
                  wal=wal, busy_timeout=busy_timeout)
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_db(path: str, sql: str,
             params: Optional[Union[Sequence[Any], Dict[str, Any]]] = None,
             *, timeout: int = 5, row_factory: bool = True,
             fetchall: bool = True) -> List[Union[sqlite3.Row, Dict[str, Any]]]:
    """
    One-shot SELECT query with auto-close.

    Opens a connection, executes the query, fetches results, and closes.

    Args:
        path: Path to the SQLite database file.
        sql: SQL SELECT statement.
        params: Optional parameters (sequence or dict).
        timeout: Connection timeout in seconds.
        row_factory: If True (default), results are sqlite3.Row objects.
        fetchall: If True (default), fetch all rows. If False, fetch one.

    Returns:
        List of rows (Row objects or dicts if row_factory is True).
    """
    with with_db(path, timeout=timeout, row_factory=row_factory,
                 commit=False) as conn:
        if params is not None:
            cur = conn.execute(sql, params)
        else:
            cur = conn.execute(sql)
        if fetchall:
            return cur.fetchall()
        else:
            row = cur.fetchone()
            return [row] if row is not None else []


def execute_db(path: str, sql: str,
               params: Optional[Union[Sequence[Any], Dict[str, Any]]] = None,
               *, timeout: int = 5, wal: bool = True,
               busy_timeout: int = 5000) -> sqlite3.Cursor:
    """
    One-shot INSERT / UPDATE / DELETE with auto-commit and auto-close.

    Opens a connection, executes the statement, commits, and closes.

    Args:
        path: Path to the SQLite database file.
        sql: SQL DML statement.
        params: Optional parameters (sequence or dict).
        timeout: Connection timeout in seconds.
        wal: Enable WAL journal mode.
        busy_timeout: Busy timeout in ms.

    Returns:
        sqlite3.Cursor (closed connection but cursor retains .lastrowid etc.).
    """
    with with_db(path, timeout=timeout, wal=wal, busy_timeout=busy_timeout,
                 commit=True) as conn:
        if params is not None:
            return conn.execute(sql, params)
        else:
            return conn.execute(sql)


# ── Batch helper for multiple statements ──────────────────────────────


def executescript_db(path: str, sql: str, *, timeout: int = 5) -> None:
    """
    Execute a multi-statement SQL script with auto-commit and auto-close.

    Args:
        path: Path to the SQLite database file.
        sql: SQL script (multiple statements).
        timeout: Connection timeout in seconds.
    """
    with with_db(path, timeout=timeout, commit=True) as conn:
        conn.executescript(sql)