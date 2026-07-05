#!/usr/bin/env python3
"""
METAR Overlap Confirmation Script

Checks whether METAR live collection has started and is overlapping with
NWP forecast data. Reports on:
  - METAR daily_stats date range
  - NWP nwp_forecasts date range
  - Gap between METAR end and NWP start
  - Whether metar_collect_live.py exists
  - Whether live METAR data is flowing (if script has been run)

⚠️  STANDALONE SCRIPT — DO NOT RUN VIA AI/AGENT.
    Run manually: python3 scripts/metar_overlap_check.py

Output: human-readable status report + exit code (0=overlap OK, 1=gap exists)
"""

import sqlite3
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
NWP_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/nwp_forecasts.db"
LIVE_SCRIPT = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/scripts/metar_collect_live.py"


def check_metar_db():
    """Check METAR database state."""
    if not os.path.exists(METAR_DB):
        return None, "METAR database not found"

    conn = sqlite3.connect(METAR_DB, timeout=10)
    c = conn.cursor()

    # daily_stats range
    try:
        c.execute("SELECT MIN(date_utc), MAX(date_utc), COUNT(*) FROM daily_stats")
        min_date, max_date, count = c.fetchone()
    except Exception as e:
        conn.close()
        return None, f"Error reading daily_stats: {e}"

    # settlement_epochs range
    try:
        c.execute("SELECT MIN(local_trading_date), MAX(local_trading_date), COUNT(*) FROM settlement_epochs")
        s_min, s_max, s_count = c.fetchone()
    except Exception:
        s_min = s_max = s_count = None

    # Check for recent data (last 7 days)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        c.execute("SELECT COUNT(*) FROM daily_stats WHERE date_utc >= ?", (week_ago,))
        recent_count = c.fetchone()[0]
    except Exception:
        recent_count = 0

    # Per-station coverage for recent dates
    try:
        c.execute("""
            SELECT station, COUNT(*), MAX(date_utc)
            FROM daily_stats
            WHERE date_utc >= ?
            GROUP BY station
            ORDER BY station
        """, (week_ago,))
        recent_stations = c.fetchall()
    except Exception:
        recent_stations = []

    conn.close()

    return {
        "daily_stats": {
            "min_date": min_date,
            "max_date": max_date,
            "count": count,
        },
        "settlement_epochs": {
            "min_date": s_min,
            "max_date": s_max,
            "count": s_count,
        },
        "recent_count": recent_count,
        "recent_stations": recent_stations,
        "week_ago": week_ago,
        "today": today,
    }, None


def check_nwp_db():
    """Check NWP database state."""
    if not os.path.exists(NWP_DB):
        return None, "NWP database not found"

    conn = sqlite3.connect(NWP_DB, timeout=10)
    c = conn.cursor()

    try:
        c.execute("SELECT MIN(fetch_date), MAX(fetch_date), COUNT(*) FROM nwp_forecasts")
        min_date, max_date, count = c.fetchone()
    except Exception as e:
        conn.close()
        return None, f"Error reading nwp_forecasts: {e}"

    try:
        c.execute("SELECT DISTINCT model FROM nwp_forecasts ORDER BY model")
        models = [r[0] for r in c.fetchall()]
    except Exception:
        models = []

    try:
        c.execute("SELECT DISTINCT station FROM nwp_forecasts ORDER BY station")
        stations = [r[0] for r in c.fetchall()]
    except Exception:
        stations = []

    # Check target_date range (forecast target dates)
    try:
        c.execute("SELECT MIN(target_date), MAX(target_date) FROM nwp_forecasts")
        t_min, t_max = c.fetchone()
    except Exception:
        t_min = t_max = None

    conn.close()

    return {
        "fetch_min": min_date,
        "fetch_max": max_date,
        "count": count,
        "models": models,
        "stations": stations,
        "target_min": t_min,
        "target_max": t_max,
    }, None


def check_live_script():
    """Check if metar_collect_live.py exists."""
    if os.path.exists(LIVE_SCRIPT):
        return True, "Found"
    return False, f"MISSING: {LIVE_SCRIPT}"


def compute_gap(metar_end, nwp_start):
    """Compute gap between METAR end date and NWP start date."""
    if not metar_end or not nwp_start:
        return None

    try:
        metar_d = datetime.strptime(metar_end, "%Y-%m-%d")
        nwp_d = datetime.strptime(nwp_start, "%Y-%m-%d")
        gap = (nwp_d - metar_d).days
        return gap
    except Exception:
        return None


def main():
    print("=" * 60)
    print("METAR / NWP Overlap Check")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    print()

    # Check METAR
    print("--- METAR Database ---")
    metar_state, metar_err = check_metar_db()
    if metar_err:
        print(f"  ERROR: {metar_err}")
    else:
        ds = metar_state["daily_stats"]
        se = metar_state["settlement_epochs"]
        print(f"  daily_stats:     {ds['min_date']} → {ds['max_date']} ({ds['count']} rows)")
        if se["count"]:
            print(f"  settlement_epochs: {se['min_date']} → {se['max_date']} ({se['count']} rows)")
        print(f"  Recent data (≥ {metar_state['week_ago']}): {metar_state['recent_count']} rows")
        if metar_state["recent_stations"]:
            print(f"  Recent stations ({len(metar_state['recent_stations'])}):")
            for st, cnt, last in metar_state["recent_stations"]:
                print(f"    {st}: {cnt} rows, last: {last}")
        else:
            print("  ⚠ NO recent METAR data — live collection may not be running")

    print()

    # Check NWP
    print("--- NWP Database ---")
    nwp_state, nwp_err = check_nwp_db()
    if nwp_err:
        print(f"  ERROR: {nwp_err}")
    else:
        print(f"  Fetch dates:     {nwp_state['fetch_min']} → {nwp_state['fetch_max']}")
        print(f"  Target dates:    {nwp_state['target_min']} → {nwp_state['target_max']}")
        print(f"  Total rows:      {nwp_state['count']}")
        print(f"  Models:          {nwp_state['models']}")
        print(f"  Stations:        {len(nwp_state['stations'])}")

    print()

    # Check live script
    print("--- Live Collection Script ---")
    script_exists, script_msg = check_live_script()
    if script_exists:
        print(f"  ✅ {script_msg}")
    else:
        print(f"  ❌ {script_msg}")
        print("  This script is needed for live METAR collection (cron every 30 min)")
        print("  Without it, METAR data will not update and overlap with NWP cannot occur")

    print()

    # Gap analysis
    print("--- Gap Analysis ---")
    if metar_state and nwp_state:
        gap = compute_gap(metar_state["daily_stats"]["max_date"], nwp_state["fetch_min"])
        if gap is not None:
            print(f"  METAR end:     {metar_state['daily_stats']['max_date']}")
            print(f"  NWP start:     {nwp_state['fetch_min']}")
            print(f"  Gap:           {gap} days")

            if gap > 0:
                print(f"  ⚠ DATA GAP of {gap} days between METAR and NWP data")
                print(f"  Need live METAR collection to fill gap before backtest can run")
                sys.exit(1)
            elif gap == 0:
                print("  ✅ Perfect overlap — METAR and NWP data are contiguous")
                sys.exit(0)
            else:
                print(f"  ℹ NWP starts before METAR ends (overlap of {-gap} days)")
                sys.exit(0)
        else:
            print("  Cannot compute gap — missing date range data")
            sys.exit(1)
    else:
        print("  Cannot compute gap — database errors")
        sys.exit(1)


if __name__ == "__main__":
    main()
