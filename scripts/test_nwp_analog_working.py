#!/usr/bin/env python3
"""
B6.8 – Verify the new compute_signal API actually works.

Usage:
    PYTHONPATH=. /home/gaddams/miniforge3/bin/python scripts/test_nwp_analog_working.py
"""
import sys
sys.path.insert(0, '.')

from core.signals.nwp_analog_signal import NwpAnalogSignal

def main():
    print("=== B6.8 compute_signal API Test ===")

    sig = NwpAnalogSignal(nwp_db_path="data/nwp_forecasts.db")
    print(f"Module ready")

    for station in ["KNYC", "KMDW"]:
        try:
            result = sig.compute_signal(station)
            print(f"\n{station}:")
            print(f"  result = {result}")
            if result is None:
                print("  (no prediction – insufficient analogs or data)")
            else:
                print(f"  direction: {result.get('direction')}")
                print(f"  confidence: {result.get('confidence')}")
                print(f"  num_analogs: {result.get('num_analogs')}")
        except Exception as e:
            print(f"{station} ERROR: {e}")
            return 1

    print("\n✅ compute_signal API is working.")
    return 0

if __name__ == "__main__":
    exit(main())