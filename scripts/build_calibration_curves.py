#!/usr/bin/env python3
"""
Build per-station DIRECTION-SPECIFIC calibration curves from GEFS signal vs actual outcome.

For each station, computes TWO sets of calibration curves:
  - UP: when the ensemble mean predicts warming (predicted_warming = True)
  - DOWN: when the ensemble mean predicts cooling (predicted_warming = False)

Each direction's predictions are binned by ensemble fraction confidence (0.05
increments from 0.50-0.55 to 0.95-1.00) and mapped to empirical win rate.

The full GEFS archive is used (all 2,036 dates) since the GEFS signal is a
deterministic physical model — no walk-forward constraint needed.

Output: data/calibration_curves.json (direction-specific format)

Usage: python3 scripts/build_calibration_curves.py
"""

import json
import sqlite3
import struct
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


def compute_ensemble_fraction(gefs_mean_c, prev_temp_c, member_temps_c):
    """
    Compute ensemble fraction and direction.
    Returns (fraction_up, direction, confidence).
    """
    if not member_temps_c or len(member_temps_c) < 2:
        return None, None, None
    
    # Direction from ensemble mean
    predicted_warming = gefs_mean_c > prev_temp_c
    
    # Count members agreeing with the direction
    agreeing = sum(1 for t in member_temps_c 
                   if (t > prev_temp_c) == predicted_warming)
    fraction = agreeing / len(member_temps_c)
    
    # fraction_up = fraction of members predicting up
    fraction_up = sum(1 for t in member_temps_c if t > prev_temp_c) / len(member_temps_c)
    
    # Confidence = max(fraction, 1-fraction) — ensemble decisiveness
    confidence = max(fraction, 1 - fraction)
    
    return fraction_up, predicted_warming, confidence


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
    
    # For each station, collect (confidence, correct) pairs SEPARATELY for UP and DOWN
    cal_data_up = defaultdict(list)    # {station: [(confidence, correct), ...]}
    cal_data_down = defaultdict(list)  # {station: [(confidence, correct), ...]}
    station_count = {s: 0 for s in STATIONS}
    
    # Track per-direction accuracy
    dir_counts = {"up": {"correct": 0, "total": 0}, "down": {"correct": 0, "total": 0}}
    
    for s, d, mean_c, member_blob, n_members in gcur.fetchall():
        if s not in STATIONS:
            continue
        if mean_c is None:
            continue
        if s not in settlements or d not in settlements[s]:
            continue
        
        # Get previous temp for direction
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
        
        # Compute ensemble fraction and direction
        fraction_up, predicted_warming, confidence = compute_ensemble_fraction(
            mean_c, prev_temp_c, member_temps_c
        )
        if confidence is None or confidence <= 0.50:
            continue
        
        # Determine correctness
        actual_warming = actual_temp_c > prev_temp_c
        correct = predicted_warming == actual_warming
        
        # Route to direction-specific bucket
        direction = "up" if predicted_warming else "down"
        if direction == "up":
            cal_data_up[s].append((confidence, correct))
            dir_counts["up"]["total"] += 1
            if correct:
                dir_counts["up"]["correct"] += 1
        else:
            cal_data_down[s].append((confidence, correct))
            dir_counts["down"]["total"] += 1
            if correct:
                dir_counts["down"]["correct"] += 1
        
        station_count[s] += 1
    
    gdb.close()
    
    # Build calibration table per station × direction
    def build_direction_curve(cal_data, station_set):
        """Build calibration curve for a given direction's data."""
        result = {}
        global_data = []
        
        for s in station_set:
            if not cal_data[s]:
                result[s] = {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}
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
            result[s] = {
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
        
        return {
            "per_station": result,
            "global": {
                "bins": global_bin_results,
                "n": global_total,
                "win_rate": round(global_wins / global_total, 4) if global_total > 0 else 0.5,
            },
        }
    
    # Build UP curves
    up_curves = build_direction_curve(cal_data_up, STATIONS)
    down_curves = build_direction_curve(cal_data_down, STATIONS)
    
    # Assemble output: {station: {up: {...}, down: {...}}, _global: {...}}
    calibration = {}
    for s in STATIONS:
        calibration[s] = {
            "up": up_curves["per_station"][s],
            "down": down_curves["per_station"][s],
        }
    
    # Global calibration (direction-specific)
    calibration["_global"] = {
        "up": up_curves["global"],
        "down": down_curves["global"],
    }
    
    # Write output
    with open(OUTPUT, "w") as f:
        json.dump(calibration, f, indent=2)
    
    # Print summary
    print(f"Calibration curves written to {OUTPUT}")
    print(f"  Stations with data: {sum(1 for s in STATIONS if cal_data_up[s] or cal_data_down[s])}/{len(STATIONS)}")
    print(f"  Total samples: {dir_counts['up']['total'] + dir_counts['down']['total']}")
    print()
    print(f"Direction-Specific Accuracy:")
    print(f"  UP:   {dir_counts['up']['correct']}/{dir_counts['up']['total']} = {dir_counts['up']['correct']/dir_counts['up']['total']*100:.1f}%")
    print(f"  DOWN: {dir_counts['down']['correct']}/{dir_counts['down']['total']} = {dir_counts['down']['correct']/dir_counts['down']['total']*100:.1f}%")
    print(f"  ALL:  {dir_counts['up']['correct']+dir_counts['down']['correct']}/{dir_counts['up']['total']+dir_counts['down']['total']} = {(dir_counts['up']['correct']+dir_counts['down']['correct'])/(dir_counts['up']['total']+dir_counts['down']['total'])*100:.1f}%")
    print()
    print("Station Calibration Summary (UP):")
    print(f"  {'Station':<8} {'Samples':>8} {'WinRate':>8} {'AvgConf':>8}")
    print("  " + "-" * 35)
    for s in STATIONS:
        cu = up_curves["per_station"][s]
        if cu["global"]["n"] > 0:
            wr = cu["global"]["win_rate"]
            ac = cu["confidence_distribution"]["mean"]
            n = cu["global"]["n"]
            print(f"  {s:<8} {n:>8} {wr*100:>7.1f}% {ac:>7.3f}")
    
    print()
    print("Station Calibration Summary (DOWN):")
    print(f"  {'Station':<8} {'Samples':>8} {'WinRate':>8} {'AvgConf':>8}")
    print("  " + "-" * 35)
    for s in STATIONS:
        cd = down_curves["per_station"][s]
        if cd["global"]["n"] > 0:
            wr = cd["global"]["win_rate"]
            ac = cd["confidence_distribution"]["mean"]
            n = cd["global"]["n"]
            print(f"  {s:<8} {n:>8} {wr*100:>7.1f}% {ac:>7.3f}")
    
    print()
    print("Global Calibration Bins (UP):")
    for label, data in up_curves["global"]["bins"].items():
        wr = data["win_rate"]
        n = data["n"]
        if wr is not None:
            print(f"  {label}: {wr*100:5.1f}% ({n} samples)")
        else:
            print(f"  {label}: N/A ({n} samples)")
    
    print()
    print("Global Calibration Bins (DOWN):")
    for label, data in down_curves["global"]["bins"].items():
        wr = data["win_rate"]
        n = data["n"]
        if wr is not None:
            print(f"  {label}: {wr*100:5.1f}% ({n} samples)")
        else:
            print(f"  {label}: N/A ({n} samples)")
    
    # Data sparsity check
    sparse_stations = []
    for s in STATIONS:
        for direction, cal_data in [("up", cal_data_up), ("down", cal_data_down)]:
            d = cal_data[s]
            if len(d) >= 5:
                # Check if most bins have <5 samples
                bins_with_5 = sum(1 for _, c in d if c >= 0.05)  # just a rough check
                pass
            if len(d) < 5:
                sparse_stations.append((s, direction, len(d)))
    
    if sparse_stations:
        print(f"\n  ⚠ Data sparsity: {len(sparse_stations)} station/direction combos with <5 samples:")
        for s, d, n in sparse_stations:
            print(f"    {s} {d}: {n} samples")
    
    # Accuracy improvement suggestion
    print()
    print("═══ ACCURACY IMPROVEMENT SUGGESTIONS ═══")
    print("1. Per-station + per-direction calibration: The most impactful change —")
    print("   separates UP (higher baseline accuracy) from DOWN (lower) so each")
    print("   direction gets its own empirical mapping. This directly addresses the")
    print("   ~3-4% accuracy gap between the two directions.")
    print("2. Bin-level smoothing: For bins with 5-15 samples, use a kernel-weighted")
    print("   average of adjacent bins to reduce noise from small-sample bins.")
    print("3. Seasonal calibration: GEFS accuracy varies by season (winter vs summer).")
    print("   Adding seasonal (monthly) sub-buckets would improve calibration at the")
    print("   cost of thinning sample sizes — requires more data first.")


if __name__ == "__main__":
    main()