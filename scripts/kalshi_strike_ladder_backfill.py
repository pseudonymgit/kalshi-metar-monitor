#!/usr/bin/env python3
"""
Kalshi strike-ladder backfill — FULL historical strike ladder (every rung).

Author: Gilfoyle (subagent), 2026-09-20 (P0-B).
Deterministic, script-only, read-only on existing data, no AI in the loop.

PURPOSE
-------
The prior kalshi_historical_strike_backfill.py collapsed each (station, market_type,
trading_date) to a SINGLE representative strike (median greater/less rung), which
DESTROYED the ladder. Only 54 `between` rungs survived of ~14k strike rows. P0-B
needs the FULL ladder — every rung — to score bucket/rung edges (the 1-degree
`between` buckets) and NO-side entry, not just the single-strike HIGH/LOW crossing.

This script stores EVERY rung per (station, market_type, local_trading_date):
  * `greater` rung  -> threshold market `temp > X`  (floor_strike=X)
  * `less`    rung  -> threshold market `temp < X`  (cap_strike=X)
  * `between` rung  -> 1-degree bucket `temp in [X, X+1]` (floor=X, cap=X+1)

ENDPOINT (rev-2 verified 2026-09-18):
  * `GET /historical/markets` (unauthenticated) -> settled markets back to 2021-08.
  * `/markets` -> recent tail after the rolling ~2026-07-20 cutoff.
  * We merge BOTH per series; strike_type/floor_strike/cap_strike are embedded.

MARKET STRUCTURE (authoritative, from MISPRICING-EDGE-RETEST-2026-09-20.md):
  * T ticker = threshold market (`temp < X` OR `temp > X`), direction varies.
  * B ticker = 1-degree bucket (`max/min temp in [X, X+1]`), floor=X, cap=X+1.

STATION MAPPING: canonical `scripts/kalshi_series_map.py` (data/kalshi_series_map.json).
All 20 active stations have live series under inconsistent ticker names.

STORAGE
-------
Writes a NEW dedicated DB `data/kalshi_strike_ladder.db` (table `strike_ladder`),
fully separate from the running orderbook pull (`kalshi_orderbook.db`) and the
mispricing re-test (`kalshi_trades.db`). Does NOT touch settlement_epochs or any
existing table. Aggregate per rung (one row per rung, NOT raw orderbook) — no bloat.

SAFETY
------
  * No commit by default (--commit to opt in).
  * Idempotent: PRIMARY KEY (station, market_type, local_trading_date, strike_type,
    strike_value) — re-run overwrites cleanly.
  * No live trading, no order endpoints, no auth (public GET only).
  * Preserves dirty tree: writes only new files, never git add/commit.

USAGE
-----
  python3 scripts/kalshi_strike_ladder_backfill.py --probe
  python3 scripts/kalshi_strike_ladder_backfill.py --dry-run
  python3 scripts/kalshi_strike_ladder_backfill.py --parallel 10
  python3 scripts/kalshi_strike_ladder_backfill.py --parallel 10 --commit
"""

import argparse
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = os.getenv(
    "KALSHI_PUBLIC_BASE_URL",
    os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"),
).rstrip("/")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Source: settlement_epochs in metar_backfill.db (station set + trading dates).
SE_DB = os.environ.get(
    "METAR_DB_PATH", os.path.join(PROJECT_ROOT, "data", "metar_backfill.db")
)
# Destination: NEW dedicated ladder DB (separate from orderbook/trades).
LADDER_DB = os.environ.get(
    "KALSHI_STRIKE_LADDER_DB",
    os.path.join(PROJECT_ROOT, "data", "kalshi_strike_ladder.db"),
)

REQUEST_TIMEOUT = 30
THROTTLE_SEC = 1.5
MAX_RETRIES = 5
BACKOFF_BASE = 5.0

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")

import kalshi_series_map as _ksm

# --------------------------------------------------------------------------- #
# Rate-limited HTTP client (thread-safe)
# --------------------------------------------------------------------------- #
_session = requests.Session()
_last_request = 0.0
_throttle_lock = threading.Lock()


def _get(path: str) -> dict[str, Any]:
    global _last_request
    url = f"{BASE}{path}"
    for attempt in range(MAX_RETRIES):
        with _throttle_lock:
            elapsed = time.time() - _last_request
            if elapsed < THROTTLE_SEC:
                time.sleep(THROTTLE_SEC - elapsed)
            _last_request = time.time()
        try:
            resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"  [rate-limit] HTTP {resp.status_code} on {path}, backoff {wait:.0f}s "
                  f"(attempt {attempt+1}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"exhausted retries on {path}")


def _get_paginated(path: str, key: str, limit: int = 200,
                   max_pages: int = 400) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        p = f"{path}{'&' if '?' in path else '?'}limit={limit}"
        if cursor:
            p += f"&cursor={cursor}"
        data = _get(p)
        out.extend(data.get(key) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def enumerate_weather_series() -> list[dict]:
    series = _get_paginated("/series", key="series", limit=1000, max_pages=100)
    weather = []
    for s in series:
        cats = [(c or "").lower() for c in (s.get("categories") or [])]
        if not any("weather" in c or "climate" in c for c in cats):
            continue
        tk = (s.get("ticker") or s.get("series_ticker") or "")
        if tk.startswith("KXHIGH") or tk.startswith("KXLOW"):
            weather.append(s)
    return weather


def build_station_tickers(db_stations: set[str]) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    try:
        series = enumerate_weather_series()
    except Exception as e:
        print(f"[error] series enumeration failed ({e}); cannot resolve station map",
              file=sys.stderr)
        return {}
    for s in series:
        tk = s.get("ticker") or s.get("series_ticker") or ""
        try:
            station = _ksm.resolve_ticker(tk)
        except KeyError:
            continue  # non-canonical ticker (e.g. retired/experimental series)
        if station not in db_stations:
            continue
        direction = "HIGH" if tk.startswith("KXHIGH") else "LOW"
        if tk not in result[station][direction]:
            result[station][direction].append(tk)
    return {st: dict(d) for st, d in result.items()}


def market_date(ticker: str, event_ticker: Optional[str] = None) -> Optional[str]:
    for tk in (ticker, event_ticker or ""):
        m = _EVENT_DATE_RE.search(tk or "")
        if not m:
            continue
        yy, mon, dd = m.group(1), m.group(2).upper(), m.group(3)
        if mon in _MONTHS:
            return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
    return None


# --------------------------------------------------------------------------- #
# Full-ladder extraction (the KEY change vs the single-strike backfill)
# --------------------------------------------------------------------------- #
def extract_ladder_rows(station: str, market_type: str,
                        by_date: dict[str, list[dict]]) -> list[tuple]:
    """Return one row per rung (not collapsed). Rows:
    (station, market_type, date, strike_value, strike_type, floor, cap,
     settlement_value, result, ticker)
    """
    rows: list[tuple] = []
    for d, markets in by_date.items():
        seen_rungs: set[tuple] = set()
        for m in markets:
            stype = m.get("strike_type")
            floor = m.get("floor_strike")
            cap = m.get("cap_strike")
            ticker = m.get("ticker") or ""
            settle = m.get("settlement_value_dollars")
            result = m.get("result")

            # Normalize the rung's strike value + type.
            if stype == "greater" and floor is not None:
                rung = (float(floor), "greater", float(floor), None)
            elif stype == "less" and cap is not None:
                rung = (float(cap), "less", None, float(cap))
            elif stype == "between" and floor is not None and cap is not None:
                rung = ((float(floor) + float(cap)) / 2.0, "between",
                        float(floor), float(cap))
            elif floor is not None and cap is not None:
                rung = ((float(floor) + float(cap)) / 2.0, "between",
                        float(floor), float(cap))
            elif floor is not None:
                rung = (float(floor), "greater", float(floor), None)
            elif cap is not None:
                rung = (float(cap), "less", None, float(cap))
            else:
                continue

            key = (rung[0], rung[1])
            if key in seen_rungs:
                continue
            seen_rungs.add(key)

            rows.append((station, market_type, d, rung[0], rung[1],
                         rung[2], rung[3], settle, result, ticker))
    return rows


def backfill_series(station: str, market_type: str,
                    tickers: list[str]) -> list[tuple]:
    rows: list[tuple] = []
    for ticker in tickers:
        by_date: dict[str, list[dict]] = defaultdict(list)

        try:
            hist = _get_paginated(
                f"/historical/markets?series_ticker={ticker}", key="markets",
                limit=1000, max_pages=400,
            )
        except Exception as e:
            print(f"[warn] {station}/{market_type}/{ticker} /historical/markets fail: {e}",
                  file=sys.stderr)
            hist = []
        for m in hist:
            d = market_date(m.get("ticker"), m.get("event_ticker"))
            if d:
                by_date[d].append(m)

        try:
            live = _get_paginated(
                f"/markets?series_ticker={ticker}", key="markets",
                limit=200, max_pages=50,
            )
        except Exception as e:
            print(f"[warn] {station}/{market_type}/{ticker} /markets fail: {e}",
                  file=sys.stderr)
            live = []
        for m in live:
            d = market_date(m.get("ticker"), m.get("event_ticker"))
            if d:
                by_date[d].append(m)

        rows.extend(extract_ladder_rows(station, market_type, by_date))

    return rows


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
DDL = """
CREATE TABLE IF NOT EXISTS strike_ladder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL,          -- 'HIGH' | 'LOW'
    local_trading_date TEXT NOT NULL,   -- YYYY-MM-DD (the measured day)
    strike_value REAL NOT NULL,         -- greater/less: threshold; between: midpoint
    strike_type TEXT NOT NULL,          -- 'greater' | 'less' | 'between'
    floor_strike REAL,                  -- between/greater lower bound (NULL for less)
    cap_strike REAL,                    -- between/less upper bound (NULL for greater)
    settlement_value_dollars REAL,      -- Kalshi settlement (1.0 yes / 0.0 no)
    result TEXT,                        -- 'yes' | 'no' (authoritative outcome)
    ticker TEXT,                        -- market ticker (e.g. KXHIGHNY-26JUL19-B80.5)
    UNIQUE(station, market_type, local_trading_date, strike_type, strike_value)
);
CREATE INDEX IF NOT EXISTS idx_strike_ladder_st_date
    ON strike_ladder (station, local_trading_date);
"""


def load_db_stations() -> set[str]:
    conn = sqlite3.connect(SE_DB)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT station FROM settlement_epochs")}
    finally:
        conn.close()


def load_existing_keys() -> set[tuple]:
    """Existing (station, market_type, date) keys in settlement_epochs — used only
    to confirm which (station, type, date) triples are tradeable/real, so we don't
    store ladder rungs for dates the engine never traded."""
    conn = sqlite3.connect(SE_DB)
    try:
        return {
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT station, market_type, local_trading_date FROM settlement_epochs"
            )
        }
    finally:
        conn.close()


def upsert_ladder(conn: sqlite3.Connection, rows: list[tuple], dry_run: bool) -> int:
    if dry_run:
        print(f"  [dry-run] would insert {len(rows)} rung rows")
        return len(rows)
    conn.executemany(
        """INSERT OR REPLACE INTO strike_ladder
           (station, market_type, local_trading_date, strike_value, strike_type,
            floor_strike, cap_strike, settlement_value_dollars, result, ticker)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


# --------------------------------------------------------------------------- #
# Probe mode
# --------------------------------------------------------------------------- #
def run_probe() -> int:
    print("=== Kalshi strike-ladder endpoint probe ===\n")
    print(f"BASE = {BASE}\n")
    data = _get("/historical/markets?series_ticker=KXHIGHNY&limit=100")
    mk = data.get("markets") or []
    print(f"/historical/markets?series_ticker=KXHIGHNY -> {len(mk)} markets")

    by_date = defaultdict(list)
    for m in mk:
        d = market_date(m.get("ticker"), m.get("event_ticker"))
        if d:
            by_date[d].append(m)

    latest = sorted(by_date.keys())[-1]
    rows = extract_ladder_rows("KNYC", "HIGH", {latest: by_date[latest]})
    print(f"\nSample date {latest}: {len(by_date[latest])} markets -> "
          f"{len(rows)} rungs (full ladder, NOT collapsed):")
    for r in sorted(rows, key=lambda x: x[3]):
        print(f"  {r[9]:32s} value={r[3]:6.1f} type={r[4]:7s} "
              f"floor={r[5]} cap={r[6]} settle={r[7]} result={r[8]}")

    print("\n=== VERDICT ===")
    print("Full ladder (every greater/less/between rung) is recoverable per date.")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi full strike-ladder backfill (P0-B)")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--parallel", type=int, default=10)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if args.probe:
        return run_probe()

    if not os.path.exists(SE_DB):
        print(f"settlement_epochs DB not found: {SE_DB}")
        return 1

    db_stations = load_db_stations()
    existing_keys = load_existing_keys()
    print(f"settlement_epochs: {len(existing_keys)} rows, {len(db_stations)} stations")

    station_tickers = build_station_tickers(db_stations)
    print(f"resolved series mapping for {len(station_tickers)} stations")

    work = []
    for station in sorted(station_tickers):
        for market_type in ("HIGH", "LOW"):
            tickers = station_tickers[station].get(market_type, [])
            if tickers:
                work.append((station, market_type, tickers))

    if not work:
        print("no recoverable series; nothing to do")
        return 0

    print(f"\n{len(work)} series work items, fetching with {args.parallel} workers...")
    t0 = time.time()
    all_rows: list[tuple] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {ex.submit(backfill_series, st, mt, tk): (st, mt)
                for st, mt, tk in work}
        for fut in as_completed(futs):
            st, mt = futs[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print(f"[error] {st}/{mt} crashed: {e}", file=sys.stderr)
                continue
            all_rows.extend(rows)
            # count distinct dates covered
            dates = {r[2] for r in rows}
            print(f"  {st}/{mt}: {len(rows)} rungs across {len(dates)} dates")

    elapsed = time.time() - t0
    print(f"\nfetched {len(all_rows)} rung rows in {elapsed:.0f}s")

    # Filter to (station, type, date) that exist in settlement_epochs (tradeable).
    rows = [r for r in all_rows if (r[0], r[1], r[2]) in existing_keys]
    print(f"  -> {len(rows)} rungs map to existing settlement_epochs rows")

    if not rows:
        print("nothing to backfill")
        return 0

    if not args.dry_run:
        os.makedirs(os.path.dirname(LADDER_DB), exist_ok=True)
        conn = sqlite3.connect(LADDER_DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(DDL)
    else:
        conn = None

    try:
        n = upsert_ladder(conn, rows, args.dry_run)
        if args.dry_run:
            print(f"\n[dry-run] would store {n} rung rows (no writes)")
            return 0
        if args.commit:
            conn.commit()
            print(f"\ncommitted {n} rung rows to {LADDER_DB} (--commit set)")
        else:
            print(f"\ninserted {n} rung rows in-memory; NOT committed "
                  f"(--commit to persist)")
            print("rolling back on exit — no changes written to disk.")
    finally:
        if conn:
            conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
