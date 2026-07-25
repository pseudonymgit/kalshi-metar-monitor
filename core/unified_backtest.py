#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-08 Deploy: Merge risk-guardrails-2026-07-08 to main for 9-signal ensemble release]
#

"""
Unified backtest runner using core/signals/ registry.

Both the paper trading engine and calibration optimizer use this module
to run the actual ensemble backtest with real data - no simulation.

This replaces the inline signal functions in comprehensive_split_backtest.py.
"""

import sqlite3
import math
import os
import sys
import numpy as np
from collections import defaultdict
from scipy.stats import binomtest
from typing import List, Dict, Tuple, Optional

# Ensure core/ is on sys.path
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from core.signals import SignalRegistry
from core.signal_fusion import SignalFusionEngine, TimeDecaySignalManager
from core.market_cost_model import MARKET_COST_MODEL

# Canonical signal name constants (replaced Phase 19-removed imports from signals/__init__.py)
BACKTEST_SIGNALS = [
    # Core signals (verified firing, non-zero variance per correlation matrix 2026-07-25)
    'gaussian', 'gaussian_v2', 'spike_reversion', 'goldilocks',
    'pressure_delta', 'calendar_climatology', 'wind_direction_shift',
    'forecast_disagreement', 'corrected_pressure_delta',
    # Spike-reversion + Sure Thing lanes (separate from forecasting ensemble)
    'frontal_passage_intraday',
]

FULL_ENSEMBLE = BACKTEST_SIGNALS


DB_PATH = os.environ.get(
    'METAR_DB_PATH',
    '/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db'
)
FEE_RATE = MARKET_COST_MODEL.round_trip_fraction()


def load_station_data(station, conn):
    """Load daily aggregated data and settlement market data for a station.

    Returns (days, market) where market maps date -> {
        'settlement_bucket': actual high temperature (int °F),
        'reversion': whether a reversion occurred
    }.

    Direction is NOT computed here — callers must compare settlement_bucket
    to the strike price to determine actual market outcome.
    """
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

    cur.execute("""
        SELECT local_trading_date, settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
           AND settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    prev_bucket = None
    for r in cur.fetchall():
        market[r[0]] = {
            'settlement_bucket': r[1],
            'reversion': r[2] if r[2] is not None else 0,
            'prev_bucket': prev_bucket
        }
        prev_bucket = r[1]
    return days, market


def compute_sharpe(returns, fee_rate=None):
    """Compute Sharpe ratio from (confidence, correct) pairs."""
    if fee_rate is None:
        fee_rate = MARKET_COST_MODEL.round_trip_fraction()
    if not returns:
        return 0.0
    vals = []
    for conf, ok in returns:
        gross = 2 * conf if ok else -2 * conf
        fee = fee_rate * conf
        vals.append(gross - fee)
    n = len(vals)
    if n == 0:
        return 0.0
    mean = sum(vals) / n
    var = np.var(vals, ddof=1) if n > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    return mean / std if std > 0 else 0.0


def compute_brier(results):
    """Compute Brier score from (pred, actual, conf) triples."""
    if not results:
        return 0.0
    total = 0.0
    for pred, actual, conf in results:
        prob_up = conf if pred == 'up' else 1.0 - conf
        outcome = 1.0 if actual == 'up' else 0.0
        total += (prob_up - outcome) ** 2
    return total / len(results)


def compute_ece(results, n_bins=10):
    """Compute Expected Calibration Error."""
    if not results:
        return 0.0
    bins = [[] for _ in range(n_bins)]
    for pred, actual, conf in results:
        bin_idx = min(int(conf * n_bins), n_bins - 1)
        ok = (pred == actual)
        bins[bin_idx].append((conf, ok))
    ece = 0.0
    n = len(results)
    for b in bins:
        if not b:
            continue
        acc = sum(1 for _, ok in b if ok) / len(b)
        avg_conf = sum(c for c, _ in b) / len(b)
        ece += (len(b) / n) * abs(acc - avg_conf)
    return ece


def max_drawdown(results, initial=250.0, bet=10.0):
    """Compute max drawdown from (pred, actual, conf) triples."""
    bankroll = initial
    peak = bankroll
    max_dd = 0.0
    for pred, actual, conf in results:
        ok = (pred == actual)
        position = min(bet * conf, bankroll * 0.08)
        if ok:
            bankroll += position * 0.95
        else:
            bankroll -= position
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


def run_backtest(
    signal_names=None,
    stations=None,
    db_path=DB_PATH,
    min_conf=0.0,
    min_agreement=1,
    use_fusion=False,
    use_time_decay=True,
    fusion_params=None,
    train_days=180,
    test_days=30,
    verbose=False
) -> Dict:
    """
    Run a backtest using the unified signal pipeline.

    Args:
        signal_names: list of signal canonical names (default: BACKTEST_SIGNALS)
        stations: list of station codes (default: all 20)
        db_path: path to metar DB
        min_conf: minimum confidence threshold
        min_agreement: minimum number of signals that must agree on direction
        use_fusion: if True, use SignalFusionEngine for LLOP fusion
        use_time_decay: if True, apply time-decay reliability adjustment
        fusion_params: dict of fusion params (decay_factor, window, etc.)
        train_days: minimum training days before first prediction
        test_days: walk-forward test window size
        verbose: print progress

    Returns:
        Dict with keys: accuracy, sharpe, brier, ece, drawdown, trades,
                        per_signal_stats, per_station_stats
    """
    if signal_names is None:
        signal_names = BACKTEST_SIGNALS
    if stations is None:
        stations = [
            'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
            'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
            'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
        ]

    if fusion_params is None:
        fusion_params = {
            'decay_factor': 0.9,
            'window': 30,
        }

    registry = SignalRegistry(db_path)
    conn = sqlite3.connect(db_path)

    # Initialize fusion engine if needed
    fusion_engine = None
    time_decay_mgr = None
    if use_fusion:
        fusion_engine = SignalFusionEngine(signal_names, stations)
    if use_time_decay:
        time_decay_mgr = TimeDecaySignalManager(
            signal_names, stations,
            decay_factor=fusion_params.get('decay_factor', 0.9),
            window=fusion_params.get('window', 30)
        )

    all_results = []  # (pred, actual, conf)
    per_signal_stats = defaultdict(lambda: {'correct': 0, 'total': 0})
    per_station_stats = defaultdict(lambda: {'correct': 0, 'total': 0})
    signal_fired_count = defaultdict(int)

    for station in stations:
        days, market = load_station_data(station, conn)
        if len(days) < train_days + test_days:
            if verbose:
                print(f"  {station}: insufficient data ({len(days)} days)")
            continue

        # Direction is determined by comparing today's settlement to yesterday's.
        # All signals predict directional change (today vs yesterday), not
        # strike level (above/below median). See docs/plans/BACKTEST-AUDIT-FINDINGS.md
        if verbose:
            print(f"  {station}: using day-over-day directional validation")

        # Walk-forward backtest
        start = train_days
        while start + test_days <= len(days):
            for idx in range(start, min(start + test_days, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None:
                    continue

                # Determine actual market outcome: directional change
                # All signals predict 'up' = temp will be warmer than yesterday,
                # 'down' = temp will be cooler than yesterday.
                # This matches Phase 8/9 behavior documented at line 89-115 of
                # scripts/phase8_combinatorial_search.py
                if actual['settlement_bucket'] is None:
                    continue
                prev_bucket = actual.get('prev_bucket')
                if prev_bucket is None:
                    continue  # No previous day to compare
                actual_direction = 'up' if actual['settlement_bucket'] > prev_bucket else 'down'

                # Evaluate all signals
                signal_outputs = []
                for sig_name in signal_names:
                    sig = registry.get_signal(sig_name)
                    if sig is None:
                        continue
                    direction, confidence = sig.evaluate(idx, days)
                    if direction is not None and confidence >= min_conf:
                        # Apply time-decay if enabled
                        if time_decay_mgr:
                            adjusted = time_decay_mgr.adjust_confidence(
                                sig_name, station, confidence
                            )
                            confidence = adjusted
                        signal_outputs.append((sig_name, direction, confidence))
                        signal_fired_count[sig_name] += 1

                if not signal_outputs:
                    continue

                # Agreement gate: require min_agreement signals to fire
                if len(signal_outputs) < min_agreement:
                    continue

                # Fuse signals or use simple majority vote
                if fusion_engine and len(signal_outputs) >= 2:
                    fused_dir, fused_prob, fused_conf = fusion_engine.fuse_signals(
                        signal_outputs, station,
                        time_decay_mgr=time_decay_mgr if use_time_decay else None
                    )
                    if fused_dir is None:
                        continue  # DS conflict
                    pred = fused_dir
                    conf = fused_prob
                else:
                    # Simple weighted vote
                    wsum = sum(
                        (1 if p == 'up' else -1) * c
                        for _, p, c in signal_outputs
                    )
                    aw = sum(c for _, _, c in signal_outputs)
                    if aw == 0:
                        continue
                    conf = abs(wsum) / aw
                    pred = 'up' if wsum > 0 else 'down'

                if conf < min_conf:
                    continue

                is_correct = (pred == actual_direction)
                all_results.append((pred, actual_direction, conf))
                per_station_stats[station]['total'] += 1
                per_station_stats[station]['correct'] += int(is_correct)
                for sig_name, _, _ in signal_outputs:
                    per_signal_stats[sig_name]['total'] += 1
                    per_signal_stats[sig_name]['correct'] += int(is_correct)

                # Update time-decay for each signal
                if time_decay_mgr:
                    for sig_name, direction, _ in signal_outputs:
                        was_correct = (direction == actual_direction)
                        time_decay_mgr.update(sig_name, station, date, was_correct)

            start += test_days

    conn.close()

    # Compute aggregate metrics
    if not all_results:
        return {
            'accuracy': 0.0, 'fee_adjusted_accuracy': 0.0, 'sharpe': 0.0, 'brier': 1.0, 'ece': 1.0,
            'drawdown': 0.0, 'trades': 0,
            'per_signal_stats': {}, 'per_station_stats': {}
        }

    n = len(all_results)
    correct = sum(1 for p, a, _ in all_results if p == a)
    accuracy = correct / n
    sharpe = compute_sharpe([(c, p == a) for p, a, c in all_results])
    brier = compute_brier(all_results)
    ece = compute_ece(all_results)
    dd = max_drawdown(all_results)

    # ⚠️ B-Mode R8 Cycle 4.1: PAPER ACCURACY — unconfirmed against settlement data
    # These accuracy numbers are computed from directional predictions compared
    # against METAR observations, NOT against actual Kalshi settlement buckets.
    # Paper accuracy systematically differs from settlement-validated accuracy.
    # See docs/weather-engine/BACKTEST-SETTLEMENT-VALIDATION.md for the delta.
    if not hasattr(run_backtest, '_cycle4_warning_printed'):
        print("\n⚠️  WARNING: All accuracy numbers below are PAPER accuracy.")
        print("   These have NOT been validated against Kalshi settlement data.")
        print("   Settlement-validated accuracy will differ (see Cycle 4.4).\n")
        run_backtest._cycle4_warning_printed = True

    return {
        'accuracy': accuracy,
        'fee_adjusted_accuracy': accuracy - (0.5 + FEE_RATE),
        'sharpe': sharpe,
        'brier': brier,
        'ece': ece,
        'drawdown': dd,
        'trades': n,
        'accuracy_type': 'PAPER_ACCURACY_UNCONFIRMED',  # B-Mode R8 Cycle 4.1
        'per_signal_stats': {
            sig: {
                'accuracy': stats['correct'] / stats['total'] if stats['total'] > 0 else 0,
                'fee_adjusted_accuracy': (stats['correct'] / stats['total'] if stats['total'] > 0 else 0) - (0.5 + FEE_RATE),
                'total': stats['total']
            }
            for sig, stats in per_signal_stats.items()
        },
        'per_station_stats': {
            st: {
                'accuracy': stats['correct'] / stats['total'] if stats['total'] > 0 else 0,
                'fee_adjusted_accuracy': (stats['correct'] / stats['total'] if stats['total'] > 0 else 0) - (0.5 + FEE_RATE),
                'total': stats['total']
            }
            for st, stats in per_station_stats.items()
        }
    }


if __name__ == "__main__":
    print("Running unified backtest with default 7-signal ensemble...")
    results = run_backtest(verbose=True)
    print(f"\nResults: accuracy={results['accuracy']:.4f}, sharpe={results['sharpe']:.3f}, "
          f"brier={results['brier']:.3f}, ece={results['ece']:.3f}, trades={results['trades']}")
    print(f"\nPer-signal stats:")
    for sig, stats in results['per_signal_stats'].items():
        print(f"  {sig}: {stats['accuracy']:.4f} ({stats['total']} trades)")
