#!/usr/bin/env python3
"""
pull_kalshi_orderbook.py — Pull Kalshi order-book history (bid/ask spread) for
weather markets only, and collapse it to a compact per-contract summary.

SOURCE: Predexon API  (https://docs.predexon.com)
  Endpoint : GET https://api.predexon.com/v2/kalshi/orderbooks-subcent
  Auth     : x-api-key header (free signup at dashboard.predexon.com)
  Note     : orderbook-history endpoints are FREE & UNLIMITED on all plans
             (do not count against the monthly request quota). Rate limit is
             1 req/sec on the free tier.

WHY SUB-CENT: Kalshi quotes in sub-cent increments. The legacy
  `/v2/kalshi/orderbooks` rounds prices to whole cents (widens spreads, merges
  levels) and is being deprecated. `/orderbooks-subcent` returns exact decimal
  strings (e.g. "57.50") from Jan 8 2026 onward.

SCOPE: weather contracts only — the tickers already backfilled in
  `data/kalshi_trades.db` (table `contract_trades`, ~9,938 rows). We do NOT
  enumerate all of Kalshi.

AGGREGATION (no storage bloat): for each contract we do NOT store the tick
  replay. We stream the snapshots within the contract's trade window and keep
  only compact summary stats on the yes-side bid/ask spread.

OUTPUT: a NEW sqlite DB at data/kalshi_orderbook.db (idempotent; never touches
  an existing DB). One row per contract.

USAGE:
  export PREDEXON_API_KEY=...
  python3 scripts/pull_kalshi_orderbook.py [--limit N] [--dry-run]

If PREDEXON_API_KEY is unset, the script stops immediately and prints exactly
what is required to run it. It never fakes data and never hits a paid endpoint.
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

API_BASE = "https://api.predexon.com"
ORDERBOOK_ENDPOINT = f"{API_BASE}/v2/kalshi/orderbooks-subcent"

# Source of weather ticker scope (already backfilled trade VWAP data)
TRADES_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "kalshi_trades.db",
)

# Output DB (NEW — idempotent, no overwrite of existing DBs)
OUTPUT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "kalshi_orderbook.db",
)

FREE_TIER_RATE_LIMIT_S = 1.1  # 1 req/sec on free tier; leave margin
PAGE_SIZE = 2000  # max allowed by the endpoint


def iso_to_ms(iso: str) -> int:
    """Convert an ISO-8601 timestamp (may carry 'Z' or +00:00) to epoch ms."""
    iso = iso.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_page(session, ticker, start_ms, end_ms, pagination_key=None):
    params = {
        "ticker": ticker,
        "start_time": start_ms,
        "end_time": end_ms,
        "limit": PAGE_SIZE,
        "sources": "spine",
    }
    if pagination_key:
        params["pagination_key"] = pagination_key
    resp = session.get(ORDERBOOK_ENDPOINT, params=params, timeout=60)
    if resp.status_code == 429:
        # Respect free-tier rate limit with backoff; caller retries
        raise RateLimited()
    resp.raise_for_status()
    return resp.json()


class RateLimited(Exception):
    pass


def to_decimal(s):
    """Parse sub-cent decimal string to Decimal; return None if unparseable."""
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError, TypeError):
        return None


def aggregate_spread(snapshots):
    """
    snapshots: list of dicts with keys best_bid, best_ask (decimal-cent strings)
    Returns a dict of compact summary stats (or None if no valid book).
    """
    spreads = []
    first_ts = None
    last_ts = None
    for s in snapshots:
        bb = to_decimal(s.get("best_bid"))
        ba = to_decimal(s.get("best_ask"))
        if bb is None or ba is None:
            continue
        spread = ba - bb
        if spread < 0:
            # crossed/inverted book (capture artifact); skip the sample
            continue
        spreads.append(float(spread))
        ts = s.get("timestamp")
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    if not spreads:
        return None
    spreads.sort()
    n = len(spreads)
    def q(p):
        # nearest-rank quantile
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return spreads[idx]
    return {
        "spread_min": q(0.0),
        "spread_p10": q(0.10),
        "spread_median": q(0.50),
        "spread_p90": q(0.90),
        "spread_max": q(1.0),
        "n_ticks": n,
        "first_book_time": first_ts,
        "last_book_time": last_ts,
    }


def load_ticker_scope(db_path):
    """Return list of (ticker, first_trade_ms, last_trade_ms)."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, first_trade_time, last_trade_time "
        "FROM contract_trades ORDER BY ticker"
    )
    out = []
    for ticker, ft, lt in cur.fetchall():
        if not ft or not lt:
            # no trade window — nothing to anchor the book query to
            continue
        out.append((ticker, iso_to_ms(ft), iso_to_ms(lt)))
    conn.close()
    return out


def init_output_db(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS contract_spreads (
            ticker          TEXT PRIMARY KEY,
            spread_min      REAL,
            spread_p10      REAL,
            spread_median   REAL,
            spread_p90      REAL,
            spread_max      REAL,
            n_ticks         INTEGER,
            first_book_time INTEGER,
            last_book_time  INTEGER,
            fetched_at      TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def already_done(conn, ticker):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM contract_spreads WHERE ticker = ?", (ticker,))
    return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N tickers (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve scope + key, but do not hit the API")
    args = ap.parse_args()

    api_key = os.environ.get("PREDEXON_API_KEY", "").strip()
    if not api_key:
        print(
            "\n[BLOCKED] PREDEXON_API_KEY is not set.\n\n"
            "To run this puller:\n"
            "  1. Sign up (free, no credit card) at https://dashboard.predexon.com\n"
            "  2. Copy your API key.\n"
            "  3. export PREDEXON_API_KEY=<your-key>\n"
            "  4. Re-run:  python3 scripts/pull_kalshi_orderbook.py\n\n"
            "Endpoint used: GET /v2/kalshi/orderbooks-subcent\n"
            "Auth header : x-api-key: <key>\n"
            "This endpoint is FREE & UNLIMITED on the free plan (1 req/sec).\n"
            "No data was fetched and nothing was written.\n"
        )
        sys.exit(2)

    scope = load_ticker_scope(TRADES_DB)
    print(f"[scope] {len(scope)} weather contracts from {TRADES_DB}")
    if args.limit:
        scope = scope[: args.limit]
        print(f"[scope] limited to first {len(scope)} tickers")
    if args.dry_run:
        print("[dry-run] key present, scope resolved; no API calls made.")
        return

    conn = init_output_db(OUTPUT_DB)
    session = requests.Session()
    session.headers.update({"x-api-key": api_key})

    done = skipped = errors = 0
    for i, (ticker, start_ms, end_ms) in enumerate(scope):
        if already_done(conn, ticker):
            skipped += 1
            continue

        snapshots = []
        pagination_key = None
        attempts = 0
        while True:
            if attempts:
                time.sleep(FREE_TIER_RATE_LIMIT_S)
            try:
                data = fetch_page(session, ticker, start_ms, end_ms,
                                  pagination_key)
            except RateLimited:
                time.sleep(5.0)
                attempts += 1
                if attempts > 6:
                    errors += 1
                    print(f"[error] {ticker}: persistent 429, giving up")
                    break
                continue
            except requests.HTTPError as e:
                errors += 1
                print(f"[error] {ticker}: HTTP {e.response.status_code}")
                break

            snapshots.extend(data.get("snapshots", []))
            pag = data.get("pagination") or {}
            if not pag.get("has_more"):
                break
            pagination_key = pag.get("pagination_key")
            if not pagination_key:
                break

        summary = aggregate_spread(snapshots)
        if summary is not None:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO contract_spreads
                    (ticker, spread_min, spread_p10, spread_median, spread_p90,
                     spread_max, n_ticks, first_book_time, last_book_time)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (ticker, summary["spread_min"], summary["spread_p10"],
                 summary["spread_median"], summary["spread_p90"],
                 summary["spread_max"], summary["n_ticks"],
                 summary["first_book_time"], summary["last_book_time"]),
            )
            conn.commit()
            done += 1
        else:
            skipped += 1  # no valid book in window

        if (i + 1) % 50 == 0:
            print(f"[progress] {i+1}/{len(scope)} done={done} skipped={skipped} "
                  f"errors={errors}")

    conn.close()
    print(f"\n[done] done={done} skipped={skipped} errors={errors}")
    print(f"[output] {OUTPUT_DB}")


if __name__ == "__main__":
    main()
