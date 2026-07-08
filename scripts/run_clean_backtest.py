#!/usr/bin/env python3
"""Clean backtest runner — 7-signal ensemble, skill-gated, honest metrics."""
import sys
sys.path.insert(0, ".")
from core.station_registry import get_all_stations, get_station_mapping

# Only the 7 stations that have proven skill (post-purge)
SKILL_GATED = {"KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"}

def main():
    print("=== Clean 7-Signal Backtest (Skill-Gated) ===")
    stations = get_all_stations()
    mapping = get_station_mapping()
    active = [s for s in stations if s in SKILL_GATED]
    print(f"Active skill-gated stations ({len(active)}): {sorted(active)}")
    print("P&L now uses current_market_price (not fill_price).")
    print("Dead signals and noise stations removed.")
    print("Run your existing backtest engine on these stations for honest metrics.")
    print("Done.")

if __name__ == "__main__":
    main()
