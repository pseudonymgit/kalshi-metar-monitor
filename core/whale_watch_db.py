#!/usr/bin/env python3
"""
WhaleWatch — Order Book Microstructure Signal Detection

SQLite schema + CRUD for Kalshi order book snapshots, rolling baselines,
anomaly events, and signal journal.

Per the design doc (GOLDILOCKS-WHALE-WATCH.md), WhaleWatch detects informed
trading activity by analyzing aggregate order book signals (bid/ask depth,
spread asymmetry, volume concentration) that are free and publicly available.

Schema:
  ┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
  │ order_book_snap │ ──→ │ station_baseline │ ──→ │ anomaly_events  │
  └─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                                                          ▼
                                                   ┌─────────────────┐
                                                   │ signal_journal  │
                                                   └─────────────────┘

B-Mode compliant. No AI/ML. All SQLite.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
WHALE_DB = os.path.join(DATA_DIR, 'whale_watch.db')

SCHEMA_SQL = """
-- Order book snapshots (raw, append-only)
CREATE TABLE IF NOT EXISTS order_book_snap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    series_type TEXT NOT NULL,       -- 'HIGH' or 'LOW'
    bucket_ticker TEXT NOT NULL,     -- e.g., KXHIGHNYC-26
    bucket_temp_f INTEGER NOT NULL,  -- e.g., 84
    snapshot_ts TEXT NOT NULL,       -- ISO 8601 UTC
    yes_bid INTEGER,                 -- best bid in cents
    yes_ask INTEGER,                 -- best ask in cents
    no_bid INTEGER,
    no_ask INTEGER,
    yes_bid_size INTEGER,            -- contracts at best bid
    yes_ask_size INTEGER,
    no_bid_size INTEGER,
    no_ask_size INTEGER,
    last_price INTEGER,              -- cents
    volume_24h REAL,                 -- dollar volume
    open_interest INTEGER,
    spread_cents INTEGER,            -- yes_ask - yes_bid
    bid_ask_ratio REAL,              -- yes_bid_size / max(yes_ask_size, 1)
    source TEXT DEFAULT 'market_ticker',
    UNIQUE(station, series_type, bucket_ticker, snapshot_ts)
);

-- Order book full depth (anomaly-triggered, sampled)
CREATE TABLE IF NOT EXISTS order_book_depth (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    side TEXT NOT NULL,               -- 'yes' or 'no'
    price_cents INTEGER NOT NULL,
    size_contracts INTEGER NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES order_book_snap(id)
);

-- Rolling baseline per station-bucket-series
CREATE TABLE IF NOT EXISTS station_baseline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    series_type TEXT NOT NULL,
    bucket_temp_f INTEGER NOT NULL,
    hour_of_day INTEGER NOT NULL,     -- 0-23 local
    rolling_mean_20 REAL DEFAULT 0,
    rolling_std_20 REAL DEFAULT 0,
    rolling_mean_ratio_20 REAL DEFAULT 0,
    rolling_std_ratio_20 REAL DEFAULT 0,
    rolling_mean_vol_20 REAL DEFAULT 0,
    rolling_std_vol_20 REAL DEFAULT 0,
    rolling_mean_spread_20 REAL DEFAULT 0,
    rolling_std_spread_20 REAL DEFAULT 0,
    diurnal_mean REAL DEFAULT 0,
    diurnal_variance REAL DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    last_updated TEXT,
    UNIQUE(station, series_type, bucket_temp_f, hour_of_day)
);

-- Anomaly events (detected whale activity)
CREATE TABLE IF NOT EXISTS anomaly_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    series_type TEXT NOT NULL,
    bucket_temp_f INTEGER NOT NULL,
    bucket_ticker TEXT NOT NULL,
    detected_ts TEXT NOT NULL,        -- first detection
    confirmed_ts TEXT,                -- 2nd consecutive cycle
    expired_ts TEXT,                  -- strength decay > 50%
    anomaly_score REAL NOT NULL,
    anomaly_strength REAL NOT NULL,   -- min(score/5.0, 1.0)
    anomaly_components TEXT,           -- JSON: {S1, S2, S3, S4, S5}
    status TEXT DEFAULT 'suspected',  -- suspected | detected | high_conviction | expired
    yes_bid_size INTEGER,
    baseline_mean REAL,
    z_score_bid REAL,
    z_score_ratio REAL,
    z_score_volume REAL,
    spread_compression REAL,
    accumulation_score REAL,
    UNIQUE(station, series_type, bucket_temp_f, detected_ts)
);

-- Signal journal (trades generated from anomalies)
CREATE TABLE IF NOT EXISTS signal_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_id INTEGER NOT NULL,
    station TEXT NOT NULL,
    series_type TEXT NOT NULL,
    bucket_temp_f INTEGER NOT NULL,
    bucket_ticker TEXT NOT NULL,
    direction TEXT NOT NULL,           -- 'BUY_YES' or 'BUY_NO'
    entry_ts TEXT,
    entry_price_cents INTEGER,
    entry_reason TEXT,                 -- e.g., 'E1_WHALE_DETECTED'
    position_contracts INTEGER,
    position_dollars REAL,
    exit_ts TEXT,
    exit_price_cents INTEGER,
    exit_reason TEXT,                  -- E1-E6
    pnl_dollars REAL,
    settlement_hit TEXT,               -- 'YES', 'NO', 'PENDING'
    correct_prediction TEXT,           -- 'YES', 'NO', 'PENDING'
    FOREIGN KEY(anomaly_id) REFERENCES anomaly_events(id)
);

-- Create indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_snap_station ON order_book_snap(station, series_type, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_snap_bucket ON order_book_snap(bucket_ticker, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_baseline_lookup ON station_baseline(station, series_type, bucket_temp_f, hour_of_day);
CREATE INDEX IF NOT EXISTS idx_anomaly_status ON anomaly_events(station, status, detected_ts);
CREATE INDEX IF NOT EXISTS idx_journal_station ON signal_journal(station, entry_ts);
"""


def get_db(db_path: str = WHALE_DB) -> sqlite3.Connection:
    """Get a connection to the WhaleWatch DB with WAL mode."""
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None, db_path: str = WHALE_DB) -> sqlite3.Connection:
    """Initialize schema. Returns connection."""
    if conn is None:
        conn = get_db(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def insert_snapshot(conn: sqlite3.Connection, snap: dict) -> int:
    """Insert one order book snapshot. Returns rowid."""
    cur = conn.execute("""
        INSERT OR IGNORE INTO order_book_snap
        (station, series_type, bucket_ticker, bucket_temp_f, snapshot_ts,
         yes_bid, yes_ask, no_bid, no_ask,
         yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,
         last_price, volume_24h, open_interest, spread_cents, bid_ask_ratio)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snap['station'], snap['series_type'], snap['bucket_ticker'],
        snap['bucket_temp_f'], snap['snapshot_ts'],
        snap.get('yes_bid'), snap.get('yes_ask'),
        snap.get('no_bid'), snap.get('no_ask'),
        snap.get('yes_bid_size'), snap.get('yes_ask_size'),
        snap.get('no_bid_size'), snap.get('no_ask_size'),
        snap.get('last_price'), snap.get('volume_24h'),
        snap.get('open_interest'),
        snap.get('spread_cents') or
            (snap.get('yes_ask', 0) - snap.get('yes_bid', 0)),
        snap.get('bid_ask_ratio') or
            (snap.get('yes_bid_size', 0) / max(snap.get('yes_ask_size', 1), 1)),
    ))
    return cur.lastrowid or 0


def insert_depth(conn: sqlite3.Connection, snapshot_id: int,
                 side: str, levels: List[Tuple[int, int]]) -> int:
    """Insert order book depth levels. Returns count inserted."""
    if not levels:
        return 0
    rows = [(snapshot_id, side, price, size) for price, size in levels]
    conn.executemany("""
        INSERT INTO order_book_depth (snapshot_id, side, price_cents, size_contracts)
        VALUES (?,?,?,?)
    """, rows)
    return len(rows)


def get_recent_snapshots(conn: sqlite3.Connection, station: str,
                         series_type: str, bucket_temp_f: int,
                         limit: int = 20) -> List[dict]:
    """Get recent snapshots for a station-bucket for baseline computation."""
    rows = conn.execute("""
        SELECT * FROM order_book_snap
        WHERE station=? AND series_type=? AND bucket_temp_f=?
        ORDER BY snapshot_ts DESC
        LIMIT ?
    """, (station, series_type, bucket_temp_f, limit)).fetchall()
    return [dict(r) for r in rows]


def get_recent_station_snapshots(conn: sqlite3.Connection, station: str,
                                 series_type: str, limit: int = 20) -> List[dict]:
    """Get recent snapshots for all buckets at a station."""
    rows = conn.execute("""
        SELECT * FROM order_book_snap
        WHERE station=? AND series_type=?
        ORDER BY snapshot_ts DESC
        LIMIT ?
    """, (station, series_type, limit)).fetchall()
    return [dict(r) for r in rows]


def upsert_baseline(conn: sqlite3.Connection, baseline: dict) -> None:
    """Upsert a station-bucket-hour baseline row."""
    conn.execute("""
        INSERT INTO station_baseline
        (station, series_type, bucket_temp_f, hour_of_day,
         rolling_mean_20, rolling_std_20,
         rolling_mean_ratio_20, rolling_std_ratio_20,
         rolling_mean_vol_20, rolling_std_vol_20,
         rolling_mean_spread_20, rolling_std_spread_20,
         diurnal_mean, diurnal_variance,
         sample_count, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(station, series_type, bucket_temp_f, hour_of_day)
        DO UPDATE SET
            rolling_mean_20=excluded.rolling_mean_20,
            rolling_std_20=excluded.rolling_std_20,
            rolling_mean_ratio_20=excluded.rolling_mean_ratio_20,
            rolling_std_ratio_20=excluded.rolling_std_ratio_20,
            rolling_mean_vol_20=excluded.rolling_mean_vol_20,
            rolling_std_vol_20=excluded.rolling_std_vol_20,
            rolling_mean_spread_20=excluded.rolling_mean_spread_20,
            rolling_std_spread_20=excluded.rolling_std_spread_20,
            diurnal_mean=excluded.diurnal_mean,
            diurnal_variance=excluded.diurnal_variance,
            sample_count=excluded.sample_count,
            last_updated=excluded.last_updated
    """, (
        baseline['station'], baseline['series_type'],
        baseline['bucket_temp_f'], baseline['hour_of_day'],
        baseline.get('rolling_mean_20', 0),
        baseline.get('rolling_std_20', 0),
        baseline.get('rolling_mean_ratio_20', 0),
        baseline.get('rolling_std_ratio_20', 0),
        baseline.get('rolling_mean_vol_20', 0),
        baseline.get('rolling_std_vol_20', 0),
        baseline.get('rolling_mean_spread_20', 0),
        baseline.get('rolling_std_spread_20', 0),
        baseline.get('diurnal_mean', 0),
        baseline.get('diurnal_variance', 0),
        baseline.get('sample_count', 0),
        datetime.now(timezone.utc).isoformat(),
    ))


def get_baseline(conn: sqlite3.Connection, station: str,
                 series_type: str, bucket_temp_f: int,
                 hour_of_day: int) -> Optional[dict]:
    """Get baseline for a specific station-bucket-hour."""
    row = conn.execute("""
        SELECT * FROM station_baseline
        WHERE station=? AND series_type=? AND bucket_temp_f=? AND hour_of_day=?
    """, (station, series_type, bucket_temp_f, hour_of_day)).fetchone()
    return dict(row) if row else None


def insert_anomaly(conn: sqlite3.Connection, anomaly: dict) -> int:
    """Insert or update an anomaly event. Returns rowid."""
    # Use detected_ts as unique key to avoid duplicates
    existing = conn.execute("""
        SELECT id, status FROM anomaly_events
        WHERE station=? AND series_type=? AND bucket_temp_f=? AND detected_ts=?
    """, (anomaly['station'], anomaly['series_type'],
          anomaly['bucket_temp_f'], anomaly['detected_ts'])).fetchone()

    if existing:
        # Update existing
        conn.execute("""
            UPDATE anomaly_events SET
                confirmed_ts=?, anomaly_score=?, anomaly_strength=?,
                anomaly_components=?, status=?,
                yes_bid_size=?, z_score_bid=?, z_score_ratio=?,
                z_score_volume=?, spread_compression=?, accumulation_score=?
            WHERE id=?
        """, (
            anomaly.get('confirmed_ts'), anomaly['anomaly_score'],
            anomaly['anomaly_strength'],
            json.dumps(anomaly.get('anomaly_components', {})),
            anomaly.get('status', 'suspected'),
            anomaly.get('yes_bid_size'), anomaly.get('z_score_bid'),
            anomaly.get('z_score_ratio'), anomaly.get('z_score_volume'),
            anomaly.get('spread_compression'), anomaly.get('accumulation_score'),
            existing['id'],
        ))
        return existing['id']
    else:
        cur = conn.execute("""
            INSERT INTO anomaly_events
            (station, series_type, bucket_temp_f, bucket_ticker,
             detected_ts, confirmed_ts, anomaly_score, anomaly_strength,
             anomaly_components, status, yes_bid_size, z_score_bid,
             z_score_ratio, z_score_volume, spread_compression, accumulation_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            anomaly['station'], anomaly['series_type'],
            anomaly['bucket_temp_f'], anomaly['bucket_ticker'],
            anomaly['detected_ts'], anomaly.get('confirmed_ts'),
            anomaly['anomaly_score'], anomaly['anomaly_strength'],
            json.dumps(anomaly.get('anomaly_components', {})),
            anomaly.get('status', 'suspected'),
            anomaly.get('yes_bid_size'), anomaly.get('z_score_bid'),
            anomaly.get('z_score_ratio'), anomaly.get('z_score_volume'),
            anomaly.get('spread_compression'), anomaly.get('accumulation_score'),
        ))
        return cur.lastrowid or 0


def get_active_anomalies(conn: sqlite3.Connection, station: Optional[str] = None,
                         min_score: float = 2.0) -> List[dict]:
    """Get anomalies that are still active (not expired)."""
    query = """
        SELECT * FROM anomaly_events
        WHERE status IN ('suspected', 'detected', 'high_conviction')
          AND anomaly_score >= ?
    """
    params = [min_score]
    if station:
        query += " AND station=?"
        params.append(station)
    query += " ORDER BY anomaly_score DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def insert_signal(conn: sqlite3.Connection, signal: dict) -> int:
    """Insert a trade signal entry. Returns rowid."""
    cur = conn.execute("""
        INSERT INTO signal_journal
        (anomaly_id, station, series_type, bucket_temp_f, bucket_ticker,
         direction, entry_ts, entry_price_cents, entry_reason,
         position_contracts, position_dollars)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        signal['anomaly_id'], signal['station'], signal['series_type'],
        signal['bucket_temp_f'], signal['bucket_ticker'],
        signal['direction'], signal.get('entry_ts'),
        signal.get('entry_price_cents'), signal.get('entry_reason'),
        signal.get('position_contracts'), signal.get('position_dollars'),
    ))
    return cur.lastrowid or 0


def update_signal_exit(conn: sqlite3.Connection, signal_id: int, exit_info: dict) -> None:
    """Update a signal with exit information."""
    conn.execute("""
        UPDATE signal_journal SET
            exit_ts=?, exit_price_cents=?, exit_reason=?,
            pnl_dollars=?, settlement_hit=?, correct_prediction=?
        WHERE id=?
    """, (
        exit_info.get('exit_ts'), exit_info.get('exit_price_cents'),
        exit_info.get('exit_reason'), exit_info.get('pnl_dollars'),
        exit_info.get('settlement_hit'), exit_info.get('correct_prediction'),
        signal_id,
    ))


def get_snapshot_stats(conn: sqlite3.Connection) -> dict:
    """Get summary statistics for monitoring."""
    stats = {}
    stats['total_snapshots'] = conn.execute(
        "SELECT COUNT(*) FROM order_book_snap").fetchone()[0]
    stats['stations_tracked'] = conn.execute(
        "SELECT COUNT(DISTINCT station) FROM order_book_snap").fetchone()[0]
    stats['anomalies_detected'] = conn.execute(
        "SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
    stats['active_anomalies'] = conn.execute(
        "SELECT COUNT(*) FROM anomaly_events WHERE status IN ('suspected','detected','high_conviction')"
    ).fetchone()[0]
    stats['signals_generated'] = conn.execute(
        "SELECT COUNT(*) FROM signal_journal").fetchone()[0]
    stats['last_snapshot_ts'] = conn.execute(
        "SELECT MAX(snapshot_ts) FROM order_book_snap").fetchone()[0]
    return stats


if __name__ == '__main__':
    conn = init_db()
    print("WhaleWatch DB initialized at", WHALE_DB)
    print(json.dumps(get_snapshot_stats(conn), indent=2))
    conn.close()