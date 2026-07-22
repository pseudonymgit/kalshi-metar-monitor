#!/usr/bin/env python3
"""
CALIBRATION PART 2 — COMBINATORIAL SEARCH ACROSS ALL SIGNAL COMBOS

Tests all signal combinations (2-6 signals) from the 23-signal registry
using walk-forward backtest (180d train, 30d test) across all 20 stations.

Strategy (smart approach since full 23-choose-6 is 100K+ combos):
  1. Individual signal backtests (Part 1)
  2. All pairs of 11 working signals
  3. Greedy forward search: best pair → add third → add fourth → add fifth → add sixth
  4. All combos (2-6) for the top 6 signals by individual accuracy

Output to `data/combinatorial_search_all.json`
"""

import argparse
import sqlite3
import math
import json
import os
import sys
import time
import logging
import statistics
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

# Ensure core/ and scripts/ are on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core.signals import SignalRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

ALL_STATIONS = ['KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
                'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
                'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO']

DB_PATH = os.environ.get(
    'METAR_DB_PATH',
    os.path.join(PROJECT_DIR, 'data/metar_backfill.db')
)

TRAIN_DAYS = 180
TEST_DAYS = 30
AGREEMENT_LEVELS = [1, 2, 3]

# FEE_RATE from market cost model
FEE_RATE = 0.005  # round_trip_fraction()

# ─── Data loading ────────────────────────────────────────────────────────────

def load_station_days(station: str, conn: sqlite3.Connection) -> List[Dict]:
    """Load daily METAR data for one station."""
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
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
    return days


def load_market_data(conn: sqlite3.Connection, station: str) -> Dict[str, Dict]:
    """Load settlement epoch data with strike bucket mapping."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
           AND settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        market[r[0]] = {
            'settlement_bucket': r[1],
            'reversion': r[2] if r[2] is not None else 0
        }
    return market


# ─── Metrics ─────────────────────────────────────────────────────────────────

def compute_sharpe_from_results(results: List[Tuple]) -> float:
    """
    Compute Sharpe ratio from (correct, total) pairs.
    Uses the same z-statistic approach as the project.
    """
    if not results:
        return 0.0
    total = sum(r[1] for r in results)
    correct = sum(r[0] for r in results)
    if total < 2:
        return 0.0

    win_rate = correct / total
    loss_rate = 1.0 - win_rate
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)

    # Variance of binary outcomes
    std_returns = math.sqrt(win_rate * (1 - win_rate)**2 + loss_rate * (-1 - win_rate)**2) if win_rate < 1.0 and loss_rate < 1.0 else 0.5
    if std_returns <= 0:
        return 0.0

    sharpe = expected_return / std_returns if std_returns > 0 else 0.0
    sharpe *= math.sqrt(total)
    return sharpe


def compute_brier_from_results(pred_pairs: List[Tuple]) -> float:
    """
    Compute Brier score from (pred, actual) pairs where pred is 'up' or 'down'.
    Uses confidence = 0.7 for all predictions (standardized).
    """
    if not pred_pairs:
        return 0.0
    total = 0.0
    for pred, actual in pred_pairs:
        # Assume confidence 0.7 for Brier calculation
        prob_up = 0.7 if pred == 'up' else 0.3
        outcome = 1.0 if actual == 'up' else 0.0
        total += (prob_up - outcome) ** 2
    return total / len(pred_pairs)


# ─── Walk-forward backtest ───────────────────────────────────────────────────

def run_walk_forward_backtest(
    signal_names: List[str],
    registry: SignalRegistry,
    station_data: Dict[str, List[Dict]],
    station_market: Dict[str, Dict[str, Dict]],
    min_agreement: int = 1
) -> Dict:
    """
    Run a walk-forward backtest for a signal combination across all stations.

    Returns dict with: correct, total, trades, stations_tested, station_breakdown
    """
    combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
    station_breakdown = {}
    pred_pairs = []  # (pred, actual) for Brier

    for station in station_data:
        days = station_data[station]
        market = station_market.get(station, {})

        if len(days) < TRAIN_DAYS + TEST_DAYS:
            continue

        # Compute strike price from training data
        training_temps = [
            market[d['date']]['settlement_bucket']
            for d in days[:TRAIN_DAYS]
            if d['date'] in market and market[d['date']]['settlement_bucket'] is not None
        ]
        if not training_temps:
            continue
        strike = statistics.median(training_temps)

        station_correct = 0
        station_total = 0
        station_trades = 0
        station_pred_pairs = []

        # Walk forward
        start = TRAIN_DAYS
        while start + TEST_DAYS <= len(days):
            test_end = min(start + TEST_DAYS, len(days))

            for idx in range(start, test_end):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None or actual['settlement_bucket'] is None:
                    continue

                actual_direction = 'up' if actual['settlement_bucket'] > strike else 'down'

                # Evaluate all signals in this combo
                votes = []  # (direction, confidence)
                for sig_name in signal_names:
                    sig = registry.get_signal(sig_name)
                    if sig is None:
                        continue
                    try:
                        direction, confidence = sig.evaluate(idx, days)
                        if direction is not None and confidence > 0:
                            votes.append((direction, confidence))
                    except Exception:
                        continue

                if len(votes) < min_agreement:
                    continue

                # Majority vote
                up_weight = sum(c for d, c in votes if d == 'up')
                down_weight = sum(c for d, c in votes if d == 'down')

                if up_weight == down_weight:
                    continue

                pred_dir = 'up' if up_weight > down_weight else 'down'

                station_trades += 1
                station_total += 1
                was_correct = 1 if pred_dir == actual_direction else 0
                station_correct += was_correct
                station_pred_pairs.append((pred_dir, actual_direction))

            start += TEST_DAYS

        if station_trades > 0:
            station_breakdown[station] = {
                'correct': station_correct,
                'total': station_total,
                'trades': station_trades
            }
            combo_results['correct'] += station_correct
            combo_results['total'] += station_total
            combo_results['trades'] += station_trades
            combo_results['stations_tested'] += 1
            pred_pairs.extend(station_pred_pairs)

    return combo_results, pred_pairs


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Calibration Part 2: Combinatorial search across all signal combos'
    )
    parser.add_argument('--db-path', type=str, default=DB_PATH, help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/combinatorial_search_all.json',
                        help='Output file')
    parser.add_argument('--test-mode', action='store_true', help='Quick test mode (small subset)')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    start_time = time.time()

    # ── Load registry ────────────────────────────────────────────────────
    registry = SignalRegistry(db_path)
    all_signal_names = list(registry.signals.keys())
    logger.info(f"Loaded {len(all_signal_names)} signals from registry: {all_signal_names}")

    # ── Load station data ────────────────────────────────────────────────
    conn = sqlite3.connect(db_path)
    station_data = {}
    station_market = {}

    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    available_stations = [r[0] for r in cur.fetchall()]

    for station in ALL_STATIONS:
        if station not in available_stations:
            logger.warning(f"Station {station} not in database, skipping")
            continue
        days = load_station_days(station, conn)
        market = load_market_data(conn, station)
        if len(days) >= TRAIN_DAYS + TEST_DAYS:
            station_data[station] = days
            station_market[station] = market
            logger.info(f"  Loaded {station}: {len(days)} days, {len(market)} market days")
        else:
            logger.info(f"  Skipping {station}: insufficient data ({len(days)} days)")

    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations")

    # ── Step 1: Identify which signals actually fire ──────────────────────
    logger.info("Step 1: Identifying working signals...")
    working_signals = []
    for sig_name in all_signal_names:
        sig = registry.get_signal(sig_name)
        if sig is None:
            continue
        # Quick test on first station
        station = list(station_data.keys())[0]
        days = station_data[station]
        count = 0
        for idx in range(TRAIN_DAYS, min(TRAIN_DAYS + 60, len(days))):
            try:
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None and confidence > 0:
                    count += 1
            except Exception:
                pass
        if count > 0:
            working_signals.append(sig_name)
            logger.info(f"  ✅ {sig_name} (fired {count} times in 60-day test)")
        else:
            logger.info(f"  ❌ {sig_name} (no output)")

    logger.info(f"Working signals: {len(working_signals)} of {len(all_signal_names)}")
    logger.info(f"  {working_signals}")

    # ── Step 2: Individual signal backtests (Part 1) ─────────────────────
    logger.info("\n" + "=" * 80)
    logger.info("Step 2: Individual signal backtests (Part 1)")
    logger.info("=" * 80)

    individual_results = {}
    for sig_name in working_signals:
        res, pred_pairs = run_walk_forward_backtest(
            [sig_name], registry, station_data, station_market,
            min_agreement=1
        )
        trades = res['trades']
        if trades > 0:
            accuracy = res['correct'] / trades
            sharpe = compute_sharpe_from_results([(res['correct'], res['total'])])
            coverage = trades / (len(station_data) * 30)  # approximate coverage
            brier = compute_brier_from_results(pred_pairs) if pred_pairs else 0.0

            individual_results[sig_name] = {
                'accuracy': round(accuracy, 5),
                'sharpe': round(sharpe, 5),
                'coverage': round(coverage, 5),
                'brier': round(brier, 5),
                'correct': res['correct'],
                'total': res['total'],
                'trades': trades,
                'stations_tested': res['stations_tested']
            }
            logger.info(f"  {sig_name:>35s}: acc={accuracy:.4f}  sharpe={sharpe:.2f}  "
                        f"cov={coverage:.3f}  brier={brier:.4f}  trades={trades:>5d}  "
                        f"stations={res['stations_tested']}")

    # Sort by accuracy for ranking
    ranked_signals = sorted(individual_results.items(), key=lambda x: x[1]['accuracy'], reverse=True)
    top_signals = [s[0] for s in ranked_signals]
    logger.info(f"\nRanked by accuracy:")
    for i, (name, stats) in enumerate(ranked_signals):
        logger.info(f"  {i+1}. {name:>35s}: acc={stats['accuracy']:.4f}")

    # ── Step 3: All pairs of working signals ─────────────────────────────
    logger.info("\n" + "=" * 80)
    logger.info("Step 3: All pairs of working signals")
    logger.info("=" * 80)

    master_results = {}
    total_tested = 0

    pair_results = []
    for sig_a, sig_b in combinations(working_signals, 2):
        for min_agree in AGREEMENT_LEVELS:
            res, pred_pairs = run_walk_forward_backtest(
                [sig_a, sig_b], registry, station_data, station_market,
                min_agreement=min_agree
            )
            total_tested += 1
            trades = res['trades']
            if trades >= 100:  # Focus on combos with at least 100 trades
                accuracy = res['correct'] / trades
                sharpe = compute_sharpe_from_results([(res['correct'], res['total'])])
                coverage = trades / (len(station_data) * 30)
                brier = compute_brier_from_results(pred_pairs) if pred_pairs else 0.0

                combo_name = f"{sig_a}+{sig_b}"
                key = f"{combo_name}_agree_{min_agree}"
                entry = {
                    'accuracy': round(accuracy, 5),
                    'sharpe': round(sharpe, 5),
                    'coverage': round(coverage, 5),
                    'brier': round(brier, 5),
                    'correct': res['correct'],
                    'total': res['total'],
                    'trades': trades,
                    'stations_tested': res['stations_tested'],
                    'min_agreement': min_agree,
                    'signals': [sig_a, sig_b],
                }
                master_results[key] = entry
                pair_results.append((sig_a, sig_b, min_agree, accuracy, sharpe, trades))
                logger.info(f"  Pair {sig_a}+{sig_b} agree={min_agree}: "
                            f"acc={accuracy:.4f}  shr={sharpe:.2f}  "
                            f"cov={coverage:.3f}  brier={brier:.4f}  trades={trades}")

    # ── Step 4: Greedy forward search ────────────────────────────────────
    logger.info("\n" + "=" * 80)
    logger.info("Step 4: Greedy forward search (build from best pairs)")
    logger.info("=" * 80)

    # Take top 5 best pairs by accuracy
    best_pairs = sorted(
        [(a, b, acc) for a, b, _, acc, _, _ in pair_results if a != b],
        key=lambda x: x[2], reverse=True
    )[:5]

    # Remove duplicates
    seen_seeds = set()
    unique_seeds = []
    for a, b, acc in best_pairs:
        seed = tuple(sorted([a, b]))
        if seed not in seen_seeds:
            seen_seeds.add(seed)
            unique_seeds.append((list(seed), acc))

    logger.info(f"Top {len(unique_seeds)} unique seed pairs for greedy search:")
    for seed, acc in unique_seeds:
        logger.info(f"  {seed[0]}+{seed[1]}: acc={acc:.4f}")

    # Greedy: for each seed pair, try adding each remaining signal
    for seed_signals, seed_acc in unique_seeds:
        current_set = set(seed_signals)
        remaining = [s for s in working_signals if s not in current_set]

        # Greedy add up to 6 signals
        for target_size in range(3, 7):
            best_next = None
            best_next_acc = 0.0

            for candidate in remaining:
                test_set = list(current_set | {candidate})
                if len(test_set) < 2:
                    continue

                # Test at each agreement level
                for min_agree in AGREEMENT_LEVELS:
                    res, pred_pairs = run_walk_forward_backtest(
                        test_set, registry, station_data, station_market,
                        min_agreement=min_agree
                    )
                    total_tested += 1
                    trades = res['trades']
                    if trades >= 100:
                        accuracy = res['correct'] / trades
                        if accuracy > best_next_acc:
                            best_next_acc = accuracy
                            best_next = (candidate, test_set, min_agree, res, pred_pairs, accuracy)

            if best_next is None:
                logger.info(f"  Greedy {target_size}-signal: no candidate with >=100 trades from {current_set}")
                break

            cand, test_set, min_agree, res, pred_pairs, accuracy = best_next
            trades = res['trades']
            sharpe = compute_sharpe_from_results([(res['correct'], res['total'])])
            coverage = trades / (len(station_data) * 30)
            brier = compute_brier_from_results(pred_pairs) if pred_pairs else 0.0

            combo_name = '+'.join(sorted(test_set))
            key = f"{combo_name}_agree_{min_agree}"
            entry = {
                'accuracy': round(accuracy, 5),
                'sharpe': round(sharpe, 5),
                'coverage': round(coverage, 5),
                'brier': round(brier, 5),
                'correct': res['correct'],
                'total': res['total'],
                'trades': trades,
                'stations_tested': res['stations_tested'],
                'min_agreement': min_agree,
                'signals': sorted(test_set),
                'greedy_step': target_size,
            }
            master_results[key] = entry

            logger.info(f"  Greedy +{cand} → {combo_name} (agree={min_agree}): "
                        f"acc={accuracy:.4f}  shr={sharpe:.2f}  "
                        f"cov={coverage:.3f}  brier={brier:.4f}  trades={trades}")

            # Update for next iteration
            current_set.add(cand)
            remaining.remove(cand)

            if args.test_mode:
                break

        if args.test_mode:
            break

    # ── Step 5: All combos of top 6 signals (2-6) ────────────────────────
    logger.info("\n" + "=" * 80)
    logger.info("Step 5: Full combinatorial of top 6 signals (2-6)")
    logger.info("=" * 80)

    top_6 = top_signals[:6]
    logger.info(f"Top 6 signals: {top_6}")

    for r in range(2, 7):
        for subset in combinations(top_6, r):
            combo_name = '+'.join(sorted(subset))
            for min_agree in AGREEMENT_LEVELS:
                # Skip if we already have this result
                key = f"{combo_name}_agree_{min_agree}"
                if key in master_results:
                    continue

                res, pred_pairs = run_walk_forward_backtest(
                    list(subset), registry, station_data, station_market,
                    min_agreement=min_agree
                )
                total_tested += 1
                trades = res['trades']
                if trades >= 100:
                    accuracy = res['correct'] / trades
                    sharpe = compute_sharpe_from_results([(res['correct'], res['total'])])
                    coverage = trades / (len(station_data) * 30)
                    brier = compute_brier_from_results(pred_pairs) if pred_pairs else 0.0

                    entry = {
                        'accuracy': round(accuracy, 5),
                        'sharpe': round(sharpe, 5),
                        'coverage': round(coverage, 5),
                        'brier': round(brier, 5),
                        'correct': res['correct'],
                        'total': res['total'],
                        'trades': trades,
                        'stations_tested': res['stations_tested'],
                        'min_agreement': min_agree,
                        'signals': sorted(subset),
                    }
                    master_results[key] = entry
                    logger.info(f"  [{r}-sig] {combo_name:<60s} agree={min_agree}: "
                                f"acc={accuracy:.4f}  shr={sharpe:.2f}  "
                                f"cov={coverage:.3f}  brier={brier:.4f}  trades={trades}")

            if args.test_mode:
                break

    # ── Assemble final output ────────────────────────────────────────────
    # Rank results
    all_results_list = list(master_results.items())

    # Top by accuracy
    sorted_acc = sorted(all_results_list, key=lambda x: x[1]['accuracy'], reverse=True)
    sorted_sharpe = sorted(all_results_list, key=lambda x: x[1]['sharpe'], reverse=True)
    sorted_cov = sorted(all_results_list, key=lambda x: x[1]['coverage'], reverse=True)

    top_by_accuracy = [
        {
            'combo': k.split('_agree_')[0],
            'agreement': v['min_agreement'],
            'accuracy': v['accuracy'],
            'sharpe': v['sharpe'],
            'coverage': v['coverage'],
            'brier': v['brier'],
            'trades': v['trades'],
            'stations_tested': v['stations_tested'],
            'signals': v['signals'],
        }
        for k, v in sorted_acc[:50]
    ]

    top_by_sharpe = [
        {
            'combo': k.split('_agree_')[0],
            'agreement': v['min_agreement'],
            'accuracy': v['accuracy'],
            'sharpe': v['sharpe'],
            'coverage': v['coverage'],
            'brier': v['brier'],
            'trades': v['trades'],
            'stations_tested': v['stations_tested'],
            'signals': v['signals'],
        }
        for k, v in sorted_sharpe[:50]
    ]

    top_by_coverage = [
        {
            'combo': k.split('_agree_')[0],
            'agreement': v['min_agreement'],
            'accuracy': v['accuracy'],
            'sharpe': v['sharpe'],
            'coverage': v['coverage'],
            'brier': v['brier'],
            'trades': v['trades'],
            'stations_tested': v['stations_tested'],
            'signals': v['signals'],
        }
        for k, v in sorted_cov[:50]
    ]

    output = {
        'combinatorial_search_all': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_registered': all_signal_names,
            'working_signals': working_signals,
            'total_combinations_tested': total_tested,
            'total_combinations_with_trades': len(master_results),
            'stations': list(station_data.keys()),
            'agreement_levels': AGREEMENT_LEVELS,
            'train_days': TRAIN_DAYS,
            'test_days': TEST_DAYS,
            'description': 'Calibration Part 2: Full combinatorial search across all 23-signal registry (11 working)',
        },
        'individual_results': individual_results,
        'results': master_results,
        'top_by_accuracy': top_by_accuracy,
        'top_by_sharpe': top_by_sharpe,
        'top_by_coverage': top_by_coverage,
    }

    # Save
    output_path = args.output
    if not os.path.isabs(output_path):
        output_path = os.path.join(PROJECT_DIR, output_path)
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\nResults saved to {output_path}")
    logger.info(f"Tested {total_tested} combinations, {len(master_results)} had trades")

    # ── Print summary ────────────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("CALIBRATION PART 2 — TOP 10 BY ACCURACY (≥100 trades)")
    print("=" * 120)
    print(f"{'Signals':<50} {'Ag':>2} {'Acc':>7} {'Sharpe':>8} {'Cov':>6} {'Brier':>7} {'Trd':>5} {'Stns':>4}")
    print("-" * 95)
    for item in top_by_accuracy[:10]:
        sigs = item['combo']
        if len(sigs) > 45:
            sigs = sigs[:42] + '...'
        print(f"{sigs:<50} {item['agreement']:>2} {item['accuracy']:>7.4f} {item['sharpe']:>8.2f} "
              f"{item['coverage']:>6.3f} {item['brier']:>7.4f} {item['trades']:>5} {item['stations_tested']:>4}")

    print("\n" + "=" * 120)
    print("CALIBRATION PART 2 — TOP 10 BY SHARPE (≥100 trades)")
    print("=" * 120)
    print(f"{'Signals':<50} {'Ag':>2} {'Acc':>7} {'Sharpe':>8} {'Cov':>6} {'Brier':>7} {'Trd':>5} {'Stns':>4}")
    print("-" * 95)
    for item in top_by_sharpe[:10]:
        sigs = item['combo']
        if len(sigs) > 45:
            sigs = sigs[:42] + '...'
        print(f"{sigs:<50} {item['agreement']:>2} {item['accuracy']:>7.4f} {item['sharpe']:>8.2f} "
              f"{item['coverage']:>6.3f} {item['brier']:>7.4f} {item['trades']:>5} {item['stations_tested']:>4}")

    print("\n" + "=" * 120)
    print("SUMMARY")
    print("=" * 120)
    print(f"  Signals in registry: {len(all_signal_names)}")
    print(f"  Working signals: {len(working_signals)}")
    print(f"  Total combinations tested: {total_tested}")
    print(f"  Combinations with ≥100 trades: {len(master_results)}")
    print(f"  Stations used: {len(station_data)}")
    print(f"  Runtime: {time.time() - start_time:.1f}s")

    return output


if __name__ == '__main__':
    results = main()