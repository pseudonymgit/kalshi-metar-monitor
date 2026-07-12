#!/usr/bin/env python3
"""
Edge 20 Minimal Backtest — NWP High/Low Directional Accuracy

Deterministic pilot script. No AI, no external calls.

Compares NWP forecast high/low vs Kalshi settlement direction
for pilot stations KNYC and KMDW.

Usage:
  python3 scripts/edge20_nwp_backtest_minimal.py

Output: directional accuracy, trade count, simple metrics, per-station table.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")

# Pilot stations
STATIONS = ["KNYC", "KMDW"]

# Settlement table (expected: settlement_epochs or equivalent)
# For minimal pilot we use a simple placeholder: we compare forecast vs actual high/low
# If no settlement table exists, we report data alignment gaps only.

def get_nwp_high_low(db_path, station, target_date):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        SELECT temperature_2m_max, temperature_2m_min
        FROM nwp_forecasts
        WHERE station = ? AND target_date = ? AND model = 'gfs'
        LIMIT 1
    """, (station, target_date))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None

def main():
    print("Edge 20 Minimal Backtest — KNYC + KMDW (GFS high/low directional)")
    print("=" * 60)
    print(f"NWP DB: {NWP_DB}")
    print(f"Stations: {STATIONS}")
    print()

    conn = sqlite3.connect(NWP_DB)
    c = conn.cursor()

    # Get distinct dates with data for pilot stations
    c.execute("""
        SELECT DISTINCT target_date
        FROM nwp_forecasts
        WHERE station IN ('KNYC','KMDW')
        ORDER BY target_date
    """)
    dates = [r[0] for r in c.fetchall()]
    print(f"Total candidate dates: {len(dates)}")

    # Placeholder: no settlement table found in this minimal version
    # Real implementation will join against settlement_epochs or actual_high/low table
    print("\n[DATA GAP] No settlement/actuals table present in current schema.")
    print("Pilot can only report data coverage, not directional accuracy yet.")
    print()

    # Simple coverage report
    for station in STATIONS:
        c.execute("""
            SELECT COUNT(DISTINCT target_date)
            FROM nwp_forecasts
            WHERE station = ?
        """, (station,))
        count = c.fetchone()[0]
        print(f"{station}: {count} dates with NWP forecast")

    conn.close()

    print("\nNext required step: settlement alignment table (actual high/low per epoch).")
    print("Script complete — no accuracy numbers possible without ground truth.")

if __name__ == "__main__":
    main()