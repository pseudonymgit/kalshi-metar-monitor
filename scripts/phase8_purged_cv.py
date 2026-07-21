#!/usr/bin/env python3
"""
PHASE 8 — TASK 8.4: Purged Walk-Forward Cross Validation

Create purged walk-forward cross-validation with a 30-day buffer between train and test sets.
This addresses the issue of look-ahead bias which can cause overly optimistic estimates.

Structure:
- 5-fold cross-validation across the 5.5-year METAR dataset
- 30-day purge buffer between train/test sets to eliminate temporal leakage
- Report accuracy variance across folds and identify best/worst performing stations
- No overlapping data between folds

Validation ensures that there's no data leakage from future periods in training.
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

# Using top performing signals from previous analysis (Phase 8.1/8.2 results)
TOP_SIGNALS = [
    'calendar_climatology',
    'gaussian',
    'gaussian_v2',
    'pressure_delta',
    'wind_direction_shift',
    'forecast_disagreement',
    'goldilocks',
    'nwp_analog', 
    'persistence',
    'temperature_advection'
]

# Default ensemble parameters
ENSEMBLE_AGREEMENT_THRESHOLD = 1
ENSEMBLE_CONFIDENCE_THRESHOLD = 0.6
USE_DEWPOINT_MODULATION = True

# Purging parameters
PURGE_BUFFER_DAYS = 30  # Number of days between train and test sets

# Cross validation parameters
CV_FOLDS = 5

# Signals that get dewpoint modulator
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}


def load_station_days(station: str, conn: sqlite3.Connection) -> List[Dict]:
    """Load daily METAR data for one station ordered by date."""
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


def load_market_data(station: str, conn: sqlite3.Connection, market_type: str = 'HIGH') -> Dict[str, str]:
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


def align_days_with_market(days: List[Dict], market: Dict[str, str]) -> List[Dict]:
    """Merge daily data with market direction."""
    aligned = []
    for d in days:
        date_key = d['date'][:10]
        if date_key in market:
            entry = dict(d)
            entry['market_dir'] = market[date_key]
            aligned.append(entry)
    return aligned


def evaluate_signals_ensemble(entry_idx: int, days: List[Dict], 
                             signal_objects: List, 
                             signal_names: List[str], 
                             agreement_threshold: int = 1,
                             confidence_threshold: float = 0.5):
    """
    Evaluate an ensemble of signals on a single day.
    """
    votes = []  
    
    for i, sig_name in enumerate(signal_names):
        sig_obj = signal_objects[i]

        # Get signal prediction 
        try:
            direction, confidence = sig_obj.evaluate(entry_idx, days)
        except:
            continue
            
        if direction is None or confidence <= 0:
            continue

        # Apply dewpoint modulator if applicable
        if USE_DEWPOINT_MODULATION and sig_name in DEWPOINT_MODULATED:
            # Check prior day for dewpoint 
            if entry_idx > 0 and 'dewpoint' in days[entry_idx - 1]:
                temp = days[entry_idx - 1].get('temp')
                dewpoint = days[entry_idx - 1].get('dewpoint')
                if temp is not None and dewpoint is not None:
                    confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)

        # Only count if above threshold
        if confidence >= confidence_threshold:
            votes.append((direction, confidence))

    # Check agreement threshold
    if len(votes) < agreement_threshold:
        return None, 0.0

    # Weighted majority vote
    up_weight = sum(conf for d, conf in votes if d == 'up')
    down_weight = sum(conf for d, conf in votes if d == 'down')

    if up_weight > down_weight:
        direction = 'up'
    elif down_weight > up_weight:
        direction = 'down'
    else:
        return None, 0.0  # Tie

    total_weight = up_weight + down_weight
    ensemble_confidence = max(0.0, min(1.0, abs(up_weight - down_weight) / total_weight))

    return direction, ensemble_confidence


def perform_fold_evaluation(train_ids: List[int], test_ids: List[int], 
                          days_aligned: List[Dict],
                          signals_obj: List,
                          signal_names: List[str],
                          conn: sqlite3.Connection) -> Dict:
    """
    Perform evaluation for one fold using specific train/test indices after purification.
    
    Args:
        train_ids: sorted list of indices for training
        test_ids: sorted list of indices for testing  
        days_aligned: List of dictionaries with date, temp, market_dir, etc.
        signals_obj: List of signal objects 
        signal_names: Names of the available signals
        conn: SQLite connection for potential market loading if needed
        
    Returns:
        Dictionary with fold performance results
    """
    total_correct = 0
    total_trades = 0
    
    for test_idx in test_ids:
        actual_direction = days_aligned[test_idx].get('market_dir', 'flat')
        if actual_direction == 'flat':
            continue
        
        prediction, confidence = evaluate_signals_ensemble(
            test_idx, days_aligned, signals_obj, signal_names,
            ENSEMBLE_AGREEMENT_THRESHOLD, ENSEMBLE_CONFIDENCE_THRESHOLD
        )
        
        if prediction is None or confidence <= 0:
            continue
            
        total_trades += 1
        if prediction == actual_direction:
            total_correct += 1
    
    accuracy = total_correct / total_trades if total_trades > 0 else 0.0
    result = {
        'total_trades': total_trades,
        'correct_predictions': total_correct,
        'accuracy': accuracy,
        'sharpe': calculate_sharpe(total_correct, total_trades)
    }
    
    logger.debug(f"Fold evaluation - Trades: {total_trades}, Correct: {total_correct}, Accuracy: {accuracy:.4f}")
    
    return result


def calculate_sharpe(correct: int, total: int) -> float:
    """
    Calculate approximate Sharpe ratio.
    """
    if total < 2:
        return 0.0
    
    win_rate = correct / total
    loss_rate = 1.0 - win_rate
    
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)
    variance = win_rate * (1.0 - expected_return)**2 + loss_rate * (-1.0 - expected_return)**2
    
    if variance <= 0:
        return 0.0
    
    sharpe = expected_return / math.sqrt(variance)
    # Annualized approximation: scale by sqrt of number of periods
    return sharpe * math.sqrt(total) if total >= 2 else sharpe


class PurgedCrossValidator:
    """Handles purged walk-forward cross validation."""
    
    def __init__(self, cv_folds: int = 5, purge_buffer_days: int = 30):
        self.cv_folds = cv_folds
        self.purge_buffer_days = purge_buffer_days
        
    def get_date_idx_map(self, days: List[Dict]) -> Dict[str, int]:
        """Map date strings to their index in the days array."""
        date_to_idx = {}
        for i, day in enumerate(days):
            date_str = day['date'][:10]  # Just the yyyy-mm-dd part
            date_to_idx[date_str] = i
        return date_to_idx
        
    def parse_date(self, date_str: str) -> datetime:
        """Parse date string to datetime object."""
        return datetime.strptime(date_str, '%Y-%m-%d')
        
    def create_purged_folds(self, days: List[Dict]) -> List[Dict[str, List[int]]]:
        """
        Create chronologically ordered, purged train/test splits.
        
        Ensures:
        - Chronological order is maintained
        - N days (purge_buffer) between train and test sets
        - Even distribution of data per fold
        """
        n_total = len(days)
        fold_size = n_total // self.cv_folds
        
        folds = []
        for fold_idx in range(self.cv_folds):
            start_idx = fold_idx * fold_size
            end_idx = (fold_idx + 1) * fold_size if fold_idx < self.cv_folds - 1 else n_total
            
            # Calculate buffer indices to ensure no temporal overlap
            # Train: everything up to the test block minus buffer
            # Buffer: purge_buffer days
            # Test: the fold block
            
            if fold_idx > 0:  # For folds after the first
                train_end = max(0, start_idx - self.purge_buffer_days)
            else:
                train_end = 0  # Include all data up to test fold
            
            train_indices = list(range(0, train_end))
            test_indices = list(range(start_idx, end_idx)) 
            
            fold = {
                'train_indices': train_indices,
                'test_indices': test_indices,
                'train_pct': len(train_indices) / len(days) if days else 0,
                'test_pct': len(test_indices) / len(days) if days else 0,
            }
            
            folds.append(fold)
            logger.debug(f"Fold {fold_idx+1}: Train {len(train_indices)}/{n_total} ({fold['train_pct']:.2f}), "
                        f"Test {len(test_indices)}/{n_total} ({fold['test_pct']:.2f})")
                        
        return folds

    def validate_station(self, station: str, signals: Dict[str, Any]) -> Dict:
        """
        Perform purged CV on a single station.
        """
        conn = sqlite3.connect('data/metar_backfill.db')
        
        # Load data
        raw_days = load_station_days(station, conn)
        if len(raw_days) < 300:  # Need substantial data for meaningful folds
            logger.warning(f"Station {station} has insufficient data ({len(raw_days)} days)")
            conn.close()
            return {
                'station': station,
                'error': f'Insufficient data ({len(raw_days)} days)',
                'success': False,
                'folds': [],
                'avg_metrics': {'accuracy': 0.0, 'sharpe': 0.0, 'total_trades': 0}
            }
        
        market_data = load_market_data(station, conn)
        aligned_days = align_days_with_market(raw_days, market_data) 
        
        if len(aligned_days) < 250:
            logger.warning(f"Station {station} has insufficient aligned data ({len(aligned_days)} days)")
            conn.close()
            return {
                'station': station,
                'error': f'Insufficient aligned data ({len(aligned_days)} days)',
                'success': False,
                'folds': [],
                'avg_metrics': {'accuracy': 0.0, 'sharpe': 0.0, 'total_trades': 0}
            }
        
        # Get signal objects
        signal_names = [name for name in TOP_SIGNALS if name in signals]
        signal_objects = [signals[name] for name in signal_names]
        
        # Create folds
        folds = self.create_purged_folds(aligned_days)
        
        # Evaluate each fold
        fold_results = []
        total_accuracies = []
        total_sharpes = []
        total_trades = 0
        
        for fold_idx, fold in enumerate(folds):
            fold_result = perform_fold_evaluation(
                fold['train_indices'],
                fold['test_indices'],
                aligned_days,
                signal_objects,
                signal_names,
                conn
            )
            
            fold_result['fold'] = fold_idx + 1
            fold_result['fold_info'] = fold
            fold_results.append(fold_result)
            
            # Collect metrics
            total_accuracies.append(fold_result['accuracy'])
            total_sharpes.append(fold_result['sharpe'])
            total_trades += fold_result['total_trades']
        
        # Calculate aggregate metrics
        avg_accuracy = sum(total_accuracies) / len(total_accuracies) if total_accuracies else 0.0
        avg_sharpe = sum(total_sharpes) / len(total_sharpes) if total_sharpes else 0.0
        accuracy_variance = sum((acc - avg_accuracy)**2 for acc in total_accuracies) / len(total_accuracies) if len(total_accuracies) > 1 else 0.0
        accuracy_std = accuracy_variance ** 0.5
        
        conn.close()
        
        return {
            'station': station,
            'success': True,
            'num_aligned_days': len(aligned_days),
            'folds': fold_results,
            'avg_metrics': {
                'accuracy': round(avg_accuracy, 6),
                'std_dev': round(accuracy_std, 6), 
                'sharpe': round(avg_sharpe, 6), 
                'total_trades': total_trades,
                'accuracy_variance': round(accuracy_variance, 6)
            },
            'fold_variability': {
                'min_accuracy': min(total_accuracies) if total_accuracies else 0.0,
                'max_accuracy': max(total_accuracies) if total_accuracies else 0.0,
                'range_accuracy': (max(total_accuracies) - min(total_accuracies)) if total_accuracies else 0.0
            }
        }


def main():
    parser = argparse.ArgumentParser(description='Phase 8 Purged Walk-Forward Cross Validation')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase8_purged_cv_results.json',
                        help='Output file')
    parser.add_argument('--stations', nargs='+', default=None,
                        help='List of stations to test (default: all available)')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Get all stations that have settlements first
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    all_stations = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    # Filter if specific stations requested
    stations_to_test = args.stations or all_stations
    logger.info(f"Testing {len(stations_to_test)} stations: {stations_to_test[:10]}{'...' if len(stations_to_test) > 10 else ''}")

    # Initialize validator
    validator = PurgedCrossValidator(cv_folds=CV_FOLDS, purge_buffer_days=PURGE_BUFFER_DAYS)
    
    # Load signal registry
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    
    logger.info(f"Starting purged cross-validation on {len(stations_to_test)} stations with {len(all_signals)} signals")
    
    # Validate each station
    all_results = {}
    successful_results = []
    
    for i, station in enumerate(stations_to_test):
        logger.info(f"[{i+1}/{len(stations_to_test)}] Processing station {station}")
        
        station_result = validator.validate_station(station, all_signals)
        all_results[station] = station_result
        
        if station_result['success']:
            successful_results.append(station_result)
    
    # Calculate overall statistics
    if successful_results:
        all_accuracies = [r['avg_metrics']['accuracy'] for r in successful_results]
        all_sharpes = [r['avg_metrics']['sharpe'] for r in successful_results] 
        all_trades = [r['avg_metrics']['total_trades'] for r in successful_results]
        
        overall_stats = {
            'mean_accuracy': sum(all_accuracies) / len(all_accuracies) if all_accuracies else 0.0,
            'mean_sharpe': sum(all_sharpes) / len(all_sharpes) if all_sharpes else 0.0,
            'total_trades': sum(all_trades),
            'stdev_accuracy': math.sqrt(sum((acc - sum(all_accuracies)/len(all_accuracies))**2 for acc in all_accuracies) /
                                      len(all_accuracies)) if all_accuracies and len(all_accuracies) > 1 else 0.0
        }
        
        # Find best and worst performing stations
        if all_accuracies:
            best_idx = all_accuracies.index(max(all_accuracies))
            worst_idx = all_accuracies.index(min(all_accuracies))
            
            overall_stats['best_performing'] = successful_results[best_idx]['station']
            overall_stats['worst_performing'] = successful_results[worst_idx]['station']
            overall_stats['best_accuracy'] = all_accuracies[best_idx]
            overall_stats['worst_accuracy'] = all_accuracies[worst_idx]
    else:
        overall_stats = {
            'mean_accuracy': 0.0,
            'mean_sharpe': 0.0,
            'total_trades': 0,
            'stdev_accuracy': 0.0,
            'best_performing': None,
            'worst_performing': None
        }
    
    logger.info(f"Cross-validation complete. Overall mean accuracy: {overall_stats['mean_accuracy']:.4f}")
    
    # Compile master results
    master_results = {
        'phase8_purged_cross_validation': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'cv_folds': CV_FOLDS,
            'purge_buffer_days': PURGE_BUFFER_DAYS, 
            'tested_signals': TOP_SIGNALS,
            'stations_tested': len(all_results),
            'successful_validations': len(successful_results),
            'ensemble_params': {
                'agreement_threshold': ENSEMBLE_AGREEMENT_THRESHOLD,
                'confidence_threshold': ENSEMBLE_CONFIDENCE_THRESHOLD,
                'use_dewpoint_modulation': USE_DEWPOINT_MODULATION,
            },
            'description': f'Purged walk-forward cross validation with {PURGE_BUFFER_DAYS}-day buffer between train/test sets'
        },
        'station_results': all_results,
        'overall_statistics': overall_stats,
        'summary_metrics': {
            'total_validations_attempted': len(stations_to_test),
            'successful_validations': len(successful_results),
            'failure_count': len(stations_to_test) - len(successful_results),
        }
    }

    # Save results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"Results saved to {args.output}")
    
    # Print summary
    print_summary(master_results)
    

def print_summary(results: Dict):
    """Print final summary of purged cross-validation."""
    
    if not results['station_results']:
        print("No results to display")
        return
        
    overall = results['overall_statistics']
    
    print("\n" + "="*100)
    print("PHASE 8.4 — PURGED WALK-FORWARD CROSS VALIDATION SUMMARY")
    print("="*100)
    
    print(f"Methodology: {CV_FOLDS}-fold time-series CV with {PURGE_BUFFER_DAYS}-day purge buffer")
    print(f"Signals used: {len(TOP_SIGNALS)} primary signals")
    print(f"Stations evaluated: {results['phase8_purged_cross_validation']['successful_validations']}/{len(results['station_results'])}")
    print()
    
    print("AGGREGATE PERFORMANCE:")
    print(f"  Mean Accuracy Across Stations: {overall['mean_accuracy']:.4f}")
    print(f"  Std Dev Accuracy: {overall['stdev_accuracy']:.4f}")  
    print(f"  Mean Sharpe: {overall['mean_sharpe']:.4f}")  
    print(f"  Total Trades Evaluated: {overall['total_trades']:,}") 
    print()
    
    if overall['best_performing']:
        print(f"BEST PERFORMING STATION: {overall['best_performing']} (Accuracy: {overall['best_accuracy']:.4f})")
        print(f"WORST PERFORMING STATION: {overall['worst_performing']} (Accuracy: {overall['worst_accuracy']:.4f})")
    
    # Identify top 5 and worst 5 stations
    successful_stations = {k: v for k, v in results['station_results'].items() 
                          if v.get('success') and 'avg_metrics' in v}
    
    if successful_stations:
        sorted_by_accuracy = sorted(successful_stations.items(), 
                                   key=lambda x: x[1]['avg_metrics']['accuracy'], 
                                   reverse=True)
                                   
        print("\nTOP 5 STATIONS:")
        for i, (station, res) in enumerate(sorted_by_accuracy[:5]):
            accuracy = res['avg_metrics']['accuracy']
            total_trades = res['avg_metrics']['total_trades']
            print(f"  {i+1:2d}. {station}: {accuracy:.4f} ({total_trades} trades)")
        
        print("\n5 WORST STATIONS:")
        for i, (station, res) in enumerate(reversed(sorted_by_accuracy[-5:])):
            accuracy = res['avg_metrics']['accuracy']
            total_trades = res['avg_metrics']['total_trades']
            print(f"  {len(sorted_by_accuracy)-4+i:2d}. {station}: {accuracy:.4f} ({total_trades} trades)")


if __name__ == '__main__':
    start = time.time()
    main()
    elapsed = time.time() - start
    logger.info(f"Purged CV execution complete. Total runtime: {elapsed:.1f}s ({elapsed/60:.2f}min)")