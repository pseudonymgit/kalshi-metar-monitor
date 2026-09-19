#!/usr/bin/env python3
"""
Pre-launch strike estimator — SIMULATED strike reconstruction for prediction-scoring.

Author: Gilfoyle (subagent), 2026-09-18.
Repo: prototypes/weather-engine-source (branch temp-sprint-branch, dirty tree preserved).

PURPOSE
-------
For the pre-launch window (station, date) where Kalshi's daily-temp series did not
yet exist, we have authoritative GHCN temperature truth (high/low, 1869→present) but
NO real strike list. To score whether a forecast would have crossed a strike, we need
a *plausible* strike. This script reconstructs that strike from climatology and flags
it provenance='simulated'. It is a SCORING PROXY ONLY — never authoritative, never P&L.

CORRECTED MODEL (see docs/plans/PRE-LAUNCH-STRIKE-ESTIMATION-2026-09-18.md)
--------------------------------------------------------------------------
Kalshi does NOT list a single round-number strike. It lists a LADDER of `greater`
strikes at a fixed 2°F grid (integer °F, 40→114). The engine (core/pnl_tracking.py
::_get_strike_price) collapses the ladder to ONE `greater` strike (HIGH) or ONE
`less` strike (LOW). That single strike sits at the station's ~50th-percentile of
daily high/low — the "CLIMO_ANCHOR" — and is recoverable from GHCN climatology.

So the estimation target is: the 2°F-grid point nearest the station's seasonal
climatological mean daily high (HIGH) / low (LOW).

ALGORITHM (deterministic, climatology-only, no live API)
--------------------------------------------------------
1. Climatology: from GHCN, compute the station's long-run mean daily high (HIGH)
   or low (LOW) for day-of-year ± a seasonal window (default ±15 days).
2. Grid snap: snap the mean to the 2°F grid (nearest even integer).
3. Percentile anchor (optional, preferred when available): if the station's
   strike_map.json entry exists, pick the strike whose empirical P(temp > strike)
   is closest to 0.5 (the CLIMO_ANCHOR) — corrects for skew.
4. Emit simulated_strike + confidence + provenance='simulated'. NEVER write into
   strike_price (reserved for real Kalshi strikes).

SAFETY (boundaries)
-------------------
* Read-only by default (--dry-run). --commit writes ONLY to a NEW table
  `pre_launch_simulated_strikes` (never touches strike_price on settlement_epochs).
* NO git commit, NO live trading, NO order endpoints, NO Kalshi auth.
* Preserves dirty tree.

USAGE
-----
  python3 scripts/pre_launch_strike_estimator.py --dry-run                # scope only
  python3 scripts/pre_launch_strike_estimator.py --backtest                # hold-out error
  python3 scripts/pre_launch_strike_estimator.py --commit                  # persist (opt-in)
  python3 scripts/pre_launch_strike_estimator.py --station KPHX --dry-run  # single station
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

SE_DB = os.environ.get("METAR_DB_PATH", os.path.join(DATA_DIR, "metar_backfill.db"))
GH_DB = os.path.join(DATA_DIR, "ghcn_settlements.db")
STRIKE_MAP = os.path.join(DATA_DIR, "strike_map.json")

GRID_STEP = 2          # Kalshi strike grid is 2°F (integer, even)
SEASONAL_WINDOW = 15   # ±days around day-of-year for climatological mean
MIN_GHCN_YEARS = 10    # minimum years of GHCN history required to trust a climatology


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _snap_to_grid(value: float) -> int:
    """Snap a temperature (°F) to the 2°F integer grid (nearest even integer)."""
    if value is None:
        return None
    return int(round(round(value) / GRID_STEP)) * GRID_STEP


def _load_launch_dates() -> dict:
    """station -> launch date (YYYY-MM-DD) from strike_map.json date_range start.

    Launch date == first date Kalshi actually listed that station's series. Dates
    BEFORE this are pre-launch (GHCN truth exists, Kalshi strike does not).
    """
    if not os.path.exists(STRIKE_MAP):
        return {}
    with open(STRIKE_MAP) as f:
        d = json.load(f)
    out = {}
    for st, s in d.get("stations", {}).items():
        dr = s.get("date_range", "")
        start = dr.split(" to ")[0].strip()
        if start:
            out[st] = start
    return out


def _load_climo_anchor() -> dict:
    """station -> CLIMO_ANCHOR strike (2°F grid point nearest 50th percentile).

    Returns {station: anchor_strike} where anchor is the strike whose empirical
    P(daily_high > strike) is closest to 0.5 in strike_map.json. This corrects
    for skew vs a raw-mean snap.
    """
    if not os.path.exists(STRIKE_MAP):
        return {}
    with open(STRIKE_MAP) as f:
        d = json.load(f)
    out = {}
    for st, s in d.get("stations", {}).items():
        strikes = s.get("strikes", {})
        if not strikes:
            continue
        # strike with win_rate closest to 0.5
        best_k = min(strikes.items(), key=lambda kv: abs(kv[1].get("win_rate", 0.0) - 0.5))[0]
        out[st] = int(best_k)
    return out


def _load_ghcn_climatology(station: str, market_type: str) -> dict:
    """{day_of_year: mean_high_or_low} from GHCN, averaged across all years.

    Returns mean daily high (HIGH) or low (LOW) per day-of-year (1..366), plus
    a count of years contributing, for confidence.
    """
    col = "high_f" if market_type == "HIGH" else "low_f"
    conn = sqlite3.connect(GH_DB)
    try:
        rows = conn.execute(
            f"SELECT date, {col} FROM ghcn_settlements WHERE station = ? AND {col} IS NOT NULL",
            (station,),
        ).fetchall()
    finally:
        conn.close()

    # Accumulate per day-of-year
    sums = defaultdict(float)
    counts = defaultdict(int)
    years = set()
    for date, val in rows:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        doy = dt.timetuple().tm_yday
        sums[doy] += float(val)
        counts[doy] += 1
        years.add(dt.year)

    climo = {doy: (sums[doy] / counts[doy]) for doy in sums}
    return climo, len(years)


def _doy_in_month(doy: int, months) -> bool:
    """Whether a day-of-year (1..366) falls in any of the given months (1..12).

    Uses a non-leap-year reference (Jan 1 = doy 1) so the mapping is stable.
    """
    try:
        dt = datetime(2001, 1, 1) + __import__("datetime").timedelta(days=doy - 1)
    except OverflowError:
        return False
    return dt.month in months


def _estimate_strike(station: str, market_type: str, date: str,
                     climo_cache: dict, climo_anchor: dict, launch_dates: dict):
    """Estimate the simulated strike for a (station, market_type, date).

    Returns (simulated_strike:int|None, confidence:str, method:str).
    None simulated_strike = cannot estimate (insufficient climatology).
    """
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None, "unestimable", "bad_date"
    doy = dt.timetuple().tm_yday

    # Cache climatology per (station, market_type)
    key = (station, market_type)
    if key not in climo_cache:
        climo_cache[key] = _load_ghcn_climatology(station, market_type)
    climo, n_years = climo_cache[key]

    if n_years < MIN_GHCN_YEARS:
        return None, "low", "insufficient_ghcn"

    # Seasonal window mean (handle year wrap)
    vals = []
    for offset in range(-SEASONAL_WINDOW, SEASONAL_WINDOW + 1):
        d = ((doy - 1 + offset) % 366) + 1
        if d in climo:
            vals.append(climo[d])
    if not vals:
        return None, "low", "no_seasonal_data"
    seasonal_mean = sum(vals) / len(vals)

    # NOTE: the strike_map.json "CLIMO_ANCHOR" is a season-aggregated artifact and
    # is NOT applied here as a blind per-day strike (it would emit a summer anchor
    # for January days — badly wrong). We use the season-aware mean-snap only.
    # The percentile_anchor path is intentionally disabled; see the plan doc §2.2.
    snapped = _snap_to_grid(seasonal_mean)
    return snapped, "medium", "seasonal_mean_snap"


# --------------------------------------------------------------------------- #
# Backtest (hold-out error quantification)
# --------------------------------------------------------------------------- #
def run_backtest(limit_station: str = None):
    """Quantify how often the simulated strike matches the real strike.

    For stations WITH real Kalshi strikes (strike_map.json), hold out the real
    CLIMO_ANCHOR and estimate it from GHCN climatology alone. Report:
      * value error (|sim - true| °F, and in grid rungs)
      * exact rung match rate
      * label error (fraction of settlement rows where win/loss flips)

    The anchor is SEASONAL: Kalshi's HIGH markets list warm-season (Apr–Sep) daily
    highs, so the 50th-percentile strike is the warm-season median daily high, not
    the annual mean (annual mean is biased low for continental stations).
    """
    climo_anchor = _load_climo_anchor()
    if not climo_anchor:
        print("No strike_map.json — cannot run backtest (no real strikes to hold out).")
        return

    results = []
    for station, true_anchor in sorted(climo_anchor.items()):
        if limit_station and station != limit_station:
            continue
        # Simulate "pre-launch": estimate the warm-season HIGH anchor from GHCN
        # climatology alone (no percentile anchor, no Kalshi data).
        climo, n_years = _load_ghcn_climatology(station, "HIGH")
        if n_years < MIN_GHCN_YEARS:
            continue
        # Warm-season (Apr–Sep) mean daily high = the season Kalshi lists HIGH for.
        warm_vals = [v for doy, v in climo.items()
                     if _doy_in_month(doy, (4, 5, 6, 7, 8, 9))]
        if not warm_vals:
            continue
        warm_mean = sum(warm_vals) / len(warm_vals)
        sim_anchor = _snap_to_grid(warm_mean)

        value_err = abs(sim_anchor - true_anchor)
        rung_err = value_err / GRID_STEP
        results.append({
            "station": station,
            "true_anchor": true_anchor,
            "sim_anchor": sim_anchor,
            "value_err_f": value_err,
            "rung_err": rung_err,
            "exact_match": value_err == 0,
        })

    # Summarize
    n = len(results)
    if n == 0:
        print("No backtestable stations.")
        return

    exact = sum(1 for r in results if r["exact_match"])
    within_1_rung = sum(1 for r in results if r["rung_err"] <= 1)
    mean_err = sum(r["value_err_f"] for r in results) / n

    print(f"\n=== HOLD-OUT BACKTEST (real CLIMO_ANCHOR vs GHCN-mean-snap) ===")
    print(f"stations tested: {n}")
    print(f"exact rung match:    {exact}/{n} ({100*exact/n:.1f}%)")
    print(f"within ±1 rung (2°F): {within_1_rung}/{n} ({100*within_1_rung/n:.1f}%)")
    print(f"mean |err|:           {mean_err:.2f}°F ({mean_err/GRID_STEP:.2f} rungs)")
    print()
    print(f"{'station':8s} {'true':>5s} {'sim':>5s} {'err°F':>6s} {'rungs':>6s}  match")
    for r in sorted(results, key=lambda x: -x["value_err_f"]):
        flag = "OK" if r["exact_match"] else ("±1" if r["rung_err"] <= 1 else "MISS")
        print(f"{r['station']:8s} {r['true_anchor']:>5d} {r['sim_anchor']:>5d} "
              f"{r['value_err_f']:>6.1f} {r['rung_err']:>6.2f}  {flag}")

    print()
    print("NOTE: this is VALUE error only. LABEL error (win/loss flip) is lower —")
    print("it depends on how often settlement temps land in the ±2°F gap between")
    print("the true and simulated rung. See the plan doc §4 for the full derivation.")
    return results


# --------------------------------------------------------------------------- #
# Main estimation (dry-run / commit)
# --------------------------------------------------------------------------- #
def enumerate_pre_launch_rows(limit_station: str = None):
    """Yield (station, market_type, date) rows in settlement_epochs that are pre-launch.

    Pre-launch = local_trading_date BEFORE the station's Kalshi launch date.
    """
    launch_dates = _load_launch_dates()
    conn = sqlite3.connect(SE_DB)
    try:
        q = """SELECT DISTINCT station, market_type, local_trading_date
               FROM settlement_epochs ORDER BY station, market_type, local_trading_date"""
        rows = conn.execute(q).fetchall()
    finally:
        conn.close()

    for station, market_type, date in rows:
        if limit_station and station != limit_station:
            continue
        if market_type not in ("HIGH", "LOW"):
            continue
        launch = launch_dates.get(station)
        if launch is None:
            # No launch date (retired station, or no strike_map entry) -> treat as
            # pre-launch only if we simply don't know; skip unknown to avoid guessing.
            continue
        if date < launch:
            yield station, market_type, date


def run_estimate(limit_station: str = None, commit: bool = False):
    """Generate simulated strikes for all pre-launch rows."""
    launch_dates = _load_launch_dates()
    climo_anchor = _load_climo_anchor()
    climo_cache = {}

    out_rows = []
    n_estimable = 0
    n_skipped = 0

    for station, market_type, date in enumerate_pre_launch_rows(limit_station):
        sim_strike, conf, method = _estimate_strike(
            station, market_type, date, climo_cache, climo_anchor, launch_dates
        )
        if sim_strike is None:
            n_skipped += 1
            continue
        n_estimable += 1
        out_rows.append({
            "station": station,
            "market_type": market_type,
            "local_trading_date": date,
            "simulated_strike": sim_strike,
            "confidence": conf,
            "method": method,
            "provenance": "simulated",
        })

    print(f"\n=== PRE-LAUNCH SIMULATED STRIKE ESTIMATION ===")
    print(f"launch dates loaded: {len(launch_dates)} stations")
    print(f"percentile anchors available: {len(climo_anchor)} stations")
    print(f"pre-launch rows estimable: {n_estimable}")
    print(f"pre-launch rows skipped (insufficient GHCN): {n_skipped}")

    if commit:
        _persist(out_rows)
        print(f"persisted {len(out_rows)} rows to pre_launch_simulated_strikes")
    else:
        print("\n[dry-run] sample of estimated rows (first 20):")
        for r in out_rows[:20]:
            print(f"  {r['station']:6s} {r['market_type']:4s} {r['local_trading_date']}  "
                  f"sim_strike={r['simulated_strike']:>4}  conf={r['confidence']}  "
                  f"method={r['method']}  provenance={r['provenance']}")
        print("\n[dry-run] no writes performed. Use --commit to persist (opt-in).")

    return out_rows


def _persist(rows):
    """Write simulated strikes to a NEW table (never touches strike_price)."""
    conn = sqlite3.connect(SE_DB)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pre_launch_simulated_strikes (
                station TEXT NOT NULL,
                market_type TEXT NOT NULL,
                local_trading_date TEXT NOT NULL,
                simulated_strike INTEGER NOT NULL,
                confidence TEXT,
                method TEXT,
                provenance TEXT NOT NULL DEFAULT 'simulated',
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (station, market_type, local_trading_date)
            )
        """)
        conn.executemany("""
            INSERT OR REPLACE INTO pre_launch_simulated_strikes
            (station, market_type, local_trading_date, simulated_strike, confidence, method, provenance)
            VALUES (:station, :market_type, :local_trading_date, :simulated_strike,
                    :confidence, :method, :provenance)
        """, rows)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Pre-launch simulated strike estimator (scoring proxy only).")
    ap.add_argument("--dry-run", action="store_true", help="scope + sample only, no writes (default)")
    ap.add_argument("--commit", action="store_true", help="persist simulated strikes to a NEW table (opt-in)")
    ap.add_argument("--backtest", action="store_true", help="hold-out error quantification")
    ap.add_argument("--station", default=None, help="limit to a single station (e.g. KPHX)")
    args = ap.parse_args()

    if args.backtest:
        run_backtest(limit_station=args.station)
        return

    run_estimate(limit_station=args.station, commit=args.commit)


if __name__ == "__main__":
    main()
