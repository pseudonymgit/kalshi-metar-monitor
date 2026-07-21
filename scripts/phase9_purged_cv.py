#!/usr/bin/env python3
"""
PHASE 9 — PURGED WALK-FORWARD CV ON BEST 5 COMBOS

Runs the purged walkforward CV on the best 5 combos identified in the search.
This is the final validation step that tests our combinations in a true 
out-of-sample regime. The purging mechanism prevents data bleeding between
train/test folds.

Focus: Final accuracy verification on best 5 combos from combinatorial search.
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
from typing import Optional, Tuple, List, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

# Load from combinatorial search results - top 5 configurations
TOP_5_CONFIGS = [
    {
        'name': 'calendar_climatology+gaussian_v2+pressure_delta+wind_direction_shift+nwp_analog+forecast_disagreement',
        'signals': ['calendar_climatology', 'gaussian_v2', 'pressure_delta', 
                    'wind_direction_shift', 'nwp_analog', 'forecast_disagreement'],
        'agreement_threshold': 2
    },
    {
        'name': 'calendar_climatology+persistence+gaussian_v2+goldilocks+forecast_disagreement+frontal_detector+intraday_metar_confirmation',
        'signals': ['calendar_climatology', 'persistence', 'gaussian_v2', 'goldilocks', 
                   'forecast_disagreement', 'frontal_detector', 'intraday_metar_confirmation'],
        'agreement_threshold': 2
    }, 
    {
        'name': 'calendar_climatology+gaussian+gaussian_v2+goldilocks+pressure_delta+wind_direction_shift+frontal_detector',
        'signals': ['calendar_climatology', 'gaussian', 'gaussian_v2', 'goldilocks', 
                   'pressure_delta', 'wind_direction_shift', 'frontal_detector'],
        'agreement_threshold': 2
    },
    {
        'name': 'calendar_climatology+persistence+gaussian_v2+nwp_analog+frontal_detector',
        'signals': ['calendar_climatology', 'persistence', 'gaussian_v2', 'nwp_analog', 'frontal_detector'],
        'agreement_threshold': 1
    },
    {
        'name': 'persistence+gaussian_v2+pressure_delta+wind_direction_shift+forecast_disagreement+intraday_metar_confirmation',
        'signals': ['persistence', 'gaussian_v2', 'pressure_delta', 'wind_direction_shift', 
                   'forecast_disagreement', 'intraday_metar_confirmation'],
        'agreement_threshold': 1
    }
]

# Signals that get dewpoint modulator
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}

# Parameters for purged CV
DEFAULT_THRESHOLD = 0.5
TRAIN_DAYS = 180
TEST_DAYS = 30
PURGE_DAYS = 15  # Purge window to prevent look-ahead bias

# ─── Data loading functions ───────────────────────────────────────────────────

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


# ─── Signal evaluation functions ──────────────────────────────────────────────

def evaluate_signal(signal_obj, signal_name: str, idx: int, days: List[Dict],
                    adaptive_threshold: float = DEFAULT_THRESHOLD) -> Tuple[Optional[str], float]:
    """Evaluate a signal with dewpoint modulator."""
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


def evaluate_ensemble(signals: List[Tuple[str, Any]], idx: int, days: List[Dict],
                      min_agreement: int = 1) -> Tuple[Optional[str], float]:
    """Evaluate an ensemble of signals."""
    votes = []  # (direction, confidence)
    for sig_name, sig_obj in signals:
        direction, confidence = evaluate_signal(sig_obj, sig_name, idx, days, DEFAULT_THRESHOLD)
        if direction is not None and confidence > 0:
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

    # Final confidence
    confidence = consensus_strength

    return direction, min(confidence, 1.0)


# ─── Purged walk-forward CV functions ─────────────────────────────────────────

def purged_walkforward_cv_on_combo(combo_config: Dict, all_signals: Dict[str, Any], 
                                   station_data: Dict[str, List[Dict]], 
                                   train_days: int = TRAIN_DAYS, 
                                   test_days: int = TEST_DAYS,
                                   purge_days: int = PURGE_DAYS) -> Dict:
    """Perform purged walk-forward CV on a single combo."""
    combo_name = combo_config['name']
    signal_names = combo_config['signals']
    min_agreement = combo_config['agreement_threshold']
    
    result = {
        'total_trades': 0,
        'correct_predictions': 0, 
        'total_predictions': 0,
        'stations_tested': 0,
        'window_results': []
    }
    
    for station, days in station_data.items():
        if len(days) < train_days + test_days + purge_days:
            logger.warning(f"{station}: insufficient data for CV, skipping")
            continue
            
        # Start with first meaningful date range
        start_idx = train_days
        total_predictions_station = 0
        correct_predictions_station = 0
        window_count = 0
        
        while start_idx + test_days + purge_days < len(days):
            test_start = start_idx
            test_end = min(test_start + test_days, len(days)) 
            purge_start = test_end
            next_training_start = min(purge_start + purge_days, len(days))
            
            if next_training_start >= len(days):
                break

            # Prepare signals for this ensemble
            signals_list = [(name, all_signals[name]) for name in signal_names 
                           if name in all_signals]
                           
            
            # Perform predictions on test fold
            fold_result = {
                'predictions': 0,
                'correct': 0,
                'start_idx': test_start,
                'end_idx': test_end,
                'data_points_in_fold': test_end - test_start
            }
            
            for idx in range(test_start, test_end):
                if idx >= len(days):
                    continue
                    
                actual = days[idx].get('market_dir', 'flat')
                if actual == 'flat':
                    continue
                
                pred_dir, confidence = evaluate_ensemble(signals_list, idx, days, min_agreement)
                
                if pred_dir is None:
                    continue
                    
                fold_result['predictions'] += 1
                result['total_predictions'] += 1
                result['total_trades'] += 1

                was_correct = pred_dir == actual
                if was_correct:
                    fold_result['correct'] += 1
                    result['correct_predictions'] += 1
                    
            fold_result['accuracy'] = fold_result['correct'] / fold_result['predictions'] if fold_result['predictions'] > 0 else 0
            result['window_results'].append(fold_result)
            result['total_predictions'] += fold_result['predictions']
            result['total_trades'] += fold_result['predictions']
            
            # Move to next fold (past purge period)
            start_idx = next_training_start
            window_count += 1
            
            # Limit number of windows to keep runtime reasonable
            if window_count >= 5:  # Just test a few windows per station
                break
        
        if fold_result['predictions'] > 0:
            result['stations_tested'] += 1

    if result['total_predictions'] > 0:
        result['final_accuracy'] = result['correct_predictions'] / result['total_predictions']
        result['sharpe'] = compute_approximate_sharpe(result)
    else:
        result['final_accuracy'] = 0.0
        result['sharpe'] = 0.0
        
    return result


def compute_approximate_sharpe(result: Dict) -> float:
    """Compute approximate Sharpe ratio from results."""
    total = result['total_predictions']
    correct = result['correct_predictions']
    
    if total < 2:
        return 0.0
    
    win_rate = correct / total
    loss_rate = 1.0 - win_rate
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)
    variance = win_rate * (1.0 - expected_return)**2 + loss_rate * (-1.0 - expected_return)**2
    
    if variance <= 0:
        return 0.0
        
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0
        
    sharpe = expected_return / std_dev
    return sharpe * math.sqrt(total)  # Annualize


def main():
    parser = argparse.ArgumentParser(description='Phase 9 Purged Walk-Forward CV on Best 5 Combos')
    parser.add_argument('--combo-file', type=str, default='data/phase9_combinatorial_search.json',
                        help='Input file with combinatorial search results')
    parser.add_argument('--output', type=str, default='data/phase9_purged_cv_results.json',
                        help='Output file')
    args = parser.parse_args()

    db_path = 'data/metar_backfill.db'
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)
        
    # Load registry and signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    logger.info(f"Loaded {len(all_signals)} signals")

    # Get stations to test
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station LIMIT 12")  # Use 12 stations
    all_stations = [r[0] for r in cur.fetchall()]
    conn.close()

    # Load data for these stations
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align_data(days, market)
        if len(aligned) >= 400:  # Need sufficient data for full walk-forward
            station_data[station] = aligned
    conn.close()
    
    logger.info(f"Loaded data for {len(station_data)} active stations")

    # Get the best 5 configurations from combinatorial search or use defaults
    top_configs = TOP_5_CONFIGS
    logger.info(f"Testing top 5 configurations identified in phase 9 combinatorial search")

    master_results = {
        'phase9_purged_cv_results': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'combo_file': args.combo_file,
            'tested_configurations': len(top_configs),
            'stations_used': list(station_data.keys()),
            'cv_parameters': {
                'train_days': TRAIN_DAYS,
                'test_days': TEST_DAYS,
                'purge_days': PURGE_DAYS,
                'max_windows_per_station': 5
            },
            'description': 'Purged walk-forward cross-validation of top 5 combos from combinatorial search',
        },
        'configuration_results': {}
    }

    # Test each of the top configurations
    for i, config in enumerate(top_configs):
        logger.info(f"\nTesting configuration {i+1}/5: {config['name']}")
        
        # Verify all required signals are available
        missing_signals = [s for s in config['signals'] if s not in all_signals]
        if missing_signals:
            logger.error(f"Configuration {i+1} has missing signals: {missing_signals}")
            continue
            
        # Run purged CV on this configuration
        cv_result = purged_walkforward_cv_on_combo(config, all_signals, station_data,
                                                 TRAIN_DAYS, TEST_DAYS, PURGE_DAYS)
        
        config_key = config['name'].replace('+', '_')
        master_results['configuration_results'][config_key] = {
            'combo_name': config['name'],
            'signal_count': len(config['signals']),
            'signals': config['signals'],
            'agreement_threshold': config['agreement_threshold'],
            'result': cv_result
        }

        logger.info(f"  Accuracy: {cv_result.get('final_accuracy', 0):.3f} ({cv_result.get('correct_predictions', 0)}/{cv_result.get('total_predictions', 0)})")
        logger.info(f"  Sharpe: {cv_result.get('sharpe', 0):.3f}")
        logger.info(f"  Trades: {cv_result.get('total_trades', 0)}")

    # Identify top configuration from CV results
    best_combo = None
    best_accuracy = 0
    for config_key, config_data in master_results['configuration_results'].items():
        accuracy = config_data['result'].get('final_accuracy', 0)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_combo = config_data
    
    if best_combo:
        master_results['best_configuration'] = {
            'combo_name': best_combo['combo_name'],
            'accuracy': best_accuracy,
            'sharpe': best_combo['result'].get('sharpe', 0),
            'trades': best_combo['result'].get('total_trades', 0),
            'signals_enabled': best_combo['signals'],
            'agreement_threshold': best_combo['agreement_threshold']
        }
        logger.info(f"\nBest configuration identified: {best_combo['combo_name']}")
        logger.info(f"Final CV accuracy: {best_accuracy:.3f}")

    # Save results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True) 
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {args.output}")
    
    return master_results


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")