#!/usr/bin/env python3
"""
validate_kalshi_settlement_sources.py — Cross-validate finalized vs historical_api settlement data.

Checks 1-6:
  1. Overlap detection: find all (station, date) pairs in both finalized and historical_api
  2. Error distribution: |finalized - historical_api| for overlapping records
  3. If max error < 0.5°F across all checks → mark historical_api as "trusted"
  4. If any error > 1°F → investigate and exclude that market
  5. If median error > 0.5°F → historical_api cannot be relied upon
  6. Writes report to data/settlement_validation_report.json

Falls back to internal consistency checks when no overlap exists (current state).

Usage:
    python3 scripts/validate_kalshi_settlement_sources.py

B-Mode compliant. No AI/ML.
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "kalshi_settlements.db")
OUT_PATH = os.path.join(REPO_ROOT, "data", "settlement_validation_report.json")


def get_connection() -> sqlite3.Connection:
    """Get read-only connection to settlements DB."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def find_overlapping_markets(cur) -> List[dict]:
    """
    Find all (station, date, ticker) pairs present in both finalized and historical_api.
    
    Returns dicts with station, target_date, event_ticker, finalized_temp, historical_temp.
    """
    cur.execute("""
        SELECT f.station, f.target_date, f.event_ticker,
               f.kalshi_temp AS finalized_temp,
               h.kalshi_temp AS historical_temp
        FROM kalshi_settlements f
        JOIN kalshi_settlements h
          ON f.event_ticker = h.event_ticker
         AND f.station = h.station
         AND f.target_date = h.target_date
        WHERE f.source_type = 'finalized'
          AND h.source_type = 'historical_api'
          AND f.kalshi_temp IS NOT NULL
          AND h.kalshi_temp IS NOT NULL
        ORDER BY f.station, f.target_date
    """)
    rows = cur.fetchall()
    return [
        {
            "station": r[0],
            "target_date": r[1],
            "event_ticker": r[2],
            "finalized_temp": r[3],
            "historical_temp": r[4],
            "abs_error": abs(r[3] - r[4]),
        }
        for r in rows
    ]


def check_internal_consistency(cur) -> dict:
    """
    Internal consistency checks for when no overlap exists.
    Checks: temp range, station coverage, date range, duplicates, source consistency.
    """
    checks = {}

    # ── Temp range sanity ──
    cur.execute("SELECT MIN(kalshi_temp), MAX(kalshi_temp), AVG(kalshi_temp) FROM kalshi_settlements")
    tmin, tmax, tavg = cur.fetchone()
    checks["temperature_range"] = {"min": tmin, "max": tmax, "mean": tavg}
    checks["temp_outside_plausible"] = (tmin < -20 or tmax > 130)

    # ── Station coverage ──
    cur.execute("SELECT station, source_type, COUNT(*) FROM kalshi_settlements GROUP BY station, source_type")
    station_rows = cur.fetchall()
    station_coverage = defaultdict(lambda: defaultdict(int))
    for s, st, cnt in station_rows:
        station_coverage[s][st] = cnt
    
    all_stations = ["KATL","KAUS","KBOS","KDCA","KDEN","KDFW","KHOU","KLAS",
                     "KLAX","KMDW","KMIA","KMSP","KMSY","KNYC","KOKC","KPHL",
                     "KPHX","KSAT","KSEA","KSFO"]
    missing = [s for s in all_stations if s not in station_coverage]
    checks["station_coverage"] = {
        "present": list(station_coverage.keys()),
        "missing": missing,
        "total": len(station_coverage),
    }

    # ── Date range ──
    cur.execute("SELECT MIN(target_date), MAX(target_date) FROM kalshi_settlements")
    dmin, dmax = cur.fetchone()
    checks["date_range"] = {"min": dmin, "max": dmax}

    # ── Duplicate detection within historical_api ──
    cur.execute("""
        SELECT station, target_date, COUNT(*), GROUP_CONCAT(source)
        FROM kalshi_settlements
        WHERE source_type = 'historical_api'
        GROUP BY station, target_date
        HAVING COUNT(*) > 1
    """)
    dups = cur.fetchall()
    checks["duplicates_in_historical_api"] = len(dups)
    if dups:
        checks["duplicate_examples"] = [
            {"station": r[0], "date": r[1], "count": r[2], "sources": r[3]}
            for r in dups[:5]
        ]

    # ── Source breakdown ──
    cur.execute("SELECT source, COUNT(*) FROM kalshi_settlements WHERE source_type='historical_api' GROUP BY source")
    checks["historical_api_breakdown"] = dict(cur.fetchall())

    return checks


def compute_error_distribution(overlaps: List[dict]) -> dict:
    """Compute error distribution from overlapping records."""
    if not overlaps:
        return {
            "n_overlapping": 0,
            "max_error": None,
            "min_error": None,
            "mean_error": None,
            "median_error": None,
            "p25_error": None,
            "p75_error": None,
            "p95_error": None,
            "p99_error": None,
            "errors_all_below_0_5": None,
            "any_error_above_1": None,
        }

    errors = np.array([o["abs_error"] for o in overlaps])
    return {
        "n_overlapping": len(overlaps),
        "max_error": float(np.max(errors)),
        "min_error": float(np.min(errors)),
        "mean_error": float(np.mean(errors)),
        "median_error": float(np.median(errors)),
        "p25_error": float(np.percentile(errors, 25)),
        "p75_error": float(np.percentile(errors, 75)),
        "p95_error": float(np.percentile(errors, 95)),
        "p99_error": float(np.percentile(errors, 99)),
        "errors_all_below_0_5": bool(np.all(errors < 0.5)),
        "any_error_above_1": bool(np.any(errors > 1.0)),
    }


def determine_trust_status(overlaps: List[dict], internal: dict) -> dict:
    """Determine whether historical_api can be trusted."""
    issues = []

    # Overlap-based checks (gold standard)
    if overlaps:
        dist = compute_error_distribution(overlaps)
        max_err = dist["max_error"]
        median_err = dist["median_error"]

        if max_err is not None and max_err < 0.5:
            trust = "trusted"
            reason = f"Max error {max_err:.3f}°F < 0.5°F across {dist['n_overlapping']} overlapping markets"
        elif median_err is not None and median_err > 0.5:
            trust = "unreliable"
            reason = f"Median error {median_err:.3f}°F > 0.5°F — historical_api cannot be relied upon"
        elif dist.get("any_error_above_1"):
            trust = "issues_found"
            reason = f"Errors > 1°F detected (max={max_err:.2f}°F) — investigate excluded markets"
        else:
            trust = "partially_trusted"
            reason = f"Max error {max_err:.3f}°F ≥ 0.5 but median {median_err:.3f}°F ≤ 0.5"
    else:
        # No overlap — fall back to internal consistency
        if internal.get("temp_outside_plausible"):
            issues.append("temperatures outside plausible range")
        if internal["station_coverage"]["missing"]:
            issues.append(f"missing stations: {internal['station_coverage']['missing']}")
        if internal.get("duplicates_in_historical_api", 0) > 0:
            issues.append(f"{internal['duplicates_in_historical_api']} duplicate (station,date) pairs")

        if not issues:
            trust = "trusted_by_consistency"
            reason = "No overlap possible, but internal consistency checks all pass"
        else:
            trust = "issues_found"
            reason = f"No overlap; internal consistency issues: {'; '.join(issues)}"

    return {
        "status": trust,
        "reason": reason,
        "issues": issues,
    }


def compute_basic_counts(cur) -> dict:
    """Get basic record counts by source type."""
    cur.execute("SELECT source_type, COUNT(*) FROM kalshi_settlements GROUP BY source_type")
    return dict(cur.fetchall())


def main():
    print("=" * 72)
    print("  KALSHI SETTLEMENT SOURCE VALIDATION")
    print("  Cross-check: finalized vs historical_api")
    print("=" * 72)

    conn = get_connection()
    cur = conn.cursor()

    # 1. Basic counts
    counts = compute_basic_counts(cur)
    print(f"\n  RECORDS: finalized={counts.get('finalized', 0)}, "
          f"historical_api={counts.get('historical_api', 0)}")

    # 2. Find overlapping markets
    overlaps = find_overlapping_markets(cur)
    print(f"\n  OVERLAPPING MARKETS: {len(overlaps)}")

    if overlaps:
        # Compute error distribution from real overlap
        dist = compute_error_distribution(overlaps)
        print(f"  Error distribution across {dist['n_overlapping']} overlapping records:")
        print(f"    Max error:   {dist['max_error']:.4f}°F")
        print(f"    Min error:   {dist['min_error']:.4f}°F")
        print(f"    Mean error:  {dist['mean_error']:.4f}°F")
        print(f"    Median error:{dist['median_error']:.4f}°F")
        print(f"    P95 error:   {dist['p95_error']:.4f}°F")
        print(f"    All < 0.5°F: {dist['errors_all_below_0_5']}")
        print(f"    Any > 1.0°F: {dist['any_error_above_1']}")

        # Check 3: max error trust
        if dist["errors_all_below_0_5"]:
            print(f"\n  ✅ Check 3 PASS: max error {dist['max_error']:.4f}°F < 0.5°F")
        else:
            print(f"\n  ⚠️ Check 3 FAIL: max error {dist['max_error']:.4f}°F ≥ 0.5°F")

        # Check 4: investigate >1°F
        if dist["any_error_above_1"]:
            large_errors = [o for o in overlaps if o["abs_error"] > 1.0]
            print(f"\n  ❌ Check 4: {len(large_errors)} markets with error > 1°F")
            for le in large_errors[:5]:
                print(f"     {le['station']} {le['target_date']} ({le['event_ticker']}): "
                      f"finalized={le['finalized_temp']}, hist={le['historical_temp']}")

        # Check 5: median error
        median_err = dist["median_error"]
        if median_err > 0.5:
            print(f"\n  ❌ Check 5 FAIL: median error {median_err:.4f}°F > 0.5°F — cannot rely on historical_api")
        else:
            print(f"\n  ✅ Check 5 PASS: median error {median_err:.4f}°F ≤ 0.5°F")
    else:
        print("  ⚠️ No overlapping (station, date) pairs between finalized and historical_api.")
        print("  Direct cross-validation not possible. Running internal consistency checks.")

    # Internal consistency (always useful)
    print(f"\n  INTERNAL CONSISTENCY CHECKS:")
    internal = check_internal_consistency(cur)
    print(f"    Temperature range: {internal['temperature_range']['min']:.1f}°F to "
          f"{internal['temperature_range']['max']:.1f}°F")
    print(f"    Stations with data: {internal['station_coverage']['total']}/20")
    if internal["station_coverage"]["missing"]:
        print(f"    Missing stations: {internal['station_coverage']['missing']}")
    print(f"    Date range: {internal['date_range']['min']} to {internal['date_range']['max']}")
    print(f"    Duplicates in historical_api: {internal['duplicates_in_historical_api']}")
    print(f"    Source breakdown: {internal['historical_api_breakdown']}")

    # Determine trust status
    decision = determine_trust_status(overlaps, internal)
    print(f"\n  DECISION: {decision['status']}")
    print(f"    Reason: {decision['reason']}")

    conn.close()

    # Build report
    report = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "db_path": DB_PATH,
            "script": "validate_kalshi_settlement_sources.py",
        },
        "counts": counts,
        "overlap_analysis": {
            "n_overlapping": len(overlaps),
            "error_distribution": compute_error_distribution(overlaps) if overlaps else None,
            "overlapping_records": overlaps[:20] if overlaps else [],  # Keep size manageable
        },
        "internal_consistency": internal,
        "checks": {
            "check_1_max_error_below_0_5": {
                "pass": overlaps and all(o["abs_error"] < 0.5 for o in overlaps) if overlaps else None,
                "note": "No overlap data available" if not overlaps else None,
            },
            "check_2_any_error_above_1": {
                "pass": not (overlaps and any(o["abs_error"] > 1.0 for o in overlaps)) if overlaps else None,
                "note": "No overlap data available" if not overlaps else None,
            },
            "check_3_median_error_below_0_5": {
                "pass": overlaps and (np.median([o["abs_error"] for o in overlaps]) <= 0.5) if overlaps else None,
                "note": "No overlap data available" if not overlaps else None,
            },
        },
        "decision": decision,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved to: {OUT_PATH}")
    print("=" * 72)


if __name__ == "__main__":
    main()