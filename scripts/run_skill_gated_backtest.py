#!/usr/bin/env python3
"""Skill-gated backtest — 7 stations only, honest metrics, no tainted signals."""
import sys
sys.path.insert(0, ".")
from scripts import split_backtest_current as sbc

SKILL_GATED = {"KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"}

# Monkey-patch the station list before the script runs its main logic
import core.station_registry as sr
orig_get_all = sr.get_all_stations
sr.get_all_stations = lambda: [s for s in orig_get_all() if s in SKILL_GATED]

print("=== Skill-Gated 7-Station Backtest ===")
print(f"Stations: {sorted(SKILL_GATED)}")
print("Running split_backtest_current.py with filter...\n")

# Now run the existing backtest (it will use the patched station list)
if hasattr(sbc, 'main'):
    sbc.main()
else:
    # Fallback: exec the script directly
    exec(open('scripts/split_backtest_current.py').read())
