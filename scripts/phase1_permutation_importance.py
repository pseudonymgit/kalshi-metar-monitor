#!/usr/bin/env python3
"""
Phase 1.3: Permutation Feature Importance

For the goldilocks+gaussian combo (and top 5 combos by accuracy), shuffles
each signal's vote independently and measures the accuracy drop.

Identifies:
- Per-signal importance ranking
- Signals that consistently reduce accuracy (negative contributors)

Scripts-only, no AI in the loop. Results saved to data/permutation_importance_results.json.
"""

import sqlite3
import json
import sys
import os
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.signals import create_signal_registry

DB_PATH = os.path.join(_REPO_ROOT, "data", "metar_backfill.db")
OUTPUT_PATH = os.path.join(_REPO_ROOT, "data", "permutation_importance_results.json")
COMBO_SEARCH_PATH = os.path.join(_REPO_ROOT, "data", "combinatorial_search_results.json")

WALK_FORWARD_TRAIN = 180  # days
WALK_FORWARD_TEST = 30    # days
N_PERMUTATIONS = 50       # Number of shuffles per signal per combo

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
    Returns a list of dicts: {date, predicted_direction, confidence, actual_direction, correct}
    """
    days = load_station_data(station, conn)
    if len(days) < WALK_FORWARD_TRAIN + WALK_FORWARD_TEST:
        return [], days

    results = []
    n = len(days)

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
            correct = 1 if direction == actual else 0
            results.append({
                'date': days[idx]['date'],
                'predicted': direction,
                'confidence': confidence,
                'actual': actual,
                'correct': correct
            })

    return results, days


def compute_ensemble_prediction(preds_by_signal, weights=None):
    """
    Compute ensemble prediction from individual signal predictions.
    Simple majority vote (or weighted if weights provided).
    Returns predicted direction and confidence.
    """
    up_votes = 0
    down_votes = 0

    for sig_name, preds in preds_by_signal.items():
        if preds is None:
            continue
        direction, confidence = preds
        if direction == 'up':
            up_votes += confidence
        elif direction == 'down':
            down_votes += confidence

    total = up_votes + down_votes
    if total < 0.01:
        return None, 0.0

    if up_votes > down_votes:
        return 'up', up_votes / total
    elif down_votes > up_votes:
        return 'down', down_votes / total
    else:
        return None, 0.0


def compute_ensemble_accuracy(signal_preds_by_date, combo_signals, station, permutations=None):
    """
    Compute accuracy of an ensemble of signals on a per-date basis.
    
    Args:
        signal_preds_by_date: {date: {signal_name: (direction, confidence)}}
        combo_signals: list of signal names in the combo
        station: station code (for logging)
        permutations: optional dict {signal_name: shuffled_indices} for permutation testing
    
    Returns:
        accuracy (float)
    """
    correct = 0
    total = 0

    for date, preds in signal_preds_by_date.items():
        # Filter to combo signals only
        combo_preds = {}
        for sig_name in combo_signals:
            if sig_name in preds and preds[sig_name] is not None:
                combo_preds[sig_name] = preds[sig_name]

        # If permutations are provided, shuffle specified signals
        if permutations:
            for sig_name, shuffled_indices in permutations.items():
                if sig_name in combo_preds:
                    # Look up the shuffled version
                    date_list = list(signal_preds_by_date.keys())
                    try:
                        orig_idx = date_list.index(date)
                        if orig_idx < len(shuffled_indices):
                            # Get the shuffled prediction for this signal
                            shuffled_date = date_list[shuffled_indices[orig_idx]]
                            if shuffled_date in signal_preds_by_date:
                                shuffled_pred = signal_preds_by_date[shuffled_date].get(sig_name)
                                if shuffled_pred:
                                    combo_preds[sig_name] = shuffled_pred
                    except ValueError:
                        pass

        if len(combo_preds) < 1:
            continue

        predicted, confidence = compute_ensemble_prediction(combo_preds)
        if predicted is None:
            continue

        # Get actual direction from any signal's prediction
        for sig_name in combo_signals:
            if sig_name in preds and preds[sig_name] is not None:
                actual = preds[sig_name].get('actual')
                if actual is not None:
                    if predicted == actual:
                        correct += 1
                    total += 1
                    break

    return correct / total if total > 0 else 0.0, total


def load_top_combos():
    """Load top 5 combos from combinatorial search results."""
    combos = [
        {'name': 'goldilocks+gaussian', 'signals': ['goldilocks', 'gaussian']},
    ]

    try:
        with open(COMBO_SEARCH_PATH, 'r') as f:
            search_data = json.load(f)

        # Extract combos ranked by accuracy
        combo_results = search_data.get('results', [])
        if not combo_results:
            # Try alternate key
            combo_results = search_data.get('combo_results', [])
        if not combo_results:
            combo_results = search_data.get('combinations', [])

        # Sort by accuracy descending
        if combo_results:
            if isinstance(combo_results[0], dict):
                combo_results.sort(key=lambda x: -x.get('accuracy', 0))
            elif isinstance(combo_results[0], list) or isinstance(combo_results[0], tuple):
                # Assume (signals, accuracy) format
                combo_results.sort(key=lambda x: -x[1])

        # Take top 5 non-duplicate combos plus goldilocks+gaussian
        added = set()
        for signals in [tuple(['goldilocks', 'gaussian'])]:
            added.add(tuple(sorted(signals)))

        for item in combo_results:
            if len(added) >= 5:
                break
            if isinstance(item, dict):
                sigs = tuple(sorted(item.get('signals', [])))
                acc = item.get('accuracy', 0)
            elif isinstance(item, (list, tuple)):
                sigs = tuple(sorted(item[0]))
                acc = item[1]
            else:
                continue

            if len(sigs) < 2:
                continue
            if sigs not in added:
                added.add(sigs)
                combos.append({
                    'name': '+'.join(sigs),
                    'signals': list(sigs),
                    'accuracy': acc
                })
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print("WARNING: Could not load combinatorial search results: {}".format(e))
        print("Using default combos (goldilocks+gaussian + 4 common combos).")
        # Add fallback combos
        fallback = [
            ['goldilocks', 'gaussian', 'pressure_delta'],
            ['goldilocks', 'persistence', 'nwp_analog'],
            ['gaussian', 'pressure_delta', 'wind_direction_shift'],
            ['goldilocks', 'gaussian', 'pressure_delta', 'nwp_analog'],
        ]
        for sigs in fallback:
            combos.append({
                'name': '+'.join(sigs),
                'signals': sigs
            })

    return combos


def main():
    print("=" * 70)
    print("Phase 1.3: Permutation Feature Importance")
    print("=" * 70)
    print("Database: {}".format(DB_PATH))
    print("Permutations per signal: {}".format(N_PERMUTATIONS))
    print()

    # Connect to DB
    conn = sqlite3.connect(DB_PATH)

    # Create signal registry
    registry = create_signal_registry(DB_PATH)
    signals = registry.get_all_signals()
    signal_names = list(signals.keys())

    # ---- Step 1: Load combos ----
    combos = load_top_combos()
    print("Combos to analyze ({}):".format(len(combos)))
    for c in combos:
        print("  {}: {}".format(c['name'], c['signals']))
    print()

    # ---- Step 2: Collect all signal predictions by date across all stations ----
    # Structure: {station: {date: {signal_name: (direction, confidence, actual)}}}
    all_signal_preds = {}

    print("Collecting predictions across all signals and stations...")
    for station in STATIONS:
        print("  {}...".format(station), end=" ", flush=True)
        station_preds = {}

        for sig_name in signal_names:
            signal = signals.get(sig_name)
            if signal is None:
                continue

            results, _ = run_walk_forward_predictions(signal, station, conn)
            for r in results:
                date = r['date']
                if date not in station_preds:
                    station_preds[date] = {}
                station_preds[date][sig_name] = {
                    'direction': r['predicted'],
                    'confidence': r['confidence'],
                    'actual': r['actual'],
                    'correct': r['correct']
                }

        all_signal_preds[station] = station_preds
        print("{} dates".format(len(station_preds)))

    print()

    # ---- Step 3: For each combo, compute baseline accuracy and per-signal permutation importance ----
    combo_results = []

    for combo in combos:
        combo_name = combo['name']
        combo_signals = combo['signals']

        # Filter to signals that exist in the registry
        valid_signals = [s for s in combo_signals if s in signals]
        if len(valid_signals) < 2:
            print("SKIP {}: fewer than 2 valid signals.".format(combo_name))
            continue

        print("Analyzing '{}' ({} signals)...".format(combo_name, len(valid_signals)))

        # Compute baseline accuracy per station
        baseline_correct = 0
        baseline_total = 0
        station_baselines = {}

        for station in STATIONS:
            station_preds = all_signal_preds.get(station, {})
            station_correct = 0
            station_total = 0

            for date, preds in station_preds.items():
                combo_preds = {}
                for sig_name in valid_signals:
                    if sig_name in preds and preds[sig_name]['direction'] is not None:
                        combo_preds[sig_name] = (
                            preds[sig_name]['direction'],
                            preds[sig_name]['confidence']
                        )

                if len(combo_preds) < 1:
                    continue

                predicted, conf = compute_ensemble_prediction(combo_preds)
                if predicted is None:
                    continue

                # Get actual from any signal
                for sig_name in valid_signals:
                    if sig_name in preds and preds[sig_name]['actual'] is not None:
                        actual = preds[sig_name]['actual']
                        if predicted == actual:
                            station_correct += 1
                        station_total += 1
                        break

            station_baselines[station] = {
                'correct': station_correct,
                'total': station_total,
                'accuracy': round(station_correct / station_total, 4) if station_total > 0 else 0.0
            }
            baseline_correct += station_correct
            baseline_total += station_total

        baseline_accuracy = baseline_correct / baseline_total if baseline_total > 0 else 0.0
        print("  Baseline accuracy: {:.4f} ({} correct / {} total)".format(
            baseline_accuracy, baseline_correct, baseline_total))

        # ---- Permutation test for each signal in the combo ----
        per_signal_importance = {}

        for sig_to_shuffle in valid_signals:
            print("  Shuffling '{}'...".format(sig_to_shuffle), end=" ", flush=True)

            permuted_accuracies = []
            for perm in range(N_PERMUTATIONS):
                # Shuffle this signal's votes across all dates for this station
                perm_correct = 0
                perm_total = 0

                for station in STATIONS:
                    station_preds = all_signal_preds.get(station, {})
                    dates = list(station_preds.keys())
                    if len(dates) < 2:
                        continue

                    # Create shuffled indices for this signal
                    original_indices = list(range(len(dates)))
                    shuffled_indices = list(original_indices)
                    random.shuffle(shuffled_indices)

                    for date_idx, date in enumerate(dates):
                        preds = station_preds[date]
                        combo_preds = {}

                        for sig_name in valid_signals:
                            if sig_name not in preds or preds[sig_name]['direction'] is None:
                                continue

                            if sig_name == sig_to_shuffle:
                                # Use shuffled version
                                shuffled_date = dates[shuffled_indices[date_idx]]
                                shuffled_pred = station_preds.get(shuffled_date, {}).get(sig_to_shuffle)
                                if shuffled_pred and shuffled_pred['direction'] is not None:
                                    combo_preds[sig_name] = (
                                        shuffled_pred['direction'],
                                        shuffled_pred['confidence']
                                    )
                            else:
                                combo_preds[sig_name] = (
                                    preds[sig_name]['direction'],
                                    preds[sig_name]['confidence']
                                )

                        if len(combo_preds) < 1:
                            continue

                        predicted, conf = compute_ensemble_prediction(combo_preds)
                        if predicted is None:
                            continue

                        for sig_name in valid_signals:
                            if sig_name in preds and preds[sig_name]['actual'] is not None:
                                actual = preds[sig_name]['actual']
                                if predicted == actual:
                                    perm_correct += 1
                                perm_total += 1
                                break

                perm_accuracy = perm_correct / perm_total if perm_total > 0 else 0.0
                permuted_accuracies.append(perm_accuracy)

            # Compute statistics
            mean_perm_acc = np.mean(permuted_accuracies)
            std_perm_acc = np.std(permuted_accuracies)
            accuracy_drop = baseline_accuracy - mean_perm_acc

            per_signal_importance[sig_to_shuffle] = {
                'baseline_accuracy': round(baseline_accuracy, 4),
                'mean_permuted_accuracy': round(float(mean_perm_acc), 4),
                'std_permuted_accuracy': round(float(std_perm_acc), 4),
                'accuracy_drop': round(float(accuracy_drop), 4),
                'accuracy_drop_pct': round(float(accuracy_drop / baseline_accuracy * 100), 2) if baseline_accuracy > 0 else 0.0,
                'n_permutations': N_PERMUTATIONS,
                'importance_rank': 0  # will be set after sorting
            }

            print("drop={:.4f} (mean={:.4f} vs baseline={:.4f})".format(
                accuracy_drop, mean_perm_acc, baseline_accuracy))

        # Rank signals by importance (accuracy drop)
        ranked = sorted(per_signal_importance.items(), key=lambda x: -x[1]['accuracy_drop'])
        for rank, (sig_name, imp) in enumerate(ranked, 1):
            imp['importance_rank'] = rank

        combo_results.append({
            'combo_name': combo_name,
            'signals': valid_signals,
            'n_signals': len(valid_signals),
            'baseline_accuracy': round(baseline_accuracy, 4),
            'baseline_correct': baseline_correct,
            'baseline_total': baseline_total,
            'per_signal_importance': per_signal_importance,
            'importance_ranking': [sig_name for sig_name, _ in ranked],
            'negative_contributors': [
                sig_name for sig_name, imp in per_signal_importance.items()
                if imp['accuracy_drop'] < 0
            ]
        })

        print("  Importance ranking: {}".format(" > ".join(
            ["{} (drop={:.4f})".format(sig, per_signal_importance[sig]['accuracy_drop'])
             for sig, _ in ranked])))
        print()

    # ---- Step 4: Aggregate importance across all combos ----
    aggregate_importance = defaultdict(list)
    all_negative_contributors = set()

    for cr in combo_results:
        for sig_name, imp in cr['per_signal_importance'].items():
            aggregate_importance[sig_name].append({
                'combo': cr['combo_name'],
                'accuracy_drop': imp['accuracy_drop'],
                'baseline_accuracy': imp['baseline_accuracy'],
                'mean_permuted_accuracy': imp['mean_permuted_accuracy'],
            })
            if imp['accuracy_drop'] < 0:
                all_negative_contributors.add(sig_name)

    # Compute mean importance per signal across all combos
    overall_importance = {}
    for sig_name, entries in aggregate_importance.items():
        drops = [e['accuracy_drop'] for e in entries]
        overall_importance[sig_name] = {
            'mean_accuracy_drop': round(float(np.mean(drops)), 4),
            'std_accuracy_drop': round(float(np.std(drops)), 4) if len(drops) > 1 else 0.0,
            'max_accuracy_drop': round(float(max(drops)), 4),
            'min_accuracy_drop': round(float(min(drops)), 4),
            'n_combos': len(entries),
            'is_negative_contributor': sig_name in all_negative_contributors
        }

    # Rank by mean importance
    overall_ranking = sorted(overall_importance.items(), key=lambda x: -x[1]['mean_accuracy_drop'])

    # ---- Build output ----
    output = {
        'metadata': {
            'database': DB_PATH,
            'walk_forward_train_days': WALK_FORWARD_TRAIN,
            'walk_forward_test_days': WALK_FORWARD_TEST,
            'n_permutations': N_PERMUTATIONS,
            'stations': STATIONS,
            'combos_analyzed': len(combo_results),
            'signals_tested': list(signal_names)
        },
        'combo_results': combo_results,
        'overall_importance': {
            sig_name: data
            for sig_name, data in overall_ranking
        },
        'overall_importance_ranking': [sig_name for sig_name, _ in overall_ranking],
        'negative_contributors': sorted(all_negative_contributors) if all_negative_contributors else [],
        'summary': {
            'most_important_signal': overall_ranking[0][0] if overall_ranking else None,
            'most_important_drop': overall_ranking[0][1]['mean_accuracy_drop'] if overall_ranking else None,
            'least_important_signal': overall_ranking[-1][0] if overall_ranking else None,
            'least_important_drop': overall_ranking[-1][1]['mean_accuracy_drop'] if overall_ranking else None,
            'negative_contributors_count': len(all_negative_contributors),
            'negative_contributors': sorted(all_negative_contributors)
        }
    }

    # Save output
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print("=" * 70)
    print("Results saved to: {}".format(OUTPUT_PATH))
    print()
    print("Overall Signal Importance Ranking:")
    for rank, (sig_name, imp) in enumerate(overall_ranking, 1):
        neg = " (NEGATIVE CONTRIBUTOR)" if imp['is_negative_contributor'] else ""
        print("  {}. {:25}  mean drop = {:.4f}  ({} combos){}".format(
            rank, sig_name, imp['mean_accuracy_drop'], imp['n_combos'], neg))

    if all_negative_contributors:
        print()
        print("Signals that consistently reduce accuracy (negative contributors):")
        for sig_name in sorted(all_negative_contributors):
            imp = overall_importance.get(sig_name, {})
            print("  {}: mean drop = {:.4f}".format(sig_name, imp.get('mean_accuracy_drop', 0)))

    conn.close()
    print("\nPhase 1.3 complete.")


if __name__ == "__main__":
    main()