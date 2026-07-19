#!/usr/bin/env python3
"""
PHASE 0.1a: Broad Label-Permutation Test

Extends the initial goldilocks+gaussian test to cover ALL 10 individual
signals and the top 5 lane combos by accuracy and top 5 by Sharpe.

Methodology:
  1. Load METAR data and settlement labels (HIGH market direction)
  2. For each signal/combo, run walk-forward backtest (180d train, 30d test, rolling)
  3. Measure real accuracy
  4. Create 10 random permutations of settlement labels
  5. Measure shuffled accuracy distribution
  6. Report z-score, 3σ significance, and whether any shuffle beat real

Constraint: Scripts-only. No AI/ML in the loop. numpy for permutations.
Self-contained signal implementations matching core/signals/.

Saves results to: data/label_permutation_broad_results.json
"""

import sqlite3
import math
import json
import os
import sys
import random
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'metar_backfill.db')
COMBO_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'combinatorial_search_10sig.json')
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'label_permutation_broad_results.json')

# Same 20 stations as the original combinatorial search
ALL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO'
]


# ═══════════════════════════════════════════════════════════════════════════
# SELF-CONTAINED SIGNAL IMPLEMENTATIONS (matching core/signals/ behavior)
# ═══════════════════════════════════════════════════════════════════════════


def signal_persistence(idx, days):
    """
    Persistence: yesterday's direction predicts same today.
    Confidence: 0.3 (weak baseline).
    Matches core/signals/persistence_signal.py PersistenceSignal.evaluate().
    """
    if idx < 2:
        return None, 0.0
    prev_high = days[idx-2]['high']
    today_high = days[idx-1]['high']
    if today_high is None or prev_high is None:
        return None, 0.0
    direction = 'up' if today_high > prev_high else 'down'
    return direction, 0.3


def signal_simple_trend(idx, days):
    """
    Simple Trend: momentum using day-over-day comparison.
    Matches core/signals/simple_trend_signal.py SimpleTrendSignal.evaluate().
    """
    if idx < 2:
        return None, 0.0
    prev_high = days[idx-2]['high']
    current_high = days[idx-1]['high']
    if current_high is None or prev_high is None:
        return None, 0.0
    direction = 'up' if current_high > prev_high else 'down'
    return direction, 0.4


def signal_gaussian(idx, days):
    """
    Gaussian: z-score from 48-day rolling window.
    z > 1.0 → predict DOWN. z < -1.0 → predict UP.
    Confidence = abs(z_score).
    Matches core/signals/gaussian_signal.py GaussianSignal.evaluate().
    """
    if idx < 49:
        return None, 0.0
    window = days[idx-49:idx-1]
    highs = [d['high'] for d in window if d['high'] is not None]
    if len(highs) < 48:
        return None, 0.0
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    current = days[idx-1]['high']
    if current is None:
        return None, 0.0
    z_score = (current - mean) / std
    if z_score > 1.0:
        return 'down', abs(z_score)
    elif z_score < -1.0:
        return 'up', abs(z_score)
    else:
        return None, 0.0


def signal_gaussian_v2(idx, days):
    """
    Gaussian V2: z-score from 30-day rolling window, tighter threshold.
    z > 0.5 → predict DOWN. z < -0.5 → predict UP.
    Matches core/signals/gaussian_v2_signal.py GaussianV2Signal.evaluate().
    """
    if idx < 31:
        return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window if d['high'] is not None]
    if len(highs) < 30:
        return None, 0.0
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    current = days[idx-1]['high']
    if current is None:
        return None, 0.0
    z_score = (current - mean) / std
    if z_score > 0.5:
        return 'down', abs(z_score)
    elif z_score < -0.5:
        return 'up', abs(z_score)
    else:
        return None, 0.0


def signal_goldilocks(idx, days):
    """
    Goldilocks: spike >= 2°F → predict reversion.
    Spike up → predict down (base 0.40). Spike down → predict up (base 0.25).
    Matches core/signals/goldilocks_signal.py GoldilocksSignal.evaluate().
    """
    if len(days) < 2 or idx < 1:
        return None, 0.0
    today = days[idx]
    yesterday = days[idx - 1]
    temp_change = today['high'] - yesterday['high']
    if abs(temp_change) < 2.0:
        return None, 0.0
    if temp_change > 0:
        daily_high_margin = max(0.0, temp_change / 5.0)
        confidence = min(1.0, 0.40 + daily_high_margin * 0.15)
        return 'down', confidence
    else:
        daily_low_margin = max(0.0, abs(temp_change) / 5.0)
        confidence = min(1.0, (0.25 + daily_low_margin * 0.10) * 0.85)
        return 'up', confidence


def signal_pressure_delta(idx, days):
    """
    Pressure Delta: 2-day cumulative pressure change.
    ΔP > 3.0mb → predict UP. ΔP < -3.0mb → predict DOWN.
    Matches core/signals/pressure_delta_signal.py PressureDeltaSignal.evaluate().
    """
    if idx < 4:
        return None, 0.0
    yesterday_pressure = days[idx-1]['pressure']
    three_days_ago_pressure = days[idx-4]['pressure']
    if yesterday_pressure is None or three_days_ago_pressure is None:
        return None, 0.0
    dp = yesterday_pressure - three_days_ago_pressure
    threshold = 3.0
    if abs(dp) < threshold:
        return None, 0.0
    direction = 'up' if dp > 0 else 'down'
    confidence = min(abs(dp) / 5.0, 0.8)
    return direction, confidence


def _circular_diff(theta1, theta2):
    """Smallest angle between two compass bearings (0-360)."""
    diff = abs(theta1 - theta2)
    return min(diff, 360 - diff)


def _is_northerly(wdir):
    return (0 <= wdir < 45) or (315 <= wdir <= 360)


def _is_southerly(wdir):
    return 135 <= wdir <= 225


def signal_wind_direction_shift(idx, days):
    """
    Wind Direction Shift: significant circular wind change (>45°) with speed >10kt.
    Matches core/signals/wind_direction_shift.py WindDirectionShiftSignal.evaluate().
    """
    lookback = 3
    if idx < lookback + 1:
        return None, 0.0

    wind_history = []
    for i in range(idx, idx - lookback - 1, -1):
        if i < 0:
            break
        d = days[i]
        if d.get('wind_dir') is not None and d.get('wind_speed') is not None:
            wind_history.append((d['date'], float(d['wind_dir']), float(d['wind_speed'])))

    if len(wind_history) < 2:
        return None, 0.0

    # Check consecutive day shift
    old_dir = wind_history[1][1]
    new_dir = wind_history[0][1]
    new_speed = wind_history[0][2]
    old_speed = wind_history[1][2]
    avg_speed = (new_speed + old_speed) / 2.0
    angle_diff = _circular_diff(old_dir, new_dir)

    shift_found = False
    if angle_diff > 45.0 and avg_speed > 10.0:
        shift_found = True

    # Check 3-day shift if 2-day failed
    if not shift_found and len(wind_history) >= 3:
        old_dir = wind_history[-1][1]
        new_dir = wind_history[0][1]
        new_speed = wind_history[0][2]
        angle_diff = _circular_diff(old_dir, new_dir)
        if angle_diff > 45.0 and new_speed > 10.0:
            shift_found = True

    if not shift_found:
        return None, 0.0

    # Infer temperature implication
    if _is_northerly(new_dir):
        direction = 'down'
        base_conf = 0.45 + min(0.25, angle_diff / 90.0)
    elif _is_southerly(new_dir):
        direction = 'up'
        base_conf = 0.45 + min(0.25, angle_diff / 90.0)
    else:
        direction = 'up' if new_dir < old_dir else 'down'
        base_conf = 0.35

    speed_boost = min(0.2, (avg_speed - 10.0) / 20.0)
    angle_boost = min(0.1, (angle_diff - 45.0) / 90.0)
    total_confidence = min(0.85, base_conf + speed_boost + angle_boost)
    total_confidence = max(0.50, total_confidence)

    return direction, total_confidence


def signal_regime(idx, days):
    """
    Regime: trade reversion only in stable (low-vol, low-slope) regimes.
    Matches core/signals/regime_signal.py RegimeSignal.evaluate().
    """
    lookback_vol = 15
    lookback_mean = 30
    if idx < lookback_mean + 1:
        return None, 0.0

    vol_window = days[idx - lookback_vol - 1: idx - 1]
    vol_highs = [d['high'] for d in vol_window]
    if len(vol_highs) < 2:
        return None, 0.0
    vol_mean = sum(vol_highs) / len(vol_highs)
    vol_var = sum((h - vol_mean) ** 2 for h in vol_highs) / (len(vol_highs) - 1)
    vol = math.sqrt(vol_var) if vol_var > 0 else 0.01
    slope = (vol_highs[-1] - vol_highs[0]) / len(vol_highs)

    if vol >= 1.0 or abs(slope) >= 0.5:
        return None, 0.0

    mean_window = days[idx - lookback_mean - 1: idx - 1]
    mean_highs = [d['high'] for d in mean_window]
    if not mean_highs:
        return None, 0.0
    mean_30 = sum(mean_highs) / len(mean_highs)

    dist = days[idx - 1]['high'] - mean_30

    if dist > 1.0:
        conf = min(max(dist / 3.0, 0.1), 0.8)
        return 'down', conf
    elif dist < -1.0:
        conf = min(max(abs(dist) / 3.0, 0.1), 0.8)
        return 'up', conf

    return None, 0.0


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def signal_forecast_disagreement(idx, days):
    """
    Forecast Disagreement: yesterday's high vs 7-day rolling mean.
    Disagreement > 5°F → bet toward the mean.
    Matches core/signals/forecast_disagreement_signal.py ForecastDisagreementSignal.evaluate().
    """
    window_days = 7
    threshold = 5.0
    if idx < window_days + 1:
        return None, 0.0

    yesterday_high = days[idx - 1]['high']
    week_days = days[idx - window_days - 1: idx - 1]
    weekly_mean = sum(d['high'] for d in week_days) / len(week_days)

    disagreement = yesterday_high - weekly_mean
    abs_disagreement = abs(disagreement)

    if abs_disagreement < threshold:
        return None, 0.0

    direction = 'down' if disagreement > 0 else 'up'
    confidence = _sigmoid((abs_disagreement - threshold) / 3.0)

    return direction, confidence


def signal_calendar_climatology(idx, days):
    """
    Calendar Climatology: 60-day z-score with 1.5 threshold, capped confidence.
    Matches core/signals/calendar_climatology_signal.py CalendarClimatologySignal.evaluate().
    """
    window = 60
    threshold = 1.5
    if idx < window + 1:
        return None, 0.0

    w = days[idx - window - 1: idx - 1]
    highs = [d['high'] for d in w]
    mean = sum(highs) / len(highs)
    var = sum((h - mean) ** 2 for h in highs) / (len(highs) - 1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01

    z = (days[idx - 1]['high'] - mean) / std

    if z > threshold:
        conf = min(abs(z) / 3.0, 0.6)
        return 'down', conf
    elif z < -threshold:
        conf = min(abs(z) / 3.0, 0.6)
        return 'up', conf

    return None, 0.0


# Signal registry: name → function
INDIVIDUAL_SIGNALS = {
    'gaussian': signal_gaussian,
    'gaussian_v2': signal_gaussian_v2,
    'persistence': signal_persistence,
    'simple_trend': signal_simple_trend,
    'goldilocks': signal_goldilocks,
    'pressure_delta': signal_pressure_delta,
    'wind_direction_shift': signal_wind_direction_shift,
    'regime': signal_regime,
    'forecast_disagreement': signal_forecast_disagreement,
    'calendar_climatology': signal_calendar_climatology,
}


# ═══════════════════════════════════════════════════════════════════════════
# COMBO COMBINE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def combine_signals(signal_fns, idx, days, min_agreement=2):
    """
    Combine multiple signals by weighted majority vote.
    Only makes a prediction when at least min_agreement signals fire.
    Matches logic from run_combinatorial_search.py combine_signals().
    """
    predictions = []
    for fn in signal_fns:
        direction, confidence = fn(idx, days)
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


# ═══════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════

def load_station_data(conn, station):
    """
    Load daily METAR data and settlement labels (HIGH market) for a station.
    Returns (days, label_map).
    """
    cur = conn.cursor()

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


def walk_forward_predictions(days, labels, signal_fns, train_days=180, test_days=30, min_agreement=2):
    """
    Run walk-forward backtest against the provided label map.
    Same method as phase0_label_permutation_test.py.
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

            predicted_dir, confidence = combine_signals(signal_fns, idx, days, min_agreement)
            if predicted_dir is not None:
                results.append((predicted_dir, actual))

        start += test_days
        if start >= train_days + 4 * test_days:
            break

    return results


def compute_accuracy(results):
    """Compute accuracy from (predicted, actual) list."""
    if not results:
        return 0.0, 0
    correct = sum(1 for p, a in results if p == a)
    return correct / len(results), len(results)


# ═══════════════════════════════════════════════════════════════════════════
# TOP COMBO EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_top_combos():
    """
    Extract top 5 by accuracy and top 5 by Sharpe from the combinatorial search.
    Returns (top_accuracy_combos, top_sharpe_combos) where each is a list of
    (combo_name, [signal_fn_list], min_agreement).
    """
    if not os.path.exists(COMBO_PATH):
        print(f"WARNING: Combo data not found at {COMBO_PATH}")
        return [], []

    with open(COMBO_PATH, 'r') as f:
        data = json.load(f)

    top_acc = data.get('top_by_accuracy', [])
    top_sharpe = data.get('top_by_sharpe', [])

    # Build combo lists (name → signal functions)
    def build_combo_entry(entry):
        combo_name = entry[0]
        signal_names = combo_name.split('+')
        signal_fns = []
        for sname in signal_names:
            if sname in INDIVIDUAL_SIGNALS:
                signal_fns.append(INDIVIDUAL_SIGNALS[sname])
            else:
                print(f"WARNING: Unknown signal '{sname}' in combo '{combo_name}'")
                return None

        # Determine appropriate min_agreement: if single signal, use 1; otherwise 2
        min_agr = 1 if len(signal_fns) == 1 else 2
        return (combo_name, signal_fns, min_agr)

    top_acc_combos = []
    for entry in top_acc[:5]:
        c = build_combo_entry(entry)
        if c:
            top_acc_combos.append(c)

    top_sharpe_combos = []
    for entry in top_sharpe[:5]:
        c = build_combo_entry(entry)
        if c:
            top_sharpe_combos.append(c)

    return top_acc_combos, top_sharpe_combos


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    random.seed(42)
    print("=" * 100)
    print("PHASE 0.1a: BROAD LABEL PERMUTATION TEST")
    print("=" * 100)
    print(f"Database: {DB_PATH}")
    print(f"Method: Walk-forward (180d train, 30d test), 10 shuffles per target")
    print()

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    # ── Build test targets ────────────────────────────────────────────────
    # Individual signals (each with min_agreement=1 since single signal)
    individual_targets = [(name, [fn], 1) for name, fn in INDIVIDUAL_SIGNALS.items()]

    # Top combos
    top_acc_targets, top_sharpe_targets = extract_top_combos()

    all_targets = (
        [("--- Individual Signals ---", None, None)] +
        individual_targets +
        [("--- Top 5 by Accuracy ---", None, None)] +
        top_acc_targets +
        [("--- Top 5 by Sharpe ---", None, None)] +
        top_sharpe_targets
    )

    # ── Pre-load all station data ─────────────────────────────────────────
    print("Loading station data...")
    station_data = {}
    for station in ALL_STATIONS:
        days, labels = load_station_data(conn, station)
        if len(days) >= 250:
            station_data[station] = (days, labels)
        else:
            print(f"  {station}: skipped ({len(days)} days)")

    print(f"  Loaded {len(station_data)} stations with sufficient data")
    print()

    # ── Run tests for each target ─────────────────────────────────────────
    results = {}
    summary_rows = []

    for entry in all_targets:
        target_name, signal_fns, min_agr = entry

        # Section headers
        if signal_fns is None:
            print(f"\n{'─'*100}")
            print(f"  {target_name}")
            print(f"{'─'*100}")
            continue

        print(f"\n  Testing: {target_name}")

        # Aggregate across all stations
        all_real_results = []
        all_shuffle_results = [[] for _ in range(10)]

        for station, (days, real_labels) in station_data.items():
            # Run against REAL labels
            real_results = walk_forward_predictions(days, real_labels, signal_fns, min_agreement=min_agr)
            all_real_results.extend(real_results)

            # Build shuffled labels
            label_dates = sorted(real_labels.keys())
            label_values = [real_labels[d] for d in label_dates]

            for shuffle_idx in range(10):
                shuffled_labels = label_values.copy()
                random.shuffle(shuffled_labels)
                shuffled_label_map = {}
                for d, val in zip(label_dates, shuffled_labels):
                    shuffled_label_map[d] = val

                shuf_results = walk_forward_predictions(days, shuffled_label_map, signal_fns, min_agreement=min_agr)
                all_shuffle_results[shuffle_idx].extend(shuf_results)

        # Compute aggregate metrics
        real_correct = sum(1 for p, a in all_real_results if p == a)
        real_total = len(all_real_results)
        real_accuracy = real_correct / real_total if real_total > 0 else 0.0

        shuffle_accuracies = []
        for shuf_idx in range(10):
            shuf_correct = sum(1 for p, a in all_shuffle_results[shuf_idx] if p == a)
            shuf_total = len(all_shuffle_results[shuf_idx])
            shuf_acc = shuf_correct / shuf_total if shuf_total > 0 else 0.0
            shuffle_accuracies.append(shuf_acc)

        mean_shuffle = sum(shuffle_accuracies) / len(shuffle_accuracies)
        std_shuffle = math.sqrt(sum((a - mean_shuffle) ** 2 for a in shuffle_accuracies) / len(shuffle_accuracies))
        best_shuffle = max(shuffle_accuracies)

        # Z-score
        z_score = (real_accuracy - mean_shuffle) / std_shuffle if std_shuffle > 0 else 0.0

        # Significance checks
        exceeds_3sigma = real_accuracy > mean_shuffle + 3 * std_shuffle
        any_shuffle_exceeded = best_shuffle >= real_accuracy
        n_exceed_real = sum(1 for a in shuffle_accuracies if a >= real_accuracy)
        perm_p_value = (n_exceed_real + 1) / (len(shuffle_accuracies) + 1)

        # Verdict
        if exceeds_3sigma and not any_shuffle_exceeded:
            verdict = "PASS"
        elif real_accuracy > mean_shuffle + 2 * std_shuffle and perm_p_value < 0.05:
            verdict = "BORDERLINE"
        else:
            verdict = "FAIL"

        summary_rows.append({
            'target_name': target_name,
            'real_accuracy': round(real_accuracy, 6),
            'real_trades': real_total,
            'real_correct': real_correct,
            'mean_shuffle_accuracy': round(mean_shuffle, 6),
            'std_shuffle_accuracy': round(std_shuffle, 6),
            'best_shuffle_accuracy': round(best_shuffle, 6),
            'z_score': round(z_score, 4),
            'exceeds_3sigma': exceeds_3sigma,
            'any_shuffle_exceeded': any_shuffle_exceeded,
            'shuffles_exceeding_real': n_exceed_real,
            'permutation_p_value': round(perm_p_value, 6),
            'verdict': verdict,
            'shuffle_accuracies': [round(a, 6) for a in shuffle_accuracies],
            'min_agreement': min_agr,
        })

        results[target_name] = summary_rows[-1]

    conn.close()

    # ── Print summary table ──────────────────────────────────────────────
    print()
    print("=" * 140)
    print("SUMMARY TABLE")
    print("=" * 140)
    print(f"{'Target':<55} {'Real Acc':>8} {'Shuf Mean':>10} {'Shuf SD':>8} {'Z-Score':>8} {'3σ?':>5} {'Exceed?':>8} {'Verdict':>10}")
    print("-" * 140)

    for row in summary_rows:
        tgt = row['target_name']
        real_acc = f"{row['real_accuracy']:.2%}"
        shuf_mean = f"{row['mean_shuffle_accuracy']:.2%}"
        shuf_sd = f"{row['std_shuffle_accuracy']:.4f}"
        zs = f"{row['z_score']:.2f}"
        s3 = "✓" if row['exceeds_3sigma'] else "✗"
        exc = "✓" if row['any_shuffle_exceeded'] else "✗"
        v = row['verdict']
        print(f"{tgt:<55} {real_acc:>8} {shuf_mean:>10} {shuf_sd:>8} {zs:>8} {s3:>5} {exc:>8} {v:>10}")

    print("-" * 140)

    # Count passes
    n_pass = sum(1 for r in summary_rows if r['verdict'] == 'PASS')
    n_border = sum(1 for r in summary_rows if r['verdict'] == 'BORDERLINE')
    n_fail = sum(1 for r in summary_rows if r['verdict'] == 'FAIL')
    print(f"PASS: {n_pass}  BORDERLINE: {n_border}  FAIL: {n_fail}")

    # ── Save results ────────────────────────────────────────────────────
    output = {
        'metadata': {
            'timestamp': datetime.utcnow().isoformat(),
            'database': str(DB_PATH),
            'combo_data_source': str(COMBO_PATH),
            'walk_forward_params': {'train_days': 180, 'test_days': 30, 'test_windows': 4},
            'num_shuffles': 10,
            'num_stations': len(ALL_STATIONS),
            'reproducible_seed': 42,
            'signals_tested': list(INDIVIDUAL_SIGNALS.keys()),
        },
        'results': results,
        'summary': {
            'pass': n_pass,
            'borderline': n_border,
            'fail': n_fail,
            'total': len(summary_rows),
        }
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {OUTPUT_PATH}")
    print("\nPHASE 0.1a COMPLETE")


if __name__ == '__main__':
    main()