#!/usr/bin/env python3
"""
Basic integration test for NwpAnalogSignal.

Usage:
    PYTHONPATH=. /home/gaddams/miniforge3/bin/python scripts/test_nwp_analog.py
"""
import sys
sys.path.insert(0, '.')

from core.signals.nwp_analog_signal import NwpAnalogSignal

def main():
    print("=== NWP Analog Signal Integration Test ===")
    
    sig = NwpAnalogSignal()
    print(f"Module loaded successfully")
    print(f"k_analogs: {sig.k_analogs}")
    print(f"nwp_db_path: {sig.nwp_db_path}")
    print("\nBasic initialization passed.")
    print("Ready for full B6.8 experiment.")
    return 0

if __name__ == "__main__":
    exit(main())