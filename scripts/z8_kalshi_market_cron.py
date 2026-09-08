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
import logging
import os
import sqlite3
import sys
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="[Z.8] %(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
LOGGER = logging.getLogger("z8")

# station -> list of possible HIGH series tickers to try
STATION_SERIES: dict[str, list[str]] = {
    "KATL": ["KXHIGHATL", "KXHIGHTATL"],
    "KAUS": ["KXHIGHAUS"],
    "KBOS": ["KXHIGHTBOS", "KXHIGHBOS"],
    "KDCA": ["KXHIGHDCA", "KXHIGHTDC"],
    "KDEN": ["KXHIGHDEN"],
    "KDFW": ["KXHIGHTDAL", "KXHIGHDFW"],
    "KHOU": ["KXHIGHTHOU", "KXHIGHHOU"],
    "KLAS": ["KXHIGHTLV", "KXHIGHLAS"],
    "KLAX": ["KXHIGHLAX"],
    "KMDW": ["KXHIGHMDW"],
    "KMIA": ["KXHIGHMIA"],
    "KMSP": ["KXHIGHTMIN", "KXHIGHMSP"],
    "KMSY": ["KXHIGHTNOLA", "KXHIGHMSY"],
    "KNYC": ["KXHIGHNY0", "KXHIGHNY"],
    "KOKC": ["KXHIGHOKC", "KXHIGHTOKC"],
    "KPHX": ["KXHIGHTPHX", "KXHIGHPHX"],
    "KSEA": ["KXHIGHTSEA", "KXHIGHSEA"],
    "KSFO": ["KXHIGHTSFO", "KXHIGHSFO"],
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "kalshi_market_snapshots.db")

_LAST_REQUEST = 0.0


def _kalshi_get(path: str) -> dict[str, Any]:
    import requests
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
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)


def main() -> int:
    t0 = time.time()
    snap_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        _ensure_schema(conn)
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
                    conn.execute("""
                        INSERT INTO market_snapshots
                            (snapshot_time, station, series_ticker, market_ticker,
                             strike_temp, market_type, yes_bid, yes_ask, mid_price,
                             volume_24h, open_interest, ltv)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (snap_ts, station, series_ticker, ticker,
                          strike, mtype, bid, ask, mid, vol, oi, ltv))
                    inserted += 1
                total += inserted
                ok += 1
                LOGGER.info("station=%s ticker=%s markets=%d", station, series_ticker, inserted)
            except Exception as e:
                fail += 1
                LOGGER.warning("station=%s FAIL: %s", station, e)
        conn.commit()
        LOGGER.info("DONE stations_ok=%d fail=%d total_markets=%d elapsed=%.1fs",
                    ok, fail, total, time.time() - t0)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    sys.exit(main())