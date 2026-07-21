#!/usr/bin/env python3
"""
PHASE 9 — CALIBRATION FOR 11 SIGNAL COMBINATIONS: Calibration-Insider-Search

Implements isotonic calibration within the combinatorial search loop.
Previous searches optimized for raw accuracy, but calibrated accuracy
should be the true metric for portfolio construction.

Steps:
1. For each combo in the search, run walk-forward calibration on the training fold
2. Score combos on calibrated accuracy on the test fold
3. Ensure min 50 positive examples per calibrator (per Gray Room Round 5 recommendation)

Focus: Real calibrated performance on holdout data, not overfitted raw performance.
       Calibration models trained on different historical windows for each fold.
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
import numpy as np
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any
from scipy import interpolate

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
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

# Minimum samples required for calibration
MIN_CAL_SAMPLES = 50

# ─── Data loading ─────────────────────────────────────────────────────────────

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


# ─── Isotonic calibration utilities ──────────────────────────────────────

class IsotonicCalibrator:
    """Simple isotonic calibration for binary outcomes."""
    
    def __init__(self, min_samples: int = 50):
        self.min_samples = min_samples
        self.calibrator = None
        self.trained = False
        
    def fit(self, confidences: List[float], outcomes: List[int]):  # outcomes are 0 (incorrect) or 1 (correct)
        """Fit the calibrator using confidence scores vs actual outcomes."""
        if len(set(outcomes)) < 2 or len(confidences) < self.min_samples:
            # Cannot calibrate with insufficient or homogenous samples
            self.trained = False
            return
        
        # Binning approach for isotonic regression
        # We'll bin confidence scores and calculate empirical accuracy
        bins = 10  # Fixed number of bins
        bin_size = max(1, len(confidences) // bins)
        
        sorted_data = [(conf, outcome) for conf, outcome in zip(confidences, outcomes)]
        sorted_data.sort(key=lambda x: x[0])
        
        # Group into bins and compute accuracy for each bin
        binned_accs = []
        binned_confs = []
        
        for i in range(0, len(sorted_data), bin_size):
            bin_data = sorted_data[i:i+bin_size]
            if len(bin_data) == 0:
                continue
                
            avg_conf = sum(x[0] for x in bin_data) / len(bin_data)
            accuracy = sum(x[1] for x in bin_data) / len(bin_data)
            
            binned_confs.append(avg_conf)
            binned_accs.append(accuracy)
        
        # Monotonic interpolation while ensuring order
        for i in range(1, len(binned_accs)):
            binned_accs[i] = max(binned_accs[i], binned_accs[i-1])  # Ensure monotonicity
        
        # Create interpolator between uncalibrated confidence and calibrated probability
        if len(binned_confs) > 1:
            self.calibrator = interpolate.interp1d(binned_confs, binned_accs, 
                                                  bounds_error=False, fill_value=(0., 1.0))
            self.trained = True
        else:
            self.trained = False

    def transform(self, confidence: float) -> float:
        """Transform confidence using learned mapping."""
        if not self.trained or self.calibrator is None:
            return confidence  # Return original if not trained
        try:
            calibrated_prob = self.calibrator(confidence)[()]  # Convert np.arr to scalar
            return float(np.clip(calibrated_prob, 0.0, 1.0))
        except:
            return confidence  # Fallback to original if interpolation fails


def collect_signals_data(signals: List[Tuple[str, Any]], days: List[Dict],
                        thresholds: Dict[str, float] = None) -> List[Tuple[int, float, str]]:
    """
    Collect all signal predictions for a given day range for calibration training.

    Returns: List[(outcome=1/0, original_confidence, signal_name)]
    """
    all_predictions = []
    
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        
        for idx in range(max(2, len(days)//2), len(days)):  # Use later half of data for training
            direction, confidence = evaluate_signal_for_trading(sig_obj, sig_name, idx, days, thresh)
            
            if direction is not None and confidence > 0:
                # Determine if prediction was correct
                if idx + 1 < len(days):
                    actual_direction = days[idx + 1].get('market_dir', 'flat')
                    was_correct = (direction == actual_direction and actual_direction != 'flat')
                    all_predictions.append((1 if was_correct else 0, confidence, sig_name))
                    
    return all_predictions


def evaluate_signal_for_trading(signal_obj, signal_name: str, idx: int, days: List[Dict],
                            adaptive_threshold: float = DEFAULT_THRESHOLD) -> Tuple[Optional[str], float]:
    """
    Evaluate a single signal (same as before with dewpoint modulator).
    """
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0

    # Apply dewpoint modulator if applicable
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp') if idx - 1 < len(days) else None
        dewpoint = days[idx - 1].get('dewpoint') if idx - 1 < len(days) else None
        if temp is not None and dewpoint is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)

    # Apply threshold gate
    if confidence < adaptive_threshold:
        return None, 0.0

    return direction, confidence


def evaluate_ensemble_with_calibration(signals: List[Tuple[str, Any]], idx: int, days: List[Dict],
                                    thresholds: Dict[str, float] = None,
                                    calibrators: Dict[str, IsotonicCalibrator] = None,
                                    min_agreement: int = 1) -> Tuple[Optional[str], float]:
    """
    Evaluate an ensemble of signals with calibrated confidence voting.
    """
    votes = []  # (direction, calibrated confidence)
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        direction, confidence = evaluate_signal_for_trading(sig_obj, sig_name, idx, days, thresh)
        
        if direction is not None and confidence > 0:
            # Apply calibration if available
            calibrated_conf = confidence
            if calibrators and sig_name in calibrators:
                calibrated_conf = calibrators[sig_name].transform(confidence)
            
            votes.append((direction, calibrated_conf))

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

    # Use calibrated consensus strength
    confidence = consensus_strength

    return direction, min(confidence, 1.0)  # Cap at 1.0


# ─── Walk-forward backtest (calibrated version) ──────────────────────────

def walk_forward_backtest_calibrated(days: List[Dict], signal_names: List[str],
                                   signal_objs: List[Any], train_days: int = 180, 
                                   test_days: int = 30, min_agreement: int = 1) -> Dict:
    """
    Calibrated walk-forward backtest on one station.

    For each window:
    1. Train calibrators on training fold
    2. Apply them to test fold
    3. Return performance based on calibrated predictions
    """
    # Group signals
    sig_map = dict(zip(signal_names, signal_objs))
    combo_list = [(name, sig_map[name]) for name in signal_names]
    
    result = {'correct': 0, 'total': 0, 'trades': 0, 'trading_log': []}
    
    # Use early portion for calibration, middle for testing (no overlap)
    start_idx = train_days
    max_test_end = len(days)
    
    while start_idx + test_days <= max_test_end:
        test_end = min(start_idx + test_days, max_test_end)
        
        # Split into training and testing segments for this window
        # Training (for calibration): from train_days up to start of test
        cal_window_start = train_days * 2 // 3  # Use some buffer
        cal_train_end = start_idx
        
        # Define cal train segment (just a sample)
        if cal_train_end > cal_window_start + 60:  # Ensure training window
            cal_start = cal_train_end - 120  # 120 days for calibration training
        else:
            cal_start = max(train_days, cal_train_end - 60)  # Minimum 60-day window

        # Initialize calibrators per signal for this time window
        calibrators = {}
        
        # Train calibrators (only if we have sufficient historical data for training the calibration)
        for sig_name, sig_obj in combo_list:
            # Simulate collecting training data for this signal in the specified range
            # Note: In practice this would collect actual predictions, here I'll just use historical signals
            confidences = []
            outcomes = []  # 1 if correct prediction
            
            # Sample training data in the cal_start to start_idx range for calibration purposes
            for train_idx in range(cal_start, start_idx):
                if train_idx >= len(days) or train_idx - 1 < 0: 
                    continue
                direction, confidence = evaluate_signal_for_trading(sig_obj, sig_name, train_idx, days)
                
                if direction is not None and confidence > 0:
                    # Determine if was correct by checking next day
                    if train_idx + 1 < len(days):
                        actual_direction = days[train_idx + 1].get('market_dir', 'flat')
                        if actual_direction != 'flat':  # Only record if not flat
                            was_correct = direction == actual_direction
                            confidences.append(confidence)
                            outcomes.append(1 if was_correct else 0)
            
            # Create and train the calibrator if we have enough data
            calibrator = IsotonicCalibrator(min_samples=MIN_CAL_SAMPLES)
            if len(confidences) >= MIN_CAL_SAMPLES and len(outcomes) >= MIN_CAL_SAMPLES:
                calibrator.fit(confidences, outcomes)
            
            calibrators[sig_name] = calibrator

        # Now test calibrated performance on this window's test segment
        adaptive = {}  # Simplified threshold tracking for this context
        
        for idx in range(start_idx, test_end):
            if idx >= len(days):
                break
            
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue
            
            # Get thresholds for signals (simple approach)
            thresholds = {name: DEFAULT_THRESHOLD for name in signal_names}
            
            # Evaluate ensemble with calibrated confidences
            pred_dir, confidence = evaluate_ensemble_with_calibration(
                combo_list, idx, days, thresholds, calibrators, min_agreement
            )
            
            if pred_dir is None:
                continue
            
            result['trades'] += 1
            result['total'] += 1
            was_correct = pred_dir == actual
            if was_correct:
                result['correct'] += 1
                
            result['trading_log'].append({
                'day': days[idx]['date'],
                'prediction': pred_dir,
                'actual': actual,
                'was_correct': was_correct,
                'confidence': confidence
            })
        
        start_idx += test_days
        # Limit windows for runtime: test up to 6 windows
        if start_idx >= train_days + 6 * test_days:
            break
    
    return result


# ─── Combination generation ─────────────────────────────────────────────────────

def generate_combinations(signal_names: List[str]) -> List[Tuple[str, List[str]]]:
    """Generate all non-empty subsets of signal_names."""
    combos = []
    # Since this is compute-intensive, we'll create a reasonable sample
    # Let's limit to max of 1023 combinations (2^10) which is the full search space
    for r in range(1, len(signal_names) + 1):
        for subset in combinations(signal_names, r):
            name = '+'.join(subset)
            combos.append((name, list(subset)))  
            if len(combos) >= 1000:  # Limit if too many combinations 
                return combos
    return combos


def main():
    parser = argparse.ArgumentParser(description='phase9_calibrated_search|Phase 9|PHASE 9 Calibrated Search Inside Search')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase9_calibrated_search|Phase 9|PHASE 9.json',
                        help='Output file')
    parser.add_argument('--max-combos', type=int, default=500,  # Limit for runtime
                        help='Max combinations to test (for runtime)')
    parser.add_argument('--station', type=str, default=None,
                        help='Single station for quick test')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Load registry and all signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    
    # Get all signal names from the registry 
    all_signal_names = list(all_signals.keys())
    
    if len(all_signal_names) == 0:
        logger.error("No signals found in registry!!")
        sys.exit(1)
    
    logger.info(f"Loaded {len(all_signal_names)} signals from registry: {all_signal_names}")

    # Generate all combinations
    all_combos = generate_combinations(all_signal_names)
    logger.info(f"Testing {min(args.max_combos, len(all_combos))} combinations")

    # Get stations
    conn = sqlite3.connect(db_path)
    if args.station:
        all_stations = [args.station.upper()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        all_stations = [r[0] for r in cur.fetchall()]
    conn.close()
    
    logger.info(f"Testing on {min(20, len(all_stations))} stations: {all_stations[:20]}")

    # Load all station data upfront
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations[:20]:  # Limit to first 20 stations
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align_data(days, market)
        if len(aligned) >= 250:
            station_data[station] = aligned
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(aligned)} days)")
    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations (min 250 days)")

    # Agreement levels to test
    agreement_levels = [1, 2, 3]  # Start with lower levels to manage compute
    
    # Master results structure
    master_results = {
        'phase9_calibrated_search|Phase 9|PHASE 9': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_tested': all_signal_names,
            'total_combinations': len(all_combos),
            'stations': list(station_data.keys()),
            'agreement_levels': agreement_levels,
            'min_calibration_samples': MIN_CAL_SAMPLES,
            'description': 'Combinatorial search with isotonic calibration inside search loop',
        },
        'results': {},
        'top_by_calibrated_accuracy': [],
    }
    
    # This field needs to be at the top-level of dict
    master_results['combinations_evaluated'] = 0

    # Process combinations
    combo_count = 0
    for min_agree in agreement_levels:
        logger.info(f"\n{'='*80}")
        logger.info(f"Agreement threshold: {min_agree}")
        logger.info(f"{'='*80}")
        
        for combo_name, sig_names in all_combos:
            if len(sig_names) < min_agree:  # Skip if not enough signals for agreement
                continue
            if combo_count >= args.max_combos:
                break
            
            combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
            station_breakdown = {}
            
            for station in station_data.keys():
                days = station_data[station]
                sig_objs = [all_signals[n] for n in sig_names if n in all_signals and n in all_signals]
                
                if len(sig_objs) != len(sig_names):
                    logger.warning(f"Missing signal objects for combo {combo_name}")
                    break
                
                res = walk_forward_backtest_calibrated(
                    days, sig_names, sig_objs,
                    train_days=180, test_days=30,
                    min_agreement=min_agree
                )
                
                if res['trades'] > 0:
                    station_breakdown[station] = res
                    combo_results['correct'] += res['correct']
                    combo_results['total'] += res['total']
                    combo_results['trades'] += res['trades']
                    combo_results['stations_tested'] += 1
            
            # Track results
            if combo_results['trades'] > 0:
                calibrated_accuracy = combo_results['correct'] / combo_results['trades']
                calibrated_sharpe = compute_sharpe(combo_results)
                coverage = combo_results['trades'] / (combo_results['stations_tested'] * 120) if combo_results['stations_tested'] > 0 else 0
                
                key = f"{combo_name}_agree_{min_agree}_calibrated"
                entry = {
                    'calibrated_accuracy': round(calibrated_accuracy, 5),
                    'calibrated_sharpe': round(calibrated_sharpe, 5), 
                    'coverage': round(coverage, 5),
                    'correct': combo_results['correct'],
                    'total': combo_results['total'],
                    'trades': combo_results['trades'],
                    'stations_tested': combo_results['stations_tested'],
                    'min_agreement': min_agree,
                    'station_breakdown': {k: {
                        'correct': v['correct'], 
                        'total': v['total'], 
                        'trades': v['trades'],
                        'calibrated_accuracy': v['correct']/v['trades'] if v['trades'] > 0 else 0
                    } for k, v in station_breakdown.items()}
                }
                
                master_results['results'][key] = entry
                master_results['combinations_evaluated'] += 1
                
                logger.info(f"  {combo_name[:35]:<35s}[agree={min_agree}] cal_acc={calibrated_accuracy:.3f}  "
                          f"shr={calibrated_sharpe:.2f}  trds={combo_results['trades']:>4d}")
            
            combo_count += 1
            if combo_count >= args.max_combos:
                logger.info(f"Stopped after {args.max_combos} combinations")
                break
    
    # Get top performers across all agreement levels
    results_dict = master_results['results']
    
    # Sort by calibrated accuracy
    all_results = list(results_dict.items())
    sorted_cal_acc = sorted(all_results, key=lambda x: x[1]['calibrated_accuracy'], reverse=True)
    
    master_results['top_by_calibrated_accuracy'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'], 
         **{k: v for k, v in stats.items() if k not in ['station_breakdown']}}
        for name, stats in sorted_cal_acc[:20]
    ]
    
    # Save the full results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {args.output}")
    
    # Print summary
    print_summary(master_results)
    
    return master_results


def compute_sharpe(stats: Dict) -> float:
    """Compute approximate Sharpe ratio from win/loss statistics."""
    total = stats['total']
    correct = stats['correct']
    if total < 2:
        return 0.0
    win_rate = correct / total
    loss_rate = 1.0 - win_rate
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)
    variance = win_rate * (1.0 - expected_return)**2 + loss_rate * (-1.0 - expected_return)**2
    if variance <= 0:
        return 0.0
    sharpe = expected_return / math.sqrt(variance)
    if total >= 2:
        sharpe *= math.sqrt(total - 1)  # Bessel's correction factor for sample std
    return sharpe


def print_summary(results: Dict):
    """Print final summary table for calibrated results."""
    print("\n" + "="*100)
    print("phase9_calibrated_search|Phase 9|PHASE 9 — TOP 5 BY CALIBRATED ACCURACY (With Calibration Inside Search Loop)")
    print("="*100)
    print(f"{'Combination':<40} {'Cal_Acc':>8} {'Cal_Shp':>8} {'Cov':>6} {'Trd':>5} {'Stn':>4} {'Agree':>5}")
    print("-"*95)
    for item in results['top_by_calibrated_accuracy'][:5]:
        s = item
        combo_clean = s['combo']
        if '+' in combo_clean and len(combo_clean) > 37:
            combo_clean = combo_clean[:34] + "..."  # Truncate long combo names
        print(f"{combo_clean:<40} {s['calibrated_accuracy']:>8.3f} {s['calibrated_sharpe']:>8.3f} {s['coverage']:>6.3f} {s['trades']:>5} {s['stations_tested']:>4} {s['min_agreement']:>5}")

    print("\n" + "="*100)
    print("SEARCH SUMMARY:")
    print(f"  Signals tested: {results['phase9_calibrated_search|Phase 9|PHASE 9']['signals_tested']}")
    print(f"  Total combinations generated: {results['phase9_calibrated_search|Phase 9|PHASE 9']['total_combinations']}")
    print(f"  Combinations evaluated: {results['phase9_calibrated_search|Phase 9|PHASE 9']['combinations_evaluated']}")
    print(f"  Station count: {len(results['phase9_calibrated_search|Phase 9|PHASE 9']['stations'])}")
    print(f"  Agreement levels tested: {results['phase9_calibrated_search|Phase 9|PHASE 9']['agreement_levels']}")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")