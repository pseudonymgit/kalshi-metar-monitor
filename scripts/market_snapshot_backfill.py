#!/usr/bin/env python3
"""
market_snapshot_backfill.py — E2 historical Kalshi price backfill (2026-10-05)
=============================================================================
Fills the gap between `kalshi_market_snapshots.db` coverage (~29 days) and the
210-day window the B2 real-price sweep needs.

Source of record: the Kalshi public API `candlesticks` endpoint
(`/series/{series}/markets/{ticker}/candlesticks`), which returns per-interval
OHLC + `yes_bid`/`yes_ask` even for settled/historical markets. Verified
2026-10-05 for tickers back to 2026-08-04.

Reconstruction: each candle's `end_period_ts` becomes `snapshot_time`; the
candle's `yes_bid.close_dollars` / `yes_ask.close_dollars` are stored as
`yes_bid` / `yes_ask`; `mid_price = (bid+ask)/2` (matches the live z8 collector's
definition at `scripts/z8_kalshi_market_cron.py`).

PROVENANCE: backfilled rows are tagged in `ltv` with `E2BACKFILL` so they are
distinguishable from live z8 snapshots. (The live schema has no provenance
column; adding one requires touching the live collector — deferred.)

Idempotent: deletes rows for the exact (market_ticker, snapshot_time) being
written before insert, so re-runs do not duplicate.

Usage:
  python3 scripts/market_snapshot_backfill.py --start 2026-09-01 --end 2026-09-05 \
      --stations KOKC,KATL --interval 60
  python3 scripts/market_snapshot_backfill.py --start 2026-03-09 --end 2026-10-05

Deterministic, no AI/ML. Read-only against Kalshi; additive writes to the
snapshot DB.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.station_registry import get_all_stations, get_station_series  # noqa: E402

DB_PATH = os.path.join(REPO, "data", "kalshi_market_snapshots.db")
BASE = "https://api.elections.kalshi.com/trade-api/v2"
_SESSION = None
_LAST_REQ = 0.0


def _get(path: str, params: Optional[dict] = None) -> dict:
    """Rate-limited GET against the Kalshi trade API (fail loud)."""
    global _SESSION, _LAST_REQ
    import requests
    if _SESSION is None:
        _SESSION = requests.Session()
    elapsed = time.time() - _LAST_REQ
    if elapsed < 0.35:
        time.sleep(0.35 - elapsed)
    _LAST_REQ = time.time()
    r = _SESSION.get(f"{BASE}{path}", params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp())


def _f(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


__all__ = ["enumerate_markets", "candles_for", "main"]


def enumerate_markets(series: str, start_iso: str, end_iso: str,
                      historical: bool = False, max_pages: int = 60) -> List[dict]:
    """All KXHIGH/LOW markets for `series` closing within [start, end].

    NOTE: /historical/markets IGNORES min_close_ts/max_close_ts (verified
    2026-10-05: a 3-day window returned 1050 markets spanning months). So the
    window filter is applied CLIENT-SIDE on `close_time` for both endpoints.
    """
    out: List[dict] = []
    cursor = None
    base = "/historical/markets" if historical else "/markets"
    lo, hi = start_iso[:10], end_iso[:10]
    pages = 0
    while pages < max_pages:
        pages += 1
        params = {"series_ticker": series, "limit": 200,
                  "min_close_ts": _epoch(start_iso), "max_close_ts": _epoch(end_iso)}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get(base, params)
        except Exception as e:
            print(f"    enumerate fail ({base}) series={series}: {e}")
            break
        ms = data.get("markets") or []
        for m in ms:
            ct = (m.get("close_time") or "")[:10]
            if ct and lo <= ct <= hi:
                out.append(m)
        cursor = data.get("cursor")
        if not cursor or not ms:
            break
    return out


def candles_for(series: str, ticker: str, start_iso: str, end_iso: str,
                interval: int) -> List[dict]:
    try:
        data = _get(f"/series/{series}/markets/{ticker}/candlesticks",
                    {"start_ts": _epoch(start_iso), "end_ts": _epoch(end_iso),
                     "period_interval": interval})
    except Exception as e:
        print(f"    candles fail {ticker}: {e}")
        return []
    return data.get("candlesticks") or []


def _candle_row(station: str, series: str, ticker: str, c: dict) -> Optional[tuple]:
    ts = c.get("end_period_ts")
    if ts is None:
        return None
    snap_iso = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
    try:
        bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
        ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
    except AttributeError:
        return None
    if bid is None and ask is None:
        # price.close as a last resort (mid only)
        px = _f((c.get("price") or {}).get("close_dollars"))
        if px is None:
            return None
        bid = ask = px
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else (bid or ask)
    vol = _f(c.get("volume_fp"))
    oi = _f(c.get("open_interest_fp"))
    return (snap_iso, station, series, ticker, None, "binary",
            bid, ask, mid, vol, oi, "E2BACKFILL")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (close window start)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (close window end)")
    ap.add_argument("--stations", default="", help="comma list (default: all)")
    ap.add_argument("--interval", type=int, default=60,
                    help="candle period minutes (60 hourly, 1440 daily)")
    ap.add_argument("--include-historical", action="store_true",
                    help="also query /historical/markets (older than ~3 months)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start_iso, end_iso = f"{args.start}T00:00:00", f"{args.end}T23:59:59"
    series_map = get_station_series()
    stations = ([s.strip() for s in args.stations.split(",") if s.strip()]
                or get_all_stations())

    conn = None if args.dry_run else sqlite3.connect(DB_PATH, timeout=30)
    tot_markets = tot_rows = 0
    for st in stations:
        for series in series_map.get(st, []):
            if not series.startswith("KXHIGH"):
                continue  # HIGH only for the current window (bucket/LOW parked)
            markets = enumerate_markets(series, start_iso, end_iso)
            if not markets and args.include_historical:
                markets = enumerate_markets(series, start_iso, end_iso, historical=True)
            n_rows = 0
            for m in markets:
                tk = m.get("ticker")
                if not tk:
                    continue
                cs = candles_for(series, tk, start_iso, end_iso, args.interval)
                for c in cs:
                    row = _candle_row(st, series, tk, c)
                    if row is None or row[8] is None:
                        continue
                    if conn is not None:
                        conn.execute(
                            "DELETE FROM market_snapshots WHERE market_ticker=? "
                            "AND snapshot_time=?", (tk, row[0]))
                        conn.execute(
                            "INSERT INTO market_snapshots (snapshot_time, station, "
                            "series_ticker, market_ticker, strike_temp, market_type, "
                            "yes_bid, yes_ask, mid_price, volume_24h, open_interest, ltv) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                    n_rows += 1
            if conn is not None:
                conn.commit()
            tot_markets += len(markets)
            tot_rows += n_rows
            print(f"  {st} {series}: markets={len(markets)} candle_rows={n_rows}")

    if conn is not None:
        conn.close()
    print(f"BACKFILL DONE: stations={len(stations)} markets={tot_markets} "
          f"rows={tot_rows} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
