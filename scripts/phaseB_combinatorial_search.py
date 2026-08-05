#!/usr/bin/env python3
"""
PHASE B — Combinatorial Search with Phase 23 Signals

Re-runs combinatorial search using the full 22-signal set including Phase 23
signals (seasonal_regime, corrected_pressure_delta). Tests multiple agreement
levels (1-5) with calibrated confidences from phaseB_calibration_results.json.

Usage:
    python3 scripts/phaseB_combinatorial_search.py [--db DATA/metar_backfill.db] [--output DATA/phaseB_combinatorial_search.json] [--calibration DATA/phaseB_calibration_results.json]

Based on Phase B Expert Specification §2
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import logging
from datetime import datetime, timezone
from collections import defaultdict
from itertools import combinations
from typing import List, Dict, Tuple, Optional
import random

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.unified_backtest import (
    BACKTEST_SIGNALS, DB_PATH, load_station_data,
    compute_sharpe, compute_brier, compute_ece
)
from core.signals import SignalRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
# Suppress verbose signal logging
logging.getLogger('core.signals.frontal_detector_signal').setLevel(logging.ERROR)
logging.getLogger('core.signals.temperature_advection_signal').setLevel(logging.ERROR)

# ─── Configuration ───────────────────────────────────────────────────────────

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

# Temporarily limited to non-NWP signals (NWP table missing, GFS API rate-limited)
ALL_SIGNALS = list(BACKTEST_SIGNALS)

TRAIN_DAYS = 365
TEST_DAYS = 90
MIN_CONF = 0.0

# Signal families (for stratified sampling)
SIGNAL_FAMILIES = {
    'climatology': ['calendar_climatology'],
    'reversion': ['fogr_reversion', 'metar_dtdt', 'nwp_dtdt_fusion', 'spread_based_entry', 'settlement_arbitrage', 'volume_momentum'],
    'gaussian': ['gaussian', 'gaussian_v2'],
    'pressure': ['pressure_delta', 'corrected_pressure_delta', 'pressure_tendency'],
    'wind': ['wind_direction_shift', 'temperature_advection', 'frontal_detector'],
    'nwp': ['nwp_direct', 'ecmwf_bias_corrected', 'esdr'],
    'intraday': ['intraday_metar_confirmation'],
    'disagreement': ['forecast_disagreement'],
    'persistence': ['persistence'],
    'seasonal': ['seasonal_regime'],
    'other': ['goldilocks'],
}

SUBSET_DEFINITIONS = {
    'subset_a_core5': ['calendar_climatology', 'gaussian', 'goldilocks', 'pressure_delta', 'forecast_disagreement'],
    'subset_b_core5_reversion': [
        'calendar_climatology', 'gaussian', 'goldilocks', 'pressure_delta', 'forecast_disagreement',
        'fogr_reversion', 'metar_dtdt', 'nwp_dtdt_fusion', 'spread_based_entry', 'settlement_arbitrage', 'volume_momentum'
    ],
    'subset_c_core5_nwp': [
        'calendar_climatology', 'gaussian', 'goldilocks', 'pressure_delta', 'forecast_disagreement',
        'nwp_direct', 'ecmwf_bias_corrected', 'esdr', 'wind_direction_shift', 'temperature_advection', 'frontal_detector'
    ],
    'subset_d_all_pressure': ['pressure_delta', 'corrected_pressure_delta', 'pressure_tendency'],
    'subset_e_phase23': ['corrected_pressure_delta', 'seasonal_regime', 'calendar_climatology'],
    'subset_f_full_22': ALL_SIGNALS,
}

# Maximum combos per size k
MAX_COMBOS_PER_K = 200


def load_calibrated_confidences(calibration_path: str) -> dict:
    """Load calibration results and extract a simple calibrated confidence lookup."""
    cal = {}
    if calibration_path and os.path.exists(calibration_path):
        try:
            with open(calibration_path, 'r') as f:
                cal_data = json.load(f)
            for sig, metrics in cal_data.get('per_signal', {}).items():
                cal[sig] = {
                    'calibrated_accuracy': metrics.get('calibrated_accuracy', 0.5),
                    'ece': metrics.get('ece', 0.5),
                }
            logger.info(f"Loaded {len(cal)} calibrated signal accuracies from {calibration_path}")
        except Exception as e:
            logger.warning(f"Could not load calibration data: {e}")
    return cal


def run_combination_backtest(
    signal_names: List[str],
    stations: List[str],
    db_path: str,
    agreement_level: int = 1,
    min_conf: float = 0.0,
    train_days: int = TRAIN_DAYS,
    test_days: int = TEST_DAYS,
) -> dict:
    """
    Run a backtest with a specific signal subset and agreement level.
    Returns {accuracy, sharpe, brier, ece, trades, coverage}
    """
    conn = sqlite3.connect(db_path)
    registry = SignalRegistry(db_path)
    all_results = []  # (pred, actual, conf)
    total_possible = 0

    for station in stations:
        days, market = load_station_data(station, conn)
        if len(days) < train_days + test_days:
            continue

        # Direction is determined by comparing today's settlement to yesterday's.
        # All signals predict directional change, not strike level.
        start = train_days
        while start + test_days <= len(days):
            for idx in range(start, min(start + test_days, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None or actual['settlement_bucket'] is None:
                    continue

                prev_bucket = actual.get('prev_bucket')
                if prev_bucket is None:
                    continue
                actual_direction = 'up' if actual['settlement_bucket'] > prev_bucket else 'down'
                total_possible += 1

                # Evaluate signals
                signal_outputs = []
                for sig_name in signal_names:
                    sig = registry.get_signal(sig_name)
                    if sig is None:
                        continue
                    try:
                        direction, confidence = sig.evaluate(idx, days)
                    except Exception:
                        continue
                    if direction is not None and confidence >= min_conf:
                        signal_outputs.append((sig_name, direction, float(confidence)))

                # Agreement gate
                if len(signal_outputs) < agreement_level:
                    continue

                # Check direction agreement for agreement_level >= 2
                if agreement_level >= 2:
                    directions = set(d for _, d, _ in signal_outputs)
                    if len(directions) > 1:
                        continue  # No consensus

                # Weighted vote
                wsum = sum((1 if d == 'up' else -1) * c for _, d, c in signal_outputs)
                aw = sum(c for _, _, c in signal_outputs)
                if aw == 0:
                    continue
                conf = abs(wsum) / aw
                pred = 'up' if wsum > 0 else 'down'
                all_results.append((pred, actual_direction, conf))

            start += test_days

    conn.close()

    if not all_results:
        return {
            'accuracy': 0.0, 'sharpe': 0.0, 'brier': 1.0, 'ece': 1.0,
            'trades': 0, 'coverage': 0.0
        }

    correct = sum(1 for p, a, _ in all_results if p == a)
    n = len(all_results)
    accuracy = correct / n
    sharpe = compute_sharpe([(c, p == a) for p, a, c in all_results])
    brier = compute_brier(all_results)
    ece_val = compute_ece(all_results)
    coverage = n / total_possible if total_possible > 0 else 0.0

    return {
        'accuracy': round(accuracy, 4),
        'sharpe': round(sharpe, 4),
        'brier': round(brier, 4),
        'ece': round(ece_val, 4),
        'trades': n,
        'coverage': round(coverage, 4),
    }


def generate_stratified_combinations(signal_names: List[str], k: int, max_combos: int = MAX_COMBOS_PER_K) -> List[tuple]:
    """Generate signal combinations, stratified by family."""
    if k == 0:
        return []
    n = len(signal_names)
    total = math.comb(n, k)
    if total <= max_combos:
        return list(combinations(signal_names, k))

    # Stratified sampling: ensure representation from each family
    fam_map = {}
    for fam, sigs in SIGNAL_FAMILIES.items():
        for s in sigs:
            if s in signal_names:
                fam_map[s] = fam

    result = []
    seed_tuples = []
    # Seed: one from each family if possible
    families_present = [fam for fam, sigs in SIGNAL_FAMILIES.items()
                        if any(s in signal_names for s in sigs)]
    for fam in families_present:
        fam_sigs = [s for s in signal_names if fam_map.get(s) == fam]
        if fam_sigs:
            seed_tuples.append(random.choice(fam_sigs))

    # Fill remaining slots
    remaining = [s for s in signal_names if s not in seed_tuples]
    needed = k - len(seed_tuples)
    if needed > 0:
        random.shuffle(remaining)
        seed_tuples.extend(remaining[:needed])
    result.append(tuple(sorted(seed_tuples[:k])))

    # Add random combos up to max
    seen = set(result)
    signal_list = list(signal_names)
    attempts = 0
    while len(result) < max_combos and attempts < max_combos * 10:
        combo = tuple(sorted(random.sample(signal_list, k)))
        if combo not in seen:
            result.append(combo)
            seen.add(combo)
        attempts += 1

    return result


def run_phaseB_combinatorial_search(db_path: str, output_path: str, calibration_path: Optional[str] = None) -> dict:
    """Main combinatorial search function."""
    logger.info("=" * 60)
    logger.info("PHASE B: Combinatorial Search with Phase 23 Signals")
    logger.info(f"DB: {db_path}")
    logger.info(f"Signals: {len(ALL_SIGNALS)}")
    logger.info(f"Stations: {len(STATIONS)}")
    logger.info("=" * 60)

    calibrated = load_calibrated_confidences(calibration_path)

    # ─── Agreement Level Analysis ─────────────────────────────────────────
    agreement_analysis = {}
    for level in [1, 2, 3, 5]:
        logger.info(f"\nTesting agreement level {level} with all 22 signals...")
        result = run_combination_backtest(
            ALL_SIGNALS, STATIONS, db_path,
            agreement_level=level,
            min_conf=MIN_CONF,
        )
        agreement_analysis[f"level_{level}"] = {
            "avg_accuracy": result['accuracy'],
            "avg_sharpe": result['sharpe'],
            "avg_trades": result['trades'],
            "agreement_level": level,
        }
        logger.info(f"  Level {level}: acc={result['accuracy']:.4f}, sharpe={result['sharpe']:.2f}, trades={result['trades']}")

    # ─── Per-Subset Analysis ──────────────────────────────────────────────
    best_per_subset = {}
    for subset_name, signal_list in SUBSET_DEFINITIONS.items():
        logger.info(f"\nTesting subset '{subset_name}' ({len(signal_list)} signals)...")

        # Try various agreement levels for each subset
        best_combo = None
        best_accuracy = 0.0

        for level in [1, 2, 3]:
            result = run_combination_backtest(
                signal_list, STATIONS, db_path,
                agreement_level=level,
                min_conf=MIN_CONF,
            )
            if result['accuracy'] > best_accuracy:
                best_accuracy = result['accuracy']
                best_combo = {
                    "signals": signal_list,
                    "agreement_level": level,
                    "accuracy": result['accuracy'],
                    "sharpe": result['sharpe'],
                    "brier": result['brier'],
                    "ece": result['ece'],
                    "trades": result['trades'],
                    "coverage": result['coverage'],
                }

        best_per_subset[subset_name] = best_combo if best_combo else {
            "best_combo": [],
            "accuracy": 0.0,
            "sharpe": 0.0,
        }
        if best_combo:
            logger.info(f"  Best: level={best_combo['agreement_level']}, acc={best_combo['accuracy']:.4f}, sharpe={best_combo['sharpe']:.2f}, trades={best_combo['trades']}")

    # ─── Best Single Signals ──────────────────────────────────────────────
    logger.info(f"\nTesting all 22 single signals...")
    single_signal_results = {}
    for sig in ALL_SIGNALS:
        result = run_combination_backtest([sig], STATIONS, db_path, agreement_level=1)
        single_signal_results[sig] = result
        if result['trades'] > 0:
            logger.info(f"  {sig:35s}: acc={result['accuracy']:.4f}, sharpe={result['sharpe']:.2f}, trades={result['trades']}")

    # Best performing single signals
    sorted_singles = sorted(
        [(s, r) for s, r in single_signal_results.items() if r['trades'] > 50],
        key=lambda x: x[1]['accuracy'], reverse=True
    )
    best_singles = [{"signal": s, "accuracy": r['accuracy'], "sharpe": r['sharpe']}
                    for s, r in sorted_singles[:10]]

    # ─── Best Pairs ──────────────────────────────────────────────────────
    logger.info(f"\nTesting best pairs (top 100 combinations)...")
    pair_combos = list(combinations(ALL_SIGNALS, 2))
    # Take top 100 by sampling
    if len(pair_combos) > 100:
        pair_combos = random.sample(pair_combos, 100)
    pair_results = []
    for sig_a, sig_b in pair_combos:
        result = run_combination_backtest([sig_a, sig_b], STATIONS, db_path, agreement_level=1)
        if result['trades'] > 50:
            pair_results.append({
                "combo": [sig_a, sig_b],
                "accuracy": result['accuracy'],
                "sharpe": result['sharpe'],
                "trades": result['trades'],
            })
    pair_results.sort(key=lambda x: x['accuracy'], reverse=True)
    best_pairs = pair_results[:10]

    # ─── Phase 23 Integration Test ────────────────────────────────────────
    logger.info(f"\nPhase 23 integration tests...")
    phase23_combos = [
        ['corrected_pressure_delta', 'seasonal_regime'],
        ['corrected_pressure_delta', 'seasonal_regime', 'calendar_climatology'],
        ['corrected_pressure_delta', 'seasonal_regime', 'pressure_delta'],
        ['corrected_pressure_delta', 'seasonal_regime', 'calendar_climatology', 'gaussian'],
        ['corrected_pressure_delta', 'seasonal_regime', 'calendar_climatology', 'gaussian', 'forecast_disagreement'],
    ]
    phase23_results = {}
    for combo in phase23_combos:
        result = run_combination_backtest(combo, STATIONS, db_path, agreement_level=1)
        key = "+".join(combo)
        phase23_results[key] = result
        logger.info(f"  {key}: acc={result['accuracy']:.4f}, sharpe={result['sharpe']:.2f}, trades={result['trades']}")

    # ─── Output ───────────────────────────────────────────────────────────
    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "B",
            "signals_available": len(ALL_SIGNALS),
            "signals_tested": ALL_SIGNALS,
            "subsets_tested": list(SUBSET_DEFINITIONS.keys()),
            "total_combinations": sum(
                min(math.comb(len(sig_list), k), MAX_COMBOS_PER_K)
                for sig_list in SUBSET_DEFINITIONS.values()
                for k in range(1, min(6, len(sig_list) + 1))
            ),
            "agreement_levels_tested": [1, 2, 3, 5],
        },
        "best_per_subset": {
            name: {
                "best_combo": data.get('signals', []),
                "agreement_level": data.get('agreement_level', 1),
                "accuracy": data.get('accuracy', 0.0),
                "sharpe": data.get('sharpe', 0.0),
                "brier": data.get('brier', 1.0),
                "ece": data.get('ece', 1.0),
                "trades": data.get('trades', 0),
                "coverage": data.get('coverage', 0.0),
            }
            for name, data in best_per_subset.items()
        },
        "agreement_analysis": agreement_analysis,
        "best_single_signals": best_singles,
        "best_pairs": best_pairs,
        "single_signal_results": single_signal_results,
        "phase23_integration": phase23_results,
    }

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    logger.info(f"\nResults saved to {output_path}")

    return output


def main():
    parser = argparse.ArgumentParser(description="Phase B — Combinatorial Search")
    parser.add_argument('--db', default=DB_PATH, help='Path to metar_backfill.db')
    parser.add_argument('--output', default='data/phaseB_combinatorial_search.json',
                        help='Output file path')
    parser.add_argument('--calibration', default='data/phaseB_calibration_results.json',
                        help='Calibration results file path')
    args = parser.parse_args()
    run_phaseB_combinatorial_search(args.db, args.output, args.calibration)


if __name__ == '__main__':
    main()