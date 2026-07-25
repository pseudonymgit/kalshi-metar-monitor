#!/usr/bin/env python3
"""
Phase 6 Clean Combinatorial Search

Tests all valid signal subsets from the clean 11-signal BACKTEST_SIGNALS set,
evaluating at agreement gates 1-5.

Uses the unified_backtest.run_backtest() directly for consistent results.

Usage:
    python3 scripts/phase6_clean_combinatorial_search.py --output data/phase6_clean_search.json
    python3 scripts/phase6_clean_combinatorial_search.py --output data/phase6_clean_search.json --mp
"""

import json
import os
import sys
import argparse
import logging
import time
import math
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.unified_backtest import run_backtest, BACKTEST_SIGNALS, compute_sharpe

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


STATIONS = [
    'KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
    'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
    'KPHX','KSAT','KSEA','KSFO'
]

# Remove goldilocks alias to avoid duplicating spike_reversion
CLEAN_SIGNALS = [s for s in BACKTEST_SIGNALS if s != 'goldilocks']


def evaluate_combo(signals, agreement, db_path, stations):
    """Evaluate a single combo at a single agreement gate."""
    result = run_backtest(
        signal_names=signals,
        stations=stations,
        db_path=db_path,
        min_conf=0.0,
        min_agreement=agreement,
        use_fusion=False,
        use_time_decay=True,
        train_days=180,
        test_days=30,
        verbose=False
    )
    return {
        'signals': signals,
        'agreement': agreement,
        'accuracy': round(result.get('accuracy', 0), 5),
        'fee_adjusted_accuracy': round(result.get('fee_adjusted_accuracy', 0), 5),
        'sharpe': round(result.get('sharpe', 0), 5),
        'brier': round(result.get('brier', 0), 5),
        'ece': round(result.get('ece', 0), 5),
        'drawdown': round(result.get('drawdown', 0), 5),
        'trades': result.get('trades', 0),
        'per_signal_stats': {
            sig: {
                'accuracy': round(stats['accuracy'], 4),
                'fee_adjusted_accuracy': round(stats['fee_adjusted_accuracy'], 4),
                'total': stats['total']
            }
            for sig, stats in result.get('per_signal_stats', {}).items()
        }
    }


def main():
    parser = argparse.ArgumentParser(description='Phase 6 Clean Combinatorial Search')
    parser.add_argument('--db-path', type=str,
                        default=os.environ.get('METAR_DB_PATH',
                            'data/metar_backfill.db'),
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase6_clean_search.json',
                        help='Output file')
    parser.add_argument('--min-signals', type=int, default=2,
                        help='Minimum signals in a combo (default 2)')
    parser.add_argument('--max-signals', type=int, default=6,
                        help='Maximum signals in a combo (default 6)')
    parser.add_argument('--mp', action='store_true',
                        help='Use multiprocessing')
    parser.add_argument('--workers', type=int, default=4,
                        help='Worker count for multiprocessing')
    parser.add_argument('--stations', nargs='+', default=None,
                        help='Station list (default: all 20)')
    parser.add_argument('--verbose', action='store_true',
                        help='Print progress')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    stations = args.stations or STATIONS

    # Generate all combos for each agreement gate 1-5
    logger.info(f"Clean signals ({len(CLEAN_SIGNALS)} total): {CLEAN_SIGNALS}")
    logger.info(f"Generating combos size {args.min_signals} to {args.max_signals}...")

    all_jobs = []
    for r in range(args.min_signals, min(args.max_signals, len(CLEAN_SIGNALS)) + 1):
        for subset in combinations(CLEAN_SIGNALS, r):
            for agreement in range(1, 6):
                all_jobs.append((list(subset), agreement))

    logger.info(f"Total combos x agreement gates = {len(all_jobs)}")

    start_time = time.time()

    if args.mp and len(all_jobs) > 1:
        logger.info(f"Running with {args.workers} workers...")
        results = []
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(evaluate_combo, sigs, ag, db_path, stations):
                (sigs, ag) for sigs, ag in all_jobs
            }
            for future in as_completed(futures):
                sigs, ag = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    if args.verbose:
                        logger.info(f"  {'+'.join(sigs):40s} gate={ag}: "
                                    f"acc={result['accuracy']:.3f} "
                                    f"fee_adj={result['fee_adjusted_accuracy']:.4f} "
                                    f"sharpe={result['sharpe']:.3f} "
                                    f"trades={result['trades']}")
                except Exception as e:
                    logger.error(f"Failed ({'+'.join(sigs)}, gate={ag}): {e}")
    else:
        logger.info("Running single-threaded...")
        results = []
        for sigs, agreement in all_jobs:
            result = evaluate_combo(sigs, agreement, db_path, stations)
            results.append(result)
            if args.verbose:
                logger.info(f"  {'+'.join(sigs):40s} gate={agreement}: "
                            f"acc={result['accuracy']:.3f} "
                            f"fee_adj={result['fee_adjusted_accuracy']:.4f} "
                            f"sharpe={result['sharpe']:.3f} "
                            f"trades={result['trades']}")

    elapsed = time.time() - start_time
    logger.info(f"Completed {len(results)} combos in {elapsed:.1f}s")

    # Sort by key metrics
    by_accuracy = sorted(results, key=lambda r: r['accuracy'], reverse=True)
    by_fee_adj = sorted(results, key=lambda r: r['fee_adjusted_accuracy'], reverse=True)
    by_sharpe = sorted(results, key=lambda r: r['sharpe'], reverse=True)
    by_trades = sorted(results, key=lambda r: r['trades'], reverse=True)

    # Top N by each metric
    TOP_N = 20

    output = {
        'phase6_clean_combinatorial_search': {
            'timestamp': datetime.utcnow().isoformat(),
            'database': db_path,
            'signals_tested': CLEAN_SIGNALS,
            'total_evaluations': len(results),
            'stations': stations,
            'agreement_gates': [1, 2, 3, 4, 5],
            'combo_sizes': f"{args.min_signals}-{args.max_signals}",
            'elapsed_seconds': round(elapsed, 1),
        },
        'results': {f"{'+'.join(r['signals'])}_gate{r['agreement']}": r for r in results},
        'top_by_accuracy': [r for r in by_accuracy[:TOP_N]],
        'top_by_fee_adjusted_accuracy': [r for r in by_fee_adj[:TOP_N]],
        'top_by_sharpe': [r for r in by_sharpe[:TOP_N]],
        'top_by_coverage': [r for r in by_trades[:TOP_N]],
        'best_overall': {},
    }

    # Best overall — highest fee_adj_accuracy with at least 100 trades
    viable = [r for r in results if r['trades'] >= 100]
    if viable:
        best = max(viable, key=lambda r: r['fee_adjusted_accuracy'])
        output['best_overall'] = {
            'combo': '+'.join(best['signals']),
            'agreement_gate': best['agreement'],
            'accuracy': best['accuracy'],
            'fee_adjusted_accuracy': best['fee_adjusted_accuracy'],
            'sharpe': best['sharpe'],
            'trades': best['trades'],
        }

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)
    logger.info(f"Results written to {args.output}")

    # Print summary
    print(f"\n{'='*70}")
    print(f"Phase 6 Clean Combinatorial Search — Summary")
    print(f"{'='*70}")
    print(f"Total combos evaluated: {len(results)}")
    print(f"Top 10 by accuracy:")
    for i, r in enumerate(by_accuracy[:10]):
        print(f"  {i+1}. {'+'.join(r['signals']):40s} "
              f"gate={r['agreement']}  acc={r['accuracy']:.4f}  "
              f"fee_adj={r['fee_adjusted_accuracy']:.4f}  "
              f"sharpe={r['sharpe']:.3f}  trades={r['trades']}")
    print(f"\nTop 10 by fee-adjusted accuracy:")
    for i, r in enumerate(by_fee_adj[:10]):
        print(f"  {i+1}. {'+'.join(r['signals']):40s} "
              f"gate={r['agreement']}  acc={r['accuracy']:.4f}  "
              f"fee_adj={r['fee_adjusted_accuracy']:.4f}  "
              f"sharpe={r['sharpe']:.3f}  trades={r['trades']}")
    if output['best_overall']:
        print(f"\nBest overall (viable): {output['best_overall']['combo']} "
              f"gate={output['best_overall']['agreement_gate']} "
              f"acc={output['best_overall']['accuracy']:.4f} "
              f"fee_adj={output['best_overall']['fee_adjusted_accuracy']:.4f}")

    print(f"\nFull results: {args.output}")


if __name__ == '__main__':
    main()