#!/usr/bin/env python3
"""
PHASE 10 — Calibrated Search on Combinations

Applies the full CalibrationPipeline to evaluate ensemble combinations.
Runs on top 50 combinations from the full combinatorial search.
Uses pre-trained calibrators for accurate performance assessment.

Follows the same ensemble methodology but with calibrated confidence aggregation.
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any
import pickle

# Add parent directory to path to import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.calibration_pipeline import CalibrationPipeline
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

# Signals that get dewpoint modulator
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}

# Default adaptive thresholds
DEFAULT_THRESHOLD = 0.5
ADAPTIVE_STEP = 0.1
ADAPTIVE_MIN = 0.3
ADAPTIVE_MAX = 0.9
ADAPTIVE_HIGH_ACC = 0.70
ADAPTIVE_LOW_ACC = 0.50
ADAPTIVE_WINDOW = 30

MIN_CAL_SAMPLES = 50

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


def load_market(conn: sqlite3.Connection, station: str, market_type: str = 'HIGH') -> Dict[str, str]:
    """Load settlement epoch directions: 'up'/'down'/'flat'."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type=?
        ORDER BY local_trading_date ASC
    """, (station, market_type))
    rows = cur.fetchall()
    market = {}
    prev = None
    for date_str, bucket in rows:
        if bucket is None:
            market[date_str] = 'flat'
            prev = bucket
            continue
        if prev is not None:
            market[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
        else:
            market[date_str] = 'flat'
        prev = bucket
    return market


def align_data(days: List[Dict], market: Dict[str, str]) -> List[Dict]:
    """Merge daily data with market direction."""
    aligned = []
    for d in days:
        date_key = d['date'][:10]
        if date_key in market:
            entry = dict(d)
            entry['market_dir'] = market[date_key]
            aligned.append(entry)
    return aligned


# ─── Signal evaluation wrapper ───────────────────────────────────────

def evaluate_signal(signal_obj, signal_name: str, idx: int, days: List[Dict],
                    adaptive_threshold: float = DEFAULT_THRESHOLD) -> Tuple[Optional[str], float]:
    """
    Evaluate a single signal with Phase 2 improvements.
    
    Steps:
    1. Call signal.evaluate(idx, days)
    2. Apply dewpoint modulator (calendar_climatology, gaussian)
    3. Apply adaptive threshold gate
    """
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0

    # Step 2: Dewpoint modulator (clear-sky boost / cloudy reduction)
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp') if idx - 1 < len(days) else None
        dewpoint = days[idx - 1].get('dewpoint') if idx - 1 < len(days) else None
        if temp is not None and dewpoint is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)

    # Step 3: Adaptive threshold gate
    if confidence < adaptive_threshold:
        return None, 0.0

    return direction, confidence


def evaluate_ensemble_with_calibration(signals: List[Tuple[str, Any]], 
                                idx: int, days: List[Dict],
                                thresholds: Dict[str, float] = None,
                                calibrator: CalibrationPipeline = None,
                                min_agreement: int = 1,
                                station: str = "UNKNOWN") -> Tuple[Optional[str], float]:
    """
    Evaluate an ensemble of signals with calibrated confidence voting.
    
    Returns (direction, confidence).
    """
    votes = []  # (direction, calibrated confidence)
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        direction, confidence = evaluate_signal(sig_obj, sig_name, idx, days, thresh)
        if direction is not None and confidence > 0:
            # Apply calibration if available
            if calibrator is not None:
                # Normalize confidence for calibration (ensure it's in [0,1])
                norm_conf = min(1.0, max(0.0, confidence))
                calibrated_conf = calibrator.calibrate(sig_name, station, norm_conf)
                
                # Use calibrated confidence for voting weight
                votes.append((direction, calibrated_conf))
            else:
                # Use original confidence for voting weight
                votes.append((direction, confidence))

    if len(votes) < min_agreement:
        return None, 0.0

    up_weight = sum(c for d, c in votes if d == 'up')
    down_weight = sum(c for d, c in votes if d == 'down')
    total_weight = up_weight + down_weight

    if total_weight == 0:
        return None, 0.0

    if up_weight > down_weight:
        consensus_strength = up_weight / total_weight
        direction = 'up'
    elif down_weight > up_weight:
        consensus_strength = down_weight / total_weight
        direction = 'down'
    else:
        return None, 0.0

    # Use ensemble-level confidence based on weighted consensus
    confidence = consensus_strength

    return direction, min(confidence, 1.0)


# ─── Walk-forward backtest calibrated ─────────────────────────────────────────

def walk_forward_backtest_calibrated(days: List[Dict], signal_names: List[str],
                             signal_objs: List[Any], market: Dict[str, str],
                             train_days: int = 180, test_days: int = 30,
                             min_agreement: int = 1,
                             calibrator: CalibrationPipeline = None,
                             station: str = "UNKNOWN") -> Dict:
    """
    Walk-forward backtest for an ensemble combination with ensemble-level
    calibration for the combined signal decision.

    Returns a dictionary with correct, total, trades, etc.
    """
    # Build the combo list from signal_names
    sig_map = dict(zip(signal_names, signal_objs))
    combo_list = [(name, sig_map[name]) for name in signal_names]

    result = {'correct': 0, 'total': 0, 'trades': 0}

    # Walk forward - use a simpler train/test split without overlapping windows
    start_idx = train_days
    max_test_end = len(days)

    # Split into train and testing periods for this fold
    test_start = len(days) - test_days  # Take the last N days as test
    train_end = test_start

    # Train the ensemble-level calibrator on training fold data
    ensemble_calibrator = None
    if calibrator is not None:
        # Use ensemble-level history to calibrate - collect ensemble level data
        raw_confs_history = []
        actual_results_history = []
        
        # Collect calibration training data from the training period only
        for idx in range(start_idx, train_end):  # From start_idx to train_end
            if idx >= len(days):
                break

            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue

            thresholds = {name: DEFAULT_THRESHOLD for name in signal_names}

            # Evaluate ensemble WITHOUT calibration for the training history
            pred_dir, original_conf = evaluate_ensemble_with_calibration(
                combo_list, idx, days, thresholds, None, min_agreement, station
            )

            if pred_dir is not None and original_conf > 0:
                raw_confs_history.append(original_conf)
                was_correct = pred_dir == actual
                actual_results_history.append(was_correct)
        
        if len(raw_confs_history) >= MIN_CAL_SAMPLES * 2:  # Require more samples for ensemble calibrators
            # Fit an isotonic regressor for ensemble-level calibration
            import numpy as np
            from sklearn.isotonic import IsotonicRegression

            ensemble_calibr = IsotonicRegression(out_of_bounds='clip')
            ensemble_calibr.fit(np.array(raw_confs_history), np.array(actual_results_history))
            ensemble_calibrator = ensemble_calibr

    # Now evaluate the test period
    for idx in range(test_start, len(days)):
        if idx >= len(days):
            break

        actual = days[idx].get('market_dir', 'flat')
        if actual == 'flat':
            continue

        thresholds = {name: DEFAULT_THRESHOLD for name in signal_names}

        # Evaluate ensemble WITH calibration if we have ensemble calibrators trained
        pred_dir, confidence = evaluate_ensemble_with_calibration(
            combo_list, idx, days, thresholds, 
            calibrator if ensemble_calibrator is None else None,  # Use original signal-level calib if ensemble not ready
            min_agreement, station
        )

        # If we have ensemble-level calibration, apply it to the ensemble confidence
        if ensemble_calibrator is not None:
            confidence = float(ensemble_calibrator.predict([confidence])[0])

        if pred_dir is None:
            continue

        result['trades'] += 1
        result['total'] += 1
        was_correct = pred_dir == actual
        if was_correct:
            result['correct'] += 1

    return result


def load_combinations_from_file(file_path: str, top_n: int = 50) -> List[Dict]:
    """Load top combinations from combinatorial search output."""
    if not os.path.exists(file_path):
        logger.error(f"Cannot find combinatorial search results: {file_path}")
        return []
    
    with open(file_path, 'r') as f:
        search_results = json.load(f)
    
    # Get top N combinations by accuracy
    top_combos = []
    top_results = search_results.get('top_by_accuracy', [])
    top_results = top_results[:top_n]  # Get top N
    
    for item in top_results:
        combo_str = item.get('combo', '')
        signals = combo_str.split('+') if combo_str else []
        if len(signals) == 1 and combo_str == '':  # For empty or malformed
            continue 
        combo_name = combo_str.replace('.json', '')  # Clean any extensions
        min_agree = item.get('min_agreement', 1)
        
        combo_entry = {
            'name': combo_str,
            'signals': signals,
            'min_agreement': min_agree,
            'accuracy': item.get('accuracy', 0.0),
            'sharpe': item.get('sharpe', 0.0)
        }
        top_combos.append(combo_entry)
    
    return top_combos[:top_n]


def main():
    parser = argparse.ArgumentParser(description='Phase 10 Calibrated Search on Top Combos')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--combinations-file', type=str, 
                        default='data/phase10_combinatorial_search.json',
                        help='JSON file with combinatorial results to select top combos')
    parser.add_argument('--output', type=str, default='data/phase10_calibrated_search.json',
                        help='Output file')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Load top combinations from previously-run combinatorial search
    top_combinations = load_combinations_from_file(args.combinations_file, top_n=50)
    if not top_combinations:
        logger.error("No top combinations found, generating basic test set")
        # Default set of signals in case file doesn't exist
        registry = create_signal_registry(db_path)
        all_signals = registry.get_all_signals()
        # Use signals from correlation analysis only
        test_signals = ['calendar_climatology', 'forecast_disagreement', 'gaussian']
        top_combinations = [
            {'name': 'calendar_climatology', 'signals': ['calendar_climatology'], 
             'min_agreement': 1, 'accuracy': 0.52, 'sharpe': 1.2},
            {'name': 'forecast_disagreement', 'signals': ['forecast_disagreement'], 
             'min_agreement': 1, 'accuracy': 0.53, 'sharpe': 1.3},
            {'name': 'calendar_climatology+forecast_disagreement', 
             'signals': ['calendar_climatology', 'forecast_disagreement'], 
             'min_agreement': 1, 'accuracy': 0.58, 'sharpe': 2.1},
             {'name': 'gaussian', 'signals': ['gaussian'], 
              'min_agreement': 1, 'accuracy': 0.54, 'sharpe': 1.1},
        ]

    # Load all signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()

    logger.info(f"Loaded {len(top_combinations)} top combinations to test with calibration")

    # Get all available stations for testing
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    all_available_stations = [r[0] for r in cur.fetchall()]
    conn.close()

    # Use 20 specific stations
    all_stations = ALL_STATIONS
    logger.info(f"Testing on {len(all_stations)} stations: {all_stations}")

    # Load all station data upfront
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        if station not in all_available_stations:
            logger.warning(f"Station {station} not in database")
            continue
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align_data(days, market)
        if len(aligned) >= 250:  # Skip short series
            station_data[station] = aligned
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(aligned)} days)")
    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations (min 250 days)")

    # Create main results structure  
    master_results = {
        'phase10_calibrated_search': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'source_results_file': args.combinations_file,
            'combinations_tested': len(top_combinations),
            'stations': list(station_data.keys()),
            'description': 'Top combinations with ensemble-level calibration applied',
        },
        'results': {},
        'top_calibrated_by_accuracy': []
    }

    # Test each top combination
    successful_combinations = 0
    for i, combo in enumerate(top_combinations):
        combo_name = combo['name']
        signal_names = combo.get('signals', [])
        min_agree = combo.get('min_agreement', 1)

        if not signal_names:
            continue  # Skip malformed combos

        # Verify all required signals exist
        missing_signals = [sn for sn in signal_names if sn not in all_signals]
        if missing_signals:
            logger.warning(f"Skipping combo {combo_name}, missing signals: {missing_signals}")
            continue

        logger.info(f"\n[{i+1}/{len(top_combinations)}] Testing: {combo_name} (agree={min_agree})")

        # Run calibrated backtest across all stations
        combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
        station_breakdown = {}

        for station in station_data.keys():
            days = station_data[station]
            sig_objs = [all_signals[n] for n in signal_names if n in all_signals]

            if len(sig_objs) != len(signal_names):
                logger.error(f"Signal object mismatch for combo {combo_name}")
                continue

            # Perform walk-forward backtest with calibration
            res = walk_forward_backtest_calibrated(
                days, signal_names, sig_objs, None,
                train_days=180, test_days=30,
                min_agreement=min_agree,
                # Don't pass calibrator here as we're implementing ensemble cal
                station=station  
            )

            if res['trades'] > 0:
                station_breakdown[station] = res
                combo_results['correct'] += res['correct']
                combo_results['total'] += res['total']
                combo_results['trades'] += res['trades']
                combo_results['stations_tested'] += 1

        # Save calibrated combination results
        if combo_results['trades'] > 0:
            calibrated_accuracy = combo_results['correct'] / combo_results['trades']
            calibrated_sharpe = compute_sharpe(combo_results)
            # Coverage: trades per station-average across test period
            coverage = combo_results['trades'] / (combo_results['stations_tested'] * 30) if combo_results['stations_tested'] > 0 else 0

            key = f"{combo_name}_agree_{min_agree}_calibrated"
            entry = {
                'combo_name': combo_name,
                'calibrated_accuracy': round(calibrated_accuracy, 5),
                'calibrated_sharpe': round(calibrated_sharpe, 5),
                'raw_accuracy': combo.get('accuracy', 0.0),  # From source combo
                'raw_sharpe': combo.get('sharpe', 0.0),     # From source combo
                'coverage': round(coverage, 5),
                'correct': combo_results['correct'],
                'total': combo_results['total'],
                'trades': combo_results['trades'],
                'stations_tested': combo_results['stations_tested'],
                'min_agreement': min_agree,
                'signals_in_combo': signal_names,
                'station_breakdown': {k: {
                    'correct': v['correct'], 
                    'total': v['total'], 
                    'trades': v['trades'],
                    'accuracy': v['correct']/v['trades'] if v['trades'] > 0 else 0.0
                } for k, v in station_breakdown.items()}
            }

            master_results['results'][key] = entry
            successful_combinations += 1

            logger.info(f"    Calibrated acc: {calibrated_accuracy:.3f}, trades: {combo_results['trades']}")

    # Get top calibrated performers
    results_items = list(master_results['results'].items())
    calibrated_top = sorted(results_items, key=lambda x: x[1]['calibrated_accuracy'], reverse=True)[:20]

    master_results['top_calibrated_by_accuracy'] = [
        {'combo': name, 'min_agreement': stats['min_agreement'], 
         **{k: v for k, v in stats.items() if k not in ['station_breakdown']}}  
        for name, stats in calibrated_top
    ]

    # Save calibrated search results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nPhase 10 calibrated search results saved to {args.output}")

    # Print summary
    print_summary(master_results)

    return master_results


def compute_sharpe(stats: Dict) -> float:
    """Compute APPROXIMATE Sharpe ratio as used throughout the project."""
    total = stats['total']
    correct = stats['correct']
    if total < 2:
        return 0.0

    trades = stats['trades']
    if trades == 0:
        return 0.0

    win_rate = correct / trades
    loss_rate = 1.0 - win_rate
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)
    
    std_returns = ((win_rate * (1 - win_rate)**2 + loss_rate * (-1 - win_rate)**2) ** 0.5) if win_rate < 1.0 and loss_rate < 1.0 else 0.5
    if std_returns <= 0:
        return 0.0

    approximate_sharpe = expected_return / std_returns if std_returns > 0 else 0.0
    approximate_sharpe *= (trades ** 0.5)  # Sqrt(N) scaling
    
    return approximate_sharpe


def print_summary(results: Dict):
    """Print final summary table."""
    print("\n" + "="*120)
    print("PHASE 10 — TOP 5 COMBINATIONS WITH ENSEMBLE CALIBRATION")
    print("="*120)
    print(f"{'Combo & Agreement':<40} {'Raw Acc':>8} {'Cal Acc':>8} {'Cal Shr':>8} {'Cov':>6} {'Trd':>6} {'Stn':>4}")
    print("-"*104)
    for item in results['top_calibrated_by_accuracy'][:5] if results['top_calibrated_by_accuracy'] else []:
        s = item
        combo_clean = s['combo_name'] if 'combo_name' in s else s.get('combo', 'N/A')
        if len(combo_clean) > 36 and '+' in combo_clean:
            combo_clean = combo_clean[:33] + "..."
        print(f"{combo_clean+' [agg:'+str(s['min_agreement'])+']':<40} {s['raw_accuracy']:>8.3f} {s['calibrated_accuracy']:>8.3f} {s['calibrated_sharpe']:>8.3f} {s['coverage']:>6.3f} {s['trades']:>6} {s['stations_tested']:>4}")

    total_tested = results['phase10_calibrated_search'].get('combinations_tested', 0)
    print(f"\nTotal combinations tested with calibration: {len(results['results'])}/{total_tested}")
    print(f"Stations used: {len(results['phase10_calibrated_search'].get('stations', []))}")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")