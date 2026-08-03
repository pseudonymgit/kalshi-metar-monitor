#!/usr/bin/env python3
"""
Build per-station calibration curves from GEFS signal vs actual outcome.

For each station, buckets confidence (0.50-1.00) into bins and computes the
empirical win rate. Produces a calibration table to replace the raw ensemble
fraction with empirically calibrated confidence.

Walk-forward constraint: calibration is computed from ALL available data
since the signal is deterministic (GEFS ensemble fraction vs Kalshi settlement).
No look-ahead bias because the GEFS forecast is a physical model, not a
trainable parameter; the calibration is a mapping from signal-strength to
empirical outcome rate, not a trainable weight.

Output: data/calibration_curves.json

Usage: python3 scripts/build_calibration_curves.py
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT = REPO_ROOT / "data" / "calibration_curves.json"

STATIONS = [
    "KNYC", "KLAX", "KPHX", "KDFW", "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KHOU", "KLAS", "KMDW", "KMIA", "KMSP", "KMSY", "KOKC", "KPHL", "KSAT", "KSEA", "KSFO",
]

# Confidence bins: [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00]
BINS = [0.50 + i * 0.05 for i in range(11)]
BIN_LABELS = [f"{BINS[i]:.2f}-{BINS[i+1]:.2f}" for i in range(len(BINS)-1)]


def compute_confidence(gefs_mean_c, prev_temp_c, member_temps_c):
    """Compute ensemble fraction confidence (same logic as compute_ensemble_signal)."""
    if not member_temps_c or len(member_temps_c) < 2:
        return 0.0
    
    # Direction: is GEFS mean predicting up or down vs previous?
    predicted_warming = gefs_mean_c > prev_temp_c
    
    # Count members agreeing with the direction
    agreeing = sum(1 for t in member_temps_c 
                   if (t > prev_temp_c) == predicted_warming)
    fraction = agreeing / len(member_temps_c)
    
    # Confidence = max(fraction, 1-fraction) — how decisive is the ensemble
    confidence = max(fraction, 1 - fraction)
    return confidence


def main():
    gdb = sqlite3.connect(GEFS_DB)
    sdb = sqlite3.connect(SETTLEMENTS_DB)
    
    # Load Kalshi settlements
    scur = sdb.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements")
    settlements = defaultdict(dict)
    for s, d, t in scur.fetchall():
        if t is not None:
            settlements[s][d] = float(t)
    
    sdb.close()
    
    # Load GEFS data
    gcur = gdb.execute("""
        SELECT station, target_date, ensemble_mean, member_values, n_members
        FROM gefs_archive
        WHERE step = 24
        ORDER BY target_date
    """)
    
    import struct
    
    # For each station, collect (confidence, correct) pairs
    cal_data = defaultdict(list)
    station_count = {s: 0 for s in STATIONS}
    
    prev_temps = {}  # station -> last temp for direction
    
    for s, d, mean_c, member_blob, n_members in gcur.fetchall():
        if s not in STATIONS:
            continue
        if mean_c is None:
            continue
        if s not in settlements or d not in settlements[s]:
            continue
        
        # Compute direction: need previous temp
        # Use the settlement temp as ground truth for prev_temp
        dates = sorted(settlements[s].keys())
        try:
            idx = dates.index(d)
        except ValueError:
            continue
        if idx == 0:
            continue
        prev_temp_f = settlements[s][dates[idx - 1]]
        actual_temp_f = settlements[s][d]
        prev_temp_c = (prev_temp_f - 32) * 5.0 / 9.0
        actual_temp_c = (actual_temp_f - 32) * 5.0 / 9.0
        
        # Decode member offsets
        n_members = n_members or 31
        if member_blob and len(member_blob) >= n_members:
            offsets = list(struct.unpack('b' * n_members, member_blob[:n_members]))
            member_temps_c = [mean_c + o * 0.1 for o in offsets]
        else:
            continue
        
        # Compute confidence
        confidence = compute_confidence(mean_c, prev_temp_c, member_temps_c)
        if confidence <= 0.50:
            continue
        
        # Determine direction
        predicted_warming = mean_c > prev_temp_c
        actual_warming = actual_temp_c > prev_temp_c
        correct = predicted_warming == actual_warming
        
        cal_data[s].append((confidence, correct))
        station_count[s] += 1
    
    gdb.close()
    
    # Build calibration table per station
    calibration = {}
    global_data = []
    
    for s in STATIONS:
        if not cal_data[s]:
            calibration[s] = {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}
            continue
        
        # Bin by confidence
        bins = {label: {"wins": 0, "total": 0} for label in BIN_LABELS}
        for conf, correct in cal_data[s]:
            for i in range(len(BINS) - 1):
                if BINS[i] <= conf < BINS[i + 1]:
                    bins[BIN_LABELS[i]]["total"] += 1
                    if correct:
                        bins[BIN_LABELS[i]]["wins"] += 1
                    break
            global_data.append((s, conf, correct))
        
        # Compute win rates per bin
        bin_results = {}
        for label, data in bins.items():
            total = data["total"]
            if total >= 5:  # Minimum sample size
                bin_results[label] = {
                    "win_rate": round(data["wins"] / total, 4),
                    "n": total,
                }
            else:
                bin_results[label] = {"win_rate": None, "n": total}
        
        # Overall station calibration
        total_wins = sum(1 for _, c in cal_data[s] if c)
        total_n = len(cal_data[s])
        calibration[s] = {
            "bins": bin_results,
            "global": {
                "n": total_n,
                "win_rate": round(total_wins / total_n, 4) if total_n > 0 else 0.5,
            },
            "confidence_distribution": {
                "mean": round(sum(c for c, _ in cal_data[s]) / total_n, 4) if total_n > 0 else 0.0,
                "min": round(min(c for c, _ in cal_data[s]), 4) if total_n > 0 else 0.0,
                "max": round(max(c for c, _ in cal_data[s]), 4) if total_n > 0 else 0.0,
            },
        }
    
    # Global calibration (all stations pooled)
    global_total = len(global_data)
    global_wins = sum(1 for _, _, c in global_data if c)
    global_bins = {label: {"wins": 0, "total": 0} for label in BIN_LABELS}
    for _, conf, correct in global_data:
        for i in range(len(BINS) - 1):
            if BINS[i] <= conf < BINS[i + 1]:
                global_bins[BIN_LABELS[i]]["total"] += 1
                if correct:
                    global_bins[BIN_LABELS[i]]["wins"] += 1
                break
    
    global_bin_results = {}
    for label, data in global_bins.items():
        total = data["total"]
        if total >= 5:
            global_bin_results[label] = {
                "win_rate": round(data["wins"] / total, 4),
                "n": total,
            }
        else:
            global_bin_results[label] = {"win_rate": None, "n": total}
    
    calibration["_global"] = {
        "bins": global_bin_results,
        "n": global_total,
        "win_rate": round(global_wins / global_total, 4) if global_total > 0 else 0.5,
    }
    
    # Write output
    with open(OUTPUT, "w") as f:
        json.dump(calibration, f, indent=2)
    
    print(f"Calibration curves written to {OUTPUT}")
    print(f"  Stations with data: {sum(1 for s in STATIONS if cal_data[s])}/{len(STATIONS)}")
    print(f"  Total samples: {global_total}")
    print(f"  Global win rate: {global_wins/global_total*100:.1f}%")
    print()
    print("Station Calibration Summary:")
    print(f"  {'Station':<8} {'Samples':>8} {'WinRate':>8} {'AvgConf':>8}")
    print("  " + "-" * 35)
    for s in STATIONS:
        if cal_data[s]:
            c = calibration[s]
            wr = c["global"]["win_rate"]
            ac = c["confidence_distribution"]["mean"]
            n = c["global"]["n"]
            print(f"  {s:<8} {n:>8} {wr*100:>7.1f}% {ac:>7.3f}")
    
    print()
    print("Global Calibration Bins:")
    for label, data in global_bin_results.items():
        wr = data["win_rate"]
        n = data["n"]
        if wr is not None:
            print(f"  {label}: {wr*100:5.1f}% ({n} samples)")
        else:
            print(f"  {label}: N/A ({n} samples)")


if __name__ == "__main__":
    main()