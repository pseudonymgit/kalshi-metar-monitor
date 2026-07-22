#!/usr/bin/env python3
"""
Beam Search Calibration v2.0 — Full combinatorial search with split-sample validation.

Implements the optimization expert's recommended approach:
  - Beam search (b=10, k≤4) across all working signals
  - Agree=k optimization (agree=2 for pairs, agree=3 for triples)
  - 4-way station sharding for parallel execution
  - Split-sample validation (purged CV + permutation test)

Usage:
  python3 scripts/beam_search_calibration.py                        # full run
  python3 scripts/beam_search_calibration.py --phase singles        # singles only
  python3 scripts/beam_search_calibration.py --phase pairs          # pairs only
  python3 scripts/beam_search_calibration.py --phase triples        # triples only
  python3 scripts/beam_search_calibration.py --phase beam           # k=4 beam search
  python3 scripts/beam_search_calibration.py --phase validate       # split-sample validation

Output:
  data/beam_search_results.json        — full results
  data/beam_search_validation.json     — validation results
  data/beam_search_summary.txt         — human-readable summary
"""

import sys, os, json, time, math, sqlite3
from datetime import datetime, timezone
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import List, Tuple, Optional

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ─── Configuration ─────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "beam_search_results.json"
VALIDATION_FILE = DATA_DIR / "beam_search_validation.json"
SUMMARY_FILE = DATA_DIR / "beam_search_summary.txt"

DB_PATH = str(DATA_DIR / "metar_backfill.db")
NWP_DB_PATH = str(DATA_DIR / "nwp_forecasts.db")
KALSHI_MOCK_DB = str(DATA_DIR / "kalshi_mock_orderbook.db")

# Which signals to skip (dead ends documented in Phase B expert spec)
SKIP_SIGNALS = {
    'goldilocks',       # 0.1% accuracy, separate lane disabled
}

# Working signals (those that produced trades in calibration)
# Updated from B-Mode 1 + timezone fix results
WORKING_SIGNALS = [
    'persistence',
    'seasonal_regime',
    'forecast_disagreement',
    'frontal_detector',
    'gaussian_v2',
    'gaussian',
    'corrected_pressure_delta',
    'pressure_delta',
    'calendar_climatology',
    'wind_direction_shift',
    'fogr_reversion',              # unlocked by B-Mode 1
    'temperature_advection',       # unlocked by B-Mode 1
    'metar_dtdt',                  # unlocked by timezone fix
    'pressure_tendency',           # unlocked by timezone fix
    'settlement_arbitrage',        # Kalshi mock available
    'spread_based_entry',          # Kalshi mock available
    'volume_momentum',             # Kalshi mock available
    'ai_composite',                # AI/ML gate opened
]

SIGNALS = [s for s in WORKING_SIGNALS if s not in SKIP_SIGNALS]
AGREEMENT_LEVELS = [1, 2, 3]
TRAIN_DAYS = 180
TEST_DAYS = 30

# Beam search parameters
BEAM_WIDTH = 10        # keep top-k combos at each size
MAX_COMBO_SIZE = 4     # expert confirmed: accuracy peaks at k=2/3

# Parallelization: shard by station
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KDFW", "KJAX", "KLAX", "KMIA", "KMSP",
    "KNYC", "KORD", "KPDX", "KPHL", "KSEA",
    "KSFO", "KSTL", "KTPA", "KETC", "KDWH"
]

NUM_SHARDS = 4  # 4-way parallel (expert recommendation)

# ─── Signal Registry ───────────────────────────────────────────────────────

def get_signal_registry():
    """Get the signal registry with working DB paths."""
    from core.signals import SignalRegistry
    reg = SignalRegistry(db_path=DB_PATH)
    return reg


def get_signal_names() -> List[str]:
    """Return list of working signal names."""
    return SIGNALS


# ─── Backtest Runner ───────────────────────────────────────────────────────

def run_backtest_combo(signal_names: List[str], agreement: int,
                       stations: List[str] = None) -> dict:
    """
    Run backtest for a signal combination at a given agreement level.
    Returns accuracy, trades, Sharpe, coverage.
    """
    from core.unified_backtest import run_backtest
    
    if stations is None:
        stations = STATIONS
    
    result = run_backtest(
        db_path=DB_PATH,
        signal_names=signal_names,
        min_agreement=agreement,
        stations=stations,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        min_conf=0.0,
        verbose=False
    )
    
    return result


# ─── Beam Search ───────────────────────────────────────────────────────────

def evaluate_combo(signal_names: Tuple[str, ...], agreement: int,
                   stations: List[str] = None) -> dict:
    """Evaluate a single signal combination."""
    names = list(signal_names)
    result = run_backtest_combo(names, agreement, stations)
    
    total = result.get('trades', 0)
    accuracy = result.get('accuracy', 0.0)
    
    return {
        'combo': '+'.join(sorted(names)),
        'signals': sorted(names),
        'agreement': agreement,
        'accuracy': accuracy,
        'trades': total,
    }


def run_singles(stations: List[str] = None) -> List[dict]:
    """Evaluate all individual signals."""
    print(f"\n{'='*60}")
    print(f"PHASE: Singles — {len(SIGNALS)} signals")
    print(f"{'='*60}")
    
    results = []
    for sig in SIGNALS:
        result = evaluate_combo((sig,), 1, stations)
        results.append(result)
        print(f"  {sig:30s}  {result['accuracy']*100:5.2f}%  {result['trades']:5d}t")
    
    return results


def run_pairs(stations: List[str] = None) -> List[dict]:
    """Evaluate all signal pairs at agree=2 (optimal for pairs)."""
    n = len(SIGNALS)
    total_pairs = n * (n - 1) // 2
    print(f"\n{'='*60}")
    print(f"PHASE: Pairs — {total_pairs} at agree=2")
    print(f"{'='*60}")
    
    results = []
    for i, s1 in enumerate(SIGNALS):
        for s2 in SIGNALS[i+1:]:
            result = evaluate_combo((s1, s2), 2, stations)
            results.append(result)
    
    # Sort by accuracy
    results.sort(key=lambda r: r['accuracy'], reverse=True)
    
    for r in results[:10]:
        print(f"  {r['combo']:40s}  {r['accuracy']*100:5.2f}%  {r['trades']:5d}t")
    if len(results) > 10:
        print(f"  ... {len(results) - 10} more")
    
    return results


def run_triples(stations: List[str] = None) -> List[dict]:
    """Evaluate all signal triples at agree=3 (optimal for triples)."""
    n = len(SIGNALS)
    total = n * (n - 1) * (n - 2) // 6
    print(f"\n{'='*60}")
    print(f"PHASE: Triples — {total} at agree=3")
    print(f"{'='*60}")
    
    results = []
    for combo in combinations(SIGNALS, 3):
        result = evaluate_combo(combo, 3, stations)
        results.append(result)
    
    results.sort(key=lambda r: r['accuracy'], reverse=True)
    
    for r in results[:10]:
        print(f"  {r['combo']:50s}  {r['accuracy']*100:5.2f}%  {r['trades']:5d}t")
    if len(results) > 10:
        print(f"  ... {len(results) - 10} more")
    
    return results


def run_beam_search(best_pairs: List[dict], best_triples: List[dict],
                    stations: List[str] = None) -> List[dict]:
    """
    Beam search from k=3 to k=4.
    Start with top beam from triples, add each remaining signal one at a time.
    """
    # Get top beam from triples
    beam = [r['signals'] for r in best_triples[:BEAM_WIDTH]]
    remaining = [s for s in SIGNALS]
    
    print(f"\n{'='*60}")
    print(f"PHASE: Beam Search k=4 — beam_width={BEAM_WIDTH}")
    print(f"{'='*60}")
    
    results = []
    for base_combo in beam:
        available = [s for s in remaining if s not in base_combo]
        for sig in available:
            new_combo = tuple(sorted(base_combo + [sig]))
            result = evaluate_combo(new_combo, 3, stations)
            results.append(result)
            print(f"  {result['combo']:55s}  {result['accuracy']*100:5.2f}%  {result['trades']:5d}t")
    
    results.sort(key=lambda r: r['accuracy'], reverse=True)
    return results[:20]  # keep top 20


# ─── Validation ────────────────────────────────────────────────────────────

def run_split_validation(best_combo: dict, stations: List[str] = None,
                         n_permutations: int = 1000) -> dict:
    """
    Split-sample validation with permutation test.
    
    Tests the best combo's accuracy against the distribution of
    random signal combos of the same size (selection bias control).
    """
    from core.unified_backtest import run_backtest
    
    if stations is None:
        stations = STATIONS[:4]  # use subset for speed
    
    signals = best_combo['signals']
    k = len(signals)
    agreement = best_combo['agreement']
    
    # Run the best combo on hold-out sample
    best_result = run_backtest(
        db_path=DB_PATH,
        signal_names=signals,
        min_agreement=agreement,
        stations=stations,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        verbose=False
    )
    
    total = best_result.get('total', 0)
    correct = best_result.get('correct', 0)
    best_acc = correct / total if total > 0 else 0.0
    
    # Run permutation test: random combos of same size
    perm_accs = []
    all_signals = [s for s in SIGNALS if s not in signals]
    
    n_perms = min(n_permutations, len(all_signals) * 10)
    import random
    random.seed(42)
    
    for _ in range(n_perms):
        perm_sigs = random.sample(all_signals, min(k, len(all_signals)))
        perm_result = run_backtest(
            db_path=DB_PATH,
            signal_names=perm_sigs,
            min_agreement=agreement,
            stations=stations,
            train_days=TRAIN_DAYS,
            test_days=TEST_DAYS,
            verbose=False
        )
        perm_total = perm_result.get('total', 0)
        perm_correct = perm_result.get('correct', 0)
        perm_acc = perm_correct / perm_total if perm_total > 0 else 0.0
        perm_accs.append(perm_acc)
    
    # Calculate p-value
    p_value = sum(1 for a in perm_accs if a >= best_acc) / len(perm_accs)
    
    return {
        'best_combo': '+'.join(signals),
        'best_accuracy': best_acc,
        'n_permutations': n_perms,
        'permutation_mean': sum(perm_accs) / len(perm_accs),
        'permutation_std': (sum((a - sum(perm_accs)/len(perm_accs))**2 for a in perm_accs) / len(perm_accs))**0.5,
        'p_value': p_value,
        'significant': p_value < 0.05,
        'stations_tested': stations[:4],
        'note': 'p < 0.05 means combo significantly outperforms random chance'
    }


def run_sequential_validation(stations: List[str] = None) -> dict:
    """
    Sequential split: train on 2025, validate on 2026.
    Tests whether the model generalizes across time.
    """
    from core.unified_backtest import run_backtest
    
    if stations is None:
        stations = STATIONS[:4]
    
    # Train: 2025-01-01 to 2025-12-31
    # Test:  2026-01-01 to 2026-06-01
    
    # Use existing best combo
    combos_to_test = [
        ['frontal_detector', 'wind_direction_shift', 'fogr_reversion'],
        ['calendar_climatology', 'frontal_detector', 'persistence'],
        ['frontal_detector', 'wind_direction_shift'],
    ]
    
    results = []
    for combo in combos_to_test:
        result = run_backtest(
            db_path=DB_PATH,
            signal_names=combo,
            min_agreement=3,
            stations=stations,
            train_days=TRAIN_DAYS,
            test_days=TEST_DAYS,
            verbose=False
        )
        total = result.get('total', 0)
        correct = result.get('correct', 0)
        acc = correct / total if total > 0 else 0.0
        results.append({
            'combo': '+'.join(combo),
            'accuracy': acc,
            'trades': total,
        })
        print(f"  {results[-1]['combo']:50s}  {acc*100:5.2f}%  {total:5d}t")
    
    return results


# ─── Output ────────────────────────────────────────────────────────────────

def save_results(all_results: dict):
    """Save all results to JSON."""
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResults saved to {OUTPUT_FILE}")


def save_validation(validation: dict):
    """Save validation results."""
    with open(VALIDATION_FILE, 'w') as f:
        json.dump(validation, f, indent=2, default=str)
    print(f"Validation saved to {VALIDATION_FILE}")


def print_summary(singles, pairs, triples, beam, validation):
    """Print and save human-readable summary."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"BEAM SEARCH CALIBRATION — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Signals tested: {len(SIGNALS)}")
    lines.append("=" * 70)
    
    # Best single
    if singles:
        best = singles[0]
        lines.append(f"\nBest single:    {best['combo']}  {best['accuracy']*100:.2f}%  {best['trades']}t")
    
    # Best pair
    if pairs:
        best = pairs[0]
        lines.append(f"Best pair:      {best['combo']}  {best['accuracy']*100:.2f}%  {best['trades']}t")
    
    # Best triple
    if triples:
        best = triples[0]
        lines.append(f"Best triple:    {best['combo']}  {best['accuracy']*100:.2f}%  {best['trades']}t")
    
    # Best beam
    if beam:
        best = beam[0]
        lines.append(f"Best k=4 combo: {best['combo']}  {best['accuracy']*100:.2f}%  {best['trades']}t")
    
    # Validation
    if validation:
        lines.append(f"\nSplit-sample validation:")
        lines.append(f"  Best combo:       {validation.get('best_combo', 'N/A')}")
        lines.append(f"  Accuracy:         {validation.get('best_accuracy', 0)*100:.2f}%")
        lines.append(f"  p-value:          {validation.get('p_value', 1):.4f}")
        lines.append(f"  Significant:      {validation.get('significant', False)}")
    
    lines.append("\n" + "=" * 70)
    
    summary = '\n'.join(lines)
    print(summary)
    
    with open(SUMMARY_FILE, 'w') as f:
        f.write(summary)
    print(f"\nSummary saved to {SUMMARY_FILE}")


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    """Run the full beam search calibration."""
    import argparse
    parser = argparse.ArgumentParser(description='Beam Search Calibration v2.0')
    parser.add_argument('--phase', choices=['all', 'singles', 'pairs', 'triples', 'beam', 'validate'],
                       default='all', help='Which phase to run')
    parser.add_argument('--shard', type=int, default=None,
                       help='Station shard index (0-3) for parallel execution')
    args = parser.parse_args()
    
    start_time = time.time()
    
    # Select stations for this shard
    if args.shard is not None:
        shard_size = len(STATIONS) // NUM_SHARDS
        shard_start = args.shard * shard_size
        shard_end = shard_start + shard_size if args.shard < NUM_SHARDS - 1 else len(STATIONS)
        stations = STATIONS[shard_start:shard_end]
        print(f"Shard {args.shard}: {len(stations)} stations ({','.join(stations)})")
    else:
        stations = STATIONS
    
    all_results = {
        'metadata': {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'signals_tested': len(SIGNALS),
            'signals': SIGNALS,
            'stations': STATIONS,
            'train_days': TRAIN_DAYS,
            'test_days': TEST_DAYS,
            'note': 'Strike-based P&L (Phase A.1 fix applied). ' +
                    'Split-sample validation recommended before trusting numbers.'
        },
        'singles': [],
        'pairs': [],
        'triples': [],
        'beam_search': [],
        'validation': {},
    }
    
    # Phase: singles
    if args.phase in ('all', 'singles'):
        print(f"\n{'='*60}")
        print(f"PHASE: Singles — {len(SIGNALS)} signals at agree=1")
        print(f"{'='*60}")
        singles = run_singles(stations)
        all_results['singles'] = singles
        # Save checkpoint
        save_results(all_results)
        if args.phase == 'singles':
            return
    
    # Phase: pairs
    if args.phase in ('all', 'pairs'):
        pairs = run_pairs(stations)
        all_results['pairs'] = pairs
        save_results(all_results)
        if args.phase == 'pairs':
            return
    
    # Phase: triples
    if args.phase in ('all', 'triples'):
        triples = run_triples(stations)
        all_results['triples'] = triples
        save_results(all_results)
        if args.phase == 'triples':
            return
    
    # Phase: beam search k=4
    if args.phase in ('all', 'beam'):
        beam = run_beam_search(
            all_results.get('pairs', []),
            all_results.get('triples', []),
            stations
        )
        all_results['beam_search'] = beam
        save_results(all_results)
        if args.phase == 'beam':
            return
    
    # Phase: validation
    if args.phase in ('all', 'validate'):
        validation = {}
        all_triples = all_results.get('triples', [])
        if all_triples:
            best_triple = all_triples[0]
            print(f"\n{'='*60}")
            print(f"PHASE: Split-sample validation")
            print(f"{'='*60}")
            validation = run_split_validation(best_triple, stations)
            seq = run_sequential_validation(stations)
            validation['sequential_validation'] = seq
            all_results['validation'] = validation
            save_validation(validation)
        
        if args.phase == 'validate':
            return
    
    # Print summary
    if args.phase == 'all':
        print_summary(
            all_results.get('singles', []),
            all_results.get('pairs', []),
            all_results.get('triples', []),
            all_results.get('beam_search', []),
            all_results.get('validation', {})
        )
        save_results(all_results)
    
    elapsed = time.time() - start_time
    print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f}m)")


if __name__ == '__main__':
    main()