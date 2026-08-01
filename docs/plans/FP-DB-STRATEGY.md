# FP-DB-STRATEGY: First-Principles Database Connection Strategy

**Status:** Design proposal  
**Date:** 2026-08-01  
**Author:** First-Principles Database Architecture Expert  
**Target:** Replace 63+ scattered `sqlite3.connect()` calls with a unified, per-DB-tuned connection manager.

---

## 1. Architectural Analysis

### 1.1 Current State (Problems)

| Problem | Impact | Prevalence |
|---------|--------|-----------|
| Raw `sqlite3.connect()` everywhere | No PRAGMA consistency, defaults used | 63+ files |
| `sqlite_utils.py` exists but unused (except `dashboard.py`) | No one knows about it | 1/63 consumers |
| `alert_retry_queue.py` repeats `PRAGMA WAL + busy_timeout=5000` 9× | Boilerplate, easy to forget | ~20 files repeat manually |
| `alert_state_machine.py` uses `sqlite3.connect(self._db_path)` — no timeout | Zero busy_timeout default (0ms = immediate fail) | High-risk for cron clashes |
| No `mmap_size` or `cache_size` anywhere | Read-heavy DBs (GEFS, METAR) miss 10-100× performance gains | 0/30 DBs |
| Connection-per-method in tight loops | File descriptor pressure, WAL checkpoint storms | `alert_state_machine`, `alert_retry_queue`, `alert_throttle` |
| Multiple processes hit same .db | WAL handles writers, but 0 busy_timeout on some means `SQLITE_BUSY` crashes | All paper-trading + alert files |
| `alert_reconciliation.py` uses context manager (`with sqlite3.connect`) | Good pattern, but no PRAGMAs set | Only file doing this |

### 1.2 First-Principles: What SQLite Actually Needs

SQLite is **not** a client-server database. Connection pooling is actively harmful for writes (each connection has its own transaction state). The optimal pattern for SQLite in a multi-process system:

1. **One connection per thread/process per DB** — held open for the lifetime of the operation.
2. **WAL mode** — allows concurrent reads + one writer.
3. **busy_timeout** — tells the writer to retry instead of failing.
4. **Read-only connections** — skip WAL overhead, use `mmap` for bulk scans.
5. **NO explicit connection pooling class** — it adds complexity for zero benefit with SQLite.

---

## 2. Recommended Connection Pattern

### 2.1 Architecture

```
┌─────────────────────────────────────────┐
│            core/db_connection.py          │
│  (Single source of truth for all DB I/O) │
├─────────────────────────────────────────┤
│  get_connection(db_name, mode='rw')      │
│  └─> Module-level cache (dict)           │
│       └─> {per-DB config} -> PRAGMAs     │
│  get_readonly_connection(db_name)        │
│  └─> Separate cache for RO connections   │
│  close_all()                             │
│  DBConfig dataclass                      │
│  DB_REGISTRY dict                        │
└─────────────────────────────────────────┘
         │                     ▲
         │  import from        │
         ▼                     │
┌──────────────────┐   ┌──────────────────┐
│  All 63 modules   │   │  Scripts (18+)   │
│  (core/*.py)      │   │  (scripts/*.py)  │
└──────────────────┘   └──────────────────┘
```

### 2.2 Pattern Decision

**Recommended:** Context manager + module-level connection cache.

```
with get_connection('metar_backfill') as conn:
    conn.execute("SELECT ...")
```

- **Module-level cache**: Each process gets 1 connection per DB, held open. Prevents connection storms.
- **Context manager**: Guarantees `close()` on exceptions, but for cached connections the context manager is a no-op on close (reuses connection).
- **Key insight**: For long-running operations (backtests, sweep scripts), cache the connection. For one-shot scripts, let the context manager dispose it.

---

## 3. Per-DB Configuration Table

### 3.1 DB Registry

```python
@dataclass
class DBConfig:
    path: str                   # Relative or absolute path
    wal: bool = True            # WAL journal mode
    busy_timeout_ms: int = 5000 # Retry duration for busy
    cache_size_mb: int = -64   # Negative = KB, positive = pages. -64 = 64MB
    mmap_size_mb: int = 0      # 0 = disabled. >0 = memory-map up to N MB
    synchronous: str = 'NORMAL'# NORMAL for WAL (safe), FULL for DELETE
    temp_store: str = 'MEMORY' # Store temp tables/indexes in memory
    page_size: int = 4096      # Default. 8192 for write-heavy.
    read_only: bool = False    # Force read-only URI mode
    auto_vacuum: int = 1       # 1 = incremental (recommended for large DBs)
    foreign_keys: bool = True  # Enforce FK constraints
```

### 3.2 Per-DB Settings

| DB File | Size | WAL | busy_timeout | cache_size | mmap_size | sync | Notes |
|---------|:----:|:---:|:-----------:|:----------:|:---------:|:----:|-------|
| **gefs_archive.db** | 54MB | YES | 30000ms | 256MB | 256MB | NORMAL | Read-optimized. Large sequential scans (363K rows × BLOBs). mmap+large cache = 10-50× faster bulk reads. |
| **nwp_forecasts.db** | 140MB | YES | 20000ms | 128MB | 128MB | NORMAL | Mixed read/write. Daily bulk insert, heavy read for backtests. |
| **kalshi_settlements.db** | 1.3MB | YES | 10000ms | 64MB | 0 | NORMAL | Daily write, heavy read. Small file, mmap overkill. |
| **metar_backfill.db** | 349MB | YES | 30000ms | 256MB | 349MB | NORMAL | Largest DB. 30-min writes, medium reads. Full mmap. |
| **paper_trading_dev.db** | 32KB | YES | 15000ms | 16MB | 0 | FULL | Write-heavy (4h cron + alerts). Small file, FULL sync for durability. |
| **paper_trading_live/prod/sbox.db** | varies | YES | 20000ms | 64MB | 0 | FULL | Same as dev but production. FULL sync to prevent data loss. |
| **alerts-prod.db** | 100KB | YES | 10000ms | 16MB | 0 | FULL | High-write-frequency. Small file, FULL sync. |
| **alert_retry_queue.db** | small | YES | 15000ms | 16MB | 0 | FULL | Retry queue needs durability. |
| **alert_state_machine.db** | small | YES | 10000ms | 16MB | 0 | NORMAL | State transitions, moderately written. |
| **ecmwf_archive.db** | varies | YES | 30000ms | 128MB | 128MB | NORMAL | Read-heavy archive, similar to gefs. |
| **tigge_archive.db** | varies | YES | 30000ms | 128MB | 128MB | NORMAL | Read-heavy archive. |
| **era5_archive.db** | varies | YES | 30000ms | 256MB | 256MB | NORMAL | Largest archive candidate. |
| **forecast_disagreement*.db** | small | YES | 10000ms | 32MB | 0 | NORMAL | Light usage. |
| **trade_journal.db** | small | YES | 15000ms | 16MB | 0 | FULL | Trade records need durability. |
| **weather_data.db / weather_engine.db** | varies | YES | 10000ms | 32MB | 0 | NORMAL | Legacy, phase out if possible. |
| **weatherapi_archive.db** | varies | YES | 10000ms | 32MB | 0 | NORMAL | API cache, disposable. |
| **kalshi_mock_orderbook.db** | small | YES | 5000ms | 16MB | 0 | NORMAL | Test artifact. |
| **isd_*.db** | varies | YES | 20000ms | 64MB | 64MB | NORMAL | ISD data, medium read. |
| **phase1_paper_trades.db** | small | YES | 5000ms | 16MB | 0 | NORMAL | Historical archive. |

### 3.3 Design Rationale

**Why WAL universally?**
- WAL allows concurrent reads while a write is in progress.
- WAL reduces write amplification (sequential WAL append vs random b-tree writes).
- WAL's only downside is slightly larger checkpoint overhead, which is fine for this system.
- **Exception:** If any DB is on a network filesystem (NFS, SMB), disable WAL — it relies on `fdatasync`.
- **Recommendation:** Store all `.db` files on local SSD. Confirmed check before deploying.

**Why different busy_timeout values?**
- **Large reads (GEFS, METAR, NWP):** 30s — a bulk scan may take seconds, and we don't want it to abort on a brief write-lock. The writer will finish in milliseconds under WAL.
- **Paper trading/alerts:** 10-15s — these are fast operations. If busy for >15s, something is wrong; better to surface the error.
- **Light DBs (forecast_disagreement):** 5-10s — minimal contention expected.

**Why mmap_size for only large DBs?**
- Memory-mapping bypasses the SQLite pager for pure reads. 10-50× faster for bulk sequential scans.
- Only safe for read-only or read-mostly workloads (mmap of a growing file can segfault).
- Set to file size (or slightly above) for full coverage.
- On 32-bit systems, mmap is limited to ~2GB. We're on 64-bit Linux, so no concern.

**Why cache_size varies?**
- Larger cache = fewer page faults on repeated queries.
- GEFS: 256MB cache caches most of the 54MB file entirely.
- METAR: 256MB for 349MB file — caches hot working set.
- Small DBs: 16-32MB is generous but essentially free on a system with GBs of RAM.

---

## 4. Implementation Spec: `core/db_connection.py`

### 4.1 Full Implementation

```python
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
        rows = conn.execute("SELECT ... FROM gefs_archive ...").fetchall()

    # Close all (call at process shutdown, NOT during normal operation)
    from core.db_connection import close_all
    close_all()

Design principles:
    1. One connection per DB per process (cached in module-level dict).
    2. Context manager for ergonomic use without connection-per-operation.
    3. Read-only connections use URI mode + mmap for bulk reads.
    4. WAL universally applied (safe for local SSD, NOT safe for NFS).
    5. busytimeout tuned per DB — see DB_REGISTRY.
    6. NO connection pooling — SQLite doesn't benefit from it (each conn = separate WAL view).
"""
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Optional, Iterator, Tuple
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
CORE_DIR = REPO_ROOT  # actually core/live nets and alert DBs are in core/
# # Hard-coded fallbacks for Render environment
# RENDER_DATA_ROOT = os.path.expanduser("~/.openclaw-next/workspace/prototypes/weather-engine-source/data")


@dataclass
class DBConfig:
    """Per-database configuration."""
    db_key: str                          # Machine-readable key
    relative_path: str                   # Path relative to DATA_DIR or absolute
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
# DB Registry — single source of truth
# ─────────────────────────────────────────────────────────────────────
# Paths: relative to DATA_DIR unless starting with '/'
# Key order: most-read first for cache locality

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
        relative_path='alerts-prod.db',     # note: hyphenated filename
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
    'weatherapi_archive': DBConfig(
        db_key='weatherapi_archive',
        relative_path='weatherapi_archive.db',
        description='Weather API archive — cache, disposable',
        busy_timeout_ms=10000,
        cache_size_kb=-32 * 1024,
        synchronous='NORMAL',
    ),
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
}


# ─────────────────────────────────────────────────────────────────────
# Connection Cache
# ─────────────────────────────────────────────────────────────────────

# Module-level connection cache.
# Key: (db_key, mode) -> sqlite3.Connection
# This ensures one connection per DB per mode per process.
_connections: Dict[Tuple[str, str], sqlite3.Connection] = {}


def _resolve_path(config: DBConfig) -> str:
    """Resolve relative path to absolute."""
    if config.relative_path.startswith('/'):
        return config.relative_path
    return os.path.join(DATA_DIR, config.relative_path)


def _apply_pragmas(conn: sqlite3.Connection, config: DBConfig):
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
    # Additional performance pragmas
    conn.execute("PRAGMA case_sensitive_like=OFF;")  # default, explicit
    conn.execute("PRAGMA trusted_schema=OFF;")       # security
    conn.execute("PRAGMA recursive_triggers=OFF;")   # default, explicit


def _new_connection(config: DBConfig) -> sqlite3.Connection:
    """Create a fresh connection with PRAGMAs applied."""
    path = _resolve_path(config)
    timeout_s = config.busy_timeout_ms // 1000
    if timeout_s < 1:
        timeout_s = 1

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
    logger.debug(f"Opened connection: {config.db_key} (mode={'ro' if config.read_only else 'rw'}, "
                 f"path={path})")

    return conn


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def get_config(db_key: str) -> DBConfig:
    """Look up a DB configuration by key. Raises KeyError if not found."""
    if db_key not in DB_REGISTRY:
        raise KeyError(
            f"Unknown database key '{db_key}'. "
            f"Available: {', '.join(sorted(DB_REGISTRY.keys()))}"
        )
    return DB_REGISTRY[db_key]


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
        config.read_only = False
        _connections[cache_key] = _new_connection(config)
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


def close_all():
    """
    Close all cached connections.

    Call at process shutdown or between major operations.
    During normal operation, keep connections open.
    """
    for (db_key, mode), conn in _connections.items():
        try:
            conn.close()
            logger.debug(f"Closed connection: {db_key} ({mode})")
        except Exception as e:
            logger.warning(f"Error closing {db_key} ({mode}): {e}")
    _connections.clear()


def close(db_key: str):
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
                logger.debug(f"Closed connection: {db_key} ({mode})")
            except Exception as e:
                logger.warning(f"Error closing {db_key} ({mode}): {e}")


def debug_status() -> Dict[str, str]:
    """Return status of all cached connections (non-invasive, no locks)."""
    return {
        f"{db_key} ({mode})": str(type(conn).__name__)
        for (db_key, mode), conn in _connections.items()
    }


# ─────────────────────────────────────────────────────────────────────
# WAL Checkpoint Helper
# ─────────────────────────────────────────────────────────────────────

def force_checkpoint(db_key: str):
    """
    Force a WAL checkpoint on a database connection.
    
    Call after large batch writes to reclaim WAL file space.
    Usually not needed — SQLite auto-checkpoints.
    """
    with get_connection(db_key) as conn:
        pages, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        logger.info(f"WAL checkpoint on {db_key}: {pages} pages checkpointed")


# ─────────────────────────────────────────────────────────────────────
# Database Size Helper
# ─────────────────────────────────────────────────────────────────────

def db_file_size(db_key: str) -> Optional[int]:
    """Return the on-disk file size in bytes, or None if missing."""
    config = get_config(db_key)
    path = _resolve_path(config)
    try:
        return os.path.getsize(path)
    except OSError:
        return None
```

### 4.2 Interface Summary

| Function | Purpose | When to use |
|----------|---------|-------------|
| `get_connection(key)` | RW connection (cached) | 95% of use cases |
| `get_readonly_connection(key)` | RO connection (mmap-capable) | Bulk reads, backtests, dashboards |
| `get_config(key)` | Look up DBConfig | Scripts that need path or tuning info |
| `close_all()` | Shutdown cleanup | Process exit, `atexit` register |
| `close(key)` | Close one DB | Clean disconnect for specific DB |
| `force_checkpoint(key)` | Manual WAL checkpoint | After large batch writes |
| `debug_status()` | Connection cache state | Monitoring, health checks |

### 4.3 Error Strategy

- **`KeyError` on unknown `db_key`**: Hard fail with available keys listed.
- **`sqlite3.OperationalError` on busy**: Propagated up. The `busy_timeout` handles retries; if it fires, something is genuinely stuck.
- **File not found**: Let it raise `sqlite3.OperationalError` (same behavior as current raw calls). Callers can catch it.
- **No retry layer in this module**: Retry logic belongs at the operation level, not the connection level.

---

## 5. Migration Guide

### 5.1 Pre-Migration Audit

These are the exact patterns to replace:

**Pattern A: Raw `sqlite3.connect` with manual PRAGMAs (worst)**
Files: `alert_state_machine.py`, `alert_retry_queue.py`, `cloud_cover_modulation.py`, `calibration_dashboard.py`, `calibrate_all_signals.py`, `climatology_pillar.py`, `paper_trading_engine.py`, `paper_trader.py`, `risk_controls.py`, `data_freshness_monitor.py`, etc.

**Pattern B: Raw `sqlite3.connect` with NO PRAGMAs**
Files: `alert_reconciliation.py`, `alert_throttle.py`, `ensemble_agreement.py`, `kalshi_monitor.py`, `order_manager.py`, `metar_monitor.py`, `data_freshness.py`, etc.

**Pattern C: URI-mode read-only connects**
Files: `data_integrity_gates.py`, `data_quality_gate.py` (scripts)
These should use `get_readonly_connection()` instead.

### 5.2 Migration Steps (ordered by risk)

#### Phase 1: Replace sqlite_utils.py entirely
1. **Create `core/db_connection.py`** with the full implementation above.
2. **Update `core/__init__.py`** to export `get_connection`, `get_readonly_connection` (optional, but handy).
3. **Do NOT delete `core/sqlite_utils.py` yet** — keep as backward compat wrapper:

```python
# sqlite_utils.py — DEPRECATED, will be removed
from core.db_connection import get_connection, get_readonly_connection, close_all
import warnings

def get_sqlite_connection(db_path, timeout=30):
    warnings.warn("get_sqlite_connection() is deprecated. Use get_connection() with a DB key.", DeprecationWarning, stacklevel=2)
    return get_connection('metar_backfill')  # best guess default

# ... etc
```

#### Phase 2: Migrate high-risk files first
| Priority | Files | Reason | Migration complexity |
|:--------:|-------|--------|:--------------------:|
| P0 | `alert_retry_queue.py` | 9× raw sqlite3.connect, 1s timeout → likely silent failures | Simple search/replace |
| P0 | `alert_state_machine.py` | 8× raw connect, no timeout | Simple search/replace |
| P0 | `alert_throttle.py` | 5× raw connect | Simple search/replace |
| P0 | `cloud_cover_modulation.py` | Hardcoded paths, 60s timeout, duplicate connections | Medium |
| P1 | `paper_trading_engine.py` | Full sync durability, schema creation with ATTACH, need to preserve | High (complex) |
| P1 | `paper_trader.py` | Paper trading cron | Medium |
| P1 | `calibrate_all_signals.py` | Script with hardcoded paths | Low |
| P1 | `calibration_dashboard.py` | Hardcoded absolute path | Low |
| P1 | `climatology_pillar.py` | Mixed read/write | Low |
| P2 | `dashboard.py` | Already imports from sqlite_utils | Trivial (change import) |
| P2 | `ensemble_agreement.py` | Simple open/close per call | Low |
| P2 | All other `core/*.py` | Raw connect variants | Low-Medium |
| P3 | `scripts/*.py` | 18+ scripts, many with hardcoded paths | Medium |

**For each file, the migration pattern is:**
```python
# BEFORE:
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout=5000;")
# ... do work ...
conn.close()

# AFTER:
from core.db_connection import get_connection

with get_connection('metar_backfill') as conn:
    # ... do work ...
    pass
# No close needed — connection is cached
```

**For read-only scans (GEFS, ECMWF, etc.):**
```python
# BEFORE:
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

# AFTER:
from core.db_connection import get_readonly_connection
with get_readonly_connection('gefs_archive') as conn:
    rows = conn.execute("SELECT ...").fetchall()
```

#### Phase 3: Standardize DB path resolution
- Remove all hardcoded absolute paths (e.g., `/home/gaddams/.openclaw-next/workspace/...`).
- Replace with `get_config(db_key).relative_path` resolved against `DATA_DIR`.
- Each module that needs a path uses:
```python
from core.db_connection import get_config
DB_PATH = os.path.join(DATA_DIR, "metar_backfill.db")
# OR just:
config = get_config('metar_backfill')
```

#### Phase 4: Remove redundant PRAGMA boilerplate
- After migration, delete every `conn.execute("PRAGMA journal_mode=WAL;")` and `conn.execute("PRAGMA busy_timeout=...;")` from migrated files.
- These are now handled centrally in `_apply_pragmas()`.

#### Phase 5: Delete `sqlite_utils.py` (after full migration verified)
- Move any remaining consumers to `db_connection.py`.
- Delete the deprecated wrapper.
- Update any import references in `__init__.py`.

### 5.3 File-by-File Migration Map

| DB Key | Module Files That Connect to It | Primary Write Mutex |
|--------|--------------------------------|-------------------|
| `metar_backfill` | dashboard, cloud_cover_modulation, climatology_pillar, calibration_dashboard, calibrate_all_signals, decision_output, data_freshness, data_freshness_monitor, db_health_monitor, ensemble_agreement | metar_monitor (30min) |
| `nwp_forecasts` | ensemble_agreement, forecast_confidence_modulator, data_freshness, data_freshness_monitor | data_collector (daily) |
| `gefs_archive` | scripts/data_integrity_gates, scripts/data_quality_gate, scripts/backtest_* | Sweep scripts (rare) |
| `kalshi_settlements` | kalshi_monitor, settlement_processor, settlement_cascade | settlement_processor (daily) |
| `paper_trading_*` | paper_trader, paper_trading_engine, trade_execution, order_manager | paper_trading cron (4h) |
| `alerts_prod` | alert_reconciliation, alert_formatter, alert_dispatcher, alert_builder | alert_pipeline (per alert) |
| `alert_retry_queue` | alert_retry_queue (self-contained) | alert_retry_queue |
| `alert_state_machine` | alert_state_machine (self-contained) | alert_state_machine |
| `trade_journal` | trade_journal, pnl_tracking | pnl_tracking (hourly) |

---

## 6. GEFS BLOB Read Optimization Strategy

The GEFS archive DB stores ~363K rows, each with ~250-byte BLOBs of ensemble member values. This is the most demanding read pattern in the system.

### 6.1 Current Problem
- 363K rows × 250 bytes = ~90MB of raw BLOB data
- With current no-PRAGMA setup, each query pays per-page I/O cost
- Repeated queries re-fetch pages from disk

### 6.2 Optimized Strategy

1. **mmap (primary):** Set `mmap_size_bytes=256MB` for `gefs_archive`. This memory-maps the entire file. SQLite reads directly from the mmap region — zero-copy for read queries. **Expected: 10-50× faster** for bulk scans.

2. **Cache (secondary):** Set `cache_size=256MB`. SQLite caches decoded pages in memory. If a query re-scans recently accessed rows, the pages are already in cache.

3. **Row factory:** Use `sqlite3.Row` (already in the design) for dict-like access. For bulk BLOB reads, consider bypassing the row factory:
```python
# For maximum throughput on BLOB reads:
conn.row_factory = None  # returns tuples, ~2× faster than Row
for row in conn.execute("SELECT station, target_date, member_values FROM gefs_archive WHERE step=?", (24,)):
    station, target_date, blob = row
    values = struct.unpack('<' + 'f' * (len(blob) // 4), blob)
```

4. **Batch chunking:** For scripts that process all 363K rows, stream with `fetchmany()` instead of `fetchall()`:
```python
cursor = conn.execute("SELECT ...")
while True:
    batch = cursor.fetchmany(10000)
    if not batch:
        break
    process_batch(batch)
```

5. **Columns to fetch:** Only select columns you need. `SELECT *` from a table with BLOBs materializes all BLOB data into memory.

6. **Covering index:** If queries filter by station + step, add a covering index:
```sql
CREATE INDEX IF NOT EXISTS idx_gefs_station_step ON gefs_archive(station, step) INCLUDE(target_date, member_values);
```
(This requires SQLite 3.39+; otherwise use a standard composite index.)

### 6.3 Expected Performance

| Pattern | Current (no tuning) | With mmap + cache | Speedup |
|---------|-------------------:|------------------:|:-------:|
| Full table scan (363K rows) | ~3-5s | ~0.1-0.3s | 15-50× |
| Station-filtered scan | ~0.5-1s | ~0.02-0.05s | 20-50× |
| Multiple sequential queries | ~2-8s (disk re-read) | ~0.01-0.1s (cache hit) | 20-80× |

---

## 7. Verification Plan

### 7.1 Unit Tests (in `core/tests/` or existing test suite)

```python
"""
Tests for core/db_connection.py
"""
import os
import tempfile
import sqlite3
import threading
import time
from core.db_connection import (
    get_connection,
    get_readonly_connection,
    get_config,
    close_all,
    close,
    DB_REGISTRY,
    DATA_DIR,
)


def _make_temp_db(name: str) -> str:
    """Create a temporary DB for testing."""
    fd, path = tempfile.mkstemp(suffix='.db', prefix=f'test_{name}_')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'hello')")
    conn.commit()
    conn.close()
    return path


class TestConnectionManager:
    """Verify connection caching, PRAGMA application, and lifecycle."""

    def test_get_connection_caches(self):
        """Calling get_connection twice returns the same connection."""
        # We need a test DB. For now, verify via debug_status counts.
        close_all()
        # Get config but point to temp DB
        cfg = get_config('metar_backfill')
        original_path = cfg.relative_path
        try:
            cfg.relative_path = _make_temp_db('test_cache')
            a = get_connection('metar_backfill')
            b = get_connection('metar_backfill')
            assert a is b, "get_connection returned different objects"
        finally:
            cfg.relative_path = original_path
            close('metar_backfill')
            # Cleanup temp file
            os.unlink(cfg.relative_path)

    def test_get_readonly_connection(self):
        """Read-only connection should be a separate cached conn."""
        close_all()
        cfg = get_config('metar_backfill')
        original_path = cfg.relative_path
        try:
            cfg.relative_path = _make_temp_db('test_ro')
            rw = get_connection('metar_backfill')
            ro = get_readonly_connection('metar_backfill')
            assert rw is not ro, "RO and RW connections should differ"
            # Read from RO should work
            row = ro.execute("SELECT value FROM test WHERE id=1").fetchone()
            assert row['value'] == 'hello'
        finally:
            cfg.relative_path = original_path
            close_all()

    def test_pragma_applied(self):
        """Verify PRAGMAs are applied on connection creation."""
        close_all()
        cfg = get_config('nwp_forecasts')
        original_path = cfg.relative_path
        try:
            cfg.relative_path = _make_temp_db('test_pragma')
            with get_connection('nwp_forecasts') as conn:
                journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
                assert journal == 'wal', f"Expected WAL, got {journal}"
                timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                assert timeout == 20000, f"Expected 20000, got {timeout}"
                cache = conn.execute("PRAGMA cache_size").fetchone()[0]
                assert cache == -131072, f"Expected -131072, got {cache}"  # -128*1024
        finally:
            cfg.relative_path = original_path
            close_all()

    def test_close_all_clears_cache(self):
        """After close_all, connections are removed."""
        close_all()
        cfg = get_config('metar_backfill')
        original_path = cfg.relative_path
        try:
            cfg.relative_path = _make_temp_db('test_close')
            get_connection('metar_backfill')
            assert len(debug_status()) == 1
            close_all()
            assert len(debug_status()) == 0
        finally:
            cfg.relative_path = original_path
```

### 7.2 Concurrent Load Test

This simulates the real contention pattern: one writer (paper trading cron) + multiple readers (sweep scripts, dashboards).

```python
"""
Concurrent load test: multiple processes hitting the same DB.
Run this from the scripts/ directory.
"""
import sqlite3
import threading
import time
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

TEST_DB = "/tmp/fp_db_concurrent_test.db"
NUM_WRITERS = 1
NUM_READERS = 5
NUM_OPERATIONS = 50  # per thread


def setup():
    """Create a test database similar to paper_trading_dev."""
    if os.path.exists(TEST_DB):
        os.unlink(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT,
            value REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def writer_thread(thread_id):
    """Simulate paper trading writes."""
    conn = sqlite3.connect(TEST_DB, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    for i in range(NUM_OPERATIONS):
        conn.execute(
            "INSERT INTO trades (station, value) VALUES (?, ?)",
            (f"KORD_{thread_id}", random.uniform(10, 30))
        )
        conn.commit()
        time.sleep(random.uniform(0.001, 0.01))  # simulate work
    conn.close()
    return f"Writer {thread_id}: done {NUM_OPERATIONS} inserts"


def reader_thread(thread_id):
    """Simulate sweep script / dashboard reads."""
    conn = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    for i in range(NUM_OPERATIONS):
        rows = conn.execute(
            "SELECT COUNT(*) as cnt, AVG(value) as avg_val FROM trades"
        ).fetchall()
        # Read and potentially sleep to simulate work
        time.sleep(random.uniform(0.001, 0.005))
    conn.close()
    return f"Reader {thread_id}: done {NUM_OPERATIONS} reads"


def run_concurrent_test():
    """Run concurrent load test and report results."""
    setup()
    
    with ThreadPoolExecutor(max_workers=NUM_WRITERS + NUM_READERS) as executor:
        futures = []
        # Submit writers
        for w in range(NUM_WRITERS):
            futures.append(executor.submit(writer_thread, w))
        # Submit readers
        for r in range(NUM_READERS):
            futures.append(executor.submit(reader_thread, r))
        
        results = []
        start = time.time()
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                results.append(f"FAILED: {e}")
        elapsed = time.time() - start
    
    print(f"=== Concurrent Load Test Results ===")
    print(f"Writers: {NUM_WRITERS}, Readers: {NUM_READERS}")
    print(f"Ops per thread: {NUM_OPERATIONS}")
    print(f"Total time: {elapsed:.2f}s")
    print(f"Results:")
    for r in results:
        print(f"  {r}")
    
    # Cleanup
    os.unlink(TEST_DB)


if __name__ == "__main__":
    run_concurrent_test()
```

### 7.3 Verification Checklist

After migration, verify each file against this checklist:

| Check | Pass/Fail |
|-------|:---------:|
| All `sqlite3.connect()` replaced with `get_connection()` | ⬜ |
| All hardcoded absolute paths removed | ⬜ |
| All manual `PRAGMA journal_mode=WAL` removed | ⬜ |
| All manual `PRAGMA busy_timeout=` removed | ⬜ |
| Read-only bulk queries use `get_readonly_connection()` | ⬜ |
| `close_all()` called in script `__main__` blocks | ⬜ |
| `close_all()` NOT called in long-running daemons between operations | ⬜ |
| No `sqlite3` imports in migrated files (except for type hints) | ⬜ |
| `alert_retry_queue.py` timeout 1s → 15s (lowest-risk fix, highest impact) | ⬜ |
| GEFS read scripts use chunked `fetchmany()` not `fetchall()` | ⬜ |

### 7.4 Test Questions to Answer

1. **Does concurrent write (paper trading cron) + read (backtest) produce `SQLITE_BUSY`?**
   - Expected: No, with WAL + 15-30s busytimeout.
   - Test: Run the load test above with real file sizes (or truncated copies).

2. **Does mmap improve GEFS bulk read performance?**
   - Expected: 10-50× improvement.
   - Test: Time a `SELECT * FROM gefs_archive WHERE step=24` with and without mmap.

3. **Does the connection cache leak file descriptors under long-running processes?**
   - Expected: No more than 1 fd per DB per process (currently unbounded).
   - Test: `python3 -c "from core.db_connection import *; get_connection('metar_backfill'); import time; time.sleep(5)"` — check `/proc/PID/fd`.

4. **Does `get_readonly_connection()` actually use mmap?**
   - Expected: Yes, read-only URI connections enable mmap.
   - Test: `cat /proc/PID/maps | grep gefs_archive` — should show mapped region.

5. **Are WAL files (`.db-wal`, `.db-shm`) cleaned up properly?**
   - Expected: Auto-checkpoint on commit. For long bulk writes, `force_checkpoint()` truncates.
   - Test: Check `ls -la *.db-wal` after a large write operation.

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|:----------:|:------:|-----------|
| Migration introduces import error in production cron | Low | High | Phase migration: P0 first, test each cron individually |
| `busy_timeout` increase (1s→15s) hides a real deadlock | Low | Medium | Log `SQLITE_BUSY` retries via `sqlite3.set_authorizer` hook or enable `PRAGMA mmap_size` logging (no built-in); instead, monitor conn creation time |
| mmap segfault if file is truncated during read | Very low | Critical | Only use mmap on write-rare DBs (GEFS) where writes are batch-atomic |
| Connection cache prevents file rename between test runs | Medium | Low | `close(db_key)` before file operations |
| WAL grows unbounded without checkpointing | Medium | Low | `PRAGMA wal_autocheckpoint=1000` (default); `force_checkpoint()` after bulk writes |
| Network filesystem doesn't support WAL | Low | Medium | Verify all DBs are on local SSD. Add startup healthcheck. |

---

## 9. Summary

| Decision | Recommendation | Rationale |
|----------|---------------|-----------|
| Connection pattern | Context manager + module-level cache | Best of both: ergonomic, cached, no connection-per-operation |
| WAL mode | Universal | WAL is strictly better for all access patterns in this system |
| busy_timeout | Per-DB: 5-30s | Large readers need longer retry windows; small writers don't |
| Read-only connections | Separate cache with mmap | 10-50× faster bulk reads; must be separate from RW due to WAL isolation |
| Connection pooling | NOT recommended | SQLite gains nothing; one connection per DB per process is optimal |
| Connection lifecycle | Open once per process phase | Zero cost to hold open; re-opening loses cache state |
| GEFS BLOBs | mmap + chunked fetchmany | Avoids 90MB materialization; streaming reduces peak memory |
| Cache size | 256MB for large DBs, 16-64MB for small | Working set fits in RAM; leftover memory is wasted anyway |

**Total lines of code to replace:** ~200+ `sqlite3.connect()` calls across 63+ `core/*.py` and 18+ `scripts/*.py` files.
**New central code:** ~250 lines in `core/db_connection.py`.
**Expected net reduction:** ~500-700 lines of duplicated PRAGMA boilerplate removed.