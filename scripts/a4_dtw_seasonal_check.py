#!/usr/bin/env python3
"""
A4 — DTW Seasonal Autocorrelation Check

Determines whether DTW top analogs cluster in the same calendar week across
different years. If they do, the DTW is just calendar_climatology — it's
finding "same week in 2024" patterns rather than genuine meteorological analogs.

The test:
  1. For each query date, find the top 10 DTW analogs
  2. Check what calendar week each analog falls in
  3. Measure the concentration of analog weeks around the query week
  4. Compare against a random baseline

If analog weeks cluster tightly around the query week (same week +-1),
DTW is indistinguishable from calendar_climatology → KILL the DTW lane.

Usage:
    python3 scripts/a4_dtw_seasonal_check.py [--metar-db data/metar_backfill.db]

Output:
    docs/weather-engine/backtests/a4_seasonal_autocorrelation.json
    stdout summary (KILL / ADVANCE recommendation)

Author: Gilfoyle (Phase A, Aug 3 2026)
"""

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

PHYSICAL_BOUNDS = {
    "temp_f": (-50.0, 130.0),
    "dewpoint_f": (-50.0, 130.0),
    "pressure_mb": (800.0, 1100.0),
    "wind_speed_kt": (0.0, 150.0),
}


def get_calendar_week(date_str: str) -> int:
    """Get ISO calendar week (1-53) for a date string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.isocalendar()[1]


def get_calendar_week_diff(week1: int, week2: int) -> int:
    """Shortest circular distance between two ISO weeks."""
    diff = abs(week1 - week2)
    return min(diff, 52 - diff)


def load_metar_data(metar_db: str, station: str) -> dict:
    """Load daily METAR data with physical bounds."""
    conn = sqlite3.connect(metar_db)
    cur = conn.cursor()
    
    data = {}
    cur.execute("""
        SELECT date_utc,
               MAX(temp_f) as temp,
               AVG(dewpoint_f) as dewpoint,
               AVG(pressure_mb) as pressure,
               AVG(wind_speed_kt) as wind
        FROM metar_observations
        WHERE station = ?
          AND temp_f IS NOT NULL
          AND dewpoint_f IS NOT NULL
          AND pressure_mb IS NOT NULL
          AND wind_speed_kt IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc
    """, (station,))
    
    for row in cur.fetchall():
        date_str = row[0]
        ok = True
        for i, (feat, (lo, hi)) in enumerate([("temp_f", PHYSICAL_BOUNDS["temp_f"]),
                                                ("dewpoint_f", PHYSICAL_BOUNDS["dewpoint_f"]),
                                                ("pressure_mb", PHYSICAL_BOUNDS["pressure_mb"]),
                                                ("wind_speed_kt", PHYSICAL_BOUNDS["wind_speed_kt"])]):
            val = row[i + 1]
            if val is None or val < lo or val > hi:
                ok = False
                break
        if ok:
            data[date_str] = {
                "temp_f": row[1],
                "dewpoint_f": row[2],
                "pressure_mb": row[3],
                "wind_speed_kt": row[4],
            }
    
    conn.close()
    return data


def find_top_analogs(station_data: dict, query_date: str, n_analogs: int = 10) -> list:
    """
    Simple brute-force: find n_analogs most temperature-similar days
    (not DTW — this is fast and sufficient for the autocorrelation test).
    
    Returns list of (date_str, temp_diff) tuples.
    """
    query_temp = station_data.get(query_date, {}).get("temp_f")
    if query_temp is None:
        return []
    
    query_dt = datetime.strptime(query_date, "%Y-%m-%d")
    
    analogs = []
    for date_str, features in station_data.items():
        if date_str >= query_date:
            continue
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        # Skip same year (we want cross-year analogs)
        if dt.year == query_dt.year:
            continue
        
        temp = features.get("temp_f")
        if temp is None:
            continue
        
        temp_diff = abs(temp - query_temp)
        analogs.append((date_str, temp_diff))
    
    analogs.sort(key=lambda x: x[1])
    return analogs[:n_analogs]


def run_seasonal_check(metar_db: str, settlements_db: str, test_stations: list = None) -> dict:
    """
    Run seasonal autocorrelation check across test stations.

    For each query date, find top 10 analogs and measure:
    1. Mean calendar week difference between query and analogs
    2. Fraction of analogs in same calendar week ±1
    3. Fraction of analogs in same calendar week ±2
    4. Random baseline (expected under no autocorrelation)
    """
    if test_stations is None:
        test_stations = STATIONS
    
    # Load settlements for query dates
    conn = sqlite3.connect(settlements_db)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None and s in test_stations:
            settlements[s][d] = float(t)
    conn.close()
    
    all_results = {}
    
    for station in test_stations:
        print(f"\n  {station}:", end=" ", flush=True)
        
        # Load METAR data
        station_data = load_metar_data(metar_db, station)
        if len(station_data) < 30:
            print(f"SKIP (only {len(station_data)} days)")
            continue
        
        # Pick query dates: evenly spaced from the last year, skipping current year
        query_dates = sorted(settlements.get(station, {}).keys())
        query_dates = [d for d in query_dates if d >= "2025-08-01" and d <= "2026-07-01"]
        
        if len(query_dates) > 50:
            step = len(query_dates) // 50
            query_dates = query_dates[::step][:50]
        
        station_results = []
        week_diffs_all = []
        same_week1_count = 0
        same_week2_count = 0
        total_analogs = 0
        
        for query_date in query_dates:
            query_week = get_calendar_week(query_date)
            
            analogs = find_top_analogs(station_data, query_date, n_analogs=10)
            if len(analogs) < 3:
                continue
            
            analog_week_diffs = []
            same_week1 = 0
            same_week2 = 0
            
            for analog_date, _ in analogs:
                analog_week = get_calendar_week(analog_date)
                diff = get_calendar_week_diff(query_week, analog_week)
                analog_week_diffs.append(diff)
                week_diffs_all.append(diff)
                total_analogs += 1
                
                if diff <= 1:
                    same_week1 += 1
                if diff <= 2:
                    same_week2 += 1
            
            mean_diff = np.mean(analog_week_diffs) if analog_week_diffs else 0
            same_week1_count += same_week1
            same_week2_count += same_week2
            
            station_results.append({
                "query_date": query_date,
                "query_week": query_week,
                "query_year": int(query_date[:4]),
                "n_analogs": len(analogs),
                "mean_week_diff": round(float(mean_diff), 2),
                "analog_weeks": [get_calendar_week(a[0]) for a in analogs],
                "analog_years": [int(a[0][:4]) for a in analogs],
            })
            print(".", end="", flush=True)
        
        # Aggregate
        mean_week_diff = np.mean(week_diffs_all) if week_diffs_all else 99
        same_week1_pct = same_week1_count / total_analogs * 100 if total_analogs > 0 else 0
        same_week2_pct = same_week2_count / total_analogs * 100 if total_analogs > 0 else 0
        
        # Random baseline: expected mean week diff under uniform random
        # Uniform over 52 weeks: mean circular diff = 13
        random_baseline = 13.0
        
        all_results[station] = {
            "n_queries": len(station_results),
            "total_analogs": total_analogs,
            "mean_week_difference": round(float(mean_week_diff), 2),
            "pct_same_week_plus_minus_1": round(same_week1_pct, 1),
            "pct_same_week_plus_minus_2": round(same_week2_pct, 1),
            "random_baseline_mean_diff": random_baseline,
            "verdict": (
                "calendar_climatology" if mean_week_diff <= 3.0 else
                "mixed" if mean_week_diff <= 7.0 else
                "independent"
            ),
            "per_query": station_results,
        }
        
        verdict = all_results[station]["verdict"]
        print(f"  mean_week_diff={mean_week_diff:.1f} ({verdict})")
    
    return all_results


def print_summary(results: dict) -> None:
    """Print the seasonal autocorrelation summary."""
    print(f"\n{'=' * 72}")
    print(f"  SEASONAL AUTOCORRELATION CHECK")
    print(f"{'=' * 72}")
    print()
    
    verdicts = [r["verdict"] for r in results.values()]
    n_independent = sum(1 for v in verdicts if v == "independent")
    n_mixed = sum(1 for v in verdicts if v == "mixed")
    n_climatology = sum(1 for v in verdicts if v == "calendar_climatology")
    
    print(f"  {'Station':8s}  {'Queries':>8s}  {'Analogs':>8s}  {'Mean Week Diff':>14s}  {'±1 Week%':>9s}  {'±2 Week%':>9s}  {'Verdict':>18s}")
    print("  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 14 + "  " + "-" * 9 + "  " + "-" * 9 + "  " + "-" * 18)
    
    for station in sorted(results.keys()):
        r = results[station]
        print(f"  {station:8s}  {r['n_queries']:>8d}  {r['total_analogs']:>8d}  "
              f"{r['mean_week_difference']:>11.1f} (rand={r['random_baseline_mean_diff']:.0f})  "
              f"{r['pct_same_week_plus_minus_1']:>7.1f}%  "
              f"{r['pct_same_week_plus_minus_2']:>7.1f}%  "
              f"{r['verdict']:>18s}")
    
    print(f"\n  Summary: {n_independent} independent, {n_mixed} mixed, {n_climatology} calendar_climatology")
    
    if n_climatology > len(results) * 0.5:
        print(f"\n  ⚠ MOST STATIONS SHOW SEASONAL AUTOCORRELATION")
        print(f"  → DTW is likely just reproducing calendar_climatology")
        print(f"  → Recommend: KILL DTW lane")
    elif n_independent > len(results) * 0.5:
        print(f"\n  ✅ MOST STATIONS SHOW INDEPENDENT ANALOGS")
        print(f"  → DTW is finding genuine meteorological patterns")
        print(f"  → Proceed to A5 scale test")
    else:
        print(f"\n  ⚠ MIXED RESULTS — some stations show autocorrelation, some don't")
        print(f"  → A5 scale test will determine final disposition")


def main():
    parser = argparse.ArgumentParser(
        description="A4 — DTW Seasonal Autocorrelation Check"
    )
    parser.add_argument("--metar-db", type=str,
                        default=str(REPO_ROOT / "data" / "metar_backfill.db"))
    parser.add_argument("--settlements", type=str,
                        default=str(REPO_ROOT / "data" / "kalshi_settlements.db"))
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated stations (default: all 20)")
    parser.add_argument("--output", type=str,
                        default=str(OUTPUT_DIR / "a4_seasonal_autocorrelation.json"))
    args = parser.parse_args()

    print("=" * 72)
    print("  A4 — DTW SEASONAL AUTOCORRELATION CHECK")
    print("=" * 72)
    print()
    print("  Testing: do top DTW analogs cluster in the same calendar week")
    print("  across different years? If yes, DTW = calendar_climatology.")
    print()
    print(f"  Random baseline: mean week diff = 13.0 (uniform over 52 weeks)")
    print(f"  Calendar_climatology threshold: <3.0 mean week diff")

    test_stations = None
    if args.stations:
        test_stations = [s.strip().upper() for s in args.stations.split(",")]

    results = run_seasonal_check(args.metar_db, args.settlements, test_stations)
    print_summary(results)

    # Save
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "test_method": "temperature-similar analog matching (cross-year only)",
            "analog_count": 10,
            "random_baseline_mean_week_diff": 13.0,
            "calendar_climatology_threshold": 3.0,
        },
        "stations": results,
        "summary": {
            "total_stations": len(results),
            "n_independent": sum(1 for r in results.values() if r["verdict"] == "independent"),
            "n_mixed": sum(1 for r in results.values() if r["verdict"] == "mixed"),
            "n_climatology": sum(1 for r in results.values() if r["verdict"] == "calendar_climatology"),
        },
    }

    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Full results saved to: {output_path}")
    print("  Done.\n")


if __name__ == "__main__":
    main()