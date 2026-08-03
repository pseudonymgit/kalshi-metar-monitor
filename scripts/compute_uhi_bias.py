#!/usr/bin/env python3
"""
Compute Urban Heat Island bias correction table.

Computes per-station × per-month bias (GEFS ensemble_mean vs Kalshi settlement temp)
in °F. The bias is GEFS_°F - Kalshi_°F (positive means GEFS overpredicts).

Output: data/uhi_bias_table.json

Usage: python3 scripts/compute_uhi_bias.py
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT = REPO_ROOT / "data" / "uhi_bias_table.json"

STATIONS = [
    "KNYC", "KLAX", "KPHX", "KDFW", "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KHOU", "KLAS", "KMDW", "KMIA", "KMSP", "KMSY", "KOKC", "KPHL", "KSAT", "KSEA", "KSFO",
]


def main():
    gdb = sqlite3.connect(GEFS_DB)
    sdb = sqlite3.connect(SETTLEMENTS_DB)

    # Load GEFS ensemble_mean (°C)
    gcur = gdb.execute("SELECT station, target_date, ensemble_mean FROM gefs_archive WHERE step=24")
    gefs = defaultdict(dict)
    for s, d, t in gcur.fetchall():
        if t is not None:
            gefs[s][d] = float(t)

    # Load Kalshi settlements (°F)
    scur = sdb.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements")
    settle = defaultdict(dict)
    for s, d, t in scur.fetchall():
        if t is not None:
            settle[s][d] = float(t)

    gdb.close()
    sdb.close()

    # Compute per-station × per-month bias
    bias_table = {}
    for s in STATIONS:
        monthly = {}
        for m in range(1, 13):
            diffs = []
            for d in gefs.get(s, {}):
                if d in settle.get(s, {}):
                    m_date = int(d.split("-")[1])
                    if m_date == m:
                        gefs_f = gefs[s][d] * 9.0 / 5.0 + 32.0
                        kalshi_f = settle[s][d]
                        diffs.append(gefs_f - kalshi_f)
            if diffs:
                monthly[str(m)] = round(sum(diffs) / len(diffs), 4)
        
        # Fill missing months with nearest available
        filled = {}
        avail = sorted(int(k) for k in monthly.keys())
        if avail:
            for m in range(1, 13):
                if str(m) in monthly:
                    filled[str(m)] = monthly[str(m)]
                else:
                    # Nearest available month (cyclic)
                    nearest = min(avail, key=lambda x: min(abs(x - m), 12 - abs(x - m)))
                    filled[str(m)] = monthly.get(str(nearest), 0.0)
        else:
            filled = {str(m): 0.0 for m in range(1, 13)}
        
        bias_table[s] = filled

    # Write output
    with open(OUTPUT, "w") as f:
        json.dump(bias_table, f, indent=2)
    
    print(f"UHI bias table written to {OUTPUT}")
    print(f"  Stations: {len(bias_table)}")
    for s in STATIONS:
        summer = [bias_table[s][str(m)] for m in (6, 7, 8)]
        mean_jja = sum(summer) / 3 if summer else 0
        print(f"  {s}: JJA bias={mean_jja:+.3f}°F, range=[{min(summer):+.3f}, {max(summer):+.3f}]°F")


if __name__ == "__main__":
    main()