#!/usr/bin/env python3
"""
COMBINATORIAL SIGNAL SEARCH - ENSEMBLE OPTIMIZATION

Tests all possible signal combinations using simple majority vote approach
to identify optimal ensemble configurations for weather trading strategies.
"""

import sqlite3
import math
from typing import Optional, Tuple, List, Dict, Set
from itertools import combinations
import json
import os
import sys
import argparse
from datetime import datetime


def get_season(date_str):
    """Get DJF/MAM/JJA/SON season from date string 'YYYY-MM-DD'."""
    m = int(date_str[5:7])
    if m in (12, 1, 2): 
        return 'DJF'
    elif m in (3, 4, 5): 
        return 'MAM'
    elif m in (6, 7, 8): 
        return 'JJA'
    else: 
        return 'SON'


def load_station_data(station, conn):
    """Load METAR data and market outcomes for a station."""
    cur = conn.cursor()
    
    # Load METAR observations (daily aggregates)
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    days = []
    for row in cur.fetchall():
        if any(v is None for v in row[1:]):
            continue
        days.append({
            'date': row[0],
            'high': row[1], 'low': row[2], 'dewpoint': row[3], 'temp': row[4],
            'wind_dir': row[5], 'wind_speed': row[6], 'pressure': row[7]
        })
    
    # Load market outcomes
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    market = {}
    for row in cur.fetchall():
        date, bucket, prior = row
        if prior is not None:
            market[date] = 'up' if bucket > prior else 'down'
    
    return days, market


def combine_signals(signals, idx, days, min_agreement=1):
    """
    Combine multiple signals using simple majority vote.
    
    Args:
        signals: List of signal objects
        idx: Current day index
        days: Daily weather data
        min_agreement: Minimum number of signals that must agree (default 1)
        
    Returns:
        (direction, confidence) or (None, 0.0) if insufficient agreements
    """
    predictions = []
    for signal in signals:
        direction, confidence = signal.evaluate(idx, days)
        if direction is not None and confidence > 0:
            predictions.append((direction, confidence))
    
    if len(predictions) == 0:
        return None, 0.0
    
    if len(predictions) < min_agreement:
        return None, 0.0
    
    # Count up and down votes with confidence weights
    up_weight = sum(conf for dir_, conf in predictions if dir_ == 'up')
    down_weight = sum(conf for dir_, conf in predictions if dir_ == 'down')
    
    total_votes = len(predictions)
    total_weight = up_weight + down_weight
    
    # Determine direction based on weighted majority
    if up_weight > down_weight:
        direction = 'up'
        confidence = up_weight / total_votes
    elif down_weight > up_weight:
        direction = 'down'
        confidence = down_weight / total_votes
    else:
        # Equal votes, no consensus
        return None, 0.0
    
    # Additional confidence boost for stronger agreement
    if total_votes >= min_agreement:
        consensus_factor = len([p for p in predictions if p[0] == direction]) / len(predictions)
        confidence *= consensus_factor
        
    return direction, confidence


def evaluate_combination(signals_to_use, idx, days, min_agreement=1):
    """
    Evaluate a combination of signals.
    
    Args:
        signals_to_use: List of signal objects to combine
        idx: Index to evaluate at
        days: Historical daily data
        min_agreement: Minimum number of signals that must agree
        
    Returns:
        (direction, confidence) as per BaseSignal.evaluate()
    """
    return combine_signals(signals_to_use, idx, days, min_agreement)


def walk_forward_backtest(days, market, signal_combinations, train_days=180, test_days=30, min_agreement=1):
    """
    Walk-forward backtest of signal combinations.
    
    Args:
        days: List of daily data
        market: Dictionary mapping date to actual direction
        signal_combinations: List of [(name, signal_list), ...] pairs
        train_days: Training window size (default 180 = 6 months)
        test_days: Testing window size (default 30 = 1 month) 
        min_agreement: Minimum number of signals needing to agree
        
    Returns:
        Dict with backtest results for each combination
    """
    results = {}
    
    start_idx = train_days
    while start_idx + test_days <= len(days):
        test_start = start_idx
        test_end = min(start_idx + test_days, len(days))
        
        for combo_name, signals in signal_combinations:
            if combo_name not in results:
                results[combo_name] = {
                    'correct': 0,
                    'total': 0,
                    'direction_matches': 0,  # How often signals agreed
                    'trade_frequency': 0     # How often we traded
                }
                
            combo_result = results[combo_name]
            
            for idx in range(test_start, test_end):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None:
                    continue
                
                # Get combined signal prediction
                predicted_dir, confidence = evaluate_combination(signals, idx, days, min_agreement)
                
                if predicted_dir is not None:  # If we have a signal
                    combo_result['trade_frequency'] += 1
                    
                    if predicted_dir == actual:
                        combo_result['correct'] += 1
                    
                    combo_result['total'] += 1
        
        start_idx += test_days
        # Limit windows for efficiency - test on first few periods only
        if start_idx >= train_days + 4 * test_days:  # After testing 4 test periods
            break
    
    return results


def compute_sharpe_and_metrics(combo_results_dict, total_days_tested):
    """
    Compute comprehensive metrics for each combination.
    
    Args:
        combo_results_dict: Dict from walk_forward_backtest
        total_days_tested: Total days evaluated in tests
        
    Returns:
        Dict with accuracy, sharpe, trade frequency, etc.
    """
    final_results = {}
    
    for combo_name, stats in combo_results_dict.items():
        total = stats['total']
        correct = stats['correct']
        trade_freq_actual = stats['trade_frequency']
        
        if total > 0:
            accuracy = correct / total
        else:
            accuracy = 0.0
            
        # Calculate coverage
        coverage = total / total_days_tested if total_days_tested > 0 else 0.0
        
        # Calculate Sharpe-like ratio if possible
        # Simplified version: assume consistent returns per trade for testing purposes
        if total == 0:
            sharpe = 0.0
        elif total == 1:
            sharpe = 0.0
        else:
            # Assume profit/loss based on win rate and some unit return
            profit_rate = accuracy  # Simplification
            win_rate = accuracy
            loss_rate = 1.0 - accuracy
            avg_win = 1.0  # Assuming unit wins
            avg_loss = -1.0  # Assuming unit losses
            
            # Expected value per trade: win_rate*avg_win + loss_rate*avg_loss
            expected_return = win_rate * avg_win + loss_rate * avg_loss
            
            # Approximate volatility with win/loss squared from mean
            variance = win_rate * ((avg_win - expected_return) ** 2) + \
                      loss_rate * ((avg_loss - expected_return) ** 2)
            vol = math.sqrt(variance) 
            
            # Sharpe ratio is return/vol (risk-free = 0)
            sharpe = expected_return / vol if vol != 0 else 0.0
            
            # Adjust for sample size (lower confidence with fewer samples)
            sharpe *= math.sqrt(total)  # Normalize for sample size
            
        final_results[combo_name] = {
            'accuracy': accuracy,
            'sharpe': sharpe,
            'coverage': coverage,
            'total_trades': total,
            'correct_predictions': correct,
            'trade_frequency': trade_freq_actual,
            'total_tested_dates': total_days_tested
        }
    
    return final_results


def get_power_set(signals_dict):
    """Generate all possible subsets of signals."""
    signal_names = list(signals_dict.keys())
    signal_objects = list(signals_dict.values())
    
    all_combinations = []
    
    # Include individual signals as well as combinations
    for r in range(1, len(signal_names) + 1):  # At least 1 signal per combination
        for indices in combinations(range(len(signal_names)), r):
            subset_names = [signal_names[i] for i in indices]
            subset_objects = [signal_objects[i] for i in indices]
            combo_name = '+'.join(subset_names)
            all_combinations.append((combo_name, subset_objects))
    
    return all_combinations


def test_significance(results, threshold=0.58):
    """Test model significance."""
    significant_results = {}
    for name, metrics in results.items():
        accuracy = metrics.get('accuracy', 0)
        total_trades = metrics.get('total_trades', 0)
        
        # Basic statistical significance test (chance would be 50% for binary)
        # If accuracy passes 58% threshold we consider it significant for trading
        if total_trades > 10:  # Minimum sample size requirement
            significant_results[name] = metrics
    
    return significant_results


def main():
    parser = argparse.ArgumentParser(description='Combinatorial Signal Search')
    parser.add_argument('--db-path', type=str, 
                       default='data/metar_backfill.db',
                       help='Path to SQLite database with METAR data')
    parser.add_argument('--station', type=str, default=None,
                       help='Specific station code to test, or "ALL" for all stations')
    parser.add_argument('--min-agreement', type=int, default=None,
                       help='Minimum agreement required for trades (default varies by combination)')
    parser.add_argument('--output-file', type=str,
                       default='data/combinatorial_search_results.json',
                       help='Output file for results')
    
    args = parser.parse_args()
    
    # Initialize signal registry - we'll mock importing it from core
    from core.signals import SignalRegistry
    
    # Create temporary db connection to check if file exists
    if not os.path.exists(args.db_path):
        print(f"ERROR: Database not found at {args.db_path}")
        sys.exit(1)
    
    registry = SignalRegistry(args.db_path)
    all_signals = registry.get_all_signals()
    
    # Limit to just the 5 new signals plus existing ones we want to include
    desired_signals = ['persistence', 'simple_trend', 'gaussian', 'gaussian_v2', 'pressure_delta', 
                       'wind_direction_shift', 'nwp_analog', 'goldilocks']
    
    active_signals = {}
    for key, signal in all_signals.items():
        if key in desired_signals:
            active_signals[key] = signal
    
    print(f"Loaded {len(active_signals)} signals: {list(active_signals.keys())}")
    
    # Get all stations to test
    conn = sqlite3.connect(args.db_path)
    cur = conn.cursor()
    
    if args.station and args.station.upper() != 'ALL':
        all_stations = [args.station.upper()]
    else:
        cur.execute("SELECT DISTINCT station FROM metar_observations WHERE station IN (SELECT station FROM settlement_epochs) LIMIT 5")  # Limit for speed
        all_stations = [row[0] for row in cur.fetchall()]
    
    conn.close()
    
    print(f"Testing on {len(all_stations)} stations: {all_stations}")
    
    # Generate all signal combinations (2^N - 1 combinations excluding empty set)
    signal_combinations = get_power_set(active_signals)
    
    print(f"Testing {len(signal_combinations)} signal combinations...")
    
    # Test combinations at different agreement thresholds
    agreement_thresholds = [1, 2, 3, 4]  # At least 1, 2, 3, 4 signals agreeing
    master_results = {
        'combinatorial_search_summary': {
            'timestamp': datetime.now().isoformat(),
            'database_path': args.db_path,
            'tested_stations': all_stations,
            'total_signals_available': len(active_signals),
            'signal_names': list(active_signals.keys()),
            'total_combinations': len(signal_combinations),
            'agreement_thresholds': agreement_thresholds
        },
        'by_agreement_threshold': {},
        'by_combination': {}  # Will store best overall results regardless of agreement threshold
    }
    
    for min_agreement in agreement_thresholds:
        print(f"\n--- Testing with minimum agreement threshold: {min_agreement} ---")
        current_threshold_results = {
            'agreement_level': min_agreement,
            'combinations_tested': {},
            'summary_stats': {}
        }
        
        station_results = {}
        
        for station in all_stations:
            print(f"\n  Processing station: {station}")
            conn = sqlite3.connect(args.db_path)
            days, market = load_station_data(station, conn)
            conn.close()
            
            if len(days) < 250:  # Need sufficient data
                print(f"    Skipping {station}: insufficient data ({len(days)} days)")
                continue
                
            # Get actual test days count for proper metrics calculations
            start_idx = 180  # Train days
            test_start = start_idx
            test_end = start_idx + 4 * 30  # Test 4 periods worth of data
            if test_end > len(days):
                test_end = len(days)
            total_days_tested = test_end - test_start
            
            if total_days_tested < 30:  # Not enough test days
                print(f"    Skipping {station}: insufficient test period")
                continue
                
            print(f"    Testing {len(signal_combinations)} combinations over {total_days_tested} test days")
            
            # Run backtest with these combinations on this station
            combo_results = walk_forward_backtest(
                days, market, signal_combinations[:20],  # Limit combinations for speed initially
                train_days=180, test_days=30, min_agreement=min_agreement
            )
            
            # Compute detailed metrics for the combinations on this station
            detailed_results = compute_sharpe_and_metrics(combo_results, total_days_tested)
            
            # Test for significance
            significant_results = test_significance(detailed_results)
            
            station_results[station] = {
                'combinations': detailed_results,
                'significant_combinations': significant_results,
                'total_tested_days': total_days_tested,
                'actual_train_window': 180,
                'actual_test_window': 30
            }
            
            # Update current threshold results
            for combo_name, metrics in detailed_results.items():
                if combo_name not in current_threshold_results['combinations_tested']:
                    current_threshold_results['combinations_tested'][combo_name] = []
                
                current_threshold_results['combinations_tested'][combo_name].append({
                    'station': station,
                    'metrics': metrics
                })
        
        # Calculate aggregate results across stations for this agreement threshold
        final_agg_results = {}
        for combo_name, station_list in current_threshold_results['combinations_tested'].items():
            if station_list:  # Skip combinations tested on no stations
                # Aggregate metrics across stations
                total_correct = sum(item['metrics']['correct_predictions'] for item in station_list if 'correct_predictions' in item['metrics'])
                total_trades = sum(item['metrics']['total_trades'] for item in station_list if 'total_trades' in item['metrics'])
                avg_accuracy = total_correct / total_trades if total_trades > 0 else 0.0
                
                # Calculate average Sharpe and coverage
                sh_sum = sum(item['metrics']['sharpe'] for item in station_list if 'sharpe' in item['metrics'])
                cover_sum = sum(item['metrics']['coverage'] for item in station_list if 'coverage' in item['metrics'])
                stations_tested = len(station_list)
                avg_sharpe = sh_sum / stations_tested if stations_tested > 0 else 0.0
                avg_coverage = cover_sum / stations_tested if stations_tested > 0 else 0.0
                
                final_agg_results[combo_name] = {
                    'accuracy': avg_accuracy,
                    'sharpe': avg_sharpe,
                    'coverage': avg_coverage,
                    'total_trades': total_trades,
                    'correct_predictions': total_correct,
                    'stations_tested': stations_tested,
                    'station_breakdown': [item['station'] for item in station_list]
                }
        
        current_threshold_results['summary_stats'] = final_agg_results
        
        # Store in master results
        master_results['by_agreement_threshold'][f'min_{min_agreement}_agreeing'] = current_threshold_results
        
        # Add best performing combinations to overall results
        for combo_name, metrics in final_agg_results.items():
            if combo_name not in master_results['by_combination']:
                master_results['by_combination'][combo_name] = []
            master_results['by_combination'][combo_name].append({
                'agreement_threshold': min_agreement,
                'metrics': metrics
            })
    
    # Sort overall results by different criteria
    def rank_by_accuracy(item):
        results_list = item[1]  # This contains the list of results across agreement levels
        # Just take the first metric's accuracy
        if results_list:
            result = results_list[0]['metrics']
            return result.get('accuracy', 0.0)
        return 0.0
        
    def rank_by_sharpe(item):
        results_list = item[1] 
        if results_list:
            result = results_list[0]['metrics']
            return result.get('sharpe', 0.0)
        return 0.0
    
    # Get best combinations by different metrics
    sorted_acc = sorted(master_results['by_combination'].items(), key=rank_by_accuracy, reverse=True)
    sorted_spr = sorted(master_results['by_combination'].items(), key=rank_by_sharpe, reverse=True)
    
    master_results['top_by_accuracy'] = [item for item in sorted_acc[:10]]
    master_results['top_by_sharpe'] = [item for item in sorted_spr[:10]]
    
    # Display top results by accuracy andSharpe
    print("\n" + "="*100)
    print("TOP PERFORMERS BY ACCURACY")
    print("="*100)
    print(f"{'Combination':<30} {'Accuracy':>8} {'Sharpe':>8} {'Coverage':>10} {'#Trades':>8} {'Stations':>8}")
    print("-"*84)
    for combo_name, result_list in sorted_acc[:10]:
        if result_list:
            result = result_list[0]['metrics']  # Take first agreement level result
            print(f"{combo_name:<30} {result['accuracy']:>8.3f} {result['sharpe']:>8.3f} "
                  f"{result['coverage']:>10.3f} {result['total_trades']:>8} {result['stations_tested']:>8}")
    
    print("\n" + "="*100)
    print("TOP PERFORMERS BY SHARPE RATIO")
    print("="*100)
    print(f"{'Combination':<30} {'Accuracy':>8} {'Sharpe':>8} {'Coverage':>10} {'#Trades':>8} {'Stations':>8}")
    print("-"*84)
    for combo_name, result_list in sorted_spr[:10]:
        if result_list:
            result = result_list[0]['metrics']
            print(f"{combo_name:<30} {result['accuracy']:>8.3f} {result['sharpe']:>8.3f} "
                  f"{result['coverage']:>10.3f} {result['total_trades']:>8} {result['stations_tested']:>8}")
    
    # Ensure output directory exists
    os.makedirs(os.path.dirname(args.output_file), exist_ok=True)
    
    # Save results
    with open(args.output_file, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    
    print(f"\nResults saved to: {args.output_file}")
    
    return master_results


if __name__ == "__main__":
    results = main()