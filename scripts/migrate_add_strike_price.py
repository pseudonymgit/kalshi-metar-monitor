#!/usr/bin/env python3
"""
Reconciliation-aware settlement-truth + strike backfill (Priority 0, 2026-09-18).

Ground-truth correction, v4 — DATE-TIERED. Reconciled against S5-SOURCE-RECONCILIATION
and Dan's refinements (anchor to authoritative weather truth + close the recency gap).

Dan's model — TWO distinct axes (keep separate) + DATE-TIERED ground truth:
  1. PREDICTION-ACCURACY axis (the strike-crossing LABEL) — DATE-TIERED source ladder:
     * Deep history   ( -> 2026-09-12): GHCN daily high/low (authoritative weather truth)
     * Confirmed-recent (9/13 -> 9/16): Kalshi finalized kalshi_temp (economic truth)
     * Freshest        (9/13 -> now) : WeatherAPI max_temp_f (recency bridge, NOT authoritative)
  2. P&L axis (did the trade pay out): Kalshi source_type='finalized' (separate column).

Why date-tiered: GHCN has a 6-day NOAA lag (latest 09-12), Kalshi finalized a 2-day
lag (latest 09-16), WeatherAPI is current through today (09-18). Anchoring to GHCN
alone leaves the newest markets (settling 9/13 -> now) with NO ground truth.

Caveat: WeatherAPI is commercial re-analysis and won't exactly match Kalshi's
settlement source; it bridges the recency gap only and is reconciled later when
GHCN catches up.

  Strike (from core/market_monitor.py _market_strike_value): floor_strike (greater),
    cap_strike (less), floor/cap midpoint (between). Persisted in market_snapshots.

What this script does (idempotent, non-destructive, no commit):
  1. Adds strike_price REAL + strike_type TEXT to settlement_epochs (additive).
  2. Builds settlement_truth side table (date-tiered ground truth + P&L axis).
  3. Backfills settlement_epochs.strike_price/strike_type where a real strike exists.

Flagging: 2026-05-28 and 2026-08-10 -> trustworthy=0 (GHCN-vs-Kalshi disagreement).
settlement_bucket (METAR proxy) is NEVER used as truth.

Usage:
  python3 scripts/migrate_add_strike_price.py [--dry-run]
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(CORE_DIR, "data")

SE_DB = os.environ.get("METAR_DB_PATH", os.path.join(DATA_DIR, "metar_backfill.db"))
KS_DB = os.path.join(DATA_DIR, "kalshi_settlements.db")
GH_DB = os.path.join(DATA_DIR, "ghcn_settlements.db")
WA_DB = os.path.join(DATA_DIR, "weatherapi_archive.db")
SNAP_DB = os.path.join(DATA_DIR, "kalshi_market_snapshots.db")

# Date-tier boundaries (Dan's refinement, 2026-09-18). Source coverage:
#   GHCN        -> 2026-09-12 (6-day NOAA lag)     — authoritative weather truth
#   Kalshi fin  -> 2026-09-16 (2-day lag)          — economic truth (P&L axis)
#   WeatherAPI  -> 2026-09-18 (today, 20 stns)      — best-available recency bridge
GHCN_MAX_DATE = "2026-09-12"    # last date GHCN is authoritative
KALSHI_MAX_DATE = "2026-09-16"  # last date Kalshi finalized is available

# S5-flagged untrustworthy dates (Kalshi finalized ~20-30 F cooler than weather truth).
UNTRUSTWORTHY_DATES = {"2026-05-28", "2026-08-10"}

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-([TB])(\d+(?:\.\d+)?)")


def load_finalized_temp():
    """{(station, market_type, date): temp_f} from kalshi_settlements.finalized."""
    out = {}
    if not os.path.exists(KS_DB):
        return out
    conn = sqlite3.connect(KS_DB)
    try:
        rows = conn.execute(
            """SELECT station, series, target_date, kalshi_temp FROM kalshi_settlements
               WHERE source_type='finalized' AND kalshi_temp IS NOT NULL"""
        ).fetchall()
    finally:
        conn.close()
    for station, series, date, temp in rows:
        market_type = "HIGH" if "HIGH" in (series or "") else "LOW"
        out[(station, market_type, date)] = float(temp)
    return out


def load_ghcn_temp():
    """{(station, market_type, date): temp_f} from ghcn_settlements (high_f for HIGH, low_f for LOW)."""
    out = {}
    if not os.path.exists(GH_DB):
        return out
    conn = sqlite3.connect(GH_DB)
    try:
        rows = conn.execute("SELECT station, date, high_f, low_f FROM ghcn_settlements").fetchall()
    finally:
        conn.close()
    for station, date, high, low in rows:
        if high is not None:
            out[(station, "HIGH", date)] = float(high)
        if low is not None:
            out[(station, "LOW", date)] = float(low)
    return out


def load_weatherapi_temp():
    """{(station, market_type, date): temp_f} from weatherapi_archive.daily (recency bridge).

    WeatherAPI is commercial re-analysis, NOT authoritative — it bridges the
    recency gap (9/13 -> now) until GHCN catches up. It will not exactly match
    Kalshi's settlement source; treat as best-available, reconcile later.
    """
    out = {}
    if not os.path.exists(WA_DB):
        return out
    conn = sqlite3.connect(WA_DB)
    try:
        rows = conn.execute(
            "SELECT station, date, max_temp_f, min_temp_f FROM daily"
        ).fetchall()
    finally:
        conn.close()
    for station, date, maxf, minf in rows:
        if maxf is not None:
            out[(station, "HIGH", date)] = float(maxf)
        if minf is not None:
            out[(station, "LOW", date)] = float(minf)
    return out


def load_strikes():
    """{(station, market_type, date): (strike, strike_type)} from market_snapshots.

    Uses the market_monitor extraction semantics: for a HIGH series, the binary
    contract is `greater` (floor_strike); for LOW, `less` (cap_strike); `between`
    is a bin. The ticker prefix KXHIGH/KXLOW determines series; the `-T`/`-B`
    suffix determines strike kind. We only persist the binary strike the engine
    trades (greater for HIGH, less for LOW).
    """
    binary = defaultdict(list)  # (station, series, date) -> [strike]
    between = defaultdict(list)
    if not os.path.exists(SNAP_DB):
        return {}
    conn = sqlite3.connect(SNAP_DB)
    try:
        rows = conn.execute(
            "SELECT station, market_ticker, strike_temp FROM market_snapshots WHERE strike_temp IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    for station, ticker, strike in rows:
        m = _TICKER_RE.search(ticker or "")
        if not m:
            continue
        yr, mon, day, kind, _ = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        date = f"20{yr}-{_MONTHS[mon]:02d}-{int(day):02d}"
        series = "HIGH" if "HIGH" in ticker else "LOW"
        if kind == "T":
            binary[(station, series, date)].append(float(strike))
        else:
            between[(station, series, date)].append(float(strike))

    strikes = {}
    for (station, series, date), vals in binary.items():
        vals = sorted(vals)
        # Representative binary strike = median rung of the greater/less ladder.
        strikes[(station, series, date)] = (vals[len(vals) // 2],
                                           "greater" if series == "HIGH" else "less")
    for (station, series, date), vals in between.items():
        if (station, series, date) not in strikes:
            vals = sorted(vals)
            strikes[(station, series, date)] = (vals[len(vals) // 2], "between")
    return strikes


def load_epoch_strikes(conn):
    """{(station, market_type, date): (strike_price, strike_type)} already persisted in

    settlement_epochs. This is the AUTHORITATIVE strike source: it reflects the real
    Kalshi /historical/markets backfill (14k+ rows), which is strictly richer than the
    market_snapshots-derived map (dozens of rows). The stale market_snapshots/strike_map
    path must NEVER overwrite these — a hard data-safety gate.
    """
    out = {}
    rows = conn.execute(
        "SELECT station, market_type, local_trading_date, strike_price, strike_type "
        "FROM settlement_epochs WHERE strike_price IS NOT NULL"
    ).fetchall()
    for station, market_type, date, strike, strike_type in rows:
        out[(station, market_type, date)] = (strike, strike_type)
    return out


def add_columns(conn, dry_run):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settlement_epochs)")}
    stmts = []
    if "strike_price" not in cols:
        stmts.append("ALTER TABLE settlement_epochs ADD COLUMN strike_price REAL")
    if "strike_type" not in cols:
        stmts.append("ALTER TABLE settlement_epochs ADD COLUMN strike_type TEXT")
    for s in stmts:
        if dry_run:
            print(f"[dry-run] {s}")
        else:
            conn.execute(s)
    return stmts


def build_truth_table(conn, finalized, ghcn, weatherapi, strikes, epoch_strikes, dry_run):
    """Create/replace settlement_truth with the DATE-TIERED truth join.

    Dan's refined model (2026-09-18): the strike-crossing ground truth is DATE-TIERED.
    Two axes stay distinct:
      1. PREDICTION-ACCURACY axis (the strike-crossing LABEL) — source is DATE-TIERED:
         * Deep history  ( -> 2026-09-12): GHCN daily high/low (authoritative weather)
         * Confirmed-rec (9/13 -> 9/16): Kalshi finalized kalshi_temp (economic truth)
         * Freshest      (9/13 -> now):  WeatherAPI max_temp_f (recency bridge, NOT authoritative)
      2. P&L axis                         -> Kalshi source_type='finalized' (separate column).

    Columns:
      settlement_temp_f   -> date-tiered ground truth (the label source)
      temp_source         -> 'ghcn' | 'kalshi_finalized' | 'weatherapi' | NULL
      kalshi_temp_f       -> Kalshi finalized temp (P&L axis, separate, never the label)
      strike_price/type   -> from market_monitor extraction
      trustworthy         -> 0 for disagreement dates / missing temp / recency-bridge caveat
    """
    create_sql = """
    CREATE TABLE IF NOT EXISTS settlement_truth (
        station TEXT NOT NULL,
        market_type TEXT NOT NULL,
        local_trading_date TEXT NOT NULL,
        settlement_temp_f REAL,     -- date-tiered ground truth (GHCN/Kalshi/WeatherAPI)
        temp_source TEXT,           -- 'ghcn' | 'kalshi_finalized' | 'weatherapi' | NULL
        kalshi_temp_f REAL,         -- Kalshi finalized temp (P&L axis only, NOT the label)
        strike_price REAL,
        strike_type TEXT,           -- 'greater' | 'less' | 'between' | NULL
        trustworthy INTEGER,        -- 0 for disagreement dates / missing temp
        PRIMARY KEY (station, market_type, local_trading_date)
    )
    """
    if dry_run:
        print("[dry-run] CREATE settlement_truth (side table)")
        return 0
    conn.execute("DROP TABLE IF EXISTS settlement_truth")
    conn.execute(create_sql)

    # Union of keys from all sources.
    # Strike resolution is DATE-INDEPENDENT and NON-DESTRUCTIVE: the richest strike
    # source (settlement_epochs /historical backfill) always wins over the stale
    # market_snapshots map. This is the hard gate that prevents strike regression.
    merged_strikes = dict(strikes)  # market_snapshots (sparse, stale)
    merged_strikes.update(epoch_strikes)  # /historical backfill (rich, authoritative)
    keys = set(finalized) | set(ghcn) | set(weatherapi) | set(merged_strikes)
    rows = []
    for (station, market_type, date) in keys:
        ghcn_temp = ghcn.get((station, market_type, date))
        kalshi_temp = finalized.get((station, market_type, date))
        wa_temp = weatherapi.get((station, market_type, date))

        # DATE-TIERED ground truth for the prediction-accuracy label.
        if date <= GHCN_MAX_DATE and ghcn_temp is not None:
            temp = ghcn_temp
            temp_source = "ghcn"
        elif date <= KALSHI_MAX_DATE and kalshi_temp is not None:
            temp = kalshi_temp
            temp_source = "kalshi_finalized"
        elif wa_temp is not None:
            temp = wa_temp
            temp_source = "weatherapi"
        elif ghcn_temp is not None:
            temp = ghcn_temp
            temp_source = "ghcn"
        elif kalshi_temp is not None:
            temp = kalshi_temp
            temp_source = "kalshi_finalized"
        else:
            temp = None
            temp_source = None

        strike_entry = merged_strikes.get((station, market_type, date))
        strike = strike_entry[0] if strike_entry else None
        strike_type = strike_entry[1] if strike_entry else None
        trustworthy = 1 if (temp is not None and date not in UNTRUSTWORTHY_DATES) else 0
        rows.append((station, market_type, date, temp, temp_source, kalshi_temp,
                     strike, strike_type, trustworthy))

    conn.executemany(
        """INSERT OR REPLACE INTO settlement_truth
           (station, market_type, local_trading_date, settlement_temp_f,
            temp_source, kalshi_temp_f, strike_price, strike_type, trustworthy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def backfill_epochs(conn, strikes, dry_run):
    """Backfill settlement_epochs.strike_price/strike_type from the strike map.

    NON-DESTRUCTIVE (hard gate): only fills rows where strike_price IS NULL. Existing
    non-null strikes (the real /historical backfill) are NEVER overwritten, so a stale
    strike map can never regress coverage.
    """
    cur = conn.execute(
        "SELECT id, station, market_type, local_trading_date, strike_price "
        "FROM settlement_epochs"
    )
    updates = []
    for row_id, station, market_type, date, existing in cur.fetchall():
        if existing is not None:
            continue  # preserve real backfill — never clobber
        key = (station, market_type, date)
        if key in strikes:
            strike, strike_type = strikes[key]
            updates.append((strike, strike_type, row_id))
    if dry_run:
        print(f"[dry-run] would update {len(updates)} settlement_epochs rows")
        return len(updates)
    conn.executemany(
        "UPDATE settlement_epochs SET strike_price=?, strike_type=? WHERE id=?", updates
    )
    conn.commit()
    return len(updates)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    finalized = load_finalized_temp()
    ghcn = load_ghcn_temp()
    weatherapi = load_weatherapi_temp()
    strikes = load_strikes()

    if not os.path.exists(SE_DB):
        print(f"settlement_epochs DB not found: {SE_DB}")
        sys.exit(1)

    conn = sqlite3.connect(SE_DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")

    # Authoritative strike source: existing settlement_epochs backfill (14k+ rows).
    # This is richer than market_snapshots/strike_map and must never be clobbered.
    epoch_strikes = load_epoch_strikes(conn)

    added = add_columns(conn, args.dry_run)
    n_truth = build_truth_table(conn, finalized, ghcn, weatherapi, strikes,
                                epoch_strikes, args.dry_run)
    n_epochs = backfill_epochs(conn, strikes, args.dry_run)

    if not args.dry_run:
        with_strike = conn.execute(
            "SELECT COUNT(*) FROM settlement_truth WHERE strike_price IS NOT NULL"
        ).fetchone()[0]
        by_tier = {}
        for src in ("ghcn", "kalshi_finalized", "weatherapi"):
            by_tier[src] = conn.execute(
                "SELECT COUNT(*) FROM settlement_truth WHERE temp_source=?", (src,)
            ).fetchone()[0]
        untrust = conn.execute(
            "SELECT COUNT(*) FROM settlement_truth WHERE trustworthy=0"
        ).fetchone()[0]
        conn.close()
        print(f"added columns: {len(added)}")
        print(f"settlement_truth rows: {n_truth}")
        print(f"  temp_source=ghcn (deep history -> {GHCN_MAX_DATE}): {by_tier['ghcn']}")
        print(f"  temp_source=kalshi_finalized ({GHCN_MAX_DATE} -> {KALSHI_MAX_DATE}): {by_tier['kalshi_finalized']}")
        print(f"  temp_source=weatherapi ({KALSHI_MAX_DATE} -> now, recency bridge): {by_tier['weatherapi']}")
        print(f"  with strike: {with_strike}")
        print(f"  untrustworthy (flagged): {untrust}")
        print(f"settlement_epochs rows backfilled w/ strike: {n_epochs}")
    else:
        conn.close()
        print(f"[dry-run] added columns: {len(added)}; truth rows: {n_truth}; epoch backfill: {n_epochs}")


if __name__ == "__main__":
    main()
