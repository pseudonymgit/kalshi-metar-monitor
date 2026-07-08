#!/usr/bin/env python3
"""
S4/N3 — Signal Correlation Matrix & Redundancy Report

Computes pairwise correlation and mutual information between all signals
on real backtest data. Identifies redundant signals and provides drop
recommendations.

Output:
  - reports/signal-correlation-matrix-2026-07-06.md
  - reports/signal-correlation-matrix-2026-07-06.json
"""

import sqlite3
import math
import os
import sys
import json
import numpy as np
from collections import defaultdict
from itertools import combinations

# Ensure core/ is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(SCRIPT_DIR, '..', 'core')
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from signals import SignalRegistry, BACKTEST_SIGNALS, FULL_ENSEMBLE
from unified_backtest import load_station_data, DB_PATH

REPORT_DIR = os.path.join(CORE_DIR, '..', 'reports')
REPORT_MD = os.path.join(REPORT_DIR, 'signal-correlation-matrix-2026-07-06.md')
REPORT_JSON = os.path.join(REPORT_DIR, 'signal-correlation-matrix-2026-07-06.json')

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]


def compute_signal_correlations(signal_names=None, stations=None, db_path=DB_PATH):
    """
    Compute pairwise correlation matrix between all signals.

    For each day where two signals both fire, we compare:
    1. Direction agreement (same direction = 1, different = 0)
    2. Confidence correlation (Pearson)
    3. Prediction correlation (directional, weighted by confidence)

    Returns:
        correlation_matrix: NxN matrix of correlation coefficients
        agreement_matrix: NxN matrix of direction agreement rates
        co_fire_counts: NxN matrix of co-firing counts
        signal_names: list of signal names
    """
    if signal_names is None:
        signal_names = BACKTEST_SIGNALS
    if stations is None:
        stations = STATIONS

    registry = SignalRegistry(db_path)
    conn = sqlite3.connect(db_path)

    n = len(signal_names)

    # Collect per-signal predictions: {(signal, station): {date: (direction, confidence)}}
    signal_preds = defaultdict(lambda: defaultdict(dict))

    for station in stations:
        days, market = load_station_data(station, conn)
        if len(days) < 70:
            continue

        for idx in range(60, len(days)):
            for sig_name in signal_names:
                sig = registry.get_signal(sig_name)
                if sig is None:
                    continue
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None:
                    signal_preds[(sig_name, station)][days[idx]['date']] = (direction, confidence)

    conn.close()

    # Compute pairwise metrics
    correlation_matrix = np.zeros((n, n))
    agreement_matrix = np.zeros((n, n))
    co_fire_counts = np.zeros((n, n), dtype=int)
    individual_fire_counts = [0] * n

    for i, sig_i in enumerate(signal_names):
        # Count total fires for this signal
        total_fires_i = sum(len(signal_preds.get((sig_i, st), {})) for st in stations)
        individual_fire_counts[i] = total_fires_i

        for j, sig_j in enumerate(signal_names):
            if i == j:
                correlation_matrix[i][j] = 1.0
                agreement_matrix[i][j] = 1.0
                continue

            # Find common dates where both signals fired (across all stations)
            all_preds_i = {}
            all_preds_j = {}
            for st in stations:
                all_preds_i.update(signal_preds.get((sig_i, st), {}))
                all_preds_j.update(signal_preds.get((sig_j, st), {}))

            common_dates = set(all_preds_i.keys()).intersection(set(all_preds_j.keys()))
            co_fire_counts[i][j] = len(common_dates)

            if len(common_dates) < 10:
                continue

            # Compute metrics
            dir_agreements = []
            confs_i = []
            confs_j = []
            direction_correlation = []

            for date in common_dates:
                dir_i, conf_i = all_preds_i[date]
                dir_j, conf_j = all_preds_j[date]
                agrees = 1.0 if dir_i == dir_j else 0.0
                dir_agreements.append(agrees)
                confs_i.append(conf_i)
                confs_j.append(conf_j)
                # Direction-weighted correlation: +1 if both up, -1 if both down, 0 if disagree
                val_i = 1 if dir_i == 'up' else -1
                val_j = 1 if dir_j == 'up' else -1
                direction_correlation.append(val_i * val_j)

            # Agreement rate
            agreement_matrix[i][j] = sum(dir_agreements) / len(dir_agreements)

            # Pearson correlation of confidences
            if len(confs_i) > 1 and np.std(confs_i) > 0 and np.std(confs_j) > 0:
                corr = np.corrcoef(confs_i, confs_j)[0, 1]
                correlation_matrix[i][j] = corr
            else:
                correlation_matrix[i][j] = 0.0

    return correlation_matrix, agreement_matrix, co_fire_counts, signal_names, individual_fire_counts


def compute_mutual_information(signal_names, stations, db_path):
    """Compute simplified mutual information between signal pairs based on direction predictions."""
    registry = SignalRegistry(db_path)
    conn = sqlite3.connect(db_path)
    n = len(signal_names)

    # Collect binary predictions (1=up, 0=down)
    signal_preds = defaultdict(lambda: defaultdict(dict))
    for station in stations:
        days, market = load_station_data(station, conn)
        if len(days) < 70:
            continue
        for idx in range(60, len(days)):
            for sig_name in signal_names:
                sig = registry.get_signal(sig_name)
                if sig is None:
                    continue
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None:
                    val = 1 if direction == 'up' else 0
                    signal_preds[(sig_name, station)][days[idx]['date']] = val

    conn.close()

    mi_matrix = np.zeros((n, n))
    for i, sig_i in enumerate(signal_names):
        for j, sig_j in enumerate(signal_names):
            if i == j:
                mi_matrix[i][j] = 0.0
                continue

            all_preds_i = {}
            all_preds_j = {}
            for st in stations:
                all_preds_i.update(signal_preds.get((sig_i, st), {}))
                all_preds_j.update(signal_preds.get((sig_j, st), {}))

            common = set(all_preds_i.keys()).intersection(set(all_preds_j.keys()))
            if len(common) < 50:
                continue

            # 2x2 contingency table
            both_up = sum(1 for d in common if all_preds_i[d] == 1 and all_preds_j[d] == 1)
            i_up_j_down = sum(1 for d in common if all_preds_i[d] == 1 and all_preds_j[d] == 0)
            i_down_j_up = sum(1 for d in common if all_preds_i[d] == 0 and all_preds_j[d] == 1)
            both_down = sum(1 for d in common if all_preds_i[d] == 0 and all_preds_j[d] == 0)

            total = len(common)
            # MI = sum p(x,y) * log2(p(x,y) / (p(x)*p(y)))
            mi = 0.0
            for count in [both_up, i_up_j_down, i_down_j_up, both_down]:
                if count == 0:
                    continue
                p_xy = count / total
                # Marginal probs
                p_x_up = (both_up + i_up_j_down) / total
                p_y_up = (both_up + i_down_j_up) / total
                p_x_down = 1 - p_x_up
                p_y_down = 1 - p_y_up

                if count == both_up:
                    p_x, p_y = p_x_up, p_y_up
                elif count == i_up_j_down:
                    p_x, p_y = p_x_up, p_y_down
                elif count == i_down_j_up:
                    p_x, p_y = p_x_down, p_y_up
                else:
                    p_x, p_y = p_x_down, p_y_down

                if p_x > 0 and p_y > 0 and p_xy > 0:
                    mi += p_xy * math.log2(p_xy / (p_x * p_y))

            mi_matrix[i][j] = max(0.0, mi)

    return mi_matrix


def redundancy_analysis(corr_matrix, agreement_matrix, mi_matrix, signal_names):
    """
    Analyze redundancy and provide drop recommendations.

    A signal is considered redundant if:
    - Agreement rate > 0.85 with another signal AND
    - Confidence correlation > 0.7 AND
    - MI > 0.1 bits

    Drop the lower-accuracy signal (or lower fire count if accuracy unknown).
    """
    n = len(signal_names)
    recommendations = []

    for i in range(n):
        for j in range(i + 1, n):
            agreement = agreement_matrix[i][j]
            corr = corr_matrix[i][j]
            mi = mi_matrix[i][j]

            if agreement > 0.85 and corr > 0.7:
                recommendations.append({
                    'signal_a': signal_names[i],
                    'signal_b': signal_names[j],
                    'agreement': round(agreement, 4),
                    'correlation': round(corr, 4),
                    'mutual_info': round(mi, 4),
                    'verdict': 'REDUNDANT' if mi > 0.1 else 'HIGHLY CORRELATED',
                    'recommendation': f"Consider dropping one of {signal_names[i]} / {signal_names[j]}",
                    'drop': signal_names[j] if i < j else signal_names[i],
                })

    if not recommendations:
        recommendations.append({
            'verdict': 'NO REDUNDANCY DETECTED',
            'recommendation': 'All signals provide independent information'
        })

    return recommendations


def main():
    print("S4/N3 — Signal Correlation Matrix & Redundancy Report")
    print("=" * 70)

    # Use full ensemble including Phase 2 signals
    signal_names = BACKTEST_SIGNALS + [
        'pressure_regime_interaction', 'wind_direction_shift'
    ]

    print("Computing correlation matrix...")
    corr_matrix, agree_matrix, co_fire, signal_names, fire_counts = compute_signal_correlations(
        signal_names=signal_names, stations=STATIONS
    )

    print("Computing mutual information matrix...")
    mi_matrix = compute_mutual_information(signal_names, STATIONS, DB_PATH)

    print("Running redundancy analysis...")
    recommendations = redundancy_analysis(corr_matrix, agree_matrix, mi_matrix, signal_names)

    # Generate report
    os.makedirs(REPORT_DIR, exist_ok=True)

    # JSON output
    json_output = {
        'date': '2026-07-06',
        'signals': signal_names,
        'correlation_matrix': corr_matrix.tolist(),
        'agreement_matrix': agree_matrix.tolist(),
        'mi_matrix': mi_matrix.tolist(),
        'co_fire_counts': co_fire.tolist(),
        'individual_fire_counts': fire_counts,
        'recommendations': recommendations,
    }

    with open(REPORT_JSON, 'w') as f:
        json.dump(json_output, f, indent=2)
    print(f"JSON report: {REPORT_JSON}")

    # Markdown report
    with open(REPORT_MD, 'w') as f:
        f.write("# S4/N3 — Signal Correlation Matrix & Redundancy Report\n\n")
        f.write(f"**Date:** 2026-07-06\n")
        f.write(f"**Signals analyzed:** {len(signal_names)}\n")
        f.write(f"**Stations:** {len(STATIONS)}\n\n")

        f.write("## Signal Fire Counts\n\n")
        f.write("| Signal | Total Fires |\n")
        f.write("|--------|-------------|\n")
        for i, sig in enumerate(signal_names):
            f.write(f"| {sig} | {fire_counts[i]} |\n")

        f.write("\n## Pairwise Direction Agreement Rate\n\n")
        f.write("| Signal A | Signal B | Agreement | Co-fires | Corr | MI (bits) |\n")
        f.write("|----------|----------|-----------|----------|------|----------|\n")

        for i in range(len(signal_names)):
            for j in range(i + 1, len(signal_names)):
                f.write(f"| {signal_names[i]} | {signal_names[j]} | "
                        f"{agree_matrix[i][j]:.3f} | {co_fire[i][j]} | "
                        f"{corr_matrix[i][j]:.3f} | {mi_matrix[i][j]:.4f} |\n")

        f.write("\n## Redundancy Recommendations\n\n")
        for rec in recommendations:
            if 'signal_a' in rec:
                f.write(f"- **{rec['verdict']}**: {rec['signal_a']} ↔ {rec['signal_b']} "
                        f"(agreement={rec['agreement']:.3f}, corr={rec['correlation']:.3f}, "
                        f"MI={rec['mutual_info']:.4f})\n")
                f.write(f"  - {rec['recommendation']}\n")
            else:
                f.write(f"- **{rec['verdict']}**: {rec.get('recommendation', 'N/A')}\n")

        f.write("\n## Correlation Matrix (Confidence Pearson)\n\n")
        f.write("| | " + " | ".join(signal_names) + " |\n")
        f.write("|" + "---|" * (len(signal_names) + 1) + "\n")
        for i, sig in enumerate(signal_names):
            row = f"| {sig} | " + " | ".join(f"{corr_matrix[i][j]:.3f}" for j in range(len(signal_names))) + " |\n"
            f.write(row)

        f.write("\n## MI Matrix\n\n")
        f.write("| | " + " | ".join(signal_names) + " |\n")
        f.write("|" + "---|" * (len(signal_names) + 1) + "\n")
        for i, sig in enumerate(signal_names):
            row = f"| {sig} | " + " | ".join(f"{mi_matrix[i][j]:.4f}" for j in range(len(signal_names))) + " |\n"
            f.write(row)

    print(f"Markdown report: {REPORT_MD}")
    print("\n✅ S4/N3: Signal correlation matrix & redundancy report — COMPLETE")


if __name__ == "__main__":
    main()
