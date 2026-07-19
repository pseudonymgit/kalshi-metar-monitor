#!/usr/bin/env python3
"""
Phase 1.1: Signal Correlation Matrix

Loads the METAR backfill DB, runs walk-forward predictions for each signal
across all stations, computes pairwise Spearman rank correlation between
signal predictions, and identifies redundant pairs (rho >= 0.70).

Scripts-only, no AI in the loop. Uses numpy/scipy for stats.
"""

import sqlite3
import math
import json
import sys
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.signals import create_signal_registry

DB_PATH = os.path.join(_REPO_ROOT, "data", "metar_backfill.db")
OUTPUT_PATH = os.path.join(_REPO_ROOT, "data", "signal_correlation_matrix.json")
WALK_FORWARD_TRAIN = 180  # days
WALK_FORWARD_TEST = 30    # days

# Station list (all 21 stations from the DB)
STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDAL', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC',
    'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]


def load_station_data(station, conn):
    """Load daily weather data for a station from METAR observations."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
        })
    return days


def compute_actual_direction(days, idx):
    """Compute the actual direction (up/down) of temperature change from day idx-1 to day idx."""
    if idx < 1 or idx >= len(days):
        return None
    prev_high = days[idx - 1]['high']
    curr_high = days[idx]['high']
    if prev_high is None or curr_high is None:
        return None
    return 'up' if curr_high > prev_high else 'down'


def run_walk_forward_predictions(signal, station, conn):
    """
    Run walk-forward predictions for a single signal on a single station.
    Uses 180-day training window, 30-day test window.
    Returns a list of dicts: {date, predicted_direction, confidence, actual_direction}
    """
    days = load_station_data(station, conn)
    if len(days) < WALK_FORWARD_TRAIN + WALK_FORWARD_TEST:
        return []

    results = []
    n = len(days)

    # Walk-forward: slide test window with 50% overlap
    for test_start in range(WALK_FORWARD_TRAIN, n - WALK_FORWARD_TEST + 1, WALK_FORWARD_TEST // 2):
        test_end = min(test_start + WALK_FORWARD_TEST, n)
        for idx in range(test_start, test_end):
            if idx < signal.min_lookback:
                continue
            direction, confidence = signal.evaluate(idx, days)
            if direction is None:
                continue
            actual = compute_actual_direction(days, idx)
            if actual is None:
                continue
            results.append({
                'date': days[idx]['date'],
                'predicted': direction,
                'confidence': confidence,
                'actual': actual
            })

    return results


def encode_direction(d):
    """Encode 'up' as 1, 'down' as -1, other as 0."""
    return 1 if d == 'up' else (-1 if d == 'down' else 0)


def print_matrix(aggregate_matrix, signal_names):
    """Pretty-print the aggregate correlation matrix."""
    n = len(signal_names)
    # Build header
    header = "Signal".ljust(25)
    for s in signal_names:
        header += s[:8].rjust(8)
    print(header)
    print("-" * len(header))
    for i in range(n):
        row = signal_names[i].ljust(25)
        for j in range(n):
            if i == j:
                row += "{:8.2f}".format(1.0)
            elif aggregate_matrix[i][j] > 0:
                row += "{:8.3f}".format(aggregate_matrix[i][j])
            else:
                row += "       -"
        print(row)


def main():
    print("=" * 70)
    print("Phase 1.1: Signal Correlation Matrix")
    print("=" * 70)
    print("Database: {}".format(DB_PATH))
    print("Walk-forward: {}d train, {}d test".format(WALK_FORWARD_TRAIN, WALK_FORWARD_TEST))
    print("Stations: {}".format(len(STATIONS)))
    print()

    # Connect to DB
    conn = sqlite3.connect(DB_PATH)

    # Create signal registry
    registry = create_signal_registry(DB_PATH)
    signals = registry.get_all_signals()
    signal_names = list(signals.keys())
    n_signals = len(signal_names)

    print("Signals ({}): {}".format(n_signals, ", ".join(signal_names)))
    print()

    # Collect per-station predictions for each signal
    # Structure: {station: {signal_name: [(date, encoded_direction, confidence), ...]}}
    all_predictions = {}

    for station in STATIONS:
        print("Processing {}...".format(station), end=" ", flush=True)
        station_preds = {}
        min_records = float('inf')
        min_signal = ""
        total_records = 0

        for sig_name in signal_names:
            signal = signals[sig_name]
            results = run_walk_forward_predictions(signal, station, conn)
            pred_list = [(r['date'], encode_direction(r['predicted']), r['confidence']) for r in results]
            station_preds[sig_name] = pred_list
            total_records += len(pred_list)
            if len(pred_list) < min_records:
                min_records = len(pred_list)
                min_signal = sig_name

        all_predictions[station] = station_preds
        print("{} predictions, min signal: {} ({} records)".format(
            total_records, min_signal, min_records))

    # Compute pairwise Spearman correlation per station
    print("\nComputing correlation matrices...")
    per_station_matrices = {}
    aggregate_corr_sum = np.zeros((n_signals, n_signals))
    aggregate_corr_count = np.zeros((n_signals, n_signals))

    for station in STATIONS:
        station_preds = all_predictions[station]
        matrix = np.zeros((n_signals, n_signals))

        for i in range(n_signals):
            name_i = signal_names[i]
            preds_i = station_preds.get(name_i, [])
            if not preds_i:
                continue

            # Build date-indexed dict for signal i
            date_val_i = {p[0]: p[1] for p in preds_i}

            for j in range(i + 1, n_signals):
                name_j = signal_names[j]
                preds_j = station_preds.get(name_j, [])
                if not preds_j:
                    continue

                # Find common dates where both signals fired
                date_val_j = {p[0]: p[1] for p in preds_j}
                common_dates = sorted(set(date_val_i.keys()) & set(date_val_j.keys()))

                if len(common_dates) < 30:  # Need minimum samples
                    continue

                # Build paired arrays
                vals_i = [date_val_i[d] for d in common_dates]
                vals_j = [date_val_j[d] for d in common_dates]

                # Compute Spearman rank correlation
                corr, p_value = spearmanr(vals_i, vals_j)
                if not np.isnan(corr):
                    matrix[i][j] = corr
                    matrix[j][i] = corr
                    aggregate_corr_sum[i][j] += corr
                    aggregate_corr_count[i][j] += 1
                    aggregate_corr_sum[j][i] += corr
                    aggregate_corr_count[j][i] += 1

            matrix[i][i] = 1.0

        per_station_matrices[station] = matrix.tolist()

    # Aggregate matrix: mean across stations
    aggregate_matrix = np.divide(
        aggregate_corr_sum,
        aggregate_corr_count,
        where=aggregate_corr_count > 0,
        out=np.zeros((n_signals, n_signals))
    )
    for i in range(n_signals):
        aggregate_matrix[i][i] = 1.0

    # Identify redundant pairs (rho >= 0.70)
    redundant_pairs = []
    for i in range(n_signals):
        for j in range(i + 1, n_signals):
            rho = aggregate_matrix[i][j]
            if rho >= 0.70:
                count = int(aggregate_corr_count[i][j])
                redundant_pairs.append({
                    'signal_a': signal_names[i],
                    'signal_b': signal_names[j],
                    'spearman_rho': round(float(rho), 4),
                    'stations_with_data': count
                })

    # Sort by correlation descending
    redundant_pairs.sort(key=lambda x: -x['spearman_rho'])

    # Build output
    output = {
        'metadata': {
            'database': DB_PATH,
            'walk_forward_train_days': WALK_FORWARD_TRAIN,
            'walk_forward_test_days': WALK_FORWARD_TEST,
            'stations': STATIONS,
            'signal_names': signal_names,
            'n_signals': n_signals
        },
        'aggregate_matrix': aggregate_matrix.tolist(),
        'per_station_matrices': per_station_matrices,
        'redundant_pairs': redundant_pairs,
        'redundant_pairs_count': len(redundant_pairs)
    }

    # Save output
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print("\nResults saved to: {}".format(OUTPUT_PATH))
    print()

    # Print summary
    print("=" * 70)
    print("Aggregate Correlation Matrix (mean across stations)")
    print("=" * 70)
    print_matrix(aggregate_matrix, signal_names)

    print()
    if redundant_pairs:
        print("=" * 70)
        print("Redundant pairs (rho >= 0.70): {}".format(len(redundant_pairs)))
        print("=" * 70)
        for pair in redundant_pairs:
            print("  {:25} <-> {:25}  rho = {:.4f}  (stations: {})".format(
                pair['signal_a'], pair['signal_b'], pair['spearman_rho'], pair['stations_with_data']))
    else:
        print("No redundant pairs found (rho < 0.70 for all pairs).")

    conn.close()
    print("\nPhase 1.1 complete.")


if __name__ == "__main__":
    main()