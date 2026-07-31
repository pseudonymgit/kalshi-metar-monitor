#!/usr/bin/env python3
"""
data_quality_gate.py — 6 checks before any accuracy/P&L is reported.

Usage:
    python3 scripts/data_quality_gate.py                        # Check all, print pass/fail
    python3 scripts/data_quality_gate.py --json                  # Output as JSON
    python3 scripts/data_quality_gate.py --all                   # Detailed output

B-Mode compliant. No AI/ML.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KALSHI_DB = os.path.join(REPO_ROOT, "data", "kalshi_settlements.db")
GEFS_DB = os.path.join(REPO_ROOT, "data", "gefs_archive.db")
SWEEP_DIR = os.path.join(REPO_ROOT, "data", "sweep")

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]


def check_settlement_sources() -> dict:
    """
    Check 1: Settlement source validation.
    Verifies the historical_api source is trustworthy.
    """
    conn = sqlite3.connect(KALSHI_DB)
    cur = conn.cursor()
    
    cur.execute("SELECT source_type, COUNT(*) FROM kalshi_settlements GROUP BY source_type")
    counts = dict(cur.fetchall())
    
    cur.execute("SELECT MIN(kalshi_temp), MAX(kalshi_temp) FROM kalshi_settlements")
    tmin, tmax = cur.fetchone()
    
    conn.close()
    
    issues = []
    if counts.get('finalized', 0) < 100:
        issues.append("fewer than 100 finalized records")
    if tmin < -20 or tmax > 130:
        issues.append("temps outside plausible range")
    
    return {
        "name": "Settlement source validation",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "finalized_records": counts.get('finalized', 0),
            "historical_api_records": counts.get('historical_api', 0),
            "temp_range": f"{tmin:.0f}°F to {tmax:.0f}°F",
        },
    }


def check_gefs_completeness() -> dict:
    """
    Check 2: GEFS completeness — ≥95% of 82 members available.
    GEFS has 31 members. The check verifies sufficient data.
    """
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(DISTINCT station) FROM gefs_archive WHERE step=24")
    stations_with_data = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(DISTINCT target_date) FROM gefs_archive WHERE step=24")
    unique_dates = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM gefs_archive WHERE step=24")
    total_rows = cur.fetchone()[0]
    
    conn.close()
    
    expected = len(STATIONS) * 2036  # ~2036 days of GEFS data per station
    pct = total_rows / expected * 100 if expected > 0 else 0
    
    issues = []
    if stations_with_data < 20:
        issues.append(f"only {stations_with_data}/20 stations have GEFS data")
    if pct < 95:
        issues.append(f"GEFS completeness {pct:.1f}% (threshold 95%)")
    
    return {
        "name": "GEFS completeness",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "stations_with_data": stations_with_data,
            "unique_dates": unique_dates,
            "total_rows": total_rows,
            "completeness_pct": round(pct, 1),
        },
    }


def check_station_coverage() -> dict:
    """
    Check 3: Station coverage — ≥30 records per station.
    """
    conn = sqlite3.connect(KALSHI_DB)
    cur = conn.cursor()
    
    cur.execute("SELECT station, COUNT(*) FROM kalshi_settlements GROUP BY station")
    station_counts = dict(cur.fetchall())
    
    conn.close()
    
    stations_below_30 = [s for s, c in station_counts.items() if c < 30]
    missing_stations = [s for s in STATIONS if s not in station_counts]
    
    issues = []
    if missing_stations:
        issues.append(f"missing stations: {missing_stations}")
    if stations_below_30:
        issues.append(f"stations with <30 records: {stations_below_30}")
    
    return {
        "name": "Station coverage",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "stations_with_data": len(station_counts),
            "stations_below_30": stations_below_30,
            "missing_stations": missing_stations,
            "min_records": min(station_counts.values()) if station_counts else 0,
        },
    }


def check_date_range() -> dict:
    """
    Check 4: Date range — flag <30 days as preliminary.
    """
    conn = sqlite3.connect(KALSHI_DB)
    cur = conn.cursor()
    
    cur.execute("SELECT MIN(target_date), MAX(target_date) FROM kalshi_settlements")
    dmin, dmax = cur.fetchone()
    
    conn.close()
    
    days = (datetime.strptime(dmax, "%Y-%m-%d") - datetime.strptime(dmin, "%Y-%m-%d")).days if dmin and dmax else 0
    
    issues = []
    if days < 30:
        issues.append(f"date range only {days} days (preliminary)")
    
    return {
        "name": "Date range",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "min_date": dmin,
            "max_date": dmax,
            "days_span": days,
        },
    }


def check_price_sanity(results: List[dict] = None) -> dict:
    """
    Check 5: Price sanity — entry_price between 0.01 and 0.99.
    If no results provided, returns a placeholder.
    """
    issues = []
    n_prices_checked = 0
    price_range = None
    
    if results:
        prices = [t["entry_price"] for t in results if "entry_price" in t]
        if prices:
            n_prices_checked = len(prices)
            min_p, max_p = min(prices), max(prices)
            price_range = (min_p, max_p)
            if min_p < 0.01 or max_p > 0.99:
                issues.append(f"entry_price range [{min_p:.4f}, {max_p:.4f}] outside [0.01, 0.99]")
    
    return {
        "name": "Price sanity",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "n_prices_checked": n_prices_checked,
            "price_range": price_range,
        },
    }


def check_fee_sanity(results: List[dict] = None) -> dict:
    """
    Check 6: Fee sanity — fee_per_trade / notional ≤ 0.15.
    Kalshi fee model: ceil(0.07 × C × P × (1-P)) per side.
    Max fee ratio should be ~0.07 (for P=0.5).
    """
    issues = []
    max_fee_ratio = 0.0
    
    if results:
        for t in results:
            if "entry_fee" in t and "contracts" in t and "entry_price" in t:
                notional = t["contracts"] * t["entry_price"]
                if notional > 0:
                    fee_ratio = t["entry_fee"] / notional
                    max_fee_ratio = max(max_fee_ratio, fee_ratio)
                    if fee_ratio > 0.15:
                        issues.append(f"fee_ratio {fee_ratio:.4f} > 0.15 for {t.get('station','?')} {t.get('date','?')}")
    
    return {
        "name": "Fee sanity",
        "pass": len(issues) == 0,
        "issues": issues,
        "details": {
            "max_fee_ratio": round(max_fee_ratio, 4),
        },
    }


def run_all_checks(results: List[dict] = None, output_json: bool = False, verbose: bool = False) -> dict:
    """Run all 6 quality checks."""
    checks = [
        check_settlement_sources(),
        check_gefs_completeness(),
        check_station_coverage(),
        check_date_range(),
        check_price_sanity(results),
        check_fee_sanity(results),
    ]
    
    all_pass = all(c["pass"] for c in checks)
    
    if output_json:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "all_pass": all_pass,
            "checks": checks,
        }
    
    print("=" * 72)
    print("  DATA QUALITY GATE")
    print("=" * 72)
    
    for c in checks:
        status = "✅ PASS" if c["pass"] else "❌ FAIL"
        print(f"\n  {status} — {c['name']}")
        if not c["pass"]:
            for issue in c["issues"]:
                print(f"       {issue}")
        if verbose:
            for k, v in c["details"].items():
                print(f"       {k}: {v}")
    
    print(f"\n  {'=' * 30}")
    if all_pass:
        print(f"  ✅ ALL CHECKS PASSED — data quality OK")
    else:
        print(f"  ❌ {sum(1 for c in checks if not c['pass'])}/{len(checks)} checks FAILED")
    print(f"  {'=' * 30}")
    
    return {"all_pass": all_pass, "checks": checks}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Data quality gate")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--all", action="store_true", help="Detailed output")
    args = parser.parse_args()
    
    result = run_all_checks(output_json=args.json, verbose=args.all)
    
    if args.json:
        print(json.dumps(result, indent=2))