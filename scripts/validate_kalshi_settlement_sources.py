#!/usr/bin/env python3
"""
validate_kalshi_settlement_sources.py — Cross-reference and validate Kalshi settlement data.

The kalshi_settlements.db has 3 source types:
- finalized: 599 records (actual Kalshi API settlement, no 'source' key)
- historical_api: 5,470 records (from events_backfill, historical_api, live_api sources)

Since there's zero overlap between finalized and historical_api (disjoint station/date space),
we validate via: internal consistency checks, temperature range sanity, station coverage.

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
from typing import Dict, List, Tuple

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "kalshi_settlements.db")
OUT_PATH = os.path.join(REPO_ROOT, "data", "sweep", "settlement_source_validation.json")


def main():
    print("=" * 72)
    print("  KALSHI SETTLEMENT SOURCE VALIDATION")
    print("=" * 72)
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # ── 1. Basic counts ──
    cur.execute("SELECT source_type, COUNT(*) FROM kalshi_settlements GROUP BY source_type")
    counts = dict(cur.fetchall())
    print(f"\n  RECORDS: finalized={counts.get('finalized', 0)}, "
          f"historical_api={counts.get('historical_api', 0)}")
    
    # ── 2. Temperature range sanity ──
    cur.execute("SELECT MIN(kalshi_temp), MAX(kalshi_temp), AVG(kalshi_temp) FROM kalshi_settlements")
    tmin, tmax, tavg = cur.fetchone()
    print(f"\n  TEMPERATURE RANGE: {tmin:.1f}°F to {tmax:.1f}°F (mean {tavg:.1f}°F)")
    
    # Check for implausible temps
    cur.execute("SELECT COUNT(*) FROM kalshi_settlements WHERE kalshi_temp < -20 OR kalshi_temp > 130")
    outliers = cur.fetchone()[0]
    if outliers > 0:
        print(f"  ⚠️ {outliers} records outside plausible range (-20°F to 130°F)")
        cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements WHERE kalshi_temp < -20 OR kalshi_temp > 130 LIMIT 10")
        for r in cur.fetchall():
            print(f"     {r[0]}: {r[1]} = {r[2]}°F")
    else:
        print(f"  ✅ All temps in plausible range (-20°F to 130°F)")
    
    # Check for Celsius values (unlikely for US temps)
    cur.execute("SELECT COUNT(*) FROM kalshi_settlements WHERE kalshi_temp > 0 AND kalshi_temp < 50")
    winter_records = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kalshi_settlements WHERE kalshi_temp > 50")
    summer_records = cur.fetchone()[0]
    print(f"  Winter-range (0-50°F): {winter_records} records")
    print(f"  Summer-range (>50°F): {summer_records} records")
    if winter_records > 0 and summer_records > 0:
        print(f"  ✅ Both winter and summer temps present — looks like Fahrenheit")
    
    # ── 3. Station coverage ──
    print(f"\n  STATION COVERAGE:")
    cur.execute("""
        SELECT station, 
               COUNT(*) as n_records,
               MIN(target_date) as first_date,
               MAX(target_date) as last_date,
               source_type
        FROM kalshi_settlements 
        GROUP BY station, source_type
        ORDER BY station, source_type
    """)
    rows = cur.fetchall()
    stations = set()
    for r in rows:
        stations.add(r[0])
        print(f"  {r[0]}: {r[1]:5d} records ({r[4]}) — {r[2]} to {r[3]}")
    print(f"  Total unique stations: {len(stations)}")
    missing = [s for s in ["KATL","KAUS","KBOS","KDCA","KDEN","KDFW","KHOU","KLAS",
                           "KLAX","KMDW","KMIA","KMSP","KMSY","KNYC","KOKC","KPHL",
                           "KPHX","KSAT","KSEA","KSFO"] if s not in stations]
    if missing:
        print(f"  ⚠️ Missing stations: {missing}")
    else:
        print(f"  ✅ All 20 stations present")
    
    # ── 4. Date range coverage ──
    print(f"\n  DATE RANGE:")
    cur.execute("SELECT MIN(target_date), MAX(target_date) FROM kalshi_settlements")
    dr = cur.fetchone()
    print(f"  {dr[0]} to {dr[1]}")
    
    # Check for gaps
    cur.execute("SELECT COUNT(DISTINCT target_date) FROM kalshi_settlements")
    n_dates = cur.fetchone()[0]
    print(f"  Unique dates: {n_dates}")
    
    # ── 5. Source consistency within historical_api ──
    print(f"\n  HISTORICAL API SOURCE BREAKDOWN:")
    cur.execute("SELECT source, COUNT(*) FROM kalshi_settlements WHERE source_type='historical_api' GROUP BY source")
    for r in cur.fetchall():
        print(f"  {r[0]}: {r[1]} records")
    
    # Check for duplicate station/date within historical_api
    cur.execute("""
        SELECT station, target_date, COUNT(*), GROUP_CONCAT(source)
        FROM kalshi_settlements
        WHERE source_type = 'historical_api'
        GROUP BY station, target_date
        HAVING COUNT(*) > 1
        LIMIT 10
    """)
    dups = cur.fetchall()
    if dups:
        print(f"  ⚠️ {len(dups)} duplicate station/date pairs in historical_api")
        for r in dups[:5]:
            print(f"     {r[0]}: {r[1]} — {r[2]} copies, sources: {r[3]}")
    else:
        print(f"  ✅ No duplicate (station, date) pairs in historical_api")
    
    conn.close()
    
    # ── 6. Decision ──
    print(f"\n  DECISION:")
    issues = []
    if outliers > 0:
        issues.append(f"{outliers} temperature outliers")
    if missing:
        issues.append(f"{len(missing)} missing stations")
    if dups:
        issues.append(f"{len(dups)} duplicate records")
    
    if not issues and counts.get('finalized', 0) > 0 and counts.get('historical_api', 0) > 0:
        print(f"  ✅ TRUSTED — all checks passed")
        trust_status = "trusted"
    elif not issues:
        print(f"  ⚠️ PARTIALLY TRUSTED — no data quality issues, but limited coverage")
        trust_status = "partially_trusted"
    else:
        print(f"  ❌ ISSUES FOUND: {', '.join(issues)}")
        trust_status = "issues_found"
    
    # Note about cross-validation
    print(f"\n  NOTE: Finalized ({counts.get('finalized', 0)}) and historical_api "
          f"({counts.get('historical_api', 0)}) have zero overlap")
    print(f"  in station/date space. Direct cross-validation not possible.")
    print(f"  historical_api covers earlier dates and different stations than finalized.")
    
    # ── Save ──
    result = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "db_path": DB_PATH,
        },
        "counts": counts,
        "temperature_range": {"min": tmin, "max": tmax, "mean": tavg},
        "outliers": int(outliers),
        "stations": len(stations),
        "missing_stations": missing,
        "date_range": {"min": dr[0], "max": dr[1]},
        "n_unique_dates": n_dates,
        "duplicates": len(dups),
        "decision": {"status": trust_status, "max_error_f": None, "note": "No cross-validation possible (disjoint spaces)"},
    }
    
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Results saved to: {OUT_PATH}")


if __name__ == "__main__":
    main()