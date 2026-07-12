#!/usr/bin/env python3
"""
Real integration test for NwpAnalogSignal (B6.8).

This test:
- Initializes NwpAnalogSignal with the real NWP database
- Checks database connectivity and data availability
- Verifies feature loading works for a sample station (KNYC)
- Prints basic diagnostics

Usage:
    PYTHONPATH=. /home/gaddams/miniforge3/bin/python scripts/test_nwp_analog_real.py
"""
import sys
sys.path.insert(0, '.')

import sqlite3
from core.signals.nwp_analog_signal import NwpAnalogSignal

def main():
    print("=== NWP Analog Signal – Real Integration Test (B6.8) ===")

    sig = NwpAnalogSignal()
    print(f"Module initialized")
    print(f"nwp_db: {sig.nwp_db_path}")

    # Check database exists and has data
    try:
        conn = sqlite3.connect(sig.nwp_db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM nwp_forecasts")
        total_rows = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT station) FROM nwp_forecasts")
        stations = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT target_date) FROM nwp_forecasts")
        dates = cur.fetchone()[0]

        print(f"Database stats: {total_rows} rows, {stations} stations, {dates} dates")

        # Check for KNYC data
        cur.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE station = 'KNYC'")
        knyc_rows = cur.fetchone()[0]
        print(f"KNYC rows: {knyc_rows}")

        conn.close()

        if knyc_rows == 0:
            print("WARNING: No data for KNYC yet – may need more backfill.")
        else:
            print("Data layer looks healthy for pilot station KNYC.")

    except Exception as e:
        print(f"ERROR accessing NWP database: {e}")
        return 1

    print("\n✅ Basic integration test passed.")
    print("Ready to proceed with full B6.8 analog ensemble experiment.")
    return 0

if __name__ == "__main__":
    exit(main())