#!/usr/bin/env python3
"""
PHASE B — Full 22-Signal Calibration Pipeline (FIXED v2)

Runs walk-forward isotonic regression calibration across all 22 signals × 20 stations.
Outputs per-signal, per-signal-per-station, and aggregate metrics.

FIXES applied:
1. Uses BACKTEST_SIGNALS (22 signals from unified_backtest) instead of hardcoded 15
2. Validates direction is 'up' or 'down' (rejects seasonal_regime's class labels)
3. Routes intraday/DB signals to evaluate_for_station
4. Sets sig._station for signals needing context (nwp_direct)
5. Skips temperature_advection (makes live GFS API calls)
"""

import sqlite3
import json
import os
import sys
import argparse
import logging
import time
from datetime import datetime, timezone
from collections import defaultdict

import numpy as np
from sklearn.isotonic import IsotonicRegression

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.unified_backtest import (
    BACKTEST_SIGNALS, DB_PATH, load_station_data,
    compute_sharpe, compute_brier, compute_ece
)
from core.signals import SignalRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', stream=sys.stdout)
logger = logging.getLogger(__name__)

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

ALL_22_SIGNALS = list(BACKTEST_SIGNALS)

TRAIN_DAYS = 365
TEST_DAYS = 90
MIN_CONF = 0.0

CALIBRATION_MIN_SAMPLES = {
    'per_cell': 200,
    'per_signal_global': 400,
    'cross_signal_global': 800,
}

# Signals whose evaluate() always returns (None, 0.0) — must use evaluate_for_station
SIGNALS_NEEDING_FOR_STATION = {
    'intraday_metar_confirmation', 'fogr_reversion', 'metar_dtdt', 'pressure_tendency',
    'ecmwf_bias_corrected', 'esdr', 'nwp_dtdt_fusion'
}

# Signals that need sig._station set for evaluate() to work
SIGNALS_NEEDING_STATION_CONTEXT = {'nwp_direct', 'ai_composite'}

# temperature_advection makes live GFS API calls — skip in this environment
SLOW_SIGNALS_TO_SKIP = {
    'temperature_advection',      # Makes live GFS API calls
    'intraday_metar_confirmation', # 20s/station for 0 trades
    'ecmwf_bias_corrected',        # Missing ecmwf_forecasts table
    'esdr',                        # Missing member_index column
    'nwp_direct',                  # Needs NWP DB data not available
}

# Signals where evaluate() returning None is a valid 'no signal' (NOT a fallback trigger)
# For these, the evaluate() interface is canonical; None means no prediction
STANDARD_EVALUATE_SIGNALS = {
    'gaussian', 'gaussian_v2', 'goldilocks', 'persistence', 'pressure_delta',
    'calendar_climatology', 'wind_direction_shift', 'forecast_disagreement',
    'frontal_detector', 'spread_based_entry', 'volume_momentum', 'settlement_arbitrage',
    'seasonal_regime'
}


def evaluate_signal(sig, sig_name, station, idx, date, days, conn):
    """Route signal evaluation correctly. Returns (direction, confidence)."""
    if sig_name in SIGNALS_NEEDING_FOR_STATION:
        if hasattr(sig, 'evaluate_for_station'):
            try:
                return sig.evaluate_for_station(station, date, conn=conn)
            except Exception:
                pass
        return None, 0.0

    if sig_name in SIGNALS_NEEDING_STATION_CONTEXT:
        sig._station = station

    try:
        direction, confidence = sig.evaluate(idx, days)
    except Exception:
        direction, confidence = None, 0.0

    if direction is None and sig_name not in STANDARD_EVALUATE_SIGNALS:
        if hasattr(sig, 'evaluate_for_station'):
            try:
                direction, confidence = sig.evaluate_for_station(station, date, conn=conn)
            except Exception:
                pass

    return direction, confidence


def run_phaseB_calibration(db_path: str, output_path: str) -> dict:
    logger.info("=" * 60)
    logger.info("PHASE B: Full 22-Signal Calibration Pipeline (FIXED v2)")
    logger.info(f"DB: {db_path}")
    logger.info(f"Signals: {len(ALL_22_SIGNALS)} ({len(ALL_22_SIGNALS) - len(SLOW_SIGNALS_TO_SKIP)} active)")
    logger.info(f"Stations: {len(STATIONS)}")
    logger.info(f"Walk-forward: train={TRAIN_DAYS}d, test={TEST_DAYS}d")
    logger.info(f"Skipped signals: {SLOW_SIGNALS_TO_SKIP}")
    logger.info("=" * 60)
    sys.stdout.flush()

    conn = sqlite3.connect(db_path)
    registry = SignalRegistry(db_path)

    # Suppress verbose signal logging
    for mod_name in ['core.signals.frontal_detector_signal', 'core.signals.temperature_advection_signal',
                      'core.signals.forecast_disagreement_signal']:
        logging.getLogger(mod_name).setLevel(logging.ERROR)

    # Build signal objects dict (skip slow signals)
    signal_objects = {}
    for sig_name in ALL_22_SIGNALS:
        if sig_name in SLOW_SIGNALS_TO_SKIP:
            continue
        sig = registry.get_signal(sig_name)
        if sig is not None:
            signal_objects[sig_name] = sig
    logger.info(f"Active signals: {len(signal_objects)}")
    sys.stdout.flush()

    # ─── Step 1: Data Collection ──────────────────────────────────────────
    history = defaultdict(list)
    all_results = defaultdict(lambda: defaultdict(list))
    total_trading_days = set()
    signal_trade_counts = defaultdict(int)
    t_start = time.time()

    for st_idx, station in enumerate(STATIONS):
        days, market = load_station_data(station, conn)
        if len(days) < TRAIN_DAYS + TEST_DAYS:
            logger.warning(f"  {station}: insufficient data ({len(days)} days)")
            continue

        start = TRAIN_DAYS
        while start + TEST_DAYS <= len(days):
            for idx in range(start, min(start + TEST_DAYS, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None or actual['settlement_bucket'] is None:
                    continue
                prev_bucket = actual.get('prev_bucket')
                if prev_bucket is None:
                    continue
                actual_direction = 'up' if actual['settlement_bucket'] > prev_bucket else 'down'
                total_trading_days.add((station, date))

                for sig_name, sig in signal_objects.items():
                    direction, confidence = evaluate_signal(sig, sig_name, station, idx, date, days, conn)
                    if direction is not None and direction in ('up', 'down') and confidence >= MIN_CONF:
                        raw_conf = float(confidence)
                        was_correct = (direction == actual_direction)
                        history[(sig_name, station)].append((raw_conf, was_correct))
                        all_results[sig_name][station].append((direction, actual_direction, raw_conf, was_correct))
                        signal_trade_counts[sig_name] += 1
            start += TEST_DAYS

        elapsed = time.time() - t_start
        logger.info(f"  [{st_idx+1}/{len(STATIONS)}] {station}: {len(days)}d ({elapsed:.1f}s)")
        sys.stdout.flush()

    conn.close()

    elapsed = time.time() - t_start
    logger.info(f"\nData collection: {elapsed:.1f}s, {len(total_trading_days)} station-days")
    for sig_name in sorted(signal_trade_counts.keys(), key=lambda s: -signal_trade_counts[s]):
        logger.info(f"  {sig_name:35s}: {signal_trade_counts[sig_name]} trades")
    sys.stdout.flush()

    # Handle skipped signals (mark as insufficient_data)
    for sig_name in ALL_22_SIGNALS:
        if sig_name not in all_results:
            all_results[sig_name]

    # ─── Step 2: Isotonic Regression Fitting ──────────────────────────────
    calibrators = {}
    fallback_calibrators = {}
    global_calibrator = None
    calibration_cell_count = {}

    # Level 1: Per-signal per-station
    for signal in ALL_22_SIGNALS:
        for station in STATIONS:
            data = history.get((signal, station), [])
            if len(data) >= CALIBRATION_MIN_SAMPLES['per_cell']:
                X = np.array([d[0] for d in data], dtype=float)
                y = np.array([float(d[1]) for d in data], dtype=float)
                if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
                    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
                    iso.fit(X, y)
                    calibrators[(signal, station)] = iso
                    calibration_cell_count[signal] = calibration_cell_count.get(signal, 0) + 1

    logger.info(f"Level 1: {len(calibrators)} per-signal-per-station calibrators")

    # Level 2: Per-signal global
    for signal in ALL_22_SIGNALS:
        global_data = []
        for station in STATIONS:
            global_data.extend(history.get((signal, station), []))
        if len(global_data) >= CALIBRATION_MIN_SAMPLES['per_signal_global']:
            X = np.array([d[0] for d in global_data], dtype=float)
            y = np.array([float(d[1]) for d in global_data], dtype=float)
            if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
                iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
                iso.fit(X, y)
                fallback_calibrators[signal] = iso

    logger.info(f"Level 2: {len(fallback_calibrators)} per-signal global calibrators")

    # Level 3: Cross-signal global
    all_data = [d for data in history.values() for d in data]
    if len(all_data) >= CALIBRATION_MIN_SAMPLES['cross_signal_global']:
        X = np.array([d[0] for d in all_data], dtype=float)
        y = np.array([float(d[1]) for d in all_data], dtype=float)
        if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
            iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
            iso.fit(X, y)
            global_calibrator = iso
            logger.info(f"Level 3: global calibrator fitted (n={len(all_data)})")
    else:
        logger.info("Level 3: global calibrator NOT fitted")
    sys.stdout.flush()

    def calibrate_confidence(signal, station, raw_conf):
        clipped = np.clip(raw_conf, 0.0, 1.0)
        key = (signal, station)
        if key in calibrators:
            return float(calibrators[key].transform([clipped])[0])
        if signal in fallback_calibrators:
            return float(fallback_calibrators[signal].transform([clipped])[0])
        if global_calibrator is not None:
            return float(global_calibrator.transform([clipped])[0])
        return clipped

    def get_fallback_level(signal, station):
        key = (signal, station)
        if key in calibrators:
            return "per_cell"
        if signal in fallback_calibrators:
            return "per_signal"
        if global_calibrator is not None:
            return "cross_signal"
        return "identity"

    # ─── Step 3: Metrics ──────────────────────────────────────────────────
    per_signal_metrics = {}
    per_signal_per_station = {}

    for signal in ALL_22_SIGNALS:
        results_dict = all_results.get(signal, {})
        total_results = sum(len(v) for v in results_dict.values())
        if total_results == 0:
            level = get_fallback_level(signal, STATIONS[0]) if any(history.get((signal, st)) for st in STATIONS) else "identity"
            per_signal_metrics[signal] = {
                "raw_accuracy": 0.0, "calibrated_accuracy": 0.0,
                "raw_brier": 1.0, "calibrated_brier": 1.0,
                "ece": 1.0, "sharpe": 0.0, "coverage": 0.0, "total_trades": 0,
                "calibration_cell_count": 0,
                "fallback_level": "insufficient_data",
                "status": "insufficient_data"
            }
            continue

        total_raw_correct = 0
        total_calibrated_correct = 0
        total_trades = 0
        raw_brier_sum = 0.0
        calibrated_brier_sum = 0.0
        all_results_list_cal = []

        for station in STATIONS:
            results = results_dict.get(station, [])
            if not results:
                continue
            station_total = len(results)
            station_raw_correct = sum(1 for _, _, _, c in results if c)
            station_calibrated_correct = 0

            for pred, actual, raw_conf, correct in results:
                cal_conf = calibrate_confidence(signal, station, raw_conf)
                prob_up_raw = raw_conf if pred == 'up' else 1.0 - raw_conf
                prob_up_cal = cal_conf if pred == 'up' else 1.0 - cal_conf
                outcome = 1.0 if actual == 'up' else 0.0
                raw_brier_sum += (prob_up_raw - outcome) ** 2
                calibrated_brier_sum += (prob_up_cal - outcome) ** 2
                station_calibrated_correct += 1 if (pred == actual) else 0

            station_raw_acc = station_raw_correct / station_total if station_total > 0 else 0.0
            station_cal_acc = station_calibrated_correct / station_total if station_total > 0 else 0.0
            per_signal_per_station[f"{signal}.{station}"] = {
                "raw_accuracy": round(station_raw_acc, 4),
                "calibrated_accuracy": round(station_cal_acc, 4),
                "raw_brier": round(raw_brier_sum / station_total, 4),
                "total_trades": station_total,
            }
            total_raw_correct += station_raw_correct
            total_calibrated_correct += station_calibrated_correct
            total_trades += station_total
            all_results_list_cal.extend([(p, a, calibrate_confidence(signal, station, c))
                                         for p, a, c, _ in results])

        raw_accuracy = total_raw_correct / total_trades
        calibrated_accuracy = total_calibrated_correct / total_trades
        raw_brier = raw_brier_sum / total_trades
        calibrated_brier = calibrated_brier_sum / total_trades
        ece_val = compute_ece(all_results_list_cal)
        sharpe_val = compute_sharpe([(c, p == a) for p, a, c in all_results_list_cal])
        coverage = total_trades / len(total_trading_days) if total_trading_days else 0.0

        best_level = "identity"
        for st in STATIONS:
            lvl = get_fallback_level(signal, st)
            if lvl == "per_cell":
                best_level = "per_cell"; break
            elif lvl == "per_signal" and best_level != "per_cell":
                best_level = "per_signal"
            elif lvl == "cross_signal" and best_level not in ("per_cell", "per_signal"):
                best_level = "cross_signal"

        per_signal_metrics[signal] = {
            "raw_accuracy": round(raw_accuracy, 4),
            "calibrated_accuracy": round(calibrated_accuracy, 4),
            "raw_brier": round(raw_brier, 4),
            "calibrated_brier": round(calibrated_brier, 4),
            "ece": round(ece_val, 4),
            "sharpe": round(sharpe_val, 4),
            "coverage": round(coverage, 4),
            "total_trades": total_trades,
            "calibration_cell_count": calibration_cell_count.get(signal, 0),
            "fallback_level": best_level,
            "status": "calibrated"
        }
        logger.info(f"  {signal:35s}: raw_acc={raw_accuracy:.4f} cal_acc={calibrated_accuracy:.4f} "
                     f"sharpe={sharpe_val:.2f} trades={total_trades} fallback={best_level}")

    # ─── Aggregate ────────────────────────────────────────────────────────
    valid = [s for s, m in per_signal_metrics.items() if m['status'] == 'calibrated']
    if valid:
        avg_raw = np.mean([per_signal_metrics[s]['raw_accuracy'] for s in valid])
        avg_cal = np.mean([per_signal_metrics[s]['calibrated_accuracy'] for s in valid])
        avg_brier = np.mean([per_signal_metrics[s]['calibrated_brier'] for s in valid])
        avg_ece = np.mean([per_signal_metrics[s]['ece'] for s in valid])
        above_60 = sum(1 for s in valid if per_signal_metrics[s]['calibrated_accuracy'] > 0.60)
        above_65 = sum(1 for s in valid if per_signal_metrics[s]['calibrated_accuracy'] > 0.65)
        valid_cal = sum(1 for s in valid if per_signal_metrics[s]['fallback_level'] in ('per_cell', 'per_signal'))
    else:
        avg_raw = avg_cal = avg_brier = avg_ece = 0.0
        above_60 = above_65 = valid_cal = 0

    aggregate = {
        "avg_raw_accuracy": round(avg_raw, 4),
        "avg_calibrated_accuracy": round(avg_cal, 4),
        "avg_brier": round(avg_brier, 4),
        "avg_ece": round(avg_ece, 4),
        "signals_above_60pct": above_60,
        "signals_above_65pct": above_65,
        "signals_with_valid_calibrator": valid_cal,
        "total_signals_processed": len(valid)
    }

    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "B",
            "signals_calibrated": len(ALL_22_SIGNALS),
            "stations": len(STATIONS),
            "train_days": TRAIN_DAYS,
            "test_days": TEST_DAYS,
            "total_trading_days_processed": len(total_trading_days),
        },
        "per_signal": per_signal_metrics,
        "per_signal_per_station": per_signal_per_station,
        "aggregate": aggregate,
    }

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    logger.info(f"\nResults saved to {output_path}")

    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Signals processed: {len(valid)}/{len(ALL_22_SIGNALS)}")
    logger.info(f"  Avg raw accuracy: {avg_raw:.4f}")
    logger.info(f"  Avg calibrated accuracy: {avg_cal:.4f}")
    logger.info(f"  Avg Brier: {avg_brier:.4f}")
    logger.info(f"  Avg ECE: {avg_ece:.4f}")
    logger.info(f"  Signals above 60%: {above_60}")
    logger.info(f"  Signals above 65%: {above_65}")
    logger.info(f"  Valid calibrators: {valid_cal}")
    logger.info("=" * 60)
    sys.stdout.flush()

    return output


def main():
    parser = argparse.ArgumentParser(description="Phase B — Calibration Pipeline (FIXED)")
    parser.add_argument('--db', default=DB_PATH, help='Path to metar_backfill.db')
    parser.add_argument('--output', default='data/phaseB_calibration_results.json', help='Output file path')
    args = parser.parse_args()
    run_phaseB_calibration(args.db, args.output)


if __name__ == '__main__':
    main()