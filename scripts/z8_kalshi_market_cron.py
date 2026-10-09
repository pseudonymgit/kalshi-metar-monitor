#!/usr/bin/env python3
"""
Z.8 — Kalshi market snapshot collector.

Captures bid/ask/mid/volume/OI for all Kalshi weather markets
at each station, stores in data/kalshi_market_snapshots.db.

Usage:
    python3 scripts/z8_kalshi_market_cron.py              # run once
    python3 scripts/z8_kalshi_market_cron.py --once        # same
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from typing import Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.station_registry import get_all_stations, get_station_series

logging.basicConfig(
    level=logging.INFO,
    format="[Z.8] %(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
LOGGER = logging.getLogger("z8")

# station -> list of possible HIGH series tickers to try, derived at import time
# from the canonical registry + data/kalshi_series_map.json (cross-checked
# against data/kalshi_official_mapping.json). HIGH series first; legacy aliases
# ordered after as fallbacks. Drift (registry/map disagreement) raises loudly.
STATION_SERIES: dict[str, list[str]] = get_station_series()

EXPECTED_STATIONS = get_all_stations()
if sorted(STATION_SERIES) != EXPECTED_STATIONS:
    raise RuntimeError(
        "Z8 SERIES DRIFT: derived STATION_SERIES != canonical registry. "
        f"derived={sorted(STATION_SERIES)} canonical={EXPECTED_STATIONS}"
    )

DB_PATH = os.path.join(PROJECT_ROOT, "data", "kalshi_market_snapshots.db")

_LAST_REQUEST = 0.0


def _kalshi_get(path: str) -> dict[str, Any]:
    try:
        import requests
    except ImportError as e:
        # Fail LOUD: a missing HTTP dep must never look like "no active markets".
        raise RuntimeError(
            "z8: the `requests` module is required but not importable in this "
            f"interpreter ({sys.executable}). Install it (e.g. "
            "`python3 -m pip install --user --break-system-packages requests`). "
            "Refusing to run and report a false-green empty snapshot."
        ) from e
    global _LAST_REQUEST
    elapsed = time.time() - _LAST_REQUEST
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _LAST_REQUEST = time.time()
    BASE = "https://api.elections.kalshi.com/trade-api/v2"
    resp = requests.get(f"{BASE}{path}", timeout=30)
    if resp.status_code == 429:
        LOGGER.warning("429, retrying after 5s")
        time.sleep(5)
        resp = requests.get(f"{BASE}{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


ROW_HASH_ALGORITHM = "sha256"
LEGACY_ROW_HASH = "legacy-unpinned"


def compute_row_hash(
    snapshot_time: str,
    station: str,
    series_ticker: str,
    market_ticker: str,
    strike_temp: Optional[float],
    market_type: Optional[str],
    yes_bid: Optional[float],
    yes_ask: Optional[float],
    mid_price: Optional[float],
    volume_24h: Optional[float],
    open_interest: Optional[float],
    ltv: Optional[float],
) -> str:
    """Deterministic checksum over a snapshot row's payload (forward-only provenance).

    Pins each ingested row to its exact field values so a later rewrite/splice
    of the stored market data is detectable. Canonical JSON (sorted keys, tight
    separators) keeps the digest stable across Python versions.
    """
    payload = {
        "snapshot_time": snapshot_time,
        "station": station,
        "series_ticker": series_ticker,
        "market_ticker": market_ticker,
        "strike_temp": strike_temp,
        "market_type": market_type,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "mid_price": mid_price,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
        "ltv": ltv,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            station TEXT NOT NULL,
            series_ticker TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            strike_temp REAL,
            market_type TEXT,
            yes_bid REAL,
            yes_ask REAL,
            mid_price REAL,
            volume_24h REAL,
            open_interest REAL,
            ltv REAL,
            created_at TEXT DEFAULT (datetime('now')),
            row_hash TEXT
        )
    """)
    # Forward-only provenance: add the checksum column to pre-existing DBs.
    # ADD COLUMN with a constant DEFAULT is metadata-only in SQLite — the value
    # is recorded in the schema and read back for old rows, so there is NO
    # full-table rewrite. Existing rows therefore surface as `legacy-unpinned`
    # (never silently presented as if they had been pinned).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    if "row_hash" not in cols:
        conn.execute(
            "ALTER TABLE market_snapshots "
            f"ADD COLUMN row_hash TEXT DEFAULT '{LEGACY_ROW_HASH}'"
        )
    # Append-only ledger of collector runs, so a gap or partial ingest is
    # attributable rather than inferred from the snapshot rows alone.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_ingest_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            stations_ok INTEGER,
            stations_fail INTEGER,
            rows_inserted INTEGER,
            row_hash_algorithm TEXT,
            note TEXT
        )
    """)


def main() -> int:
    t0 = time.time()
    snap_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        _ensure_schema(conn)
        run_cur = conn.execute(
            "INSERT INTO snapshot_ingest_runs "
            "(started_at, row_hash_algorithm) VALUES (?, ?)",
            (snap_ts, ROW_HASH_ALGORITHM),
        )
        run_id = run_cur.lastrowid
        total, ok, fail = 0, 0, 0
        for station, tickers in sorted(STATION_SERIES.items()):
            found = False
            series_ticker = tickers[0]  # default to first
            for t in tickers:
                try:
                    data = _kalshi_get(f"/markets?series_ticker={t}&limit=200")
                    markets = data.get("markets") or []
                    if markets:
                        found = True
                        series_ticker = t
                        break
                except Exception:
                    continue
            if not found:
                LOGGER.info("station=%s no active markets", station)
                continue
            try:
                data = _kalshi_get(f"/markets?series_ticker={series_ticker}&limit=200")
                markets = data.get("markets") or []
                inserted = 0
                for m in markets:
                    ticker = m.get("ticker", "")
                    strike = m.get("strike_price") or m.get("strike")
                    bid = (m.get("yes_bid_dollars") or m.get("yes_bid"))
                    ask = (m.get("yes_ask_dollars") or m.get("yes_ask"))
                    if bid is not None:
                        bid = float(bid)
                    if ask is not None:
                        ask = float(ask)
                    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
                    vol = (m.get("volume_24h_fp") or m.get("volume_24h"))
                    oi = (m.get("open_interest_fp") or m.get("open_interest"))
                    if vol is not None:
                        vol = float(vol)
                    if oi is not None:
                        oi = float(oi)
                    ltv = m.get("last_traded_value")
                    if ltv is not None:
                        ltv = float(ltv)
                    mtype = m.get("type") or m.get("market_type")
                    if strike is not None:
                        strike = float(strike)
                    if not ticker:
                        continue
                    row_hash = compute_row_hash(
                        snap_ts, station, series_ticker, ticker,
                        strike, mtype, bid, ask, mid, vol, oi, ltv,
                    )
                    conn.execute("""
                        INSERT INTO market_snapshots
                            (snapshot_time, station, series_ticker, market_ticker,
                             strike_temp, market_type, yes_bid, yes_ask, mid_price,
                             volume_24h, open_interest, ltv, row_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (snap_ts, station, series_ticker, ticker,
                          strike, mtype, bid, ask, mid, vol, oi, ltv, row_hash))
                    inserted += 1
                total += inserted
                ok += 1
                LOGGER.info("station=%s ticker=%s markets=%d", station, series_ticker, inserted)
            except Exception as e:
                fail += 1
                LOGGER.warning("station=%s FAIL: %s", station, e)
        # Close out the ingest-run ledger row before the single commit so the
        # run's outcome is attributable even if the process dies afterwards.
        conn.execute(
            "UPDATE snapshot_ingest_runs SET finished_at = ?, stations_ok = ?, "
            "stations_fail = ?, rows_inserted = ? WHERE run_id = ?",
            (datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
             ok, fail, total, run_id),
        )
        conn.commit()
        LOGGER.info("DONE stations_ok=%d fail=%d total_markets=%d elapsed=%.1fs",
                    ok, fail, total, time.time() - t0)
        # Fail LOUD on a total wipe-out: zero stations with zero markets means the
        # collector did not actually collect (e.g. a missing dep swallowed per
        # ticker). Report non-zero so the cron cannot go false-green.
        if ok == 0:
            LOGGER.error(
                "Z8 ZERO STATIONS COLLECTED — refusing to report success "
                "(ok=0, fail=%d). Check dependency/API health.", fail)
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    sys.exit(main())