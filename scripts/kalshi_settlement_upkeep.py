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

# Keep-fresh: the frozen canonical snapshot lives in metar_backfill.db (the
# single home for settlement truth) and is read at settle time by
# core.pnl_tracking.load_canonical_snapshots.
METAR_SNAPSHOT_DB = os.path.join(_DATA_DIR, "metar_backfill.db")
GHCN_SNAPSHOT_DB = os.path.join(_DATA_DIR, "ghcn_settlements.db")
_SNAPSHOT_MARKET_TYPE = "HIGH"

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.canonical_settlement import (  # noqa: E402
    CANONICAL_TEMP_SOURCE,
    ImmutableSettlementError,
    assert_canonical_immutable,
    ensure_snapshot_table,
    freeze_snapshot,
    read_snapshot,
)

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


def fetch_expiration_value_with_anchor(event_tkr: str) -> Optional[Tuple[float, Optional[str]]]:
    """Fetch (expiration_value, close_time) for one event ticker.

    `close_time` is the market finalization timestamp — used as the snapshot
    `settled_at_utc` as-of anchor so a frozen value is pinned to the moment the
    market finalizes (Kalshi HIGH markets finalize T+1 ~05:00Z).
    """
    url = f"{_KALSHI_BASE}/markets?event_ticker={event_tkr}&limit=50"
    data = _rate_limited_get(url)
    if data is None:
        return None
    for m in data.get("markets") or []:
        raw = m.get("expiration_value")
        if raw is None:
            continue
        try:
            temp = float(raw)
        except (TypeError, ValueError):
            continue
        anchor = m.get("close_time") or m.get("expiration_time")
        return temp, anchor
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
# Keep-fresh: append the finalized canonical settlement to the frozen snapshot
# ═══════════════════════════════════════════════════════════════════════════

FallbackValue = "Callable[[str, str], Optional[float]]"

# A frozen row written from one of these sources IS canonical truth, so a
# disagreement with a fresh canonical fetch is a real move -> fail loud. Rows
# from any other source (the backfill's weatherapi/GHCN last-resort proxy) were
# never canonical; a divergence there is surfaced loudly but does not wedge the
# forward path (it cannot "move" a value that was never canonical).
_CANONICAL_FROZEN_SOURCES = frozenset({CANONICAL_TEMP_SOURCE, "kalshi_finalized"})


def _snapshot_fallback_value(station: str, date_str: str) -> Optional[float]:
    """Cross-check / secondary source for a *past* station-day the live Kalshi
    API has not reported yet — same order as the backfill:
    kalshi_settlements(finalized) -> GHCN.
    These are canonical-equivalent cross-checks (both encode the official daily
    high). weatherapi is deliberately NOT used: it is a non-canonical blend and
    seeding it forward would pin a settlement the ruling says must come from the
    Kalshi API / GHCN. A day with no canonical-equivalent value stays unfrozen
    (the settle writer fails closed and the trade stays open) rather than being
    settled on a proxy.
    """
    for path, query in (
        (DB_PATH, "SELECT kalshi_temp FROM kalshi_settlements WHERE station=? "
                   "AND target_date=? AND source_type='finalized' "
                   "AND kalshi_temp IS NOT NULL"),
        (GHCN_SNAPSHOT_DB, "SELECT high_f FROM ghcn_settlements WHERE station=? "
                           "AND date=? AND high_f IS NOT NULL"),
    ):
        if not os.path.exists(path):
            continue
        try:
            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = ro.execute(query, (station, date_str)).fetchone()
            finally:
                ro.close()
        except sqlite3.Error:
            continue
        if row and row[0] is not None:
            val = float(row[0])
            # Kalshi settles HIGH markets on whole °F; GHCN carries a decimal.
            return round(val) if path == GHCN_SNAPSHOT_DB else val
    return None


def freeze_canonical_snapshots(*, days_back: int = 7, dry_run: bool = False,
                               snapshot_db: Optional[str] = None,
                               stations: Optional[List[str]] = None,
                               now: Optional[datetime] = None,
                               live_fetcher=None,
                               fallback_value=None) -> Dict[str, int]:
    """Keep-fresh: append the finalized canonical settlement for each settled
    station-day to the frozen `settlement_snapshot` (write-once).

    Source priority (canonical first): Kalshi live API `expiration_value`;
    where the live market has not reported for a *past* settled day, fall back to
    kalshi_settlements(finalized) -> GHCN (canonical-equivalent cross-checks).
    The current UTC day is never frozen: a HIGH market settles T+1 ~05:00Z, so
    today has no settlement yet and must not be pinned from a proxy.

    Immutability: an existing frozen row is never changed. A fetched canonical
    value that differs from a *canonically sourced* frozen row FAILS LOUD
    (`assert_canonical_immutable` -> `ImmutableSettlementError`) after scanning
    the whole window — it does NOT overwrite. A frozen row seeded from a
    non-canonical last-resort proxy (weatherapi/GHCN) that diverges from
    canonical truth is reported loudly (`snapshot_provenance_drift`) without
    wedging the forward path; it is never overwritten either.

    HIGH-only while no LOW markets exist: the fallback chain has no LOW source,
    so a LOW station-day simply never freezes (the settle writer already fails
    closed on a missing row).
    """
    live_fetcher = live_fetcher or fetch_expiration_value_with_anchor
    fallback_value = fallback_value or _snapshot_fallback_value
    snapshot_db = snapshot_db or METAR_SNAPSHOT_DB
    stations = sorted(stations or STATION_TO_KALSHI_CODE.keys())
    now = now or datetime.now(timezone.utc)
    frozen_at_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    stats = {"snapshot_inserted": 0, "snapshot_identical": 0,
             "snapshot_conflicts": 0, "snapshot_provenance_drift": 0,
             "snapshot_missing": 0, "snapshot_api_calls": 0}
    conflicts: List[str] = []

    conn = sqlite3.connect(snapshot_db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        ensure_snapshot_table(conn)
        for station in stations:
            for day_offset in range(days_back):
                date_str = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
                key = (station, _SNAPSHOT_MARKET_TYPE, date_str)
                frozen = read_snapshot(conn, *key)

                if frozen is not None:
                    # Never re-derive an existing row's value from a weaker source.
                    # Compare against canonical live truth only; if the live market
                    # is not finalized yet there is nothing new to compare.
                    e_tkr = event_ticker(station, now - timedelta(days=day_offset),
                                         _SNAPSHOT_MARKET_TYPE)
                    if e_tkr is None:
                        continue
                    fetched = live_fetcher(e_tkr)
                    stats["snapshot_api_calls"] += 1
                    if fetched is None:
                        stats["snapshot_missing"] += 1
                        continue
                    temp = fetched[0] if isinstance(fetched, tuple) else fetched
                    if frozen["temp_source"] in _CANONICAL_FROZEN_SOURCES:
                        try:
                            assert_canonical_immutable(frozen["settlement_temp_f"],
                                                       temp, key)
                            stats["snapshot_identical"] += 1
                        except ImmutableSettlementError as exc:
                            conflicts.append(str(exc))
                            stats["snapshot_conflicts"] += 1
                    elif float(frozen["settlement_temp_f"]) != float(temp):
                        stats["snapshot_provenance_drift"] += 1
                        _LOGGER.warning(
                            "snapshot_provenance_drift station=%s date=%s "
                            "frozen=%s(%s) canonical=%s -- frozen row left "
                            "untouched; proxy seed needs governance disposition",
                            station, date_str, frozen["settlement_temp_f"],
                            frozen["temp_source"], temp)
                    else:
                        stats["snapshot_identical"] += 1
                    time.sleep(0.1)
                    continue

                # No frozen row -> append canonical truth for a newly settled day.
                e_tkr = event_ticker(station, now - timedelta(days=day_offset),
                                     _SNAPSHOT_MARKET_TYPE)
                if e_tkr is None:
                    continue
                fetched = live_fetcher(e_tkr)
                stats["snapshot_api_calls"] += 1
                temp = anchor = None
                if fetched is not None:
                    temp = fetched[0] if isinstance(fetched, tuple) else fetched
                    anchor = fetched[1] if isinstance(fetched, tuple) and len(fetched) > 1 else None
                    source = CANONICAL_TEMP_SOURCE
                else:  # live market not finalized yet -> canonical cross-check
                    if day_offset == 0:
                        # Today's HIGH market has not settled (T+1) -> never freeze.
                        stats["snapshot_missing"] += 1
                        time.sleep(0.1)
                        continue
                    temp = fallback_value(station, date_str)
                    source = "fallback_cross_check"
                if temp is None:
                    stats["snapshot_missing"] += 1
                    time.sleep(0.1)
                    continue
                if dry_run:
                    _LOGGER.info("DRY-RUN: would freeze station=%s date=%s ticker=%s "
                                 "temp=%s source=%s", station, date_str, e_tkr, temp, source)
                    stats["snapshot_inserted"] += 1
                else:
                    inserted, _skipped, _conf = freeze_snapshot(
                        conn, [{"station": station,
                                "market_type": _SNAPSHOT_MARKET_TYPE,
                                "local_trading_date": date_str,
                                "settlement_temp_f": temp,
                                "temp_source": source,
                                "settled_at_utc": anchor}],
                        frozen_at_utc=frozen_at_utc)
                    if inserted:
                        stats["snapshot_inserted"] += 1
                        _LOGGER.info("snapshot_frozen station=%s date=%s ticker=%s "
                                     "temp=%s source=%s", station, date_str, e_tkr,
                                     temp, source)
                time.sleep(0.1)
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    _LOGGER.info("keep_fresh_done inserted=%d identical=%d conflicts=%d "
                 "provenance_drift=%d missing=%d api_calls=%d",
                 stats["snapshot_inserted"], stats["snapshot_identical"],
                 stats["snapshot_conflicts"], stats["snapshot_provenance_drift"],
                 stats["snapshot_missing"], stats["snapshot_api_calls"])
    if conflicts:
        raise ImmutableSettlementError(
            "frozen settlement value(s) would move — failing loud, no overwrite: "
            + "; ".join(conflicts))
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# Main upkeep logic
# ═══════════════════════════════════════════════════════════════════════════

def run_upkeep(days_back: int = 7, dry_run: bool = False,
               freeze_snapshots: bool = True) -> Dict[str, int]:
    """
    Run the daily upkeep cycle.

    Returns a dict of counters: inserted, upgraded, skipped, errors, and (when
    ``freeze_snapshots``) the keep-fresh snapshot counters.
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

    # Keep-fresh: append finalized canonical settlement to the frozen snapshot so
    # the forward settle path has canonical truth for newly settled days.
    if freeze_snapshots:
        stats.update(freeze_canonical_snapshots(days_back=days_back, dry_run=dry_run))

    return stats


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi settlement daily upkeep")
    parser.add_argument("--days-back", type=int, default=7, help="Days to look back (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying DB")
    parser.add_argument("--no-freeze-snapshots", action="store_true",
                        help="Skip the keep-fresh step that appends to settlement_snapshot")
    args = parser.parse_args()

    try:
        stats = run_upkeep(days_back=args.days_back, dry_run=args.dry_run,
                           freeze_snapshots=not args.no_freeze_snapshots)
    except ImmutableSettlementError as exc:
        # Fail loud (repo convention: immutability violations exit 3). The
        # conflict was surfaced; no frozen row was overwritten.
        print(f"\nIMMUTABILITY VIOLATION — no overwrite performed:\n{exc}", file=sys.stderr)
        sys.exit(3)
    print(f"\nSummary: {stats['inserted']} inserted, {stats['upgraded']} upgraded, "
          f"{stats['skipped']} skipped, {stats['errors']} errors, {stats['api_calls']} API calls")
    if 'snapshot_inserted' in stats:
        print(f"Snapshot keep-fresh: {stats['snapshot_inserted']} frozen, "
              f"{stats['snapshot_identical']} identical, {stats['snapshot_conflicts']} conflicts, "
              f"{stats['snapshot_provenance_drift']} provenance-drift, "
              f"{stats['snapshot_missing']} not-yet-finalized")