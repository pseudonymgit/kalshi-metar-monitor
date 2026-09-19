#!/usr/bin/env python3
"""
Kalshi historical-strike backfill — optimized, parallel, idempotent (read-only scope).

Author: Gilfoyle (subagent), 2026-09-18 (rev 2 — /historical/markets).
Approved by Dan: read-only on existing data, script-only, optimized, parallel.

PURPOSE
-------
settlement_epochs has strike_price (REAL) + strike_type (TEXT) columns (added by
migrate_add_strike_price.py) but only 56 rows are populated. This script backfills
the strike for each closed settlement_epochs row from Kalshi's public API.

ENDPOINT VERDICT (rev 2 — CORRECTED 2026-09-18):
  * `GET /historical/markets` (unauthenticated) returns SETTLED markets back to
    AUGUST 2021 (the full span Kalshi actually listed), NOT the ~7.5-month live
    window. This is the correct entry point. Verified: KXHIGHNY -> 9,214 markets
    back to 2021-08-07; KXHIGHCHI -> 9,211 back to 2021-08-20.
  * Each historical market embeds the strike directly: `strike_type`, `floor_strike`,
    `cap_strike`, `settlement_value_dollars`. NO reconstruction needed.
  * The live/historical partition cutoff is ROLLING ~2026-07-20T00:00:00Z.
    Markets settled BEFORE that live only in `/historical/*`; AFTER that in `/markets`.
    So we merge: `/historical/markets` (deep) + `/markets` (recent tail).
  * `/historical/markets` supports only `limit`, `cursor`, `tickers`,
    `event_ticker`, `series_ticker`, `mve_filter`. `min/max_settled_ts` are
    SILENTLY IGNORED (verified) — paginate by cursor newest-first to reach 2021.
  * The prior rev-1 script targeted `/events` + `/markets?event_ticker` (7.5-month
    retention). That was WRONG. This rev replaces it entirely.

STRIKE SEMANTICS (from market JSON, authoritative):
  * floor_strike set, cap_strike NULL  -> strike_type='greater', strike_price=floor_strike
  * cap_strike set, floor_strike NULL  -> strike_type='less',    strike_price=cap_strike
  * both set                            -> strike_type='between', strike_price=(floor+cap)/2
  Strike type is INDEPENDENT of HIGH/LOW series. The engine trades `greater` for
  HIGH series and `less` for LOW series; `between` is a bin fallback.

STATION MAPPING (canonical — the CORRECTED view):
  Kalshi series tickers are KXHIGH<CODE> / KXLOW<CODE> where <CODE> is NOT the
  ICAO and is NOT predictable (some have a leading 'T', some don't). FIVE naming
  conventions map the SAME daily-high product to different tickers.

  The canonical city->station->aliases map lives in
  scripts/kalshi_series_map.py (data file data/kalshi_series_map.json). It folds
  every naming variant (current + legacy + duplicate) into ONE station, keyed by
  canonical ICAO. This supersedes the prior hardcoded STATION_TO_KALSHI_CODE
  reverse map AND the false "11 retired stations" conclusion — all 20 tradeable
  cities have live series (see docs/plans/KALSHI-CITY-TICKER-MAPPING-2026-09-18.md).

  We resolve enumerated series tickers via kalshi_series_map.resolve_ticker() and
  intersect against the stations ACTUALLY PRESENT in settlement_epochs.

SAFETY (boundaries):
  * Read-only on existing data: only UPDATE the 2 strike columns on settlement_epochs.
  * NO commit by default (--commit to opt in). Changes roll back on exit otherwise.
  * Idempotent upsert keyed by (station, market_type, local_trading_date).
  * No live trading, no order endpoints, no auth required (public GET only).
  * Preserves dirty tree: writes only new files, never git add/commit.

USAGE
-----
  python3 scripts/kalshi_historical_strike_backfill.py --probe            # endpoint verdict + one-series sample
  python3 scripts/kalshi_historical_strike_backfill.py --dry-run          # scope only, NO writes
  python3 scripts/kalshi_historical_strike_backfill.py --parallel 10     # real backfill, no commit
  python3 scripts/kalshi_historical_strike_backfill.py --parallel 10 --commit  # opt-in commit
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
# Allow importing kalshi_series_map (sibling in scripts/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
SE_DB = os.environ.get(
    "METAR_DB_PATH", os.path.join(PROJECT_ROOT, "data", "metar_backfill.db")
)

# Roll over live/historical partition cutoff (advances over time). Markets settled
# before this are in /historical/*; after in /markets. We fetch BOTH and dedup.
HISTORICAL_CUTOFF_ISO = "2026-07-20T00:00:00Z"

REQUEST_TIMEOUT = 30
THROTTLE_SEC = 1.5          # inter-request floor, matching z8 cron
MAX_RETRIES = 5             # 429/5xx backoff retries
BACKOFF_BASE = 5.0          # seconds; exponential (5, 10, 20, 40, 80)

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
# ticker suffix: KXHIGHTATL-26SEP19-T86  -> (YY=26, MMM=SEP, DD=19)
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")

# --------------------------------------------------------------------------- #
# Station <-> Kalshi ticker resolution (CANONICAL — scripts/kalshi_series_map)
# --------------------------------------------------------------------------- #
# Import the canonical city->station->aliases resolver. This is the single
# source of truth and supersedes the prior hardcoded STATION_TO_KALSHI_CODE
# reverse map + the false "11 retired stations" conclusion (all 20 tradeable
# cities have live series under inconsistent ticker names).
import kalshi_series_map as _ksm


def _code_suffix(ticker: str) -> str:
    """Strip the KXHIGH/KXLOW prefix, return the remaining code (e.g. 'NY', 'TATL')."""
    return _ksm._code_suffix(ticker)


def _station_for_code(code: str) -> Optional[str]:
    """Map a Kalshi code suffix (e.g. 'NY', 'TNYC', 'TATL', 'AUS', 'LV') to a
    canonical station ICAO, or None. Uses the canonical map's normalization.
    """
    return _ksm.station_for_code_suffix(code)


# --------------------------------------------------------------------------- #
# Rate-limited HTTP client (thread-safe)
# --------------------------------------------------------------------------- #
_session = requests.Session()
_last_request = 0.0
_throttle_lock = threading.Lock()


def _get(path: str) -> dict[str, Any]:
    """GET (public) with throttle + backoff."""
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


def _get_paginated(path: str, key: str, limit: int = 200, max_pages: int = 400) -> list[dict]:
    """Follow `cursor` pagination, returning the concatenated list of `key` records.

    `key` is the list field in the response: 'markets', 'events', or 'series'.
    max_pages default is high (400) because /historical/markets needs many pages
    to reach 2021 (9,214 markets / 1000 per page ~ 10 pages; /markets uses 200).
    """
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


# --------------------------------------------------------------------------- #
# Series enumeration -> station -> {HIGH: [tickers], LOW: [tickers]}
# --------------------------------------------------------------------------- #
def enumerate_weather_series() -> list[dict]:
    """Return all KXHIGH*/KXLOW* daily-temperature series from /series.

    Filters to category 'Climate and Weather' (case-insensitive) AND a ticker
    prefix of KXHIGH/KXLOW, excluding non-weather namesakes (KXHIGHINFLATION,
    KXHIGHMOVDJT, KXHIGHMOVKH, KXLOW, KXLOWA, KXLOWCC, KXLOWESTRATE, ...).
    """
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
    """
    Return {station: {'HIGH': [tickers], 'LOW': [tickers]}}.

    Dynamic enumeration: GET /series -> filter KXHIGH*/KXLOW* Climate/Weather ->
    map each ticker's code suffix -> station via _station_for_code -> keep only
    stations present in settlement_epochs. This avoids the hardcoded 20-station
    loop that silently missed ~half the cities due to inconsistent ticker naming.
    """
    result: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    try:
        series = enumerate_weather_series()
    except Exception as e:
        print(f"[error] series enumeration failed ({e}); cannot resolve station map",
              file=sys.stderr)
        return {}

    for s in series:
        tk = s.get("ticker") or s.get("series_ticker") or ""
        station = _ksm.resolve_ticker(tk)
        if station is None or station not in db_stations:
            continue
        direction = "HIGH" if tk.startswith("KXHIGH") else "LOW"
        if tk not in result[station][direction]:
            result[station][direction].append(tk)

    return {st: dict(d) for st, d in result.items()}


# --------------------------------------------------------------------------- #
# Strike extraction
# --------------------------------------------------------------------------- #
def collapse_strikes(markets: list[dict], market_type: str) -> Optional[tuple[float, str]]:
    """Collapse one event's markets to a single representative strike per type.

    For a HIGH series, the engine trades the `greater` binary (floor_strike); for
    LOW, the `less` binary (cap_strike). We pick the median rung across the ladder
    (matching migrate semantics of the median rung the engine actually trades).
    """
    greater: list[float] = []
    less: list[float] = []
    between: list[float] = []

    for m in markets:
        stype = m.get("strike_type")
        floor = m.get("floor_strike")
        cap = m.get("cap_strike")
        if stype == "greater" and floor is not None:
            greater.append(float(floor))
        elif stype == "less" and cap is not None:
            less.append(float(cap))
        elif stype == "between" and floor is not None and cap is not None:
            between.append((float(floor) + float(cap)) / 2.0)
        elif floor is not None:
            greater.append(float(floor))
        elif cap is not None:
            less.append(float(cap))

    if market_type == "HIGH" and greater:
        return (sorted(greater)[len(greater) // 2], "greater")
    if market_type == "LOW" and less:
        return (sorted(less)[len(less) // 2], "less")
    if between:
        return (sorted(between)[len(between) // 2], "between")
    if greater:
        return (sorted(greater)[len(greater) // 2], "greater")
    if less:
        return (sorted(less)[len(less) // 2], "less")
    return None


def market_date(ticker: str, event_ticker: Optional[str] = None) -> Optional[str]:
    """Parse the trading date from a market/event ticker.

    KXHIGHNY-26JUL18-T86  -> '2026-07-18' (the trading day the temp was measured).
    Falls back to the event_ticker if the market ticker lacks a parseable suffix.
    """
    for tk in (ticker, event_ticker or ""):
        m = _EVENT_DATE_RE.search(tk or "")
        if not m:
            continue
        yy, mon, dd = m.group(1), m.group(2).upper(), m.group(3)
        if mon in _MONTHS:
            return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
    return None


# --------------------------------------------------------------------------- #
# Per-series backfill worker
# --------------------------------------------------------------------------- #
def backfill_series(station: str, market_type: str, tickers: list[str]) -> list[tuple]:
    """
    Fetch historical + live markets for one (station, market_type) series across
    all its candidate tickers, and return (station, market_type, date,
    strike_price, strike_type) rows ready to upsert.

    Each ticker is fetched via /historical/markets (deep, 2021->cutoff) AND
    /markets (recent tail, cutoff->now). Markets are grouped by trading date and
    collapsed to a single representative strike per date.
    """
    rows: list[tuple] = []
    for ticker in tickers:
        # Collect markets keyed by trading date from BOTH endpoints.
        by_date: dict[str, list[dict]] = defaultdict(list)

        # 1) Historical (deep) — settled before ~2026-07-20.
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

        # 2) Live (recent tail) — settled after the cutoff.
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

        # Collapse each date's markets to a single representative strike.
        for d, markets in by_date.items():
            strike = collapse_strikes(markets, market_type)
            if strike is None:
                continue
            rows.append((station, market_type, d, strike[0], strike[1]))

    return rows


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def load_db_stations() -> set[str]:
    conn = sqlite3.connect(SE_DB)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT station FROM settlement_epochs")}
    finally:
        conn.close()


def load_existing_keys() -> set[tuple]:
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


def upsert(conn: sqlite3.Connection, rows: list[tuple], dry_run: bool) -> int:
    """Idempotent upsert of strike_price/strike_type keyed by (station, market_type, date).

    Only touches the two strike columns. No commit here (caller controls commit).
    """
    seen: set[tuple] = set()
    updates: list[tuple] = []
    for station, market_type, date, strike, strike_type in rows:
        key = (station, market_type, date)
        if key in seen:
            continue
        seen.add(key)
        updates.append((strike, strike_type, station, market_type, date))

    if dry_run:
        print(f"  [dry-run] would upsert {len(updates)} rows")
        return len(updates)

    conn.executemany(
        """UPDATE settlement_epochs
           SET strike_price=?, strike_type=?
           WHERE station=? AND market_type=? AND local_trading_date=?""",
        updates,
    )
    return len(updates)


# --------------------------------------------------------------------------- #
# Probe mode
# --------------------------------------------------------------------------- #
def run_probe() -> int:
    print("=== Kalshi historical-strike endpoint probe (rev 2 — /historical/markets) ===\n")
    print(f"BASE = {BASE}\n")

    # Historical endpoint — the CORRECT entry point.
    print("--- /historical/markets (unauthenticated, 2021->present) ---")
    data = _get("/historical/markets?series_ticker=KXHIGHNY&limit=5")
    mk = data.get("markets") or []
    print(f"[1] /historical/markets?series_ticker=KXHIGHNY -> {len(mk)} sample markets "
          f"(cursor={bool(data.get('cursor'))})")
    if mk:
        m = mk[0]
        print(f"    sample: {m.get('ticker')} | strike_type={m.get('strike_type')} | "
              f"floor={m.get('floor_strike')} cap={m.get('cap_strike')} | "
              f"settle_value={m.get('settlement_value_dollars')} | result={m.get('result')}")
        print(f"    close_time={m.get('close_time')} settlement_ts={m.get('settlement_ts')}")

    # Full depth sizing for one series.
    hist_full = _get_paginated("/historical/markets?series_ticker=KXHIGHNY",
                               key="markets", limit=1000, max_pages=400)
    dates = sorted(market_date(m.get("ticker"), m.get("event_ticker"))
                   for m in hist_full if market_date(m.get("ticker"), m.get("event_ticker")))
    print(f"[2] KXHIGHNY full history -> {len(hist_full)} markets")
    print(f"    date range: {dates[0]} -> {dates[-1]}" if dates else "    (no dates)")

    # Live endpoint — recent tail after cutoff.
    live = _get_paginated("/markets?series_ticker=KXHIGHNY", key="markets",
                          limit=200, max_pages=50)
    ldates = sorted(market_date(m.get("ticker"), m.get("event_ticker"))
                    for m in live if market_date(m.get("ticker"), m.get("event_ticker")))
    print(f"[3] /markets?series_ticker=KXHIGHNY (live tail) -> {len(live)} markets")
    print(f"    date range: {ldates[0]} -> {ldates[-1]}" if ldates else "    (none)")

    # Series enumeration sanity.
    try:
        series = enumerate_weather_series()
        print(f"\n[4] /series -> {len(series)} KXHIGH*/KXLOW* Climate/Weather series")
        mapped = {}
        for s in series:
            st = _ksm.resolve_ticker(s.get("ticker") or s.get("series_ticker") or "")
            if st:
                mapped.setdefault(st, []).append(s.get("ticker"))
        print(f"    -> {len(mapped)} map to settlement_epochs stations:")
        for st in sorted(mapped):
            print(f"       {st}: {mapped[st]}")
    except Exception as e:
        print(f"[warn] series enumeration failed: {e}")

    print("\n=== VERDICT ===")
    print("GET /historical/markets (unauthenticated) returns settled markets back to")
    print("AUGUST 2021, with strike_type/floor_strike/cap_strike embedded. This is the")
    print("correct endpoint. /markets provides the recent tail after the rolling")
    print("~2026-07-20 cutoff. No reconstruction, no third-party data, no auth needed.")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi historical-strike backfill (parallel, idempotent)")
    ap.add_argument("--probe", action="store_true", help="endpoint verdict + one-series sample (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="scope only, NO writes")
    ap.add_argument("--parallel", type=int, default=10, help="concurrent series workers (default 10)")
    ap.add_argument("--commit", action="store_true", help="opt-in: commit the SQLite writes (default: rollback)")
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
    print(f"resolved series mapping for {len(station_tickers)} stations:")
    for st in sorted(station_tickers):
        print(f"  {st}: {dict(station_tickers[st])}")

    # Build work items: (station, market_type, tickers)
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
            print(f"  {st}/{mt}: {len(rows)} strike rows")

    elapsed = time.time() - t0
    print(f"\nfetched {len(all_rows)} strike rows in {elapsed:.0f}s")

    # Filter to rows whose key actually exists in settlement_epochs (idempotent).
    rows = [r for r in all_rows if (r[0], r[1], r[2]) in existing_keys]
    print(f"  -> {len(rows)} rows map to existing settlement_epochs rows")

    if not rows:
        print("nothing to backfill")
        return 0

    # Connect (WAL, busy timeout), upsert, and DO NOT commit unless --commit.
    conn = sqlite3.connect(SE_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        n = upsert(conn, rows, args.dry_run)
        if args.dry_run:
            print(f"\n[dry-run] would backfill {n} settlement_epochs rows (no writes)")
            conn.close()
            return 0
        if args.commit:
            conn.commit()
            print(f"\ncommitted {n} strike backfills (--commit was set)")
        else:
            print(f"\nbackfilled {n} rows in-memory; NOT committed (--commit to persist)")
            print("rolling back on exit — no changes written to disk.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
