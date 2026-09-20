#!/usr/bin/env python3
"""
Backfill authoritative YES/NO settlement results for settled contracts.

The mispricing re-test needs the TRUE win outcome per contract. The ticker's T/B
suffix does NOT encode a simple threshold: T = threshold ("temp > X" OR "temp < X",
direction varies per market), B = 1-degree bucket ("temp in [X, X+1]"). The prior
"crossing" settle_outcome is WRONG. The authoritative result is Kalshi's own
`result` field ("yes"/"no") on the finalized market.

This script queries GET /markets/{ticker} for the `result` field for every
settled contract, stores to data/kalshi_settlement_results.db (idempotent upsert),
and is read-only on existing data (only writes the NEW results DB).
"""
import argparse
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
TRADES = 'data/kalshi_trades.db'
OUT_DB = 'data/kalshi_settlement_results.db'

TICK = re.compile(r'^KX(HIGH|LOW)(\w+)-(\d{2}[A-Z]{3}\d{2})-(T|B)(\d+(?:\.\d+)?)$')

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "WeatherEngineSettlementResult/1.0", "Accept": "application/json"})

import threading
_LOCK = threading.Lock()
_LAST = 0.0


def _get(path, timeout=20):
    global _LAST
    with _LOCK:
        wait = 0.35 - (time.time() - _LAST)
        if wait > 0:
            time.sleep(wait)
        _LAST = time.time()
    for _ in range(6):
        r = SESSION.get(f"{BASE}{path}", timeout=timeout)
        if r.status_code == 429:
            time.sleep(3.0)
            continue
        if r.status_code == 503:
            time.sleep(3.0)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def fetch_result(ticker):
    try:
        d = _get(f"/markets/{ticker}")
        m = d.get("market") or {}
        return {
            "ticker": ticker,
            "result": m.get("result"),          # "yes"/"no"/None (unsettled)
            "status": m.get("status"),
            "cap_strike": m.get("cap_strike"),
            "floor_strike": m.get("floor_strike"),
            "expiration_value": m.get("expiration_value"),
            "_error": None,
        }
    except Exception as e:
        return {"ticker": ticker, "result": None, "status": None,
                "cap_strike": None, "floor_strike": None, "expiration_value": None,
                "_error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()

    c = sqlite3.connect(TRADES)
    rows = c.execute("SELECT ticker FROM contract_trades WHERE trade_count > 0").fetchall()
    c.close()
    tickers = [r[0] for r in rows]
    if args.limit:
        tickers = tickers[:args.limit]

    conn = sqlite3.connect(OUT_DB, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS settlement_results (
        ticker TEXT PRIMARY KEY,
        result TEXT,
        status TEXT,
        cap_strike REAL,
        floor_strike REAL,
        expiration_value REAL,
        error TEXT,
        fetched_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()

    done = set()
    if args.resume:
        done = {r[0] for r in conn.execute("SELECT ticker FROM settlement_results WHERE result IS NOT NULL")}
    tickers = [t for t in tickers if t not in done]

    total = len(tickers)
    print(f"Fetching settlement results for {total} tickers (parallel={args.parallel})...")
    ok = yes = no = unsettled = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(fetch_result, t): t for t in tickers}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            conn.execute(
                "INSERT OR REPLACE INTO settlement_results "
                "(ticker, result, status, cap_strike, floor_strike, expiration_value, error) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["ticker"], r["result"], r["status"], r["cap_strike"],
                 r["floor_strike"], r["expiration_value"], r["_error"]),
            )
            if r["_error"]:
                fail += 1
            elif r["result"] == "yes":
                yes += 1; ok += 1
            elif r["result"] == "no":
                no += 1; ok += 1
            else:
                unsettled += 1
            if i % 100 == 0:
                conn.commit()
                el = time.time() - t0
                rate = i / el if el > 0 else 0
                print(f"  {i}/{total} done (yes={yes} no={no} unsettled={unsettled} fail={fail}) {el:.0f}s ({rate:.1f}/s)", flush=True)
    conn.commit()
    conn.close()
    el = time.time() - t0
    print(f"DONE ok={ok} (yes={yes} no={no}) unsettled={unsettled} fail={fail} elapsed={el:.0f}s")
    print(f"output: {OUT_DB}")


if __name__ == "__main__":
    main()
