#!/usr/bin/env python3
"""
REAL-MODE V5 — Kalshi Passive Collector with Persistent Storage

Read-only passive collector for Kalshi weather market data.
Extends the collector-only v5 design with normalized SQLite persistence,
hard 2GB storage cap, 48-hour expiry, startup hash assertion, and
proper error handling.

ARCHITECTURE:
    ┌──────────────────────────────────────────────────┐
    │  KalshiPassiveCollector (from collector-only v5) │
    │  - Static 20-station map                         │
    │  - Endpoint allowlist + deny-list enforcement    │
    │  - Per-station independent collection             │
    │  - Rat< 0.1s between requests                     │
    │  - No credentials, no auth                        │
    └────────────┬─────────────────────────────────────┘
                 │ collect_all() returns dict
    ┌────────────▼─────────────────────────────────────┐
    │  RealModeStorage                                  │
    │  - Normalized SQLite schema (3 tables)            │
    │  - No raw JSON hoarding                           │
    │  - Hard 2GB fail-closed cap (pre-write check)     │
    │  - 48-hour expiry (auto-purge on write)           │
    │  - Lock file (prevent concurrent runs)            │
    │  - Startup hash assertion (self-masking SHA-256)  │
    │  - Provenance logging (runs, rows, bytes)         │
    └──────────────────────────────────────────────────┘

ENDPOINT SCOPE (auth=False, public only):
    GET /series/{ticker}
    GET /events?series_ticker=...&with_markets=true
    GET /markets/{ticker}
    GET /markets?series_ticker=...

STORAGE:
    SQLite at data/stream3_v5.db — normalized tables:
    - series: 1 row per station per market type
    - events: 1 row per event, FK to series
    - markets: 1 row per market, FK to event/series

AUTHORIZED BY: Gerri (pending fresh review)
VERSION: real-v5.0
"""

import sys
import os
import json
import time
import hashlib
import logging
import sqlite3
import threading
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

import requests
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# ── File Identity ──────────────────────────────────────────────────────
# Self-masking: the guard line below is excluded from the hash computation
# by hashing the file content with this line replaced by a fixed placeholder.
# This avoids the fixed-point problem where the hash changes when the guard
# line is updated.
# guard-hash-placeholder: REALV5_00000000

# ── Static 20-Station Map ──────────────────────────────────────────────
# Verified against Kalshi API settlement_sources (2026-07-28).
# ICAO code → Kalshi 3-letter code used in KXHIGH/KXLOW series.

STATION_MAP: Dict[str, str] = {
    "KATL": "TATL",       # Atlanta Hartsfield-Jackson
    "KAUS": "AUS",         # Austin-Bergstrom
    "KBOS": "TBOS",        # Boston Logan
    "KDCA": "TDC",         # Washington Reagan National
    "KDEN": "DEN",         # Denver International
    "KDFW": "TDAL",        # Dallas-Fort Worth
    "KHOU": "THOU",        # Houston Bush Intercontinental
    "KLAS": "TLV",         # Las Vegas McCarran
    "KLAX": "LAX",         # Los Angeles International
    "KMDW": "CHI",         # Chicago Midway
    "KMIA": "MIA",         # Miami International
    "KMSP": "TMIN",        # Minneapolis-St. Paul
    "KMSY": "TNOLA",       # New Orleans Louis Armstrong
    "KNYC": "NY",          # New York Central Park
    "KOKC": "TOKC",        # Oklahoma City Will Rogers
    "KPHL": "PHIL",        # Philadelphia International
    "KPHX": "TPHX",        # Phoenix Sky Harbor
    "KSAT": "TSATX",       # San Antonio International
    "KSEA": "TSEA",        # Seattle-Tacoma
    "KSFO": "TSFO",        # San Francisco International
}

STATION_INFO: Dict[str, Dict[str, str]] = {
    "KATL": {"city": "Atlanta", "state": "GA", "tz": "America/New_York"},
    "KAUS": {"city": "Austin", "state": "TX", "tz": "America/Chicago"},
    "KBOS": {"city": "Boston", "state": "MA", "tz": "America/New_York"},
    "KDCA": {"city": "Washington DC", "state": "VA", "tz": "America/New_York"},
    "KDEN": {"city": "Denver", "state": "CO", "tz": "America/Denver"},
    "KDFW": {"city": "Dallas-Fort Worth", "state": "TX", "tz": "America/Chicago"},
    "KHOU": {"city": "Houston", "state": "TX", "tz": "America/Chicago"},
    "KLAS": {"city": "Las Vegas", "state": "NV", "tz": "America/Los_Angeles"},
    "KLAX": {"city": "Los Angeles", "state": "CA", "tz": "America/Los_Angeles"},
    "KMDW": {"city": "Chicago", "state": "IL", "tz": "America/Chicago"},
    "KMIA": {"city": "Miami", "state": "FL", "tz": "America/New_York"},
    "KMSP": {"city": "Minneapolis", "state": "MN", "tz": "America/Chicago"},
    "KMSY": {"city": "New Orleans", "state": "LA", "tz": "America/Chicago"},
    "KNYC": {"city": "New York", "state": "NY", "tz": "America/New_York"},
    "KOKC": {"city": "Oklahoma City", "state": "OK", "tz": "America/Chicago"},
    "KPHL": {"city": "Philadelphia", "state": "PA", "tz": "America/New_York"},
    "KPHX": {"city": "Phoenix", "state": "AZ", "tz": "America/Phoenix"},
    "KSAT": {"city": "San Antonio", "state": "TX", "tz": "America/Chicago"},
    "KSEA": {"city": "Seattle", "state": "WA", "tz": "America/Los_Angeles"},
    "KSFO": {"city": "San Francisco", "state": "CA", "tz": "America/Los_Angeles"},
}

# ── Endpoint Restrictions ──────────────────────────────────────────────
# Kalshi trade-api/v2 verified endpoints (2026-08-23)

ALLOWED_ENDPOINTS: List[str] = [
    '/markets',           # /markets/{ticker} and /markets?series_ticker= (plural)
    '/series/',           # /series/{ticker} — series info
    '/events',            # /events?series_ticker=... — event listing
]

DENIED_PREFIXES: List[str] = [
    '/portfolio', '/orders', '/account', '/balance',
    '/member', '/position', '/fills', '/transfer',
    '/deposit', '/withdrawal', '/trades', '/candles',
    '/settlements', '/market/',  # singular path (404 on v2 API)
]

# ── Market Types ───────────────────────────────────────────────────────

MARKET_TYPES: List[Tuple[str, str]] = [
    ("HIGH", "KXHIGH"),
    ("LOW", "KXLOW"),
]

# ── Storage Configuration ──────────────────────────────────────────────

STORAGE_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "stream3_v5.db"
)
LOCK_PATH: str = STORAGE_PATH + ".lock"
MAX_STORAGE_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB hard cap
RETENTION_HOURS: int = 48  # auto-purge data older than this
MAX_COLLECTION_RUNTIME: int = 3600  # 1 hour max collection run
MIN_COLLECTION_INTERVAL: int = 300  # 5 min minimum between collections
REQUIRED_HASH: str = "0289960106cab0528599a87b5a9ac1a827ed0ab9f15c8a9b0fd7366ef7e9bd1e"  # Updated at build time

# ── Storage Schema ─────────────────────────────────────────────────────

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS v5_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL CHECK(market_type IN ('HIGH','LOW')),
    series_ticker TEXT NOT NULL,
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    open_time TEXT DEFAULT '',
    settlement_sources TEXT DEFAULT '[]',
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station, market_type)
);

CREATE TABLE IF NOT EXISTS v5_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    title TEXT DEFAULT '',
    strike_date TEXT DEFAULT '',
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(event_ticker, collected_at)
);

CREATE TABLE IF NOT EXISTS v5_markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    ticker TEXT NOT NULL,
    title TEXT DEFAULT '',
    result TEXT DEFAULT '',
    last_price TEXT DEFAULT '',
    close_time TEXT DEFAULT '',
    yes_bid TEXT DEFAULT '',
    no_bid TEXT DEFAULT '',
    yes_ask TEXT DEFAULT '',
    no_ask TEXT DEFAULT '',
    open_interest TEXT DEFAULT '',
    volume TEXT DEFAULT '',
    floor_strike TEXT DEFAULT '',
    cap_strike TEXT DEFAULT '',
    liquidity TEXT DEFAULT '',
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, collected_at)
);

CREATE INDEX IF NOT EXISTS idx_v5_markets_station ON v5_markets(station);
CREATE INDEX IF NOT EXISTS idx_v5_markets_event ON v5_markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_v5_markets_close ON v5_markets(close_time);
CREATE INDEX IF NOT EXISTS idx_v5_events_series ON v5_events(series_ticker);
CREATE INDEX IF NOT EXISTS idx_v5_events_strike ON v5_events(strike_date);

CREATE TABLE IF NOT EXISTS v5_provenance (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL,
    stations_ok INTEGER DEFAULT 0,
    stations_failed INTEGER DEFAULT 0,
    total_markets INTEGER DEFAULT 0,
    data_size_bytes INTEGER DEFAULT 0,
    storage_size_bytes INTEGER DEFAULT 0,
    storage_cap_ok INTEGER DEFAULT 1,
    errors TEXT DEFAULT '[]',
    collector_hash TEXT DEFAULT ''
);
"""


# ═════════════════════════════════════════════════════════════════════════
# Hash Assertion
# ═════════════════════════════════════════════════════════════════════════

def _guard_line() -> str:
    """Return the guard line whose hash sum is excluded from self-hash."""
    return "# guard-hash-placeholder:"


def compute_file_hash(path: str = None) -> str:
    """Compute SHA-256 of this file, excluding the guard line.
    
    The guard line (containing 'guard-hash-placeholder') is replaced
    with a fixed string before hashing, avoiding the fixed-point problem.
    """
    if path is None:
        path = __file__
    with open(path, 'rb') as f:
        content = f.read()
    
    # Replace the guard line with a fixed placeholder
    # (the line changes when the hash is updated, but the hash
    #  should only reflect the actual code, not the guard value)
    guard_marker = b'guard-hash-placeholder:'
    required_hash_marker = b'REQUIRED_HASH:'
    lines = content.split(b'\n')
    cleaned = []
    for line in lines:
        if guard_marker in line:
            cleaned.append(b'# guard-hash-placeholder: FIXED_FOR_HASH')
        elif required_hash_marker in line:
            cleaned.append(b'REQUIRED_HASH: str = "FIXED_FOR_HASH"')
        else:
            cleaned.append(line)
    cleaned_content = b'\n'.join(cleaned)
    return hashlib.sha256(cleaned_content).hexdigest()


def verify_file_hash(required_hash: str, path: str = None) -> bool:
    """Verify file hash matches required hash. Exit 99 on mismatch."""
    actual = compute_file_hash(path)
    if actual == required_hash:
        return True
    logger.error(
        f"HASH MISMATCH: expected {required_hash[:16]}..., "
        f"got {actual[:16]}... — aborting"
    )
    sys.exit(99)


# ═════════════════════════════════════════════════════════════════════════
# Collector (based on v5 collector-only design)
# ═════════════════════════════════════════════════════════════════════════

class KalshiPassiveCollector:
    """Read-only passive Kalshi weather market data collector.
    
    Reuses the verified v5 collector design: same endpoint construction,
    allowlist enforcement, station map, and rate limiting.
    No credentials, no auth, no ~/.kalshi loading.
    """
    
    def __init__(self, api_base: str = "https://api.elections.kalshi.com/trade-api/v2/"):
        self.api_base = api_base.rstrip('/') + '/'
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'WeatherEngine/1.0',
            'Accept': 'application/json',
        })
        self._last_request_time: float = 0.0
        self._min_interval: float = 0.1  # 100ms (~10 req/s)
        self.allowed_endpoints = ALLOWED_ENDPOINTS
        self.denied_prefixes = DENIED_PREFIXES
        self.station_map = dict(STATION_MAP)
        self.station_info = dict(STATION_INFO)
    
    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()
    
    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        path = endpoint.split('?')[0].rstrip('/')
        for denied in self.denied_prefixes:
            if path.startswith(denied):
                logger.warning(f"BLOCKED [deny-list]: {endpoint[:80]}")
                return None
        allowed = any(path.startswith(a) for a in self.allowed_endpoints)
        if not allowed:
            logger.warning(f"BLOCKED [allowlist]: {endpoint[:80]}")
            return None
        clean_path = endpoint.lstrip('/')
        url = urljoin(self.api_base, clean_path)
        self._rate_limit()
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"OK: {url[:80]} ({len(str(data))} bytes)")
            return data
        except requests.exceptions.Timeout:
            logger.warning(f"TIMEOUT: {url[:80]}")
            return None
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else '?'
            logger.warning(f"HTTP {code}: {url[:80]}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"FAILED: {url[:80]} -> {e}")
            return None
    
    def get_series_info(self, series_ticker: str) -> Optional[Dict]:
        result = self._make_request(f"/series/{series_ticker}")
        return result.get('series') if isinstance(result, dict) else None
    
    def get_events(self, series_ticker: str, limit: int = 3) -> List[Dict]:
        result = self._make_request(
            f"/events?series_ticker={series_ticker}&with_markets=true&limit={limit}"
        )
        return result.get('events', []) if isinstance(result, dict) else []
    
    def get_markets_by_series(self, series_ticker: str, limit: int = 50) -> List[Dict]:
        result = self._make_request(
            f"/markets?series_ticker={series_ticker}&limit={limit}"
        )
        return result.get('markets', []) if isinstance(result, dict) else []
    
    def get_market_detail(self, ticker: str) -> Optional[Dict]:
        result = self._make_request(f"/markets/{ticker}")
        return result.get('market') if isinstance(result, dict) else None
    
    def collect_station(self, station_code: str) -> Dict[str, Any]:
        station_key = station_code.upper()
        info = self.station_info.get(station_key, {})
        result: Dict[str, Any] = {
            "station": station_key,
            "city": info.get("city", ""),
            "state": info.get("state", ""),
            "timezone": info.get("tz", ""),
            "collection_timestamp": datetime.now(timezone.utc).isoformat(),
            "series": {},
            "events": [],
            "markets": [],
            "errors": [],
        }
        for market_type, prefix in MARKET_TYPES:
            code = self.station_map.get(station_key)
            if not code:
                result["errors"].append(f"{market_type}: No Kalshi code for {station_key}")
                continue
            series_ticker = f"{prefix}{code}"
            
            # Step 1: GET /series/{ticker}
            try:
                series_data = self.get_series_info(series_ticker)
                if series_data:
                    result["series"][market_type] = {
                        "ticker": series_ticker,
                        "title": series_data.get("title", ""),
                        "category": series_data.get("category", ""),
                        "settlement_sources": series_data.get("settlement_sources", []),
                        "open_time": series_data.get("open_time", ""),
                    }
                else:
                    result["errors"].append(f"{market_type}: Series returned no data")
                    continue
            except Exception as e:
                result["errors"].append(f"{market_type}: Series error: {e}")
                continue
            
            # Step 2: GET /events?series_ticker=...&with_markets=true
            try:
                events = self.get_events(series_ticker, limit=5)
                for ev in events:
                    event_entry = {
                        "event_ticker": ev.get("event_ticker", ""),
                        "title": ev.get("title", ""),
                        "strike_date": ev.get("strike_date", ""),
                        "market_count": len(ev.get("markets", [])),
                    }
                    result["events"].append(event_entry)
                    for m in ev.get("markets", []):
                        ticker = m.get("ticker", "")
                        if not ticker:
                            continue
                        market_entry = {
                            "ticker": ticker,
                            "title": m.get("title", ""),
                            "result": m.get("result", ""),
                            "last_price": m.get("last_price_dollars", ""),
                            "close_time": m.get("close_time", ""),
                            "floor_strike": m.get("floor_strike"),
                            "cap_strike": m.get("cap_strike"),
                        }
                        try:
                            detail = self.get_market_detail(ticker)
                            if detail:
                                market_entry.update({
                                    "yes_bid": detail.get("yes_bid"),
                                    "no_bid": detail.get("no_bid"),
                                    "yes_ask": detail.get("yes_ask"),
                                    "no_ask": detail.get("no_ask"),
                                    "last_price": detail.get("last_price", market_entry["last_price"]),
                                    "open_interest": detail.get("open_interest"),
                                    "volume": detail.get("volume"),
                                })
                        except Exception as e:
                            result["errors"].append(f"Detail error {ticker}: {e}")
                        result["markets"].append(market_entry)
            except Exception as e:
                result["errors"].append(f"Events error: {e}")
            
            # Step 3: GET /markets?series_ticker=... (bulk, catches remaining)
            try:
                bulk_markets = self.get_markets_by_series(series_ticker, limit=50)
                seen = set(m["ticker"] for m in result["markets"])
                for bm in bulk_markets:
                    ticker = bm.get("ticker", "")
                    if ticker and ticker not in seen:
                        market_entry = {
                            "ticker": ticker,
                            "title": bm.get("title", "") or bm.get("sub_title", ""),
                            "result": bm.get("result", ""),
                            "last_price": bm.get("last_price_dollars", ""),
                            "close_time": bm.get("close_time", ""),
                            "floor_strike": bm.get("floor_strike"),
                            "cap_strike": bm.get("cap_strike"),
                            "liquidity": bm.get("liquidity_dollars", ""),
                            "volume": bm.get("volume"),
                        }
                        try:
                            detail = self.get_market_detail(ticker)
                            if detail:
                                market_entry["yes_bid"] = detail.get("yes_bid")
                                market_entry["no_bid"] = detail.get("no_bid")
                                market_entry["open_interest"] = detail.get("open_interest")
                        except Exception:
                            pass
                        result["markets"].append(market_entry)
                        seen.add(ticker)
            except Exception as e:
                result["errors"].append(f"Bulk error: {e}")
        
        result["markets"].sort(key=lambda m: m.get("ticker", ""))
        result["market_count"] = len(result["markets"])
        return result
    
    def collect_all(self) -> Dict[str, Any]:
        stations = sorted(self.station_map.keys())
        logger.info(f"Collecting {len(stations)} stations")
        start_ts = datetime.now(timezone.utc)
        result: Dict[str, Any] = {
            "collector_version": "real-v5",
            "collection_start": start_ts.isoformat(),
            "total_stations": len(stations),
            "stations": [],
            "success_count": 0,
            "failed_count": 0,
            "total_markets": 0,
            "errors": [],
        }
        for i, station in enumerate(stations, 1):
            logger.info(f"[{i:02d}/{len(stations)}] {station}")
            try:
                sr = self.collect_station(station)
                result["stations"].append(sr)
                result["total_markets"] += sr.get("market_count", 0)
                if sr.get("errors"):
                    result["failed_count"] += 1
                    result["errors"].extend(f"{station}: {e}" for e in sr["errors"])
                else:
                    result["success_count"] += 1
            except Exception as e:
                result["failed_count"] += 1
                result["errors"].append(f"{station}: CRASHED: {e}")
        end_ts = datetime.now(timezone.utc)
        result["collection_end"] = end_ts.isoformat()
        result["duration_seconds"] = (end_ts - start_ts).total_seconds()
        result["success_rate"] = (
            result["success_count"] / result["total_stations"]
            if result["total_stations"] > 0 else 0.0
        )
        logger.info(
            f"Collection complete: {result['success_count']}/{result['total_stations']} "
            f"stations, {result['total_markets']} markets, "
            f"{result['duration_seconds']:.1f}s"
        )
        return result
    
    def dry_run(self, lookback_hours: int = 48) -> Dict[str, Any]:
        logger.info(f"Dry run (lookback_hours={lookback_hours})")
        data = self.collect_all()
        serialized = json.dumps(data, default=str).encode('utf-8')
        size_bytes = len(serialized)
        size_mb = size_bytes / (1024 * 1024)
        data["dry_run"] = {
            "lookback_hours": lookback_hours,
            "data_size_bytes": size_bytes,
            "data_size_mb": round(size_mb, 2),
            "disk_cap_bytes": MAX_STORAGE_BYTES,
            "disk_cap_ok": size_bytes < MAX_STORAGE_BYTES,
            "credential_free": True,
            "auth_enabled": False,
            "db_writes": 0,
        }
        logger.info(
            f"Dry run complete: {data['success_count']}/{data['total_stations']} "
            f"stations, {data['total_markets']} markets, {size_mb:.2f}MB"
        )
        return data


# ═════════════════════════════════════════════════════════════════════════
# Real-Mode Storage
# ═════════════════════════════════════════════════════════════════════════

class RealModeStorage:
    """Persistent SQLite storage for normalized market data.
    
    Enforces:
    - 2 GB hard fail-closed cap (pre-write check)
    - 48-hour expiry (auto-purge old data on write)
    - No concurrent runs (lock file)
    - Startup hash assertion
    """
    
    def __init__(self, db_path: str = STORAGE_PATH):
        self.db_path = db_path
        self.lock_path = db_path + ".lock"
        self._conn: Optional[sqlite3.Connection] = None
    
    def acquire_lock(self) -> bool:
        """Acquire lock file. Returns True if acquired, False if held."""
        try:
            self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            # Write PID for debugging
            os.write(self._lock_fd, str(os.getpid()).encode())
            return True
        except FileExistsError:
            return False
    
    def release_lock(self) -> None:
        if hasattr(self, '_lock_fd') and self._lock_fd is not None:
            os.close(self._lock_fd)
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass
            self._lock_fd = None
    
    def get_storage_size(self) -> int:
        """Return current DB file size in bytes."""
        try:
            return os.path.getsize(self.db_path)
        except FileNotFoundError:
            return 0
    
    def check_storage_cap(self, estimated_write_bytes: int = 0) -> bool:
        """Check if storage cap would be exceeded. Returns True if OK."""
        current = self.get_storage_size()
        if current + estimated_write_bytes >= MAX_STORAGE_BYTES:
            logger.error(
                f"STORAGE CAP REACHED: {current}/{MAX_STORAGE_BYTES} bytes "
                f"({current/MAX_STORAGE_BYTES:.1%})"
            )
            return False
        return True
    
    def open(self) -> sqlite3.Connection:
        """Open connection, create schema, enforce storage cap, purge old data."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # Check storage cap before opening
        current = self.get_storage_size()
        if current >= MAX_STORAGE_BYTES:
            raise RuntimeError(
                f"STORAGE CAP REACHED: {current}/{MAX_STORAGE_BYTES} bytes. "
                "Cannot open DB. Run cleanup or increase cap."
            )
        
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=100")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        
        # Create schema
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        
        # Purge data older than retention window
        self._purge_expired()
        
        return self._conn
    
    def close(self) -> None:
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None
        self.release_lock()
    
    def _purge_expired(self) -> None:
        """Delete rows older than RETENTION_HOURS."""
        if not self._conn:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat()
        total = 0
        for table in ['v5_markets', 'v5_events', 'v5_series']:
            cursor = self._conn.execute(
                f"DELETE FROM {table} WHERE collected_at < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            total += deleted
            if deleted > 0:
                logger.info(f"Purged {deleted} rows from {table} (cutoff: {cutoff})")
        if total > 0:
            self._conn.commit()
            logger.info(f"Total purged: {total} rows")
    
    def store_collection(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Store normalized collection data into SQLite.
        
        Returns dict with storage stats (rows inserted, bytes, errors).
        """
        if not self._conn:
            raise RuntimeError("Storage not opened. Call open() first.")
        
        # Check storage cap
        estimated_size = self.get_storage_size() + 500_000  # ~500KB safety margin
        if not self.check_storage_cap(estimated_size):
            raise RuntimeError("Storage cap exceeded — aborting write")
        
        stats = {
            "series_inserted": 0,
            "events_inserted": 0,
            "markets_inserted": 0,
            "errors": [],
        }
        
        collected_at = datetime.now(timezone.utc).isoformat()
        batch_markets = 0
        
        for station_result in data.get("stations", []):
            station = station_result.get("station", "UNKNOWN")
            
            # Store series
            for market_type, series_data in station_result.get("series", {}).items():
                series_ticker = series_data.get("ticker", "")
                try:
                    self._conn.execute("""
                        INSERT OR REPLACE INTO v5_series
                        (station, market_type, series_ticker, title, category,
                         open_time, settlement_sources, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        station, market_type, series_ticker,
                        series_data.get("title", ""),
                        series_data.get("category", ""),
                        series_data.get("open_time", ""),
                        json.dumps(series_data.get("settlement_sources", [])),
                        collected_at,
                    ))
                    stats["series_inserted"] += 1
                except Exception as e:
                    stats["errors"].append(f"{station}: Series insert error: {e}")
            
            # Store events
            for ev in station_result.get("events", []):
                try:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO v5_events
                        (station, market_type, series_ticker, event_ticker,
                         title, strike_date, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        station, station_result.get("series", {}).get("HIGH", {}).get("ticker", ""),
                        station_result.get("series", {}).get("HIGH", {}).get("ticker", ""),
                        ev.get("event_ticker", ""),
                        ev.get("title", ""),
                        ev.get("strike_date", ""),
                        collected_at,
                    ))
                    stats["events_inserted"] += 1
                except Exception as e:
                    stats["errors"].append(f"{station}: Event insert error: {e}")
            
            # Store markets
            for m in station_result.get("markets", []):
                try:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO v5_markets
                        (station, market_type, series_ticker, event_ticker,
                         ticker, title, result, last_price, close_time,
                         yes_bid, no_bid, yes_ask, no_ask,
                         open_interest, volume, floor_strike, cap_strike,
                         liquidity, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        station,
                        "HIGH" if "KXHIGH" in str(station_result.get("series", {})) else "LOW",
                        station_result.get("series", {}).get("HIGH", {}).get("ticker", ""),
                        m.get("event_ticker", ""),
                        m.get("ticker", ""),
                        m.get("title", ""), m.get("result", ""),
                        m.get("last_price", ""), m.get("close_time", ""),
                        m.get("yes_bid", ""), m.get("no_bid", ""),
                        m.get("yes_ask", ""), m.get("no_ask", ""),
                        str(m.get("open_interest", "")),
                        str(m.get("volume", "")),
                        str(m.get("floor_strike", "")),
                        str(m.get("cap_strike", "")),
                        str(m.get("liquidity", "")),
                        collected_at,
                    ))
                    stats["markets_inserted"] += 1
                    batch_markets += 1
                    
                    # Periodically commit to avoid large transactions
                    if batch_markets >= 50:
                        self._conn.commit()
                        batch_markets = 0
                except Exception as e:
                    stats["errors"].append(f"{station}/{m.get('ticker','')}: {e}")
        
        # Final commit
        self._conn.commit()
        
        # Purge expired data after write
        self._purge_expired()
        
        # Record provenance
        try:
            self._conn.execute("""
                INSERT INTO v5_provenance
                (started_at, ended_at, duration_seconds, stations_ok,
                 stations_failed, total_markets, data_size_bytes,
                 storage_size_bytes, storage_cap_ok, errors, collector_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("collection_start", collected_at),
                data.get("collection_end", collected_at),
                data.get("duration_seconds", 0),
                data.get("success_count", 0),
                data.get("failed_count", 0),
                data.get("total_markets", 0),
                self.get_storage_size(),
                self.get_storage_size(),
                1 if self.check_storage_cap() else 0,
                json.dumps(data.get("errors", [])),
                compute_file_hash(),
            ))
            self._conn.commit()
        except Exception as e:
            stats["errors"].append(f"Provenance: {e}")
        
        stats["storage_size_bytes"] = self.get_storage_size()
        stats["storage_cap_bytes"] = MAX_STORAGE_BYTES
        stats["storage_cap_ok"] = self.check_storage_cap()
        stats["retention_hours"] = RETENTION_HOURS
        return stats
    def get_stats(self) -> Dict[str, Any]:
        """Return row counts per table + storage size."""
        if not self._conn:
            raise RuntimeError("Storage not opened.")
        stats = {"storage_size_bytes": self.get_storage_size(),
                 "storage_cap_bytes": MAX_STORAGE_BYTES,
                 "retention_hours": RETENTION_HOURS}
        for table in ['v5_series', 'v5_events', 'v5_markets', 'v5_provenance']:
            try:
                cur = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
                stats[table] = cur.fetchone()[0]
            except Exception as e:
                stats[table] = f"ERROR: {e}"
        return stats


# ═════════════════════════════════════════════════════════════════════════
# Real-Mode Orchestrator
# ═════════════════════════════════════════════════════════════════════════

def run_collection(verify_hash: bool = True) -> Dict[str, Any]:
    """Run a real-mode collection: verify hash, acquire lock, collect, store.
    
    Returns dict with collection + storage stats.
    """
    # 1. Verify file hash
    if verify_hash:
        verify_file_hash(REQUIRED_HASH)
    
    # 2. Set up storage
    storage = RealModeStorage()
    
    # 3. Acquire lock (prevent concurrent runs)
    if not storage.acquire_lock():
        raise RuntimeError(
            f"Another collector is running (lock held: {storage.lock_path})"
        )
    
    try:
        # 4. Open storage (enforces cap, purges expired)
        storage.open()
        
        # 5. Run collection
        collector = KalshiPassiveCollector()
        data = collector.collect_all()
        
        # 6. Store normalized data
        storage_stats = storage.store_collection(data)
        
        # 7. Compose result
        result = {
            "collector_version": "real-v5",
            "collection": {
                "start": data.get("collection_start"),
                "end": data.get("collection_end"),
                "duration_seconds": data.get("duration_seconds"),
                "stations_ok": data.get("success_count"),
                "stations_failed": data.get("failed_count"),
                "total_markets": data.get("total_markets"),
            },
            "storage": storage_stats,
            "file_hash": compute_file_hash(),
            "required_hash": REQUIRED_HASH,
            "hash_match": compute_file_hash() == REQUIRED_HASH,
        }
        return result
    finally:
        storage.close()


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Passive Collector real-v5 — persistent storage"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run in dry-run mode (in-memory, no DB writes)"
    )
    parser.add_argument(
        "--real", action="store_true",
        help="Run in real-mode (persistent storage with cap/expiry)"
    )
    parser.add_argument(
        "--lookback", type=int, default=48,
        help="Lookback window hours (informational)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output result as JSON"
    )
    parser.add_argument(
        "--show-hash", action="store_true",
        help="Print computed file hash and exit"
    )
    parser.add_argument(
        "--set-hash", action="store_true",
        help="Print the hash to embed as REQUIRED_HASH (build-time tool)"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show storage stats and exit"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.show_hash or args.set_hash:
        # Compute hash against current REQUIRED_HASH (guard already present)
        h = compute_file_hash()
        print(f"File hash (guard-excluded): {h}")
        print(f"Suggested REQUIRED_HASH line: REQUIRED_HASH: str = \"{h}\"")
        if args.show_hash:
            return
        return

    if args.stats:
        storage = RealModeStorage()
        storage.open()
        try:
            print(json.dumps(storage.get_stats(), indent=2))
        finally:
            storage.close()
        return

    if args.real:
        # Real mode requires REQUIRED_HASH to be set
        if REQUIRED_HASH == "REALV5_NO_HASH_SET":
            print("ERROR: REQUIRED_HASH not set. Run --set-hash and rebuild.")
            sys.exit(2)
        try:
            result = run_collection(verify_hash=True)
        except SystemExit as e:
            if e.code == 99:
                print("ABORTED: hash mismatch — file drifted from authorized build")
                sys.exit(99)
            raise
        except RuntimeError as e:
            print(f"RUNTIME BLOCKED: {e}")
            sys.exit(3)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            c = result["collection"]
            s = result["storage"]
            print(f"\nREAL-MODE V5 COLLECTION COMPLETE")
            print(f"{'='*60}")
            print(f"Collection: {c['stations_ok']}/{c['stations_ok']+c['stations_failed']} stations, "
                  f"{c['total_markets']} markets, {c['duration_seconds']:.1f}s")
            print(f"Storage:    {s['markets_inserted']} markets, "
                  f"{s['events_inserted']} events, {s['series_inserted']} series")
            print(f"DB size:    {s['storage_size_bytes']/1024/1024:.2f} MB / "
                  f"{s['storage_cap_bytes']/1024/1024/1024:.1f} GB cap")
            print(f"Cap OK:     {s.get('storage_cap_ok', '?')}")
            print(f"Retention:  {s['retention_hours']}h")
            print(f"File hash:  {result['file_hash'][:16]}... "
                  f"(match={result['hash_match']})")
        return result

    # Default: dry-run
    collector = KalshiPassiveCollector()
    result = collector.dry_run(lookback_hours=args.lookback)
    dr = result.get("dry_run", {})
    print(f"\nREAL-V5 DRY RUN")
    print(f"{'='*60}")
    print(f"Stations:     {result['total_stations']} "
          f"({result['success_count']} OK / {result['failed_count']} FAIL)")
    print(f"Success rate: {result['success_rate']:.0%}")
    print(f"Markets:      {result['total_markets']}")
    print(f"Duration:     {result['duration_seconds']:.1f}s")
    print(f"Data size:    {dr.get('data_size_mb', 0):.2f} MB "
          f"(cap {dr.get('disk_cap_ok','?')})")
    print(f"Credential:   FREE (no auth)")
    print(f"DB writes:    0")
    if result.get("errors"):
        print(f"\nERRORS ({len(result['errors'])}):")
        for e in result["errors"][:10]:
            print(f"  - {e}")
    if args.json:
        print(json.dumps({
            "success": result['success_count'],
            "failed": result['failed_count'],
            "rate": result['success_rate'],
            "markets": result['total_markets'],
            "duration_s": round(result['duration_seconds'], 1),
            "size_mb": dr.get('data_size_mb', 0),
        }, indent=2))
    return result


if __name__ == "__main__":
    main()
