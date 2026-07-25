#!/usr/bin/env python3
"""
PHASE 0.1: Label-Permutation Test

Tests whether the goldilocks+gaussian signal combo (best performer at 76.43%
with min_agreement=2) has genuine predictive power or is an artifact of 
data labeling.

Methodology:
  1. Load METAR data and settlement labels (HIGH market direction)
  2. Run walk-forward backtest (180d train, 30d test, rolling)
  3. Measure accuracy against real labels
  4. Create 10 random permutations of settlement labels
  5. Measure accuracy against each shuffled set
  6. Report: mean shuffle accuracy, std dev, best shuffle, threshold breaches

Constraint: Scripts-only. No AI/ML in the loop. numpy for permutations.
Self-contained signal implementations (goldilocks + gaussian).

Saves results to: data/label_permutation_results.json
"""

import sqlite3
import math
import json
import os
import sys
import random
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'metar_backfill.db')
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'label_permutation_results.json')

# Same 20 stations as the combinatorial search
ALL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO'
]


# ─── Self-contained signal implementations ─────────────────────────────────

def goldilocks_signal(idx, days):
    """
    Goldilocks signal: detect spikes >= 2°F.
    Spike up → predict down (reversion). Spike down → predict up (reversion).
    Matches core/signals.py GoldilocksSignal.evaluate().
    """
    if len(days) < 2 or idx < 1:
        return None, 0.0

    today = days[idx]
    yesterday = days[idx - 1]

    temp_change = today['high'] - yesterday['high']

    if abs(temp_change) < 2.0:
        return None, 0.0

    if temp_change > 0:
        # Spike up → predict DOWN (reversion)
        daily_high_margin = max(0.0, temp_change / 5.0)
        confidence = min(1.0, 0.40 + daily_high_margin * 0.15)
        return 'down', confidence
    else:
        # Spike down → predict UP (reversion)
        daily_low_margin = max(0.0, abs(temp_change) / 5.0)
        confidence = min(1.0, (0.25 + daily_low_margin * 0.10) * 0.85)
        return 'up', confidence


def gaussian_signal(idx, days):
    """
    Gaussian signal: z-score from 48-day rolling window.
    z > 1.0 → predict DOWN. z < -1.0 → predict UP.
    Confidence = abs(z_score).
    Matches core/signals.py GaussianSignal.evaluate().
    """
    if idx < 49:
        return None, 0.0

    window = days[idx - 49:idx - 1]
    highs = [d['high'] for d in window if d['high'] is not None]
    if len(highs) < 48:
        return None, 0.0

    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01

    current = days[idx - 1]['high']
    if current is None:
        return None, 0.0

    z_score = (current - mean) / std

    if z_score > 1.0:
        return 'down', abs(z_score)
    elif z_score < -1.0:
        return 'up', abs(z_score)
    else:
        return None, 0.0


def combine_signals(signals, idx, days, min_agreement=2):
    """
    Combine multiple signals by weighted majority vote.
    Only makes a prediction when at least min_agreement signals fire.
    Matches logic from run_combinatorial_search.py.
    """
    predictions = []
    for sig_fn in signals:
        direction, confidence = sig_fn(idx, days)
        if direction is not None and confidence > 0:
            predictions.append((direction, confidence))

    if len(predictions) == 0 or len(predictions) < min_agreement:
        return None, 0.0

    up_weight = sum(conf for d, conf in predictions if d == 'up')
    down_weight = sum(conf for d, conf in predictions if d == 'down')

    if up_weight > down_weight:
        direction = 'up'
        confidence = up_weight / len(predictions)
    elif down_weight > up_weight:
        direction = 'down'
        confidence = down_weight / len(predictions)
    else:
        return None, 0.0

    if len(predictions) >= min_agreement:
        consensus_factor = sum(1 for p in predictions if p[0] == direction) / len(predictions)
        confidence *= consensus_factor

    return direction, confidence


# ─── Data loading ──────────────────────────────────────────────────────────

def load_station_data(conn, station):
    """
    Load daily METAR data and settlement labels for a station.
    Returns (days, market_label_map).
    """
    cur = conn.cursor()

    # METAR daily aggregates
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))

    days = []
    for row in cur.fetchall():
        if any(v is None for v in row[1:]):
            continue
        days.append({
            'date': row[0],
            'high': row[1], 'low': row[2], 'dewpoint': row[3], 'temp': row[4],
            'wind_dir': row[5], 'wind_speed': row[6], 'pressure': row[7]
        })

    # Settlement labels (HIGH market: direction = up if bucket > prior)
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))

    labels = {}
    for row in cur.fetchall():
        date, bucket, prior = row
        if prior is not None:
            labels[date] = 'up' if bucket > prior else 'down'

    return days, labels


def walk_forward_predictions(days, labels, signals, train_days=180, test_days=30, min_agreement=2):
    """
    Run walk-forward backtest against the provided label map.
    Returns list of (predicted, actual) tuples and running counts.
    
    The label map can be the real labels or shuffled labels.
    """
    results = []
    start = train_days

    while start + test_days <= len(days):
        test_start = start
        test_end = min(start + test_days, len(days))

        for idx in range(test_start, test_end):
            date = days[idx]['date']
            actual = labels.get(date)
            if actual is None:
                continue

            predicted_dir, confidence = combine_signals(signals, idx, days, min_agreement)
            if predicted_dir is not None:
                results.append((predicted_dir, actual))

        start += test_days
        # Limit to same number of test windows as the original search
        if start >= train_days + 4 * test_days:
            break

    return results


def compute_accuracy(results):
    """Compute accuracy from (predicted, actual) list."""
    if not results:
        return 0.0, 0
    correct = sum(1 for p, a in results if p == a)
    return correct / len(results), len(results)


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    random.seed(42)
    print("=" * 90)
    print("PHASE 0.1: LABEL PERMUTATION TEST")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Signals: goldilocks + gaussian (min_agreement=2)")
    print(f"Method: Walk-forward (180d train, 30d test), 1000 shuffles")
    print()

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    # Use the same goldilocks+gaussian combo
    goldilocks_gaussian = [goldilocks_signal, gaussian_signal]

    # ─── Per-station results ───────────────────────────────────────────
    station_results = {}
    all_real_results = []  # Aggregate across all stations (real)
    all_shuffle_results = [[] for _ in range(1000)]  # 1000 lists, one per shuffle

    print(f"{'Station':<8} {'Real Acc':>10} {'Real Trades':>12} | {'Shuffle Avg':>12} {'Shuffle SD':>12} {'Best Shuf':>10} {'Above 55%':>10}")
    print("-" * 90)

    for station in ALL_STATIONS:
        days, real_labels = load_station_data(conn, station)

        if len(days) < 250:
            print(f"  {station:<6}  Skipped — insufficient data ({len(days)} days)")
            continue

        # ── Run against REAL labels ──
        real_results = walk_forward_predictions(days, real_labels, goldilocks_gaussian)
        real_acc, real_n = compute_accuracy(real_results)
        all_real_results.extend(real_results)

        # ── Run against 1000 shuffled label sets ──
        shuffle_accs = []
        # Collect all label-date pairs
        label_dates = sorted(real_labels.keys())
        label_values = [real_labels[d] for d in label_dates]

        for shuffle_idx in range(1000):
            # Create a permuted copy of the label values
            shuffled_labels = label_values.copy()
            random.shuffle(shuffled_labels)

            # Build shuffled label map
            shuffled_label_map = {}
            for d, val in zip(label_dates, shuffled_labels):
                shuffled_label_map[d] = val

            shuf_results = walk_forward_predictions(days, shuffled_label_map, goldilocks_gaussian)
            shuf_acc, shuf_n = compute_accuracy(shuf_results)
            shuffle_accs.append(shuf_acc)
            all_shuffle_results[shuffle_idx].extend(shuf_results)

        mean_shuffle = sum(shuffle_accs) / len(shuffle_accs)
        std_shuffle = math.sqrt(sum((a - mean_shuffle) ** 2 for a in shuffle_accs) / len(shuffle_accs))
        best_shuffle = max(shuffle_accs)
        above_55 = sum(1 for a in shuffle_accs if a > 0.55)

        station_results[station] = {
            'real_accuracy': round(real_acc, 6),
            'real_trades': real_n,
            'shuffle_accuracies': [round(a, 6) for a in shuffle_accs],
            'mean_shuffle_accuracy': round(mean_shuffle, 6),
            'std_shuffle_accuracy': round(std_shuffle, 6),
            'best_shuffle_accuracy': round(best_shuffle, 6),
            'shuffles_above_55pct': above_55
        }

        print(f"{station:<8} {real_acc:>10.2%} {real_n:>12} | "
              f"{mean_shuffle:>12.2%} {std_shuffle:>12.4f} {best_shuffle:>10.2%} {above_55:>10}/1000")

    # ─── Aggregate across all stations ──────────────────────────────────
    real_correct = sum(1 for p, a in all_real_results if p == a)
    real_total = len(all_real_results)
    real_agg_acc = real_correct / real_total if real_total > 0 else 0

    shuffle_agg_accs = []
    for shuf_idx in range(1000):
        shuf_correct = sum(1 for p, a in all_shuffle_results[shuf_idx] if p == a)
        shuf_total = len(all_shuffle_results[shuf_idx])
        shuf_acc = shuf_correct / shuf_total if shuf_total > 0 else 0
        shuffle_agg_accs.append(shuf_acc)

    agg_mean_shuffle = sum(shuffle_agg_accs) / len(shuffle_agg_accs)
    agg_std_shuffle = math.sqrt(sum((a - agg_mean_shuffle) ** 2 for a in shuffle_agg_accs) / len(shuffle_agg_accs))
    agg_best_shuffle = max(shuffle_agg_accs)
    agg_above_55 = sum(1 for a in shuffle_agg_accs if a > 0.55)

    print()
    print("=" * 90)
    print("AGGREGATE RESULTS (All Stations Combined)")
    print("=" * 90)
    print(f"  Real labels accuracy:     {real_agg_acc:.2%} ({real_correct}/{real_total})")
    print(f"  Shuffle mean accuracy:    {agg_mean_shuffle:.2%}")
    print(f"  Shuffle std deviation:    {agg_std_shuffle:.4f}")
    print(f"  Best shuffle accuracy:    {agg_best_shuffle:.2%}")
    print(f"  Shuffles above 55%:       {agg_above_55}/1000")

    # Significance check
    print()
    print("=" * 90)
    print("SIGNIFICANCE TEST")
    print("=" * 90)

    # Count how many shuffles exceeded real accuracy
    n_exceed_real = sum(1 for a in shuffle_agg_accs if a >= real_agg_acc)
    perm_p_value = (n_exceed_real + 1) / (len(shuffle_agg_accs) + 1)

    print(f"  Real accuracy:     {real_agg_acc:.2%}")
    print(f"  Shuffles that exceeded real accuracy: {n_exceed_real}/1000")
    print(f"  Permutation p-value (approximate):    {perm_p_value:.4f}")
    print(f"  Null hypothesis rejected?              {'✓ YES' if perm_p_value < 0.05 else '✗ NO — model cannot distinguish from chance'}")

    if real_agg_acc > agg_mean_shuffle + 2 * agg_std_shuffle:
        print(f"  Real accuracy is >2σ above shuffle mean: ✓ SIGNIFICANT")
    else:
        print(f"  Real accuracy is >2σ above shuffle mean: ✗ NOT SIGNIFICANT")

    # ─── Save results ──────────────────────────────────────────────────
    output = {
        'metadata': {
            'timestamp': datetime.utcnow().isoformat(),
            'database': str(DB_PATH),
            'signals_tested': 'goldilocks+gaussian',
            'min_agreement': 2,
            'walk_forward_params': {'train_days': 180, 'test_days': 30, 'test_windows': 4},
            'num_shuffles': 1000,
            'num_stations': len(ALL_STATIONS),
            'reproducible_seed': 42
        },
        'per_station': station_results,
        'aggregate': {
            'real_labels': {
                'accuracy': round(real_agg_acc, 6),
                'correct': real_correct,
                'total': real_total
            },
            'shuffled_labels': {
                'mean_accuracy': round(agg_mean_shuffle, 6),
                'std_deviation': round(agg_std_shuffle, 6),
                'best_accuracy': round(agg_best_shuffle, 6),
                'shuffles_above_55pct': agg_above_55,
                'all_accuracies': [round(a, 6) for a in shuffle_agg_accs]
            },
            'significance': {
                'permutation_p_value': round(perm_p_value, 6),
                'null_rejected': perm_p_value < 0.05,
                'exceeds_2sigma': real_agg_acc > agg_mean_shuffle + 2 * agg_std_shuffle
            }
        }
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {OUTPUT_PATH}")
    conn.close()
    print("\nPHASE 0.1 COMPLETE")


if __name__ == '__main__':
    main()
