#!/usr/bin/env python3
"""
kalshi_settlement_upkeep.py — Daily upkeep: fetch finalized Kalshi settlements
and update the kalshi_settlements.db SQLite database.

Cron schedule: 08:00 UTC daily.

What it does:
  1. Queries Kalshi API for finalized events (last 7 days)
  2. Inserts new finalized records into kalshi_settlements.db
  3. Marks existing historical_api records as finalized when they become
     available from the API
  4. Logs changes to stdout for cron mail capture

Usage:
    python3 scripts/kalshi_settlement_upkeep.py
    python3 scripts/kalshi_settlement_upkeep.py --days-back 14
    python3 scripts/kalshi_settlement_upkeep.py --dry-run

B-Mode compliant. No AI/ML.
"""

import json
import logging
import os
import sqlite3
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

# ── Paths ──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")
DB_PATH = os.path.join(_DATA_DIR, "kalshi_settlements.db")

# ── Kalshi API ──
_KALSHI_BASE = (
    os.environ.get("KALSHI_PUBLIC_BASE_URL")
    or "https://api.elections.kalshi.com/trade-api/v2"
).rstrip("/")

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "WeatherEngineSettlementUpkeep/1.0",
    "Accept": "application/json",
})

# ── Rate Limiting ──
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_REQUEST_TIME: float = 0.0
_MIN_INTERVAL = 0.55  # ~1.8 req/sec — safe for Kalshi public tier

# ── Station → Series mapping ──
STATION_TO_KALSHI_CODE = {
    "KATL": "TATL", "KAUS": "AUS", "KBOS": "TBOS", "KDCA": "TDC",
    "KDEN": "DEN", "KDFW": "TDAL", "KHOU": "THOU", "KLAS": "TLV",
    "KLAX": "LAX", "KMDW": "CHI", "KMIA": "MIA", "KMSP": "TMIN",
    "KMSY": "TNOLA", "KNYC": "NY", "KOKC": "TOKC", "KPHL": "PHIL",
    "KPHX": "TPHX", "KSAT": "TSATX", "KSEA": "TSEA", "KSFO": "TSFO",
}

STATION_ALIASES = {"KORD": "KMDW", "KJFK": "KNYC", "KLGA": "KNYC", "KDAL": "KDFW"}

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("kalshi_settlement_upkeep")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def resolve_station(station: str) -> str:
    s = station.strip().upper()
    if s in STATION_TO_KALSHI_CODE:
        return s
    return STATION_ALIASES.get(s, s)


def series_ticker(station: str, market_type: str = "HIGH") -> Optional[str]:
    st = resolve_station(station)
    code = STATION_TO_KALSHI_CODE.get(st)
    if code is None:
        return None
    prefix = "KXHIGH" if market_type.upper() == "HIGH" else "KXLOW"
    return f"{prefix}{code}"


def event_ticker(station: str, date_obj: datetime, market_type: str = "HIGH") -> Optional[str]:
    series = series_ticker(station, market_type)
    if series is None:
        return None
    date_token = date_obj.strftime("%y%b%d").upper()
    return f"{series}-{date_token}"


def _rate_limited_get(url: str, timeout: int = 15, max_retries: int = 3) -> Optional[dict]:
    global _LAST_REQUEST_TIME
    for attempt in range(1, max_retries + 1):
        with _RATE_LIMIT_LOCK:
            now = time.time()
            elapsed = now - _LAST_REQUEST_TIME
            if elapsed < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - elapsed)
            _LAST_REQUEST_TIME = time.time()
        try:
            resp = _SESSION.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            _LOGGER.warning("timeout url=%s attempt=%d/%d", url, attempt, max_retries)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as e:
            _LOGGER.warning("connection_error url=%s err=%s", url, e)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue
        except Exception as e:
            _LOGGER.error("request_failed url=%s err=%s", url, e)
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError) as e:
                _LOGGER.warning("decode_error url=%s err=%s", url, e)
                return None
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", str(2 ** attempt)))
            _LOGGER.info("429 url=%s attempt=%d/%d retry_after=%ds", url, attempt, max_retries, retry_after)
            if attempt < max_retries:
                time.sleep(retry_after)
            continue
        _LOGGER.warning("http_%d url=%s attempt=%d/%d", resp.status_code, url, attempt, max_retries)
        return None
    _LOGGER.error("exhausted_retries url=%s", url)
    return None


def fetch_expiration_value(event_tkr: str) -> Optional[float]:
    """Fetch expiration_value (ground truth temp) for a single event ticker."""
    url = f"{_KALSHI_BASE}/markets?event_ticker={event_tkr}&limit=50"
    data = _rate_limited_get(url)
    if data is None:
        return None
    markets = data.get("markets") or []
    for m in markets:
        raw = m.get("expiration_value")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return None


def fetch_events_for_series(series_tkr: str, status: str = "closed") -> List[dict]:
    """Fetch all events for a series with a given status."""
    events = []
    cursor = None
    while True:
        url = f"{_KALSHI_BASE}/events?series_ticker={series_tkr}&status={status}&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        data = _rate_limited_get(url)
        if data is None:
            break
        events.extend(data.get("events", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


# ═══════════════════════════════════════════════════════════════════════════
# Main upkeep logic
# ═══════════════════════════════════════════════════════════════════════════

def run_upkeep(days_back: int = 7, dry_run: bool = False) -> Dict[str, int]:
    """
    Run the daily upkeep cycle.

    Returns a dict of counters: inserted, upgraded, skipped, errors.
    """
    stats = {"inserted": 0, "upgraded": 0, "skipped": 0, "errors": 0, "api_calls": 0}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Ensure table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kalshi_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            series TEXT NOT NULL,
            event_ticker TEXT NOT NULL UNIQUE,
            target_date TEXT NOT NULL,
            kalshi_temp REAL,
            markets_found INTEGER,
            finalized_count INTEGER,
            source TEXT,
            source_type TEXT NOT NULL DEFAULT 'historical_api',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # For each station, check recent dates
    now = datetime.now(timezone.utc)
    stations = sorted(STATION_TO_KALSHI_CODE.keys())

    _LOGGER.info("upkeep_start stations=%d days_back=%d dry_run=%s", len(stations), days_back, dry_run)

    for station in stations:
        for day_offset in range(days_back):
            date_dt = now - timedelta(days=day_offset)
            date_str = date_dt.strftime("%Y-%m-%d")

            e_tkr = event_ticker(station, date_dt, "HIGH")
            if e_tkr is None:
                continue

            # Check if we already have this as finalized
            cur.execute(
                "SELECT id, source_type, kalshi_temp FROM kalshi_settlements WHERE event_ticker = ?",
                (e_tkr,),
            )
            row = cur.fetchone()

            if row:
                # Already in DB
                if row["source_type"] == "finalized" and row["kalshi_temp"] is not None:
                    stats["skipped"] += 1
                    continue

                # Try to fetch finalized value from API
                temp = fetch_expiration_value(e_tkr)
                stats["api_calls"] += 1

                if temp is not None:
                    if dry_run:
                        _LOGGER.info("DRY-RUN: would upgrade station=%s date=%s ticker=%s temp=%s",
                                    station, date_str, e_tkr, temp)
                        stats["upgraded"] += 1
                    else:
                        cur.execute(
                            "UPDATE kalshi_settlements SET source_type = 'finalized', kalshi_temp = ?, "
                            "source = 'live_api', updated_at = datetime('now') WHERE event_ticker = ?",
                            (temp, e_tkr),
                        )
                        stats["upgraded"] += 1
                        _LOGGER.info("upgraded station=%s date=%s ticker=%s temp=%s",
                                    station, date_str, e_tkr, temp)
                else:
                    _LOGGER.debug("not_finalized station=%s date=%s ticker=%s", station, date_str, e_tkr)
            else:
                # Not in DB at all — fetch from API
                temp = fetch_expiration_value(e_tkr)
                stats["api_calls"] += 1

                if temp is not None:
                    if dry_run:
                        _LOGGER.info("DRY-RUN: would insert station=%s date=%s ticker=%s temp=%s",
                                    station, date_str, e_tkr, temp)
                        stats["inserted"] += 1
                    else:
                        series = series_ticker(station, "HIGH")
                        cur.execute("""
                            INSERT OR IGNORE INTO kalshi_settlements
                                (station, series, event_ticker, target_date, kalshi_temp,
                                 markets_found, finalized_count, source, source_type)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (station, series, e_tkr, date_str, temp, 6, 6, "live_api", "finalized"))
                        if cur.rowcount > 0:
                            stats["inserted"] += 1
                            _LOGGER.info("inserted station=%s date=%s ticker=%s temp=%s",
                                        station, date_str, e_tkr, temp)
                else:
                    _LOGGER.debug("not_found station=%s date=%s ticker=%s", station, date_str, e_tkr)

            # Brief pause between events to avoid hammering
            time.sleep(0.1)

    if not dry_run:
        conn.commit()
    conn.close()

    _LOGGER.info(
        "upkeep_done inserted=%d upgraded=%d skipped=%d errors=%d api_calls=%d",
        stats["inserted"], stats["upgraded"], stats["skipped"], stats["errors"], stats["api_calls"],
    )
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi settlement daily upkeep")
    parser.add_argument("--days-back", type=int, default=7, help="Days to look back (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying DB")
    args = parser.parse_args()

    stats = run_upkeep(days_back=args.days_back, dry_run=args.dry_run)
    print(f"\nSummary: {stats['inserted']} inserted, {stats['upgraded']} upgraded, "
          f"{stats['skipped']} skipped, {stats['errors']} errors, {stats['api_calls']} API calls")