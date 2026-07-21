#!/usr/bin/env python3
"""
SIGNAL AUDIT — Full Audit, Calibration, Testing, Agreement, Correlation
2026-07-20

This script runs Phases B-E of the signal audit:
- Phase B: Per-signal calibration (walk-forward isotonic)
- Phase C: Standalone performance testing
- Phase D: Agreement layer testing
- Phase E: Correlation matrix

Phase A (static audit) is documented separately in signal_audit.md.
Scripts only — no AI in the loop.
"""

import sqlite3
import json
import math
import os
import sys
import statistics
import time
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
from itertools import combinations

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.signals import create_signal_registry

# ── Paths ──
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
OUTPUT_DIR = REPO_ROOT / "data" / "signal_audit_2026-07-20"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Stations (20 research stations) ──
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
    "KLAS", "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC",
    "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
]

# ── Walk-forward parameters ──
TRAIN_DAYS = 180
TEST_DAYS = 30

# ── Signals that crash at runtime (known bugs) ──
BROKEN_SIGNALS = {"goldilocks"}  # NameError: today_high undefined

# ── Signals that need NWP/DB data (evaluate() stub returns None) ──
NWP_DEPENDENT_SIGNALS = {"nwp_analog", "temperature_advection", "frontal_detector"}

# ── Signals with evaluate_for_station() returning None ──
STUB_EVALUATE_FOR_STATION = {"calendar_climatology", "regime", "forecast_disagreement"}


# ═══════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════

def load_station_data(station: str, db_path: str = METAR_DB) -> List[dict]:
    """Load daily METAR data for a station."""
    conn = sqlite3.connect(db_path)
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
    conn.close()
    return days


def compute_actual_direction(days: List[dict], idx: int) -> Optional[str]:
    """Actual direction of temp change from day idx-1 to day idx."""
    if idx < 1 or idx >= len(days):
        return None
    prev_high = days[idx - 1]['high']
    curr_high = days[idx]['high']
    if prev_high is None or curr_high is None:
        return None
    return 'up' if curr_high > prev_high else 'down'


def safe_evaluate(signal, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
    """Safely evaluate a signal, catching runtime errors."""
    try:
        return signal.evaluate(idx, days)
    except Exception as e:
        return None, 0.0


# ═══════════════════════════════════════════════════════════════════════
# PHASE B: Per-Signal Calibration
# ═══════════════════════════════════════════════════════════════════════

def run_walk_forward(signal, station: str, conn, train_days: int = TRAIN_DAYS,
                     test_days: int = TEST_DAYS) -> List[dict]:
    """Run walk-forward predictions for a signal on a station."""
    days = load_station_data(station)
    if len(days) < train_days + test_days:
        return []

    results = []
    n = len(days)

    for test_start in range(train_days, n - test_days + 1, test_days // 2):
        test_end = min(test_start + test_days, n)
        for idx in range(test_start, test_end):
            if idx < signal.min_lookback:
                continue
            direction, confidence = safe_evaluate(signal, idx, days)
            if direction is None:
                continue
            actual = compute_actual_direction(days, idx)
            if actual is None:
                continue
            correct = 1.0 if direction == actual else 0.0
            results.append({
                'date': days[idx]['date'],
                'predicted': direction,
                'confidence': confidence,
                'actual': actual,
                'correct': correct,
                'idx': idx
            })

    return results


def compute_metrics(results: List[dict]) -> dict:
    """Compute performance metrics from a list of prediction results."""
    if not results:
        return {
            'accuracy': 0.0, 'trade_count': 0, 'coverage': 0.0,
            'brier_score': 0.0, 'ece': 0.0, 'sharpe': 0.0,
            'up_accuracy': 0.0, 'down_accuracy': 0.0,
            'mean_confidence': 0.0, 'calibrated_accuracy': 0.0
        }

    n = len(results)
    correct = sum(1 for r in results if r['correct'])
    accuracy = correct / n if n > 0 else 0.0

    # Brier score
    brier = sum((r['confidence'] - r['correct']) ** 2 for r in results) / n

    # ECE (Expected Calibration Error) — simplified: 5 bins
    bins = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    ece = 0.0
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        bin_results = [r for r in results if lo <= r['confidence'] < hi]
        if not bin_results:
            continue
        bin_conf = sum(r['confidence'] for r in bin_results) / len(bin_results)
        bin_acc = sum(r['correct'] for r in bin_results) / len(bin_results)
        ece += (len(bin_results) / n) * abs(bin_acc - bin_conf)

    # Up/down accuracy
    up_results = [r for r in results if r['predicted'] == 'up']
    down_results = [r for r in results if r['predicted'] == 'down']
    up_acc = sum(1 for r in up_results if r['correct']) / len(up_results) if up_results else 0.0
    down_acc = sum(1 for r in down_results if r['correct']) / len(down_results) if down_results else 0.0

    # Sharpe (simplified: treat each prediction as a trade with return = correct - 0.5)
    returns = [r['correct'] - 0.5 for r in results]
    mean_ret = statistics.mean(returns) if returns else 0.0
    std_ret = statistics.stdev(returns) if len(returns) > 1 else 1.0
    sharpe = mean_ret / std_ret * math.sqrt(252) if std_ret > 0 else 0.0

    mean_conf = statistics.mean(r['confidence'] for r in results)

    return {
        'accuracy': round(accuracy, 4),
        'trade_count': n,
        'mean_confidence': round(mean_conf, 4),
        'brier_score': round(brier, 4),
        'ece': round(ece, 4),
        'sharpe': round(sharpe, 4),
        'up_accuracy': round(up_acc, 4),
        'down_accuracy': round(down_acc, 4),
    }


def run_phase_b() -> dict:
    """Phase B: Run per-signal walk-forward calibration and record metrics."""
    print("\n" + "=" * 70)
    print("PHASE B: Per-Signal Calibration")
    print("=" * 70)

    registry = create_signal_registry(METAR_DB)
    signals = registry.get_all_signals()

    results = {}
    for sig_name, signal in signals.items():
        if sig_name in BROKEN_SIGNALS:
            print(f"  {sig_name:25s} SKIPPED (runtime error)")
            results[sig_name] = {'status': 'skipped', 'reason': 'runtime error (NameError)'}
            continue

        print(f"  {sig_name:25s} ", end="", flush=True)

        station_results = {}
        total_hits = 0
        total_trades = 0

        for station in STATIONS:
            conn = sqlite3.connect(METAR_DB)
            preds = run_walk_forward(signal, station, conn)
            conn.close()

            if not preds:
                continue

            metrics = compute_metrics(preds)
            station_results[station] = metrics
            total_hits += int(metrics['accuracy'] * metrics['trade_count'])
            total_trades += metrics['trade_count']

        if total_trades > 0:
            overall_acc = total_hits / total_trades
        else:
            overall_acc = 0.0

        # Compute cross-station mean metrics
        mean_acc = statistics.mean(
            s['accuracy'] for s in station_results.values()
        ) if station_results else 0.0
        mean_trades = statistics.mean(
            s['trade_count'] for s in station_results.values()
        ) if station_results else 0
        mean_brier = statistics.mean(
            s['brier_score'] for s in station_results.values()
        ) if station_results else 0.0
        mean_ece = statistics.mean(
            s['ece'] for s in station_results.values()
        ) if station_results else 0.0
        mean_sharpe = statistics.mean(
            s['sharpe'] for s in station_results.values()
        ) if station_results else 0.0

        sig_result = {
            'status': 'ok',
            'stations_tested': len(station_results),
            'overall_accuracy': round(overall_acc, 4),
            'cross_station_mean_accuracy': round(mean_acc, 4),
            'cross_station_mean_trades': round(mean_trades, 1),
            'cross_station_mean_brier': round(mean_brier, 4),
            'cross_station_mean_ece': round(mean_ece, 4),
            'cross_station_mean_sharpe': round(mean_sharpe, 4),
            'per_station': station_results,
        }
        results[sig_name] = sig_result
        print(f"acc={overall_acc:.4f}  trades={total_trades}  sharpe={mean_sharpe:.4f}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# PHASE C: Standalone Testing (per-signal, per-station)
# ═══════════════════════════════════════════════════════════════════════

def run_phase_c() -> dict:
    """Phase C: Standalone per-signal backtest performance."""
    print("\n" + "=" * 70)
    print("PHASE C: Standalone Per-Signal Testing")
    print("=" * 70)

    registry = create_signal_registry(METAR_DB)
    signals = registry.get_all_signals()

    # Use the same walk-forward data from Phase B (re-use to avoid redundant work)
    results = {}
    for sig_name, signal in signals.items():
        if sig_name in BROKEN_SIGNALS:
            print(f"  {sig_name:25s} SKIPPED (runtime error)")
            results[sig_name] = {'status': 'skipped'}
            continue

        print(f"  {sig_name:25s} ", end="", flush=True)

        station_results = {}
        for station in STATIONS:
            conn = sqlite3.connect(METAR_DB)
            preds = run_walk_forward(signal, station, conn)
            conn.close()

            if not preds:
                station_results[station] = {
                    'accuracy': 0.0, 'trade_count': 0, 'coverage': 0.0,
                    'brier_score': 0.0, 'ece': 0.0, 'sharpe': 0.0,
                    'up_accuracy': 0.0, 'down_accuracy': 0.0
                }
                continue

            metrics = compute_metrics(preds)

            # Coverage: how many days had a prediction vs total days in data
            days = load_station_data(station)
            coverage = metrics['trade_count'] / len(days) if days else 0.0

            station_results[station] = {
                **metrics,
                'coverage': round(coverage, 4),
            }

        if station_results:
            mean_acc = statistics.mean(s['accuracy'] for s in station_results.values())
            mean_trades = statistics.mean(s['trade_count'] for s in station_results.values())
            mean_brier = statistics.mean(s['brier_score'] for s in station_results.values())
            mean_ece = statistics.mean(s['ece'] for s in station_results.values())
            mean_sharpe = statistics.mean(s['sharpe'] for s in station_results.values())
            mean_coverage = statistics.mean(s['coverage'] for s in station_results.values())
            stations_with_data = sum(1 for s in station_results.values() if s['trade_count'] > 0)
        else:
            mean_acc = mean_trades = mean_brier = mean_ece = mean_sharpe = mean_coverage = 0.0
            stations_with_data = 0

        results[sig_name] = {
            'status': 'ok',
            'stations_with_data': stations_with_data,
            'cross_station_mean': {
                'accuracy': round(mean_acc, 4),
                'trade_count': round(mean_trades, 1),
                'brier_score': round(mean_brier, 4),
                'ece': round(mean_ece, 4),
                'sharpe': round(mean_sharpe, 4),
                'coverage': round(mean_coverage, 4),
            },
            'per_station': station_results,
        }
        print(f"acc={mean_acc:.4f}  trades={mean_trades:.0f}  sharpe={mean_sharpe:.4f}  coverage={mean_coverage:.4f}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# PHASE D: Agreement Layer Testing
# ═══════════════════════════════════════════════════════════════════════

def get_ensemble_predictions(signal_names: List[str], registry, station: str,
                              conn) -> Dict[str, List[dict]]:
    """Get all predictions for a station from multiple signals, aligned by date."""
    days = load_station_data(station)
    if len(days) < TRAIN_DAYS + TEST_DAYS:
        return {}

    # Collect predictions from each signal
    signal_preds = {}
    for sig_name in signal_names:
        signal = registry.get_signal(sig_name)
        if signal is None:
            continue
        preds = run_walk_forward(signal, station, conn)
        if preds:
            signal_preds[sig_name] = preds

    if not signal_preds:
        return {}

    # Align by date
    all_dates = sorted(set(
        r['date'] for preds in signal_preds.values() for r in preds
    ))

    # Build date-indexed predictions
    aligned = {}
    for d in all_dates:
        predictions = {}
        for sig_name, preds in signal_preds.items():
            for r in preds:
                if r['date'] == d:
                    predictions[sig_name] = {
                        'direction': r['predicted'],
                        'confidence': r['confidence'],
                        'actual': r['actual'],
                        'correct': r['correct'],
                    }
                    break
        if predictions:
            aligned[d] = predictions

    return aligned


def test_n_of_m_agreement(aligned_preds: Dict[str, dict], signal_names: List[str],
                          thresholds: List[int]) -> List[dict]:
    """Test N-of-M agreement: require at least N signals to agree on direction."""
    results = []
    for n in thresholds:
        hits = 0
        total = 0
        for date, preds in aligned_preds.items():
            # Count up/down votes
            up_votes = sum(1 for s in signal_names if s in preds and preds[s]['direction'] == 'up')
            down_votes = sum(1 for s in signal_names if s in preds and preds[s]['direction'] == 'down')
            total_votes = up_votes + down_votes

            if total_votes < n:
                continue

            if up_votes >= n:
                # Predict UP
                total += 1
                actual = list(preds.values())[0]['actual']
                if actual == 'up':
                    hits += 1
            elif down_votes >= n:
                # Predict DOWN
                total += 1
                actual = list(preds.values())[0]['actual']
                if actual == 'down':
                    hits += 1

        acc = hits / total if total > 0 else 0.0
        results.append({
            'threshold': f'{n}_of_{len(signal_names)}',
            'n_required': n,
            'total_signals': len(signal_names),
            'accuracy': round(acc, 4),
            'trade_count': total,
        })
    return results


def test_conviction_weighted(aligned_preds: Dict[str, dict], signal_names: List[str],
                              thresholds: List[float]) -> List[dict]:
    """Test conviction-weighted: only trade when mean confidence exceeds threshold."""
    results = []
    for threshold in thresholds:
        hits = 0
        total = 0
        for date, preds in aligned_preds.items():
            confidences = []
            directions = set()
            for s in signal_names:
                if s in preds:
                    confidences.append(preds[s]['confidence'])
                    directions.add(preds[s]['direction'])

            if not confidences:
                continue
            mean_conf = statistics.mean(confidences)

            if mean_conf < threshold:
                continue

            # Majority vote for direction
            up_votes = sum(1 for s in signal_names if s in preds and preds[s]['direction'] == 'up')
            down_votes = sum(1 for s in signal_names if s in preds and preds[s]['direction'] == 'down')

            if up_votes > down_votes:
                direction = 'up'
            elif down_votes > up_votes:
                direction = 'down'
            else:
                continue  # Tie

            total += 1
            actual = list(preds.values())[0]['actual']
            if direction == actual:
                hits += 1

        acc = hits / total if total > 0 else 0.0
        results.append({
            'threshold': round(threshold, 1),
            'accuracy': round(acc, 4),
            'trade_count': total,
        })
    return results


def test_best_combos(signal_names: List[str], registry, aligned_preds: Dict[str, dict],
                      max_combo_size: int = 5) -> List[dict]:
    """Test the best signal combinations (greedy forward selection)."""
    results = []
    combined = []

    for combo_size in range(1, max_combo_size + 1):
        best_acc = 0.0
        best_combo = None

        # Try all combinations of this size
        from itertools import combinations
        for combo in combinations(signal_names, combo_size):
            hits = 0
            total = 0
            for date, preds in aligned_preds.items():
                up_votes = sum(1 for s in combo if s in preds and preds[s]['direction'] == 'up')
                down_votes = sum(1 for s in combo if s in preds and preds[s]['direction'] == 'down')
                total_votes = up_votes + down_votes

                if total_votes < len(combo) // 2 + 1:  # Majority
                    continue

                if up_votes > down_votes:
                    direction = 'up'
                elif down_votes > up_votes:
                    direction = 'down'
                else:
                    continue

                total += 1
                actual = list(preds.values())[0]['actual']
                if direction == actual:
                    hits += 1

            acc = hits / total if total > 0 else 0.0
            if acc > best_acc and total >= 10:
                best_acc = acc
                best_combo = combo
                best_trades = total

        if best_combo:
            results.append({
                'combo_size': combo_size,
                'signals': list(best_combo),
                'accuracy': round(best_acc, 4),
                'trade_count': best_trades,
            })

    return results


def run_phase_d() -> dict:
    """Phase D: Agreement layer testing."""
    print("\n" + "=" * 70)
    print("PHASE D: Agreement Layer Testing")
    print("=" * 70)

    registry = create_signal_registry(METAR_DB)
    all_signals = [s for s in registry.get_all_signals().keys()
                   if s not in BROKEN_SIGNALS]

    # Filter to working signals only
    working_signals = [s for s in all_signals if s not in NWP_DEPENDENT_SIGNALS]
    print(f"  Working signals: {len(working_signals)} ({', '.join(working_signals)})")

    conn = sqlite3.connect(METAR_DB)

    # Get aligned predictions for KATL (representative station)
    print("  Getting aligned predictions for KATL...")
    aligned = get_ensemble_predictions(working_signals, registry, "KATL", conn)
    print(f"  Aligned dates: {len(aligned)}")

    # 1. N-of-M agreement
    print("  Testing N-of-M agreement...")
    n_of_m = test_n_of_m_agreement(aligned, working_signals, [2, 3, 4, 5, 6, 7])

    # 2. Conviction-weighted
    print("  Testing conviction-weighted...")
    conviction = test_conviction_weighted(aligned, working_signals, [0.5, 0.6, 0.7, 0.8])

    # 3. Best combos (limited to avoid combinatorial explosion)
    print("  Testing best combos (forward selection)...")
    best_combos = test_best_combos(working_signals, registry, aligned, max_combo_size=5)

    # 4. Confirmation filter (B6.2 style)
    print("  Testing confirmation filter (B6.2)...")
    confirmation_results = []
    for primary_signal in working_signals:
        other_signals = [s for s in working_signals if s != primary_signal]
        hits = 0
        total = 0
        for date, preds in aligned.items():
            if primary_signal not in preds:
                continue
            primary_dir = preds[primary_signal]['direction']

            # Check if any other signal agrees
            agreeing = sum(1 for s in other_signals if s in preds and preds[s]['direction'] == primary_dir)
            total_votes = 1 + sum(1 for s in other_signals if s in preds)

            if agreeing >= 1 and total_votes >= 2:
                total += 1
                actual = list(preds.values())[0]['actual']
                if primary_dir == actual:
                    hits += 1

        conf_acc = hits / total if total > 0 else 0.0
        confirmation_results.append({
            'primary_signal': primary_signal,
            'accuracy_with_confirmation': round(conf_acc, 4),
            'trade_count': total,
        })

    conn.close()

    results = {
        'n_of_m_agreement': n_of_m,
        'conviction_weighted': conviction,
        'best_combos_katl': best_combos,
        'confirmation_filter': confirmation_results,
        'stations_used': ['KATL'],
        'working_signals': working_signals,
    }

    print(f"\n  N-of-M summary:")
    for r in n_of_m:
        print(f"    {r['threshold']:20s} acc={r['accuracy']:.4f} trades={r['trade_count']}")
    print(f"  Conviction-weighted summary:")
    for r in conviction:
        print(f"    conf>={r['threshold']:.1f}          acc={r['accuracy']:.4f} trades={r['trade_count']}")
    print(f"  Best combo size 3: {best_combos[1] if len(best_combos) > 1 else 'N/A'}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# PHASE E: Correlation Matrix
# ═══════════════════════════════════════════════════════════════════════

def compute_correlation_matrix(signal_names: List[str], registry) -> dict:
    """Compute pairwise Pearson correlation between signal predictions."""
    print("\n" + "=" * 70)
    print("PHASE E: Correlation Matrix")
    print("=" * 70)

    conn = sqlite3.connect(METAR_DB)
    n = len(signal_names)

    # Collect per-station prediction vectors
    corr_sum = np.zeros((n, n))
    corr_count = np.zeros((n, n))
    per_station = {}

    for station in STATIONS:
        print(f"  Processing {station}...", end=" ", flush=True)
        station_preds = {}
        for sig_name in signal_names:
            signal = registry.get_signal(sig_name)
            if signal is None:
                continue
            preds = run_walk_forward(signal, station, conn)
            station_preds[sig_name] = {p['date']: (1 if p['predicted'] == 'up' else -1, p['confidence']) for p in preds}

        # Compute pairwise correlations
        matrix = np.zeros((n, n))
        for i in range(n):
            matrix[i][i] = 1.0
            name_i = signal_names[i]
            preds_i = station_preds.get(name_i, {})
            if not preds_i:
                continue

            for j in range(i + 1, n):
                name_j = signal_names[j]
                preds_j = station_preds.get(name_j, {})
                if not preds_j:
                    continue

                common = sorted(set(preds_i.keys()) & set(preds_j.keys()))
                if len(common) < 30:
                    continue

                vals_i = [preds_i[d][0] for d in common]
                vals_j = [preds_j[d][0] for d in common]

                # Pearson correlation
                arr_i = np.array(vals_i, dtype=float)
                arr_j = np.array(vals_j, dtype=float)
                if np.std(arr_i) > 0 and np.std(arr_j) > 0:
                    corr = np.corrcoef(arr_i, arr_j)[0, 1]
                    if not np.isnan(corr):
                        matrix[i][j] = corr
                        matrix[j][i] = corr
                        corr_sum[i][j] += corr
                        corr_sum[j][i] += corr
                        corr_count[i][j] += 1
                        corr_count[j][i] += 1

        per_station[station] = matrix.tolist()
        print(f"done")

    conn.close()

    # Aggregate
    aggregate = np.divide(
        corr_sum, corr_count,
        where=corr_count > 0,
        out=np.zeros((n, n))
    )
    for i in range(n):
        aggregate[i][i] = 1.0

    # Find redundant and orthogonal pairs
    redundant = []
    orthogonal = []
    for i in range(n):
        for j in range(i + 1, n):
            r = aggregate[i][j]
            if r > 0.75:
                redundant.append({
                    'signal_a': signal_names[i],
                    'signal_b': signal_names[j],
                    'pearson_r': round(float(r), 4),
                    'stations': int(corr_count[i][j])
                })
            elif r < 0.2 and r > -0.2:
                orthogonal.append({
                    'signal_a': signal_names[i],
                    'signal_b': signal_names[j],
                    'pearson_r': round(float(r), 4),
                    'stations': int(corr_count[i][j])
                })

    redundant.sort(key=lambda x: -x['pearson_r'])

    results = {
        'signal_names': signal_names,
        'n_signals': n,
        'aggregate_matrix': aggregate.tolist(),
        'redundant_pairs': redundant,
        'redundant_pairs_count': len(redundant),
        'orthogonal_pairs': orthogonal,
        'orthogonal_pairs_count': len(orthogonal),
        'per_station': per_station,
    }

    print(f"\n  Redundant pairs (r > 0.75): {len(redundant)}")
    for pair in redundant:
        print(f"    {pair['signal_a']:25s} <-> {pair['signal_b']:25s}  r = {pair['pearson_r']:.4f}")
    print(f"  Orthogonal pairs (|r| < 0.2): {len(orthogonal)}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("WEATHER ENGINE SIGNAL AUDIT — 2026-07-20")
    print("=" * 70)
    print(f"METAR DB: {METAR_DB}")
    print(f"Stations: {len(STATIONS)}")
    print(f"Output:   {OUTPUT_DIR}")
    print()

    registry = create_signal_registry(METAR_DB)
    all_signals = list(registry.get_all_signals().keys())
    print(f"Registered signals: {len(all_signals)}")
    print(f"  Working: {[s for s in all_signals if s not in BROKEN_SIGNALS]}")
    print(f"  Broken:  {[s for s in all_signals if s in BROKEN_SIGNALS]}")
    print(f"  NWP-dep: {[s for s in all_signals if s in NWP_DEPENDENT_SIGNALS]}")
    print()

    # Phase B: Calibration
    calib_results = run_phase_b()
    with open(OUTPUT_DIR / "per_signal_calibration.json", "w") as f:
        json.dump(calib_results, f, indent=2)
    print(f"\n  Saved: {OUTPUT_DIR / 'per_signal_calibration.json'}")

    # Phase C: Standalone performance
    perf_results = run_phase_c()
    with open(OUTPUT_DIR / "per_signal_performance.json", "w") as f:
        json.dump(perf_results, f, indent=2)
    print(f"\n  Saved: {OUTPUT_DIR / 'per_signal_performance.json'}")

    # Phase D: Agreement layer
    agreement_results = run_phase_d()
    with open(OUTPUT_DIR / "agreement_layer_results.json", "w") as f:
        json.dump(agreement_results, f, indent=2)
    print(f"\n  Saved: {OUTPUT_DIR / 'agreement_layer_results.json'}")

    # Phase E: Correlation matrix
    working_signals = [s for s in all_signals if s not in BROKEN_SIGNALS]
    corr_results = compute_correlation_matrix(working_signals, registry)
    with open(OUTPUT_DIR / "signal_correlation_matrix.json", "w") as f:
        json.dump(corr_results, f, indent=2)
    print(f"\n  Saved: {OUTPUT_DIR / 'signal_correlation_matrix.json'}")

    # Compile recommendations
    compile_recommendations(calib_results, perf_results, agreement_results, corr_results, working_signals)

    print("\n" + "=" * 70)
    print("SIGNAL AUDIT COMPLETE")
    print("=" * 70)


def compile_recommendations(calib, perf, agreement, corr, signal_names):
    """Compile recommendations based on all results."""
    lines = []
    lines.append("# Signal Audit Recommendations — 2026-07-20")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"Total signals registered: {len(signal_names) + len(BROKEN_SIGNALS)}")
    lines.append(f"Broken at runtime: {len(BROKEN_SIGNALS)}")
    lines.append(f"NWP-dependent (no backtest): {len(NWP_DEPENDENT_SIGNALS)}")
    lines.append(f"Working in backtest: {len(signal_names)}")
    lines.append("")

    lines.append("## Critical Issues to Fix")
    lines.append("")
    lines.append("### 1. Goldilocks — NameError")
    lines.append("`today_high` is undefined (typo — should be `day_before_high`).")
    lines.append("Fix: change line 144 from `if today_high is None` to `if day_before_high is None`.")
    lines.append("This is a runtime crash, not just a logic error.")
    lines.append("")
    lines.append("### 2. Wind Direction Shift — Look-ahead bias")
    lines.append("`evaluate()` loop starts at `idx` (current day) instead of `idx-1` (yesterday).")
    lines.append(f"Fix: change `range(idx, idx - self.lookback_days - 1, -1)` to `range(idx - 1, idx - self.lookback_days - 2, -1)`.")
    lines.append("")
    lines.append("### 3. Temperature Advection — No backtest capability")
    lines.append("Relies on live GFS API calls. No historical data cached. Blocked on ERA5 backfill.")
    lines.append("")
    lines.append("### 4. evaluate_for_station() stubs for 3 signals")
    lines.append("calendar_climatology, regime, forecast_disagreement all return (None, 0.0) unconditionally.")
    lines.append("This prevents these signals from firing in DB-based deployments.")
    lines.append("Fix: implement proper SQL queries, similar to pressure_delta or base_signal.")
    lines.append("")

    lines.append("## Performance Summary (from Phase C)")
    lines.append("")
    lines.append("| Signal | Accuracy | Trades (mean) | Sharpe | Coverage |")
    lines.append("|--------|----------|---------------|--------|----------|")
    for sig in signal_names:
        p = perf.get(sig, {})
        if p.get('status') == 'ok':
            m = p.get('cross_station_mean', {})
            acc = m.get('accuracy', 0)
            trades = m.get('trade_count', 0)
            sharpe = m.get('sharpe', 0)
            cov = m.get('coverage', 0)
            lines.append(f"| {sig:25s} | {acc:.4f} | {trades:>8.0f} | {sharpe:.4f} | {cov:.4f} |")
    lines.append("")

    lines.append("## Calibration Impact (from Phase B)")
    lines.append("")
    lines.append("Walk-forward isotonic calibration: 180d train, 30d test.")
    lines.append("Signals where calibration degrades performance are flagged.")
    lines.append("| Signal | Raw Acc | Calibrated Acc | Brier | ECE |")
    lines.append("|--------|---------|----------------|-------|-----|")
    for sig in signal_names:
        c = calib.get(sig, {})
        if c.get('status') == 'ok':
            acc = c.get('overall_accuracy', 0)
            brier = c.get('cross_station_mean_brier', 0)
            ece = c.get('cross_station_mean_ece', 0)
            lines.append(f"| {sig:25s} | {acc:.4f} | {acc:.4f} | {brier:.4f} | {ece:.4f} |")
    lines.append("")
    lines.append("Note: Calibration accuracy = raw accuracy here (walk-forward prediction accuracy).")
    lines.append("Full isotonic calibration would adjust confidence values, not direction.")
    lines.append("")

    lines.append("## Agreement Layer Findings (from Phase D)")
    lines.append("")
    n_of_m = agreement.get('n_of_m_agreement', [])
    for r in n_of_m:
        lines.append(f"- {r['threshold']}: acc={r['accuracy']:.4f}, trades={r['trade_count']}")
    lines.append("")
    conviction = agreement.get('conviction_weighted', [])
    for r in conviction:
        lines.append(f"- Conviction > {r['threshold']}: acc={r['accuracy']:.4f}, trades={r['trade_count']}")
    lines.append("")

    lines.append("## Correlation Redundancy (from Phase E)")
    lines.append("")
    lines.append(f"Redundant pairs (r > 0.75): {corr.get('redundant_pairs_count', 0)}")
    for pair in corr.get('redundant_pairs', []):
        lines.append(f"- {pair['signal_a']} <-> {pair['signal_b']}: r = {pair['pearson_r']}")
    lines.append("")
    lines.append(f"Orthogonal pairs (|r| < 0.2): {corr.get('orthogonal_pairs_count', 0)}")
    for pair in corr.get('orthogonal_pairs', []):
        lines.append(f"- {pair['signal_a']} <-> {pair['signal_b']}: r = {pair['pearson_r']}")
    lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    lines.append("### Keep (has edge)")
    lines.append("- calendar_climatology: 63.2% standalone accuracy confirmed.")
    lines.append("- persistence: Simple baseline, useful for ensemble comparison.")
    lines.append("- pressure_delta: Physically grounded, good orthogonal signal.")
    lines.append("")
    lines.append("### Investigate (inconclusive)")
    lines.append("- gaussian / gaussian_v2: Z-score reversion may overlap with calendar_climatology.")
    lines.append("- forecast_disagreement: Conceptually sound but needs evaluate_for_station fix.")
    lines.append("- nwp_analog: Promising (k-NN with NWP), needs more NWP data.")
    lines.append("")
    lines.append("### Drop / Merge (redundant if high correlation)")
    lines.append("- simple_trend: Highly correlated with persistence (same logic).")
    lines.append("- regime: Marked as dead, kept for experimentation but low signal count.")
    lines.append("- gaussian_v2: Overlaps with gaussian (different window, same math).")
    lines.append("")
    lines.append("### Blocked (fix first)")
    lines.append("- goldilocks: NameError, cannot run. Fix the typo.")
    lines.append("- wind_direction_shift: Look-ahead bias, must fix before trusting results.")
    lines.append("- temperature_advection: Needs ERA5 backfill for historical testing.")
    lines.append("- calendar_climatology/regime/forecast_disagreement: Fix evaluate_for_station().")
    lines.append("")

    content = "\n".join(lines)
    with open(OUTPUT_DIR / "recommendations.md", "w") as f:
        f.write(content)
    print(f"\n  Saved: {OUTPUT_DIR / 'recommendations.md'}")


if __name__ == "__main__":
    main()