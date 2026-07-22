#!/usr/bin/env python3
"""
B-Mode 2: Full combinatorial search — all combos, all agreement levels.

Tests ALL combinations of 2-6 signals from the 11 working signals, at each
of the 3 agreement levels (1, 2, 3). Uses core/unified_backtest.py as the
backtest engine with strike-based P&L.

Working signals are identified from the calibration results (those with >0 trades).
Total combos: ~4,422 (11C2×3 + 11C3×3 + 11C4×3 + 11C5×3 + 11C6×3)

Output: data/full_combinatorial_search.json
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

# Suppress verbose signal loggers
logging.getLogger('core.signals.frontal_detector_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.calendar_climatology_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.goldilocks_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.wind_direction_shift').setLevel(logging.WARNING)
logging.getLogger('core.signals.forecast_disagreement_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.pressure_delta_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.gaussian_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.gaussian_v2_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.persistence_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.dual_polarity_signal').setLevel(logging.WARNING)
logging.getLogger('core.signals.goldilocks_signal').setLevel(logging.WARNING)

# Ensure core/ and scripts/ are on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
CORE_DIR = os.path.join(PROJECT_DIR, 'core')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

# Workaround: import core.signal_fusion first, then alias it so unified_backtest's
# bare 'from signal_fusion import ...' finds it
import importlib
import types

try:
    import core.signal_fusion
    sys.modules['signal_fusion'] = sys.modules['core.signal_fusion']
except ImportError:
    pass

from core.signals import SignalRegistry
from core.unified_backtest import (
    load_station_data,
    compute_sharpe,
    compute_brier,
    compute_ece,
    max_drawdown,
    DB_PATH,
    FEE_RATE,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

ALL_STATIONS = ['KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
                'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
                'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO']

TRAIN_DAYS = 180
TEST_DAYS = 30
AGREEMENT_LEVELS = [1, 2, 3]

# Working signals from calibration results (those with >0 trades)
WORKING_SIGNALS = [
    'calendar_climatology',
    'corrected_pressure_delta',
    'forecast_disagreement',
    'frontal_detector',
    'gaussian',
    'gaussian_v2',
    'goldilocks',
    'persistence',
    'pressure_delta',
    'seasonal_regime',
    'wind_direction_shift',
]

# Checkpoint file for resumability
CHECKPOINT_FILE = os.path.join(PROJECT_DIR, 'data', 'full_combinatorial_search_checkpoint.json')


# ─── Data loading ────────────────────────────────────────────────────────────

def load_station_data_local(conn: sqlite3.Connection, station: str) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Load daily METAR data and settlement market data for one station."""
    days, market = load_station_data(station, conn)
    # Convert to list of dicts for compatibility
    day_list = [dict(d) for d in days] if days else []
    market_dict = dict(market) if market else {}
    return day_list, market_dict


# ─── Walk-forward backtest with agreement levels ────────────────────────────

def run_combo_backtest(
    signal_names: List[str],
    registry: SignalRegistry,
    conn: sqlite3.Connection,
    station_list: List[str],
    min_agreement: int = 1,
    verbose: bool = False,
) -> Dict:
    """
    Run walk-forward backtest for a signal combination across all stations.

    This is a strike-based P&L backtest (not direction-based).
    Uses the same methodology as core/unified_backtest.py.

    Args:
        signal_names: list of signal canonical names
        registry: SignalRegistry instance
        conn: database connection
        station_list: list of station codes to test
        min_agreement: minimum number of signals that must agree (1, 2, or 3)

    Returns:
        Dict with: accuracy, sharpe, brier, ece, drawdown, trades,
                   correct, total, stations_tested, station_breakdown
    """
    all_results = []  # (pred, actual, conf)
    station_breakdown = {}

    for station in station_list:
        days, market = load_station_data_local(conn, station)
        if len(days) < TRAIN_DAYS + TEST_DAYS:
            continue

        # Compute strike price from training data (median high temperature)
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
        station_results = []

        # Walk forward
        start = TRAIN_DAYS
        while start + TEST_DAYS <= len(days):
            test_end = min(start + TEST_DAYS, len(days))

            for idx in range(start, test_end):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None or actual['settlement_bucket'] is None:
                    continue

                # Strike-based P&L: 'up' means settlement > strike
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

                # Apply agreement level filter: must have at least min_agreement signals
                if len(votes) < min_agreement:
                    continue

                # Weighted majority vote (same as unified_backtest.py)
                up_weight = sum(c for d, c in votes if d == 'up')
                down_weight = sum(c for d, c in votes if d == 'down')

                if up_weight == down_weight:
                    continue

                # Confidence is the absolute net weight divided by total weight
                total_weight = up_weight + down_weight
                conf = abs(up_weight - down_weight) / total_weight if total_weight > 0 else 0.5
                pred_dir = 'up' if up_weight > down_weight else 'down'

                is_correct = (pred_dir == actual_direction)
                station_results.append((pred_dir, actual_direction, conf))
                station_total += 1
                station_correct += int(is_correct)

            start += TEST_DAYS

        if station_total > 0:
            station_breakdown[station] = {
                'correct': station_correct,
                'total': station_total,
            }
            all_results.extend(station_results)

    # Compute aggregate metrics
    if not all_results:
        return {
            'accuracy': 0.0,
            'sharpe': 0.0,
            'brier': 1.0,
            'ece': 1.0,
            'drawdown': 0.0,
            'trades': 0,
            'correct': 0,
            'total': 0,
            'stations_tested': 0,
            'station_breakdown': {},
        }

    n = len(all_results)
    correct = sum(1 for p, a, _ in all_results if p == a)
    accuracy = correct / n
    sharpe = compute_sharpe([(c, p == a) for p, a, c in all_results])
    brier = compute_brier(all_results)
    ece = compute_ece(all_results)
    dd = max_drawdown(all_results)

    return {
        'accuracy': round(accuracy, 6),
        'sharpe': round(sharpe, 6),
        'brier': round(brier, 6),
        'ece': round(ece, 6),
        'drawdown': round(dd, 6),
        'trades': n,
        'correct': correct,
        'total': n,
        'stations_tested': len(station_breakdown),
        'station_breakdown': station_breakdown,
    }


# ─── Checkpoint helpers ─────────────────────────────────────────────────────

def save_checkpoint(results: Dict, tested_count: int, total_count: int, elapsed: float):
    """Save intermediate results to checkpoint file."""
    checkpoint = {
        'timestamp': datetime.now().isoformat(),
        'tested_combinations': tested_count,
        'total_estimated_combinations': total_count,
        'elapsed_seconds': elapsed,
        'results_count': len(results),
        'results': results,
    }
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(checkpoint, f, indent=2, default=str)
    logger.info(f"Checkpoint saved: {tested_count}/{total_count} combos tested, {len(results)} with trades")


def load_checkpoint() -> Tuple[Optional[Dict], int, float]:
    """Load checkpoint if it exists. Returns (results, tested_count, elapsed)."""
    if not os.path.exists(CHECKPOINT_FILE):
        return None, 0, 0.0
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            cp = json.load(f)
        return cp.get('results', {}), cp.get('tested_combinations', 0), cp.get('elapsed_seconds', 0.0)
    except (json.JSONDecodeError, KeyError):
        logger.warning("Corrupted checkpoint file, starting fresh")
        return None, 0, 0.0


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='B-Mode 2: Full combinatorial search across all 11 working signals'
    )
    parser.add_argument('--db-path', type=str, default=DB_PATH, help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/full_combinatorial_search.json',
                        help='Output file path (relative to project dir)')
    parser.add_argument('--test-mode', action='store_true',
                        help='Test mode: only test a small subset (1/100 of combos)')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from checkpoint if available')
    parser.add_argument('--no-checkpoint', action='store_true',
                        help='Disable checkpointing')
    parser.add_argument('--min-trades', type=int, default=50,
                        help='Minimum trades to include a combo in results (default: 50)')
    parser.add_argument('--max-signals', type=int, default=6,
                        help='Maximum number of signals in a combo (default: 6)')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_DIR, db_path)
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    start_time = time.time()

    # ── Load registry ────────────────────────────────────────────────────
    registry = SignalRegistry(db_path)
    all_signal_names = list(registry.signals.keys())
    logger.info(f"Loaded {len(all_signal_names)} signals from registry")

    # Verify working signals
    working_set = [s for s in WORKING_SIGNALS if s in all_signal_names]
    missing = [s for s in WORKING_SIGNALS if s not in all_signal_names]
    if missing:
        logger.warning(f"Signals not found in registry: {missing}")
    logger.info(f"Working signals: {len(working_set)}")
    logger.info(f"  {working_set}")

    # ── Connect to database ──────────────────────────────────────────────
    conn = sqlite3.connect(db_path)

    # ── Pre-verify which stations have data ──────────────────────────────
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    available_stations = [r[0] for r in cur.fetchall()]
    valid_stations = [s for s in ALL_STATIONS if s in available_stations]
    logger.info(f"Valid stations: {len(valid_stations)} of {len(ALL_STATIONS)}")

    # Quick station validation: check which have enough data
    station_list = []
    for station in valid_stations:
        days, market = load_station_data_local(conn, station)
        if len(days) >= TRAIN_DAYS + TEST_DAYS:
            station_list.append(station)
    logger.info(f"Stations with sufficient data: {len(station_list)}")
    logger.info(f"  {station_list}")

    # ── Generate all combos ──────────────────────────────────────────────
    max_sig = min(args.max_signals, len(working_set))
    all_combos = []
    for r in range(2, max_sig + 1):
        for subset in combinations(working_set, r):
            for agree in AGREEMENT_LEVELS:
                all_combos.append((list(subset), agree))

    total_combos = len(all_combos)
    logger.info(f"Total combos to test: {total_combos}")
    logger.info(f"  Breakdown:")
    for r in range(2, max_sig + 1):
        count = len(working_set)
        n_choose_r = math.comb(count, r)
        logger.info(f"    {r}-signal: {n_choose_r} combos × 3 levels = {n_choose_r * 3}")

    if args.test_mode:
        # Test mode: test every 100th combo
        all_combos = all_combos[::100]
        logger.info(f"Test mode: reduced to {len(all_combos)} combos")

    # ── Load checkpoint if resuming ──────────────────────────────────────
    master_results = {}
    tested_count = 0
    if args.resume and not args.test_mode:
        saved_results, tested_count, saved_elapsed = load_checkpoint()
        if saved_results:
            master_results = saved_results
            logger.info(f"Resumed from checkpoint: {len(master_results)} results, "
                        f"{tested_count} combos previously tested")
            start_time = time.time() - saved_elapsed
        else:
            logger.info("No valid checkpoint found, starting fresh")

    # ── Run the combinatorial search ─────────────────────────────────────
    logger.info("\n" + "=" * 80)
    logger.info("RUNNING FULL COMBINATORIAL SEARCH")
    logger.info("=" * 80)

    # Track best performers
    best_accuracy = 0.0
    best_sharpe = -999.0

    # For progress reporting
    batch_size = 100
    next_checkpoint = batch_size

    for i, (sig_set, agree) in enumerate(all_combos):
        if i < tested_count:
            continue  # Skip already tested combos when resuming

        combo_name = '+'.join(sorted(sig_set))
        key = f"{combo_name}_agree_{agree}"

        # Skip if already computed
        if key in master_results:
            continue

        result = run_combo_backtest(
            sig_set, registry, conn, station_list,
            min_agreement=agree,
        )

        trades = result['trades']
        if trades >= args.min_trades:
            entry = {
                'accuracy': result['accuracy'],
                'sharpe': result['sharpe'],
                'brier': result['brier'],
                'ece': result['ece'],
                'drawdown': result['drawdown'],
                'trades': trades,
                'correct': result['correct'],
                'total': result['total'],
                'stations_tested': result['stations_tested'],
                'min_agreement': agree,
                'signals': sorted(sig_set),
                'num_signals': len(sig_set),
            }
            master_results[key] = entry

            if result['accuracy'] > best_accuracy:
                best_accuracy = result['accuracy']
            if result['sharpe'] > best_sharpe:
                best_sharpe = result['sharpe']

        # Progress logging
        current = i + 1
        if current % 100 == 0 or current == total_combos or current == 1:
            elapsed = time.time() - start_time
            rate = current / elapsed if elapsed > 0 else 0
            eta = (total_combos - current) / rate if rate > 0 else 0
            logger.info(
                f"[{current}/{total_combos}] {rate:.1f} combos/s | "
                f"{len(master_results)} with trades | "
                f"best_acc={best_accuracy:.4f} best_shr={best_sharpe:.2f} | "
                f"ETA: {eta:.0f}s"
            )

        # Checkpointing
        if not args.no_checkpoint and not args.test_mode:
            if current >= next_checkpoint:
                elapsed = time.time() - start_time
                save_checkpoint(master_results, current, total_combos, elapsed)
                next_checkpoint += batch_size

    conn.close()
    total_elapsed = time.time() - start_time

    # ── Assemble final output ────────────────────────────────────────────
    logger.info(f"\n{'=' * 80}")
    logger.info(f"SEARCH COMPLETE: {len(master_results)} combos with trades")
    logger.info(f"{'=' * 80}")

    # Rank results
    all_results_list = list(master_results.items())

    # Top by accuracy
    sorted_acc = sorted(all_results_list, key=lambda x: x[1]['accuracy'], reverse=True)
    sorted_sharpe = sorted(all_results_list, key=lambda x: x[1]['sharpe'], reverse=True)
    sorted_cov = sorted(all_results_list, key=lambda x: x[1]['trades'], reverse=True)

    def make_ranked_entry(k, v):
        return {
            'combo': k.split('_agree_')[0],
            'agreement': v['min_agreement'],
            'num_signals': v['num_signals'],
            'accuracy': v['accuracy'],
            'sharpe': v['sharpe'],
            'brier': v['brier'],
            'ece': v['ece'],
            'drawdown': v['drawdown'],
            'trades': v['trades'],
            'stations_tested': v['stations_tested'],
            'signals': v['signals'],
        }

    top_by_accuracy = [make_ranked_entry(k, v) for k, v in sorted_acc[:50]]
    top_by_sharpe = [make_ranked_entry(k, v) for k, v in sorted_sharpe[:50]]
    top_by_coverage = [make_ranked_entry(k, v) for k, v in sorted_cov[:50]]

    # Summary counts by size
    size_counts = defaultdict(int)
    acc_by_size = defaultdict(list)
    for k, v in master_results.items():
        size_counts[v['num_signals']] += 1
        acc_by_size[v['num_signals']].append(v['accuracy'])

    summary_by_size = {}
    for size in sorted(size_counts.keys()):
        vals = acc_by_size[size]
        summary_by_size[size] = {
            'count': size_counts[size],
            'mean_accuracy': round(sum(vals) / len(vals), 4) if vals else 0,
            'max_accuracy': round(max(vals), 4) if vals else 0,
            'min_accuracy': round(min(vals), 4) if vals else 0,
        }

    output = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_registered': all_signal_names,
            'working_signals': working_set,
            'total_combinations_possible': total_combos,
            'total_combinations_tested': len(all_combos),
            'total_combinations_with_trades': len(master_results),
            'min_trades_threshold': args.min_trades,
            'stations': station_list,
            'agreement_levels': AGREEMENT_LEVELS,
            'train_days': TRAIN_DAYS,
            'test_days': TEST_DAYS,
            'runtime_seconds': round(total_elapsed, 1),
            'description': 'B-Mode 2: Full combinatorial search — all combos of 2-6 signals × all 3 agreement levels',
        },
        'results': master_results,
        'top_by_accuracy': top_by_accuracy,
        'top_by_sharpe': top_by_sharpe,
        'top_by_coverage': top_by_coverage,
        'summary_by_signal_count': summary_by_size,
    }

    # Save final output
    output_path = args.output
    if not os.path.isabs(output_path):
        output_path = os.path.join(PROJECT_DIR, output_path)
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\nResults saved to {output_path}")

    # Clean up checkpoint
    if os.path.exists(CHECKPOINT_FILE) and not args.test_mode:
        os.remove(CHECKPOINT_FILE)
        logger.info("Checkpoint file removed")

    # ── Print summary ────────────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("B-MODE 2 — FULL COMBINATORIAL SEARCH RESULTS")
    print("=" * 120)
    print(f"\n  Working signals: {len(working_set)}")
    print(f"  Total combos possible: {total_combos}")
    print(f"  Combos with trades: {len(master_results)}")
    print(f"  Runtime: {total_elapsed:.1f}s")
    print(f"\n  Summary by signal count:")
    for size in sorted(summary_by_size.keys()):
        s = summary_by_size[size]
        print(f"    {size}-signal: {s['count']} combos | "
              f"mean acc: {s['mean_accuracy']:.4f} | "
              f"max: {s['max_accuracy']:.4f} | min: {s['min_accuracy']:.4f}")

    print("\n" + "-" * 120)
    print("TOP 10 BY ACCURACY")
    print("-" * 120)
    print(f"{'Signals':<50} {'N':>2} {'Ag':>2} {'Acc':>8} {'Sharpe':>8} {'Cov(trd)':>8} {'Brier':>7} {'DD':>7}")
    print("-" * 95)
    for item in top_by_accuracy[:10]:
        sigs = item['combo']
        if len(sigs) > 45:
            sigs = sigs[:42] + '...'
        print(f"{sigs:<50} {item['num_signals']:>2} {item['agreement']:>2} "
              f"{item['accuracy']:>8.4f} {item['sharpe']:>8.2f} "
              f"{item['trades']:>8} {item['brier']:>7.4f} {item['drawdown']:>7.4f}")

    print("\n" + "-" * 120)
    print("TOP 10 BY SHARPE")
    print("-" * 120)
    print(f"{'Signals':<50} {'N':>2} {'Ag':>2} {'Acc':>8} {'Sharpe':>8} {'Cov(trd)':>8} {'Brier':>7} {'DD':>7}")
    print("-" * 95)
    for item in top_by_sharpe[:10]:
        sigs = item['combo']
        if len(sigs) > 45:
            sigs = sigs[:42] + '...'
        print(f"{sigs:<50} {item['num_signals']:>2} {item['agreement']:>2} "
              f"{item['accuracy']:>8.4f} {item['sharpe']:>8.2f} "
              f"{item['trades']:>8} {item['brier']:>7.4f} {item['drawdown']:>7.4f}")

    print("\n" + "-" * 120)
    print("TOP 10 BY COVERAGE (trades)")
    print("-" * 120)
    print(f"{'Signals':<50} {'N':>2} {'Ag':>2} {'Acc':>8} {'Sharpe':>8} {'Cov(trd)':>8} {'Brier':>7} {'DD':>7}")
    print("-" * 95)
    for item in top_by_coverage[:10]:
        sigs = item['combo']
        if len(sigs) > 45:
            sigs = sigs[:42] + '...'
        print(f"{sigs:<50} {item['num_signals']:>2} {item['agreement']:>2} "
              f"{item['accuracy']:>8.4f} {item['sharpe']:>8.2f} "
              f"{item['trades']:>8} {item['brier']:>7.4f} {item['drawdown']:>7.4f}")

    return output


if __name__ == '__main__':
    results = main()