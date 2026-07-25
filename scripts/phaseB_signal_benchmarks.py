#!/usr/bin/env python3
"""
PHASE B — Individual Signal Benchmarking

Benchmarks each of the 22 signals individually across all 20 stations.
Computes accuracy, coverage, Brier, ECE, Sharpe, max drawdown, station consistency,
directional symmetry, and firing rate.

Usage:
    python3 scripts/phaseB_signal_benchmarks.py [--db DATA/metar_backfill.db] [--output DATA/phaseB_signal_benchmarks.json]

Based on Phase B Expert Specification §3
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
from typing import List, Dict, Tuple, Optional

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.unified_backtest import (
    BACKTEST_SIGNALS, DB_PATH, load_station_data,
    compute_sharpe, compute_brier, compute_ece, max_drawdown
)
from core.signals import SignalRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

TRAIN_DAYS = 365
TEST_DAYS = 90
MIN_CONF = 0.0

# Signals to skip (missing NWP infrastructure, slow)
SKIP_SIGNALS = {'temperature_advection', 'intraday_metar_confirmation', 'hrrr_bias_corrected', 'esdr', 'nwp_direct'}
SIGNAL_LIST = [s for s in BACKTEST_SIGNALS if s not in SKIP_SIGNALS]


def run_single_signal_backtest(
    signal_name: str,
    stations: List[str],
    db_path: str,
    train_days: int = TRAIN_DAYS,
    test_days: int = TEST_DAYS,
    min_conf: float = MIN_CONF,
) -> dict:
    """
    Run a backtest for a single signal across all stations.
    Returns detailed per-station and aggregate metrics.
    """
    conn = sqlite3.connect(db_path)
    registry = SignalRegistry(db_path)
    sig = registry.get_signal(signal_name)
    if sig is None:
        conn.close()
        return {
            "accuracy": 0.0, "coverage": 0.0, "brier": 1.0, "ece": 1.0,
            "sharpe": 0.0, "max_drawdown": 0.0, "station_consistency": 0.0,
            "directional_symmetry": 0.0, "firing_rate": 0.0,
            "total_trades": 0, "pass": False, "warnings": ["signal_not_found"]
        }

    all_results = []          # (pred, actual, conf)
    per_station_results = []  # station-level accuracy
    up_correct = up_total = 0
    down_correct = down_total = 0
    total_possible = 0
    total_trades = 0

    for station in stations:
        days, market = load_station_data(station, conn)
        if len(days) < train_days + test_days:
            continue

        station_trades = 0
        station_correct = 0

        # Direction is day-over-day change, matching signal predictions
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

                try:
                    direction, confidence = sig.evaluate(idx, days)
                except Exception:
                    continue
                if direction is None or confidence < min_conf:
                    continue

                total_trades += 1
                station_trades += 1
                conf = float(confidence)
                is_correct = (direction == actual_direction)
                if is_correct:
                    station_correct += 1

                if direction == 'up':
                    up_total += 1
                    if is_correct:
                        up_correct += 1
                else:
                    down_total += 1
                    if is_correct:
                        down_correct += 1

                all_results.append((direction, actual_direction, conf))

            start += test_days

        if station_trades > 0:
            per_station_results.append(station_correct / station_trades)

    conn.close()

    if not all_results:
        return {
            "accuracy": 0.0, "coverage": 0.0, "brier": 1.0, "ece": 1.0,
            "sharpe": 0.0, "max_drawdown": 0.0, "station_consistency": 0.0,
            "directional_symmetry": 0.0, "firing_rate": 0.0,
            "total_trades": 0, "pass": False, "warnings": ["no_trades"],
            "per_station": {}
        }

    n = len(all_results)
    correct = sum(1 for p, a, _ in all_results if p == a)
    accuracy = correct / n
    coverage = total_trades / total_possible if total_possible > 0 else 0.0
    brier = compute_brier(all_results)
    ece_val = compute_ece(all_results)
    sharpe = compute_sharpe([(c, p == a) for p, a, c in all_results])
    dd = max_drawdown(all_results)

    # Station consistency: % of stations with acc > 0.50
    stations_above_50 = sum(1 for acc in per_station_results if acc > 0.50)
    station_consistency = stations_above_50 / len(per_station_results) if per_station_results else 0.0

    # Directional symmetry: |acc_up - acc_down|
    acc_up = up_correct / up_total if up_total > 0 else 0.0
    acc_down = down_correct / down_total if down_total > 0 else 0.0
    directional_symmetry = abs(acc_up - acc_down)

    # Firing rate: trades per trading day
    firing_rate = total_trades / total_possible if total_possible > 0 else 0.0

    # Pass/fail
    passes = accuracy > 0.55
    warnings = []
    if coverage < 0.10:
        warnings.append("low_coverage")
    if brier > 0.25:
        warnings.append("high_brier")
    if ece_val > 0.05:
        warnings.append("high_ece")
    if sharpe < 1.0:
        warnings.append("low_sharpe")
    if station_consistency < 0.60:
        warnings.append("low_station_consistency")
    if directional_symmetry > 0.10:
        warnings.append("directional_asymmetry")
    if firing_rate < 0.15:
        warnings.append("low_firing_rate")

    # Build per-station breakdown
    per_station_breakdown = {}
    conn2 = sqlite3.connect(db_path)
    for station in stations:
        days2, market2 = load_station_data(station, conn2)
        if len(days2) < train_days + test_days:
            per_station_breakdown[station] = {"accuracy": 0.0, "trades": 0, "status": "insufficient_data"}
            continue
        st_trades = 0
        st_correct = 0
        prev_bucket = None
        start = train_days
        while start + test_days <= len(days2):
            for idx in range(start, min(start + test_days, len(days2))):
                date = days2[idx]['date']
                actual = market2.get(date)
                if actual is None or actual['settlement_bucket'] is None:
                    continue
                prev_bucket = actual.get('prev_bucket')
                if prev_bucket is None:
                    continue
                actual_direction = 'up' if actual['settlement_bucket'] > prev_bucket else 'down'
                try:
                    direction, confidence = sig.evaluate(idx, days2)
                except Exception:
                    continue
                if direction is None or confidence < min_conf:
                    continue
                st_trades += 1
                if direction == actual_direction:
                    st_correct += 1
            start += test_days
        if st_trades > 0:
            per_station_breakdown[station] = {
                "accuracy": round(st_correct / st_trades, 4),
                "trades": st_trades,
                "status": "ok"
            }
        else:
            per_station_breakdown[station] = {"accuracy": 0.0, "trades": 0, "status": "no_trades"}
    conn2.close()

    return {
        "accuracy": round(accuracy, 4),
        "coverage": round(coverage, 4),
        "brier": round(brier, 4),
        "ece": round(ece_val, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(dd, 4),
        "station_consistency": round(station_consistency, 4),
        "directional_symmetry": round(directional_symmetry, 4),
        "firing_rate": round(firing_rate, 4),
        "total_trades": total_trades,
        "pass": passes,
        "warnings": warnings,
        "up_accuracy": round(acc_up, 4),
        "down_accuracy": round(acc_down, 4),
        "per_station": per_station_breakdown,
    }


def run_phaseB_signal_benchmarks(db_path: str, output_path: str, signal_list=None) -> dict:
    """Benchmark all signals."""
    if signal_list is None:
        signal_list = SIGNAL_LIST
    logger.info("=" * 60)
    logger.info("PHASE B: Individual Signal Benchmarking")
    logger.info(f"DB: {db_path}")
    logger.info(f"Signals: {len(signal_list)}")
    logger.info(f"Stations: {len(STATIONS)}")
    logger.info("=" * 60)

    # Signals we know are problematic (from spec)
    signals_to_flag = ['goldilocks']

    per_signal = {}
    for sig_name in signal_list:
        logger.info(f"  Benchmarking {sig_name}...")
        result = run_single_signal_backtest(sig_name, STATIONS, db_path)
        per_signal[sig_name] = result
        if result['total_trades'] > 0:
            logger.info(f"    acc={result['accuracy']:.4f}, brier={result['brier']:.4f}, "
                       f"sharpe={result['sharpe']:.2f}, trades={result['total_trades']}, "
                       f"pass={result['pass']}, warnings={result['warnings']}")
        else:
            logger.info(f"    no trades")

    # Summary
    passing = [s for s, m in per_signal.items() if m['pass']]
    failing = [s for s, m in per_signal.items() if not m['pass'] and m['total_trades'] > 0]
    flagged = [s for s in signals_to_flag if s in per_signal and not per_signal[s]['pass']]

    avg_accuracy = np.mean([m['accuracy'] for m in per_signal.values() if m['total_trades'] > 0])
    best_signal = max([(s, m) for s, m in per_signal.items() if m['total_trades'] > 0],
                      key=lambda x: x[1]['accuracy'], default=(None, None))
    worst_signal = min([(s, m) for s, m in per_signal.items() if m['total_trades'] > 0],
                       key=lambda x: x[1]['accuracy'], default=(None, None))

    summary = {
        "signals_passing": len(passing),
        "signals_failing": len(failing),
        "avg_accuracy": round(avg_accuracy, 4),
        "best_signal": {
            "name": best_signal[0] if best_signal[0] else "none",
            "accuracy": best_signal[1]['accuracy'] if best_signal[1] else 0.0
        } if best_signal[0] else {},
        "worst_signal": {
            "name": worst_signal[0] if worst_signal[0] else "none",
            "accuracy": worst_signal[1]['accuracy'] if worst_signal[1] else 0.0
        } if worst_signal[0] else {},
        "signals_flagged_for_removal": flagged,
    }

    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "B",
            "signals_benchmarked": len(SIGNAL_LIST),
            "stations": len(STATIONS),
            "train_days": TRAIN_DAYS,
            "test_days": TEST_DAYS,
        },
        "per_signal": per_signal,
        "summary": summary,
    }

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    logger.info(f"\nResults saved to {output_path}")

    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Signals passing: {len(passing)}/{len(SIGNAL_LIST)}")
    logger.info(f"  Signals failing: {len(failing)}")
    logger.info(f"  Avg accuracy: {avg_accuracy:.4f}")
    logger.info(f"  Best signal: {best_signal[0] if best_signal[0] else 'none'} ({best_signal[1]['accuracy'] if best_signal[1] else 0.0:.4f})")
    logger.info(f"  Worst signal: {worst_signal[0] if worst_signal[0] else 'none'} ({worst_signal[1]['accuracy'] if worst_signal[1] else 0.0:.4f})")
    logger.info(f"  Flagged for removal: {flagged}")
    logger.info("=" * 60)

    return output


def main():
    parser = argparse.ArgumentParser(description="Phase B — Individual Signal Benchmarking")
    parser.add_argument('--db', default=DB_PATH, help='Path to metar_backfill.db')
    parser.add_argument('--output', default='data/phaseB_signal_benchmarks.json',
                        help='Output file path')
    args = parser.parse_args()
    run_phaseB_signal_benchmarks(args.db, args.output)


if __name__ == '__main__':
    main()