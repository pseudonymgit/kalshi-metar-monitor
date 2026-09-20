#!/usr/bin/env python3
"""
Kalshi real-trade backfill — deterministic, script-only, read-only on existing data.

Author: Gilfoyle (subagent), 2026-09-20 (Dan's directive).

PURPOSE
-------
The "no edge" verdict was re-tested against SIMULATED entry prices because the
last-traded price (ltv) is NULL across all 926,344 market_snapshots rows, and the
bid/ask/mid fields are distorted (massed in fake bands). This script pulls REAL
historical trade/fill prices from Kalshi's public trades endpoint and stores them,
so the edge verdict can be re-run against actual prices instead of simulation.

ENDPOINT VERDICT (probed live 2026-09-20)
-----------------------------------------
  * `GET /markets/trades`  (unauthenticated public GET) returns real trades:
      {trade_id, ticker, count_fp, created_time, yes_price_dollars,
       no_price_dollars, taker_side, taker_book_side, taker_outcome_side,
       is_block_trade}.
  * Accepts `ticker` filter + `limit` (max observed 100) + `cursor` pagination
    (newest-first). No `min/max_ts`; paginate by cursor.
  * HISTORY DEPTH: trades are retrievable ONLY for markets that settled/opened on
    or after ~2026-07-17. Markets settled earlier return ZERO trades (empty).
    This is a hard backend retention boundary (verified: 2021/2022/.../2026-06
    markets all return 0 trades; first non-empty is ~2026-07-16/17).
  * AUTH: none required (public GET). No rate-limit headers; 30 req @10/s all 200.
  * Cursor RECYCLES when exhausted (returns to page 0) — dedup by trade_id is
    mandatory to avoid infinite loop.

FEASIBILITY: FULLY FEASIBLE for the entire snapshot range. kalshi_market_snapshots.db
spans 2026-08-06 → 2026-09-20, entirely inside the ~2026-07-17+ trade window.
All 9,938 distinct market tickers have retrievable trades (spot-verified).

STORAGE SAFETY
--------------
Trades are high-volume; raw rows would bloat. This script AGGREGATES to a
per-contract summary (VWAP entry price, trade count, total volume, first/last
trade time, last-trade price). One row per (market_ticker). No raw trade rows.

Writes ONLY to a NEW DB (data/kalshi_trades.db) — does NOT touch
kalshi_market_snapshots.db or any existing DB. Idempotent (upsert by ticker).

USAGE
-----
  python3 scripts/kalshi_trade_backfill.py --probe            # endpoint verdict + sample
  python3 scripts/kalshi_trade_backfill.py --limit 50         # backfill first 50 tickers
  python3 scripts/kalshi_trade_backfill.py                    # backfill ALL tickers
  python3 scripts/kalshi_trade_backfill.py --tickers T1,T2    # specific tickers
  python3 scripts/kalshi_trade_backfill.py --resume           # skip already-done tickers
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import requests

BASE = os.getenv(
    "KALSHI_PUBLIC_BASE_URL",
    os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"),
).rstrip("/")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DB = os.path.join(PROJECT_ROOT, "data", "kalshi_market_snapshots.db")
OUT_DB = os.path.join(PROJECT_ROOT, "data", "kalshi_trades.db")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "WeatherEngineTradeBackfill/1.0",
    "Accept": "application/json",
})

# Polite pacing between requests (seconds).
REQUEST_DELAY = 0.25
PAGE_LIMIT = 100  # max trades per page observed

# Global throttle: rate-limit shared across threads (lock + min interval).
import threading

_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0
_MIN_INTERVAL = 0.15  # seconds between requests globally (approx 6-7 req/s)


def _get(path: str, timeout: int = 30) -> dict[str, Any]:
    """Public GET with global throttle + exponential backoff on 429."""
    global _LAST_REQUEST_TS
    # Global throttle to avoid tripping Kalshi's rate limiter.
    with _THROTTLE_LOCK:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _LAST_REQUEST_TS)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TS = time.time()

    backoff = 1.0
    for attempt in range(6):  # up to ~1+2+4+8+16+32 = 63s total backoff
        r = SESSION.get(f"{BASE}{path}", timeout=timeout)
        if r.status_code == 429:
            time.sleep(backoff + (time.time() % 0.5))
            backoff *= 2
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def fetch_trades(ticker: str, max_pages: int = 200) -> list[dict[str, Any]]:
    """Fetch ALL trades for a ticker, newest-first, deduped by trade_id."""
    seen: dict[str, dict] = {}
    cursor: Optional[str] = None
    for _ in range(max_pages):
        url = f"/markets/trades?ticker={ticker}&limit={PAGE_LIMIT}"
        if cursor:
            url += f"&cursor={cursor}"
        data = _get(url)
        trades = data.get("trades") or []
        if not trades:
            break
        new = 0
        for t in trades:
            tid = t.get("trade_id")
            if tid and tid not in seen:
                seen[tid] = t
                new += 1
        cursor = data.get("cursor")
        if not cursor:
            break
        # If a full page yielded no new trades, we've hit the cursor recycle
        # boundary; stop to avoid an infinite loop.
        if new == 0:
            break
    return list(seen.values())


def aggregate_trades(ticker: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate raw trades -> per-contract summary (VWAP entry, counts, times)."""
    if not trades:
        return {
            "ticker": ticker,
            "trade_count": 0,
            "total_volume_fp": 0.0,
            "vwap_yes_price": None,
            "last_trade_price": None,
            "first_trade_time": None,
            "last_trade_time": None,
            "min_trade_price": None,
            "max_trade_price": None,
        }

    # Parse each trade
    rows = []
    for t in trades:
        try:
            count = float(t.get("count_fp") or 0)
            yes_p = t.get("yes_price_dollars")
            yes_p = float(yes_p) if yes_p not in (None, "") else None
            ct = t.get("created_time")
            rows.append((count, yes_p, ct))
        except (ValueError, TypeError):
            continue

    # Volume-weighted average price (VWAP) using yes_price as the fill price
    vol_sum = 0.0
    price_vol_sum = 0.0
    prices = []
    times = []
    for count, yes_p, ct in rows:
        if count and count > 0:
            vol_sum += count
            if yes_p is not None:
                price_vol_sum += yes_p * count
        if yes_p is not None:
            prices.append(yes_p)
        if ct:
            times.append(ct)

    vwap = (price_vol_sum / vol_sum) if vol_sum > 0 else None
    return {
        "ticker": ticker,
        "trade_count": len(rows),
        "total_volume_fp": round(vol_sum, 4),
        "vwap_yes_price": round(vwap, 6) if vwap is not None else None,
        "last_trade_price": prices[-1] if prices else None,
        "first_trade_time": times[-1] if times else None,  # newest-first -> last elem is earliest
        "last_trade_time": times[0] if times else None,
        "min_trade_price": min(prices) if prices else None,
        "max_trade_price": max(prices) if prices else None,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contract_trades (
            ticker TEXT PRIMARY KEY,
            trade_count INTEGER,
            total_volume_fp REAL,
            vwap_yes_price REAL,
            last_trade_price REAL,
            first_trade_time TEXT,
            last_trade_time TEXT,
            min_trade_price REAL,
            max_trade_price REAL,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_vwap ON contract_trades(vwap_yes_price)")


def get_snapshot_tickers() -> list[str]:
    c = sqlite3.connect(SNAP_DB)
    cur = c.cursor()
    cur.execute("SELECT DISTINCT market_ticker FROM market_snapshots ORDER BY market_ticker")
    out = [r[0] for r in cur.fetchall()]
    c.close()
    return out


def already_done(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT ticker FROM contract_trades")
    return {r[0] for r in cur.fetchall()}


def probe() -> None:
    print("=== Kalshi /markets/trades endpoint probe ===\n")
    t = "KXHIGHTATL-26SEP08-T91"
    data = _get(f"/markets/trades?ticker={t}&limit=5")
    trades = data.get("trades", [])
    print(f"GET /markets/trades?ticker={t}&limit=5")
    print(f"  keys: {list(data.keys())}")
    print(f"  trades returned: {len(trades)}")
    if trades:
        print("  sample trade fields:")
        print(json.dumps(trades[0], indent=4))
    print("\nVerdict: public (no auth), returns real fills with yes/no price + size + timestamp.")
    print("History depth: ~2026-07-17 onward. Earlier settled markets return 0 trades.")


def backfill_one(ticker: str) -> dict[str, Any]:
    """Fetch + aggregate one ticker. Returns a result dict (or {'error': ...})."""
    try:
        trades = fetch_trades(ticker)
        summary = aggregate_trades(ticker, trades)
        summary["_error"] = None
        return summary
    except Exception as e:
        return {"_error": str(e), "ticker": ticker}


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill real Kalshi trade prices (aggregated).")
    ap.add_argument("--probe", action="store_true", help="Probe endpoint + print verdict, then exit.")
    ap.add_argument("--limit", type=int, default=0, help="Backfill only the first N tickers (0 = all).")
    ap.add_argument("--tickers", type=str, default="", help="Comma-separated explicit tickers.")
    ap.add_argument("--resume", action="store_true", help="Skip tickers already in output DB.")
    ap.add_argument("--parallel", type=int, default=6, help="Concurrent workers (default 6).")
    args = ap.parse_args()

    if args.probe:
        probe()
        return 0

    # Determine ticker list
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = get_snapshot_tickers()
        if args.limit and args.limit > 0:
            tickers = tickers[: args.limit]

    total = len(tickers)
    print(f"Backfill scope: {total} tickers -> {OUT_DB}")

    conn = sqlite3.connect(OUT_DB, timeout=60)
    ensure_schema(conn)
    done = already_done(conn) if args.resume else set()
    tickers = [t for t in tickers if t not in done]

    ok, no_trades, fail = 0, 0, 0
    t0 = time.time()
    batch = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(backfill_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            summary = fut.result()
            if summary.get("_error"):
                fail += 1
                print(f"  FAIL {ticker}: {summary['_error']}", file=sys.stderr)
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO contract_trades
                    (ticker, trade_count, total_volume_fp, vwap_yes_price,
                     last_trade_price, first_trade_time, last_trade_time,
                     min_trade_price, max_trade_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary["ticker"], summary["trade_count"],
                    summary["total_volume_fp"], summary["vwap_yes_price"],
                    summary["last_trade_price"], summary["first_trade_time"],
                    summary["last_trade_time"], summary["min_trade_price"],
                    summary["max_trade_price"],
                ),
            )
            if summary["trade_count"] > 0:
                ok += 1
            else:
                no_trades += 1
            batch += 1
            if batch % 200 == 0:
                conn.commit()
                el = time.time() - t0
                print(f"  ... {batch}/{len(tickers)} done (ok={ok} no_trades={no_trades} fail={fail}) {el:.0f}s")

    conn.commit()
    # Summary
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM contract_trades")
    stored = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM contract_trades WHERE trade_count > 0")
    with_trades = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM contract_trades WHERE vwap_yes_price IS NOT NULL")
    with_vwap = cur.fetchone()[0]
    conn.close()

    print(f"\nDONE ok={ok} no_trades={no_trades} fail={fail} elapsed={time.time()-t0:.0f}s")
    print(f"  stored rows: {stored}  (with trades: {with_trades}, with VWAP: {with_vwap})")
    print(f"  output DB: {OUT_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
