"""
core/db_connection.py

Unified SQLite connection manager for weather trading system.

Replaces 63+ scattered sqlite3.connect() calls with a single,
configuration-driven connection cache. Each process keeps one
connection per DB (read-write) and optionally one per DB (read-only).

Usage:
    from core.db_connection import get_connection, get_readonly_connection

    # Read-write
    with get_connection('metar_backfill') as conn:
        conn.execute("INSERT INTO ...")

    # Read-only (uses mmap where configured)
    with get_readonly_connection('gefs_archive') as conn:
        rows = conn.execute("SELECT ...").fetchall()

    # Close all at process shutdown
    from core.db_connection import close_all
    close_all()

Design principles (First-Principles, FP 5.8):
    1. One connection per DB per process (cached in module-level dict).
    2. Context manager for ergonomic use without connection-per-operation.
    3. Read-only connections use URI mode + mmap for bulk reads.
    4. WAL universally applied (safe for local SSD, NOT safe for NFS).
    5. busytimeout tuned per DB — see DB_REGISTRY.
    6. NO connection pooling — SQLite doesn't benefit from it.

B-Mode R8 Cycle 4: Created during P&L Truth / infrastructure stabilization.
"""

import os
import sqlite3
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Iterator, Tuple, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

# Environment override for deployment paths
_ENV_DATA_DIR = os.environ.get('WEATHER_ENGINE_DATA_DIR', '').strip()
if _ENV_DATA_DIR:
    DATA_DIR = _ENV_DATA_DIR


@dataclass
class DBConfig:
    """Per-database configuration."""
    db_key: str                          # Machine-readable key
    relative_path: str                   # Path relative to DATA_DIR or absolute if starts with '/'
    wal: bool = True
    busy_timeout_ms: int = 5000
    cache_size_kb: int = -64 * 1024      # Negative = KB (default 64MB)
    mmap_size_bytes: int = 0             # 0 = disabled
    synchronous: str = 'NORMAL'
    temp_store: str = 'MEMORY'
    page_size: Optional[int] = None      # None = keep existing
    read_only: bool = False
    auto_vacuum: int = 1                 # 1 = incremental
    foreign_keys: bool = True
    description: str = ""


# ─────────────────────────────────────────────────────────────────────
# DB Registry — single source of truth for all 30 databases
# ų────────────────────────────────────────────────────────────────────

DB_REGISTRY: Dict[str, DBConfig] = {
    'gefs_archive': DBConfig(
        db_key='gefs_archive',
        relative_path='gefs_archive.db',
        description='GEFS ensemble archive — 54MB, 363K rows × BLOBs, read-heavy',
        busy_timeout_ms=30000,
        cache_size_kb=-256 * 1024,   # 256MB
        mmap_size_bytes=256 * 1024 * 1024,
        synchronous='NORMAL',
        read_only=False,
    ),
    'gefs_operational': DBConfig(
        db_key='gefs_operational',
        relative_path='gefs_operational.db',
        description='GEFS operational forecasts',
        busy_timeout_ms=30000,
        cache_size_kb=-128 * 1024,
        mmap_size_bytes=128 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'gefs_reforecast': DBConfig(
        db_key='gefs_reforecast',
        relative_path='gefs_reforecast.db',
        description='GEFS reforecast archive',
        busy_timeout_ms=30000,
        cache_size_kb=-128 * 1024,
        mmap_size_bytes=128 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'nwp_forecasts': DBConfig(
        db_key='nwp_forecasts',
        relative_path='nwp_forecasts.db',
        description='NWP forecast aggregator — 140MB, daily write, medium read',
        busy_timeout_ms=20000,
        cache_size_kb=-128 * 1024,   # 128MB
        mmap_size_bytes=128 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'metar_backfill': DBConfig(
        db_key='metar_backfill',
        relative_path='metar_backfill.db',
        description='METAR backfill/live — 349MB, 30min write, medium read',
        busy_timeout_ms=30000,
        cache_size_kb=-256 * 1024,   # 256MB
        mmap_size_bytes=384 * 1024 * 1024,  # slightly above file size
        synchronous='NORMAL',
    ),
    'kalshi_settlements': DBConfig(
        db_key='kalshi_settlements',
        relative_path='kalshi_settlements.db',
        description='Kalshi settlement records — 1.3MB, daily write, heavy read',
        busy_timeout_ms=10000,
        cache_size_kb=-64 * 1024,    # 64MB
        synchronous='NORMAL',
    ),
    'paper_trading_dev': DBConfig(
        db_key='paper_trading_dev',
        relative_path='paper_trading_dev.db',
        description='Paper trading dev — 32KB, 4h write, low read',
        busy_timeout_ms=15000,
        cache_size_kb=-16 * 1024,    # 16MB
        synchronous='FULL',
    ),
    'paper_trading_live': DBConfig(
        db_key='paper_trading_live',
        relative_path='paper_trading_live.db',
        description='Paper trading live — production trades',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        synchronous='FULL',
    ),
    'paper_trading_prod': DBConfig(
        db_key='paper_trading_prod',
        relative_path='paper_trading_prod.db',
        description='Paper trading prod — production trades',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        synchronous='FULL',
    ),
    'paper_trading_sbox': DBConfig(
        db_key='paper_trading_sbox',
        relative_path='paper_trading_sbox.db',
        description='Paper trading sandbox',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        synchronous='FULL',
    ),
    'paper_trading': DBConfig(
        db_key='paper_trading',
        relative_path='paper_trading.db',
        description='Paper trading (generic)',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        synchronous='FULL',
    ),
    'alerts_prod': DBConfig(
        db_key='alerts_prod',
        relative_path='alerts-prod.db',
        description='Alert production — 100KB, high write freq',
        busy_timeout_ms=10000,
        cache_size_kb=-16 * 1024,
        synchronous='FULL',
    ),
    'alert_retry_queue': DBConfig(
        db_key='alert_retry_queue',
        relative_path='alert_retry_queue.db',
        description='Alert retry queue — durability critical',
        busy_timeout_ms=15000,
        cache_size_kb=-16 * 1024,
        synchronous='FULL',
    ),
    'alert_state_machine': DBConfig(
        db_key='alert_state_machine',
        relative_path='alert_state_machine.db',
        description='Alert state machine transitions',
        busy_timeout_ms=10000,
        cache_size_kb=-16 * 1024,
        synchronous='NORMAL',
    ),
    'ecmwf_archive': DBConfig(
        db_key='ecmwf_archive',
        relative_path='ecmwf_archive.db',
        description='ECMWF archive — read-heavy',
        busy_timeout_ms=30000,
        cache_size_kb=-128 * 1024,
        mmap_size_bytes=128 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'tigge_archive': DBConfig(
        db_key='tigge_archive',
        relative_path='tigge_archive.db',
        description='TIGGE archive — read-heavy',
        busy_timeout_ms=30000,
        cache_size_kb=-128 * 1024,
        mmap_size_bytes=128 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'era5_archive': DBConfig(
        db_key='era5_archive',
        relative_path='era5_archive.db',
        description='ERA5 archive — potentially largest DB',
        busy_timeout_ms=30000,
        cache_size_kb=-256 * 1024,
        mmap_size_bytes=256 * 1024 * 1024,
        synchronous='NORMAL',
    ),
    'forecast_disagreement_live': DBConfig(
        db_key='forecast_disagreement_live',
        relative_path='forecast_disagreement_live.db',
        description='Live forecast disagreement',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'forecast_disagreement_real': DBConfig(
        db_key='forecast_disagreement_real',
        relative_path='forecast_disagreement_real.db',
        description='Real forecast disagreement',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'trade_journal': DBConfig(
        db_key='trade_journal',
        relative_path='trade_journal.db',
        description='Trade journal — durability critical',
        busy_timeout_ms=15000,
        cache_size_kb=-16 * 1024,
        synchronous='FULL',
    ),
    'weather_data': DBConfig(
        db_key='weather_data',
        relative_path='weather_data.db',
        description='Weather data (legacy)',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'weather_engine': DBConfig(
        db_key='weather_engine',
        relative_path='weather_engine.db',
        description='Weather engine (legacy)',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'kalshi_mock_orderbook': DBConfig(
        db_key='kalshi_mock_orderbook',
        relative_path='kalshi_mock_orderbook.db',
        description='Mock Kalshi orderbook — test artifact',
        busy_timeout_ms=5000,
        cache_size_kb=-16 * 1024,
        synchronous='NORMAL',
    ),
    'kalshi_market_cache': DBConfig(
        db_key='kalshi_market_cache',
        relative_path='alerts.db',
        description='Kalshi market cache in alerts DB — 100KB, read-heavy',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'weatherapi_archive': DBConfig(
        db_key='weatherapi_archive',
        relative_path='weatherapi_archive.db',
        description='Weather API archive — cache, disposable',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
<<<<<<< HEAD
=======
    'isd_lite_raw': DBConfig(
        db_key='isd_lite_raw',
        relative_path='isd_lite_raw.db',
        description='ISD lite raw data',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        synchronous='NORMAL',
    ),
    'isd_log': DBConfig(
        db_key='isd_log',
        relative_path='isd_log.db',
        description='ISD ingestion log',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
    'isd_raw': DBConfig(
        db_key='isd_raw',
        relative_path='isd_raw.db',
        description='ISD raw data',
        busy_timeout_ms=20000,
        cache_size_kb=-64 * 1024,
        mmap_size_bytes=64 * 1024 * 1024,
        synchronous='NORMAL',
    ),
>>>>>>> origin/main
    'phase1_paper_trades': DBConfig(
        db_key='phase1_paper_trades',
        relative_path='phase1_paper_trades.db',
        description='Phase1 paper trades — historical',
        busy_timeout_ms=5000,
        cache_size_kb=-16 * 1024,
        synchronous='NORMAL',
    ),
    'paper_test_settlements': DBConfig(
        db_key='paper_test_settlements',
        relative_path='paper_test_settlements.db',
        description='Paper test settlements — test artifact',
        busy_timeout_ms=5000,
        cache_size_kb=-16 * 1024,
        synchronous='NORMAL',
    ),
    # Legacy/utility databases
    'station_registry': DBConfig(
        db_key='station_registry',
        relative_path='station_registry.db',
        description='Station registry metadata',
        busy_timeout_ms=5000,
        cache_size_kb=-16 * 1024,
        synchronous='NORMAL',
    ),
    'backtest_cache': DBConfig(
        db_key='backtest_cache',
        relative_path='backtest_cache.db',
        description='Backtest result cache',
        busy_timeout_ms=10000,
        cache_size_kb=-64 * 1024,
        synchronous='NORMAL',
    ),
}

# Shortcut for most common DBs
MOST_COMMON_DBS = {
    'metar': 'metar_backfill',
    'alerts': 'alerts_prod',
    'paper': 'paper_trading',
    'paper_dev': 'paper_trading_dev',
    'paper_live': 'paper_trading_live',
    'trade_journal': 'trade_journal',
    'kalshi_settlements': 'kalshi_settlements',
}


# ─────────────────────────────────────────────────────────────────────
# Connection Cache
# ─────────────────────────────────────────────────────────────────────

# Module-level connection cache.
# Key: (db_key, mode) -> sqlite3.Connection
_connections: Dict[Tuple[str, str], sqlite3.Connection] = {}


def _resolve_path(config: DBConfig) -> str:
    """Resolve relative path to absolute."""
    if config.relative_path.startswith('/'):
        return config.relative_path
    return os.path.join(DATA_DIR, config.relative_path)


def _apply_pragmas(conn: sqlite3.Connection, config: DBConfig) -> None:
    """Apply all configured PRAGMAs to a connection."""
    if config.wal:
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={config.busy_timeout_ms};")
    conn.execute(f"PRAGMA cache_size={config.cache_size_kb};")
    if config.mmap_size_bytes > 0:
        conn.execute(f"PRAGMA mmap_size={config.mmap_size_bytes};")
    conn.execute(f"PRAGMA synchronous={config.synchronous};")
    conn.execute(f"PRAGMA temp_store={config.temp_store};")
    if config.page_size is not None:
        conn.execute(f"PRAGMA page_size={config.page_size};")
    conn.execute(f"PRAGMA auto_vacuum={config.auto_vacuum};")
    if config.foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA case_sensitive_like=OFF;")
    conn.execute("PRAGMA trusted_schema=OFF;")
    conn.execute("PRAGMA recursive_triggers=OFF;")
    logger.debug("Applied PRAGMAs for %s (mode=%s)", config.db_key,
                 'ro' if config.read_only else 'rw')


def _new_connection(config: DBConfig) -> sqlite3.Connection:
    """Create a fresh connection with PRAGMAs applied."""
    path = _resolve_path(config)
    timeout_s = max(config.busy_timeout_ms // 1000, 1)

    if config.read_only:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=timeout_s,
        )
    else:
        conn = sqlite3.connect(path, timeout=timeout_s)

    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, config)
    logger.debug("Opened connection: %s (mode=%s, path=%s)",
                 config.db_key, 'ro' if config.read_only else 'rw', path)
    return conn


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def get_config(db_key: str) -> DBConfig:
    """Look up a DB configuration by key. Raises KeyError if not found."""
    if db_key not in DB_REGISTRY:
        # Check common shortcuts
        if db_key in MOST_COMMON_DBS:
            return DB_REGISTRY[MOST_COMMON_DBS[db_key]]
        raise KeyError(
            f"Unknown database key '{db_key}'. "
            f"Available: {', '.join(sorted(DB_REGISTRY.keys()))}"
        )
    return DB_REGISTRY[db_key]


def get_connection_path(db_key: str) -> str:
    """Resolve the absolute path for a database key. No connection created."""
    config = get_config(db_key)
    return _resolve_path(config)


@contextmanager
def get_connection(db_key: str) -> Iterator[sqlite3.Connection]:
    """
    Get a cached read-write connection for the named database.

    The connection is created once per process and cached. Consecutive
    calls return the same connection. Use within a context manager for
    clean resource management.

    Usage:
        with get_connection('metar_backfill') as conn:
            conn.execute("...")
    """
    cache_key = (db_key, 'rw')
    if cache_key not in _connections:
        config = get_config(db_key)
        # Ensure we never cache a read-only connection here
        config_read_only = config.read_only
        config.read_only = False
        _connections[cache_key] = _new_connection(config)
        config.read_only = config_read_only
    yield _connections[cache_key]


@contextmanager
def get_readonly_connection(db_key: str) -> Iterator[sqlite3.Connection]:
    """
    Get a cached read-only connection for the named database.

    Read-only connections use URI mode (?mode=ro) which skips WAL
    overhead and can use mmap for bulk reads. Use for:
    - Long-running GEFS archive scans
    - Backtest data loading
    - Dashboard queries

    NOTE: Reads from a read-only connection will NOT see uncommitted
    writes from the writeable connection in the same process. If you
    need to read your own writes, use get_connection() instead.
    """
    cache_key = (db_key, 'ro')
    if cache_key not in _connections:
        config = get_config(db_key)
        config.read_only = True
        _connections[cache_key] = _new_connection(config)
    yield _connections[cache_key]


def close_all() -> None:
    """
    Close all cached connections.

    Call at process shutdown or between major operations.
    During normal operation, keep connections open.
    """
    for (db_key, mode), conn in list(_connections.items()):
        try:
            conn.close()
            logger.debug("Closed connection: %s (%s)", db_key, mode)
        except Exception as e:
            logger.warning("Error closing %s (%s): %s", db_key, mode, e)
    _connections.clear()


def close(db_key: str) -> None:
    """
    Close a specific database connection from the cache.

    Useful when a DB is known to be unused for the rest of a process.
    """
    for mode in ('rw', 'ro'):
        key = (db_key, mode)
        conn = _connections.pop(key, None)
        if conn is not None:
            try:
                conn.close()
                logger.debug("Closed connection: %s (%s)", db_key, mode)
            except Exception as e:
                logger.warning("Error closing %s (%s): %s", db_key, mode, e)


def debug_status() -> Dict[str, str]:
    """Return status of all cached connections (non-invasive)."""
    return {
        f"{db_key} ({mode})": "open"
        for (db_key, mode) in _connections
    }


def force_checkpoint(db_key: str) -> int:
    """
    Force a WAL checkpoint on a database connection.

    Call after large batch writes to reclaim WAL file space.
    Returns number of pages checkpointed.

    Usage:
        pages = force_checkpoint('metar_backfill')
        logger.info("Checkpointed %d pages", pages)
    """
    with get_connection(db_key) as conn:
        pages, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        logger.info("WAL checkpoint on %s: %d pages", db_key, pages)
        return pages or 0


def db_file_size(db_key: str) -> Optional[int]:
    """Return the on-disk file size in bytes, or None if missing."""
    config = get_config(db_key)
    path = _resolve_path(config)
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def get_registry_keys() -> List[str]:
    """Return sorted list of all registered database keys."""
    return sorted(DB_REGISTRY.keys())


# ─────────────────────────────────────────────────────────────────────
# Migration helpers for gradual rollout
# ─────────────────────────────────────────────────────────────────────

def create_database(db_key: str, schema_sql: str = "") -> str:
    """
    Create a database file if it doesn't exist, with optional schema.

    Returns the resolved path to the database file.

    Usage:
        path = create_database('paper_trading_dev', '''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                ...
            )
        ''')
    """
    config = get_config(db_key)
    path = _resolve_path(config)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)

    if not os.path.exists(path):
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL;")
        if schema_sql:
            conn.executescript(schema_sql)
        conn.commit()
        conn.close()
        logger.info("Created database: %s -> %s", db_key, path)

    return path