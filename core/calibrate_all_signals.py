#!/usr/bin/env python3
"""
Calibration Part 1: Run calibration pipeline for all 23 signals individually.

Evaluates each signal from the registry across all 20 stations, collects
raw confidence vs correctness pairs, fits the CalibrationPipeline with
walk-forward isotonic regression, and records raw/calibrated metrics.
"""

import os
import sys
import json
import sqlite3
import logging
import datetime
import statistics
import numpy as np
from collections import defaultdict

# Ensure core/ is on sys.path
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from signals import SignalRegistry
from calibration_pipeline import CalibrationPipeline

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# All 20 target stations
STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

DB_PATH = os.environ.get(
    'METAR_DB_PATH',
    os.path.join(CORE_DIR, '..', 'data', 'metar_backfill.db')
)

TRAIN_DAYS = 180
TEST_DAYS = 30
MIN_TRADES_FOR_CAL = 10


def load_station_data(station, conn):
    """Load daily aggregated weather data and settlement market data for a station."""
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
    for r in cur.fetchall():
        market[r[0]] = {
            'settlement_bucket': r[1],
            'reversion': r[2] if r[2] is not None else 0
        }
    return days, market


def compute_brier_score(confs, correct_bools):
    """
    Compute Brier score = mean((outcome - prob)^2)
    outcome = 1.0 if correct else 0.0
    prob = confidence value (already calibrated or raw)
    """
    total = 0.0
    n = len(confs)
    if n == 0:
        return 1.0
    for conf, correct in zip(confs, correct_bools):
        outcome = 1.0 if correct else 0.0
        total += (outcome - conf) ** 2
    return total / n


def calibrate_signal_single(signal_name, registry, signal_names_list, stations):
    """
    Run calibration for a single signal across all stations.

    Returns dict with raw/calibrated metrics per station and aggregate.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        calib = CalibrationPipeline(signal_names_list, stations)
        
        # Per-station raw data storage
        per_station_raw = defaultdict(lambda: {
            'correct': 0, 'total': 0,
            'raw_confs': [], 'correct_bools': []
        })
        
        # Track ALL samples with their station for Brier computation
        all_samples = []  # list of (station, confidence, is_correct)
        
        signal_obj = registry.get_signal(signal_name)
        if signal_obj is None:
            logger.warning(f"  Signal '{signal_name}' not found in registry, skipping")
            return None

        for station in stations:
            days, market = load_station_data(station, conn)
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

            # Walk-forward backtest
            start = TRAIN_DAYS
            while start + TEST_DAYS <= len(days):
                for idx in range(start, min(start + TEST_DAYS, len(days))):
                    date = days[idx]['date']
                    actual = market.get(date)
                    if actual is None or actual['settlement_bucket'] is None:
                        continue

                    actual_direction = 'up' if actual['settlement_bucket'] > strike else 'down'

                    try:
                        direction, confidence = signal_obj.evaluate(idx, days)
                    except Exception as e:
                        logger.warning(f"    {signal_name}@{station} eval error at idx {idx}: {e}")
                        continue

                    if direction is None or confidence < 0.0:
                        continue

                    confidence = max(0.0, min(1.0, confidence))
                    if confidence == 0.0:
                        continue

                    is_correct = (direction == actual_direction)

                    # Store per-station data
                    per_station_raw[station]['correct'] += int(is_correct)
                    per_station_raw[station]['total'] += 1
                    per_station_raw[station]['raw_confs'].append(confidence)
                    per_station_raw[station]['correct_bools'].append(is_correct)
                    
                    # Store sample with station metadata
                    all_samples.append((station, confidence, is_correct))

                    # Feed into calibration pipeline
                    calib.update(signal_name, station, confidence, is_correct)

                start += TEST_DAYS

    # If no trades, return empty
    if not all_samples:
        return None

    total_trades = len(all_samples)
    total_correct = sum(1 for _, _, c in all_samples if c)
    raw_accuracy = (total_correct / total_trades) * 100.0
    
    # Raw Brier: use raw confidences
    raw_confs_all = [conf for _, conf, _ in all_samples]
    raw_correct_all = [c for _, _, c in all_samples]
    raw_brier = compute_brier_score(raw_confs_all, raw_correct_all)

    # Refit calibrators on all collected data
    calib.refit()

    # Compute calibrated metrics per-station
    cal_correct = 0
    cal_trades = 0
    cal_brier_sum = 0.0
    cal_conf_samples = []  # list of (calibrated_confidence, is_correct)
    changed_count = 0

    per_station_result = {}
    for station in stations:
        raw_data = per_station_raw.get(station)
        if raw_data is None or raw_data['total'] == 0:
            continue
        
        s_correct = 0
        s_trades = 0
        s_brier_sum = 0.0
        
        for conf, correct in zip(raw_data['raw_confs'], raw_data['correct_bools']):
            cal_conf = calib.calibrate(signal_name, station, conf)
            
            s_trades += 1
            if correct:
                s_correct += 1
            
            # Brier using calibrated confidence
            outcome = 1.0 if correct else 0.0
            s_brier_sum += (outcome - cal_conf) ** 2
            
            # Track for aggregate Brier
            cal_conf_samples.append((cal_conf, correct))
            
            # Coverage check
            if abs(cal_conf - conf) > 0.001:
                changed_count += 1
        
        per_station_result[station] = {
            'raw': {'correct': raw_data['correct'], 'total': raw_data['total']},
            'cal': {'correct': s_correct, 'total': s_trades}
        }
        cal_correct += s_correct
        cal_trades += s_trades
        cal_brier_sum += s_brier_sum

    calibrated_brier = cal_brier_sum / total_trades if total_trades > 0 else 1.0
    calibrated_accuracy = (cal_correct / cal_trades * 100.0) if cal_trades > 0 else 0.0
    accuracy_delta = calibrated_accuracy - raw_accuracy
    coverage = (changed_count / total_trades * 100.0) if total_trades > 0 else 0.0

    result = {
        'signal': signal_name,
        'raw_accuracy': round(raw_accuracy, 2),
        'raw_trades': total_trades,
        'raw_correct': total_correct,
        'calibrated_accuracy': round(calibrated_accuracy, 2),
        'calibrated_trades': cal_trades,
        'calibrated_correct': cal_correct,
        'accuracy_delta_pp': round(accuracy_delta, 2),
        'raw_brier': round(raw_brier, 5),
        'calibrated_brier': round(calibrated_brier, 5),
        'brier_improvement': round(raw_brier - calibrated_brier, 5),
        'coverage_pct': round(coverage, 2),
        'per_station': per_station_result
    }

    return result


def main():
    logger.info("=" * 60)
    logger.info("Calibration Part 1: Run calibration for all 23 signals")
    logger.info(f"DB: {DB_PATH}")
    logger.info("=" * 60)

    registry = SignalRegistry(DB_PATH)
    all_signals = registry.get_all_signals()
    signal_names = sorted(all_signals.keys())
    
    logger.info(f"Found {len(signal_names)} signals in registry:")
    for sn in signal_names:
        logger.info(f"  - {sn}")

    results = []
    errors = []
    
    for i, signal_name in enumerate(signal_names):
        logger.info(f"\n[{i+1}/{len(signal_names)}] Calibrating signal: {signal_name}")
        try:
            result = calibrate_signal_single(signal_name, registry, [signal_name], STATIONS)
            if result is None:
                logger.warning(f"  No trades generated for '{signal_name}' — recording error")
                errors.append({
                    'signal': signal_name,
                    'error': 'No trades generated (signal never fired or insufficient data)'
                })
                results.append({
                    'signal': signal_name,
                    'raw_accuracy': None,
                    'raw_trades': 0,
                    'raw_correct': 0,
                    'calibrated_accuracy': None,
                    'calibrated_trades': 0,
                    'calibrated_correct': 0,
                    'accuracy_delta_pp': None,
                    'raw_brier': None,
                    'calibrated_brier': None,
                    'brier_improvement': None,
                    'coverage_pct': 0.0,
                    'per_station': {},
                    'error': 'No trades generated'
                })
            else:
                logger.info(f"  Raw accuracy: {result['raw_accuracy']}% ({result['raw_trades']} trades)")
                logger.info(f"  Cal accuracy: {result['calibrated_accuracy']}%")
                logger.info(f"  Raw Brier: {result['raw_brier']}, Cal Brier: {result['calibrated_brier']}")
                logger.info(f"  Brier improvement: {result['brier_improvement']}")
                logger.info(f"  Coverage: {result['coverage_pct']}%")
                results.append(result)
        except Exception as e:
            logger.error(f"  ERROR calibrating '{signal_name}': {e}")
            import traceback
            traceback.print_exc()
            errors.append({
                'signal': signal_name,
                'error': str(e)
            })
            results.append({
                'signal': signal_name,
                'raw_accuracy': None,
                'raw_trades': 0,
                'raw_correct': 0,
                'calibrated_accuracy': None,
                'calibrated_trades': 0,
                'calibrated_correct': 0,
                'accuracy_delta_pp': None,
                'raw_brier': None,
                'calibrated_brier': None,
                'brier_improvement': None,
                'coverage_pct': 0.0,
                'per_station': {},
                'error': str(e)
            })

    # Compute aggregate
    valid_results = [r for r in results if r['raw_trades'] > 0]
    if valid_results:
        agg_raw_acc = sum(r['raw_accuracy'] for r in valid_results) / len(valid_results)
        agg_cal_acc = sum(r['calibrated_accuracy'] for r in valid_results) / len(valid_results)
        agg_acc_delta = sum(r['accuracy_delta_pp'] for r in valid_results) / len(valid_results)
        agg_raw_brier = sum(r['raw_brier'] for r in valid_results) / len(valid_results)
        agg_cal_brier = sum(r['calibrated_brier'] for r in valid_results) / len(valid_results)
        agg_brier_imp = sum(r['brier_improvement'] for r in valid_results) / len(valid_results)
    else:
        agg_raw_acc = agg_cal_acc = agg_acc_delta = 0.0
        agg_raw_brier = agg_cal_brier = agg_brier_imp = 0.0

    output = {
        'metadata': {
            'timestamp': datetime.datetime.utcnow().isoformat(),
            'db_path': DB_PATH,
            'num_signals_calibrated': len(valid_results),
            'num_signals_total': len(signal_names),
            'num_errors': len(errors)
        },
        'calibration_results': results,
        'errors': errors,
        'aggregate': {
            'avg_raw_accuracy': round(agg_raw_acc, 2),
            'avg_cal_accuracy': round(agg_cal_acc, 2),
            'avg_accuracy_delta': round(agg_acc_delta, 2),
            'avg_raw_brier': round(agg_raw_brier, 5),
            'avg_cal_brier': round(agg_cal_brier, 5),
            'avg_brier_improvement': round(agg_brier_imp, 5),
            'signals_with_trades': len(valid_results),
            'signals_with_errors': len(errors)
        },
        'no_change_zone_check': {
            'no_change_zone': 0.01,
            'status': 'Isotonic regression with hierarchical fallback — identity if insufficient data'
        }
    }

    # Save to file
    output_path = os.path.join(CORE_DIR, '..', 'data', 'calibration_all_signals.json')
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"\n{'=' * 60}")
    logger.info(f"Calibration complete! Results saved to: {output_path}")
    logger.info(f"  Signals with trades: {len(valid_results)}/{len(signal_names)}")
    logger.info(f"  Signals with errors: {len(errors)}")
    logger.info(f"  Avg raw accuracy: {agg_raw_acc:.2f}%")
    logger.info(f"  Avg cal accuracy: {agg_cal_acc:.2f}%")
    logger.info(f"  Avg raw Brier: {agg_raw_brier:.5f}")
    logger.info(f"  Avg cal Brier: {agg_cal_brier:.5f}")
    logger.info(f"  Avg brier improvement: {agg_brier_imp:.5f}")
    logger.info(f"{'=' * 60}")

    if errors:
        logger.warning(f"\nErrors encountered:")
        for e in errors:
            logger.warning(f"  {e['signal']}: {e['error']}")

    return output


if __name__ == "__main__":
    main()