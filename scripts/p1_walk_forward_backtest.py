#!/usr/bin/env python3
"""
PHASE 1: GROUND TRUTH WALK-FORWARD BACKTEST ENGINE

This implements strict walk-forward validation:
- 6-month training window
- 1-month testing window
- Roll forward incrementally
- All metrics computed on out-of-sample data only

This is the NEW baseline. Everything else is measured against this.
"""

import sqlite3
import os
import sys
import json
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
import statistics

# Add core to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Gray Room thresholds
DIRECTIONAL_THRESHOLD = 0.58  # 58%
SHARPE_THRESHOLD = 0.8
COVERAGE_THRESHOLD = 0.30

# Walk-forward parameters
TRAIN_WINDOW_DAYS = 180   # 6 months
TEST_WINDOW_DAYS = 30     # 1 month
OVERLAP_DAYS = 30         # 1 month overlap


def get_all_stations(conn: sqlite3.Connection) -> List[str]:
    """Get all stations from the database."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM metar_observations")
    return sorted([r[0] for r in cur.fetchall()])


def get_station_data(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Get weather and market data for a station."""
    # Get daily weather
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            date_utc,
            MAX(temp_f) as high,
            MIN(temp_f) as low,
            AVG(pressure_mb) as pressure,
            AVG(dewpoint_f) as dewpoint,
            AVG(wind_direction_deg) as wind_dir
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    weather = {}
    for row in cur.fetchall():
        weather[row[0]] = {
            'high': row[1],
            'low': row[2],
            'pressure': row[3],
            'dewpoint': row[4],
            'wind_dir': row[5],
        }
    
    # Get market directions
    cur.execute("""
        SELECT local_trading_date, market_type, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    markets = defaultdict(dict)
    for row in cur.fetchall():
        date, mt, bucket, prior = row
        if prior is not None:
            direction = 'up' if bucket > prior else ('down' if bucket < prior else 'flat')
        else:
            direction = 'flat'
        markets[date][mt] = direction
    
    return {
        'weather': weather,
        'markets': dict(markets),
    }


def compute_signals(temps: List[Dict], markets: Dict) -> Dict[str, float]:
    """
    Compute all signals for a given epoch.
    
    Returns dict of signal_name -> signal_value
    """
    signals = {}
    
    if len(temps) < 2:
        return signals
    
    today = temps[-1]
    yesterday = temps[-2]
    
    # Signal 1: Simple trend
    trend = 'up' if today['high'] > yesterday['high'] else ('down' if today['high'] < yesterday['high'] else 'flat')
    signals['trend'] = 1.0 if trend == 'up' else (-1.0 if trend == 'down' else 0.0)
    
    # Signal 2: Pressure tendency (ΔP over 24h)
    if today.get('pressure') and yesterday.get('pressure'):
        dp = today['pressure'] - yesterday['pressure']
        signals['pressure_tendency'] = dp
    else:
        signals['pressure_tendency'] = 0.0
    
    # Signal 3: Pressure magnitude
    if today.get('pressure'):
        signals['pressure_magnitude'] = today['pressure']
    else:
        signals['pressure_magnitude'] = 0.0
    
    # Signal 4: Temperature magnitude
    if today.get('high') and yesterday.get('high'):
        dt = today['high'] - yesterday['high']
        signals['temp_change'] = dt
    else:
        signals['temp_change'] = 0.0
    
    # Signal 5: Dewpoint depression
    if today.get('dewpoint') and today.get('high'):
        dpd = today['high'] - today['dewpoint']
        signals['dewpoint_depression'] = dpd
    else:
        signals['dewpoint_depression'] = 0.0
    
    # Signal 6: Wind direction change
    if today.get('wind_dir') and yesterday.get('wind_dir'):
        dw = today['wind_dir'] - yesterday['wind_dir']
        # Normalize to -180 to 180
        while dw > 180:
            dw -= 360
        while dw < -180:
            dw += 360
        signals['wind_change'] = dw
    else:
        signals['wind_change'] = 0.0
    
    return signals


def compute_rolling_stats(values: List[float], window: int = 30) -> Tuple[float, float]:
    """Compute rolling mean and std."""
    if len(values) < 2:
        return 0.0, 1.0
    start = max(0, len(values) - window)
    window_values = values[start:]
    mean = sum(window_values) / len(window_values)
    variance = sum((x - mean) ** 2 for x in window_values) / len(window_values)
    std = math.sqrt(variance) if variance > 0 else 1.0
    return mean, std


def get_train_window(test_start_idx: int, all_temps: List[Dict], all_markets: Dict) -> Tuple[List[Dict], Dict]:
    """Get training window ending at test_start_idx."""
    train_start = max(0, test_start_idx - TRAIN_WINDOW_DAYS)
    train_temps = all_temps[train_start:test_start_idx]
    train_markets = {k: all_markets[k] for k in list(all_markets.keys())[train_start:test_start_idx]}
    return train_temps, train_markets


def get_test_window(test_start_idx: int, test_end_idx: int, all_temps: List[Dict], all_markets: Dict) -> Tuple[List[Dict], Dict]:
    """Get test window from test_start_idx to test_end_idx."""
    test_temps = all_temps[test_start_idx:test_end_idx]
    test_markets = {k: all_markets[k] for k in list(all_markets.keys())[test_start_idx:test_end_idx]}
    return test_temps, test_markets


def train_signal_weights(train_temps: List[Dict], train_markets: Dict) -> Dict[str, float]:
    """
    Train signal weights on training data.
    Uses log-odds formula: w = ln(accuracy / (1 - accuracy))
    """
    weights = {}
    
    if len(train_temps) < 30:
        return weights
    
    # For each signal, compute accuracy
    for signal_name in ['trend', 'pressure_tendency', 'pressure_magnitude', 'temp_change', 'dewpoint_depression', 'wind_change']:
        correct = 0
        total = 0
        
        for i in range(1, len(train_temps)):
            today = train_temps[i]
            yesterday = train_temps[i-1]
            
            # Get signal value
            signals = compute_signals([yesterday, today], train_markets)
            if signal_name not in signals:
                continue
            
            signal_value = signals[signal_name]
            
            # Determine signal direction
            if signal_name in ['pressure_tendency', 'temp_change', 'wind_change']:
                # Continuous signals
                sig_dir = 'up' if signal_value > 0 else ('down' if signal_value < 0 else 'flat')
            else:
                # Directional signals
                sig_dir = 'up' if signal_value > 0 else ('down' if signal_value < 0 else 'flat')
            
            if sig_dir == 'flat':
                continue
            
            # Get actual direction - use today's date from the temp dict
            date = today.get('date_utc')
            if not date:
                continue
            
            # Check HIGH market - use get with default empty dict
            actual = train_markets.get(date, {}).get('HIGH')
            if actual and actual != 'flat':
                if sig_dir == actual:
                    correct += 1
                total += 1
            
            # Check LOW market
            actual = train_markets.get(date, {}).get('LOW')
            if actual and actual != 'flat':
                if sig_dir == actual:
                    correct += 1
                total += 1
        
        if total > 0:
            accuracy = correct / total
            # Log-odds weight
            if accuracy > 0 and accuracy < 1:
                weights[signal_name] = math.log(accuracy / (1 - accuracy))
            else:
                weights[signal_name] = 0.0
    
    return weights


def ensemble_vote(signals: Dict[str, float], weights: Dict[str, float]) -> Tuple[str, float]:
    """
    Ensemble vote using weighted signals.
    Returns (direction, confidence)
    """
    if not weights:
        return 'flat', 0.0
    
    # Weighted sum for each direction
    up_sum = 0.0
    down_sum = 0.0
    
    for signal_name, signal_value in signals.items():
        if signal_name not in weights:
            continue
        weight = weights[signal_name]
        if signal_value > 0:
            up_sum += abs(weight) * signal_value
        elif signal_value < 0:
            down_sum += abs(weight) * abs(signal_value)
    
    # Determine direction
    if up_sum > down_sum:
        return 'up', up_sum / (up_sum + down_sum + 1e-10)
    elif down_sum > up_sum:
        return 'down', down_sum / (up_sum + down_sum + 1e-10)
    else:
        return 'flat', 0.5


def run_walk_forward_backtest(station: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Run walk-forward validation for a single station.
    """
    print(f"  Processing {station}...")
    
    # Get data
    station_data = get_station_data(station, conn)
    weather = station_data['weather']
    markets = station_data['markets']
    
    # Get sorted dates
    dates = sorted(weather.keys())
    market_dates = sorted(markets.keys())
    
    # Find common dates
    common_dates = sorted(set(dates) & set(market_dates))
    
    print(f"    Weather dates: {len(dates)}, Market dates: {len(market_dates)}, Common: {len(common_dates)}")
    
    if len(common_dates) < TRAIN_WINDOW_DAYS + TEST_WINDOW_DAYS:
        print(f"    Not enough data: {len(common_dates)} dates, need {TRAIN_WINDOW_DAYS + TEST_WINDOW_DAYS}")
        return None
    
    # Convert to list of dicts with date key
    temps = [{'date_utc': d, **weather[d]} for d in common_dates]
    
    # Create date->markets mapping
    date_markets = {d: markets[d] for d in common_dates if d in markets}
    
    # Walk-forward windows
    results = []
    test_start = TRAIN_WINDOW_DAYS
    
    print(f"    Training with {TRAIN_WINDOW_DAYS} days, testing with {TEST_WINDOW_DAYS} days, rolling by {OVERLAP_DAYS}")
    
    while test_start < len(common_dates) - TEST_WINDOW_DAYS:
        # Training window
        train_temps, train_markets = get_train_window(test_start, temps, date_markets)
        
        print(f"    Window test_start={test_start}, train_len={len(train_temps)}")
        
        # Train weights
        weights = train_signal_weights(train_temps, train_markets)
        
        print(f"    Weights trained: {len(weights)} signals")
        
        if not weights:
            print(f"    No weights trained on training window")
            break
        
        # Test window
        test_end = test_start + TEST_WINDOW_DAYS
        test_temps = temps[test_start:test_end]
        test_dates = common_dates[test_start:test_end]
        
        print(f"    Test window: {len(test_temps)} days")
        
        # Run predictions
        for i in range(len(test_temps)):
            today = test_temps[i]
            yesterday = test_temps[i-1] if i > 0 else temps[test_start - 1]
            
            signals = compute_signals([yesterday, today], date_markets)
            direction, confidence = ensemble_vote(signals, weights)
            
            # Get actual - use get with default empty dict
            test_date = test_dates[i]
            actual = date_markets.get(test_date, {}).get('HIGH', 'flat')
            
            print(f"      {test_date}: predicted={direction}, actual={actual}, confidence={confidence:.2f}")
            
            if actual != 'flat' and direction != 'flat':
                correct = (direction == actual)
                results.append({
                    'date': test_date,
                    'predicted': direction,
                    'actual': actual,
                    'correct': correct,
                    'confidence': confidence,
                })
        
        # Move to next window
        test_start += OVERLAP_DAYS
    
    # Aggregate results
    if not results:
        return None
    
    correct = sum(1 for r in results if r['correct'])
    total = len(results)
    accuracy = correct / total if total > 0 else 0
    
    # Confidence calibration
    high_conf = [r for r in results if r['confidence'] >= 0.6]
    low_conf = [r for r in results if r['confidence'] < 0.6]
    
    high_conf_correct = sum(1 for r in high_conf if r['correct'])
    low_conf_correct = sum(1 for r in low_conf if r['correct'])
    
    high_conf_accuracy = high_conf_correct / len(high_conf) if high_conf else 0
    low_conf_accuracy = low_conf_correct / len(low_conf) if low_conf else 0
    
    # Coverage
    total_possible = len(common_dates)
    coverage = total / total_possible if total_possible > 0 else 0
    
    return {
        'station': station,
        'total_predictions': total,
        'correct': correct,
        'accuracy': accuracy,
        'coverage': coverage,
        'high_conf_accuracy': high_conf_accuracy,
        'low_conf_accuracy': low_conf_accuracy,
        'calibration_pass': high_conf_accuracy > low_conf_accuracy,
    }


def main():
    """Main entry point."""
    print("=" * 80)
    print("PHASE 1: GROUND TRUTH WALK-FORWARD BACKTEST")
    print("=" * 80)
    print()
    print("Parameters:")
    print(f"  Training window: {TRAIN_WINDOW_DAYS} days (6 months)")
    print(f"  Testing window:  {TEST_WINDOW_DAYS} days (1 month)")
    print(f"  Overlap:         {OVERLAP_DAYS} days (1 month)")
    print()
    print("Methods:")
    print("  - Log-odds weights: w = ln(accuracy / (1 - accuracy))")
    print("  - Ensemble vote: weighted sum of signal directions")
    print("  - 6-month train, 1-month test, roll forward")
    print()
    
    # Connect to database
    conn = sqlite3.connect(METAR_DB, timeout=60)
    
    # Get stations
    stations = get_all_stations(conn)
    print(f"Found {len(stations)} stations with data")
    print()
    
    # Process each station
    all_results = {}
    for station in stations:
        result = run_walk_forward_backtest(station, conn)
        if result:
            all_results[station] = result
    
    conn.close()
    
    # Aggregate results
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print()
    
    total_predictions = sum(r['total_predictions'] for r in all_results.values())
    total_correct = sum(r['correct'] for r in all_results.values())
    overall_accuracy = total_correct / total_predictions if total_predictions > 0 else 0
    
    total_coverage = sum(r['coverage'] for r in all_results.values()) / len(all_results) if all_results else 0
    
    # Calibration
    all_high_conf = [r['high_conf_accuracy'] for r in all_results.values() if r['high_conf_accuracy'] > 0]
    all_low_conf = [r['low_conf_accuracy'] for r in all_results.values() if r['low_conf_accuracy'] > 0]
    
    avg_high_conf = sum(all_high_conf) / len(all_high_conf) if all_high_conf else 0
    avg_low_conf = sum(all_low_conf) / len(all_low_conf) if all_low_conf else 0
    
    calibration_pass = avg_high_conf > avg_low_conf
    
    print("OVERALL METRICS")
    print("-" * 40)
    print(f"Total predictions: {total_predictions}")
    print(f"Total correct:     {total_correct}")
    print(f"Directional accuracy: {overall_accuracy:.2%}")
    print(f"Passes threshold (≥{DIRECTIONAL_THRESHOLD:.0%}): {'✓ YES' if overall_accuracy >= DIRECTIONAL_THRESHOLD else '✗ NO'}")
    print()
    print(f"Average coverage:  {total_coverage:.2%}")
    print(f"Coverage threshold (≥{COVERAGE_THRESHOLD:.0%}): {'✓ YES' if total_coverage >= COVERAGE_THRESHOLD else '✗ NO'}")
    print()
    print("CONFIDENCE CALIBRATION")
    print("-" * 40)
    print(f"High confidence (≥60%) accuracy: {avg_high_conf:.2%}")
    print(f"Low confidence (<60%) accuracy:  {avg_low_conf:.2%}")
    print(f"Calibration (high > low): {'✓ PASS' if calibration_pass else '✗ FAIL'}")
    print()
    
    # Station breakdown
    print("STATION BREAKDOWN")
    print("-" * 40)
    print(f"{'Station':<8} {'Accuracy':>10} {'Predictions':>12} {'Coverage':>10}")
    print("-" * 46)
    
    for station in sorted(all_results.keys()):
        r = all_results[station]
        print(f"{station:<8} {r['accuracy']:>10.2%} {r['total_predictions']:>12} {r['coverage']:>10.2%}")
    print()
    
    # Final verdict
    print("=" * 80)
    print("FINAL VERDICT")
    print("=" * 80)
    print()
    
    directional_pass = overall_accuracy >= DIRECTIONAL_THRESHOLD
    coverage_pass = total_coverage >= COVERAGE_THRESHOLD
    
    if directional_pass and coverage_pass and calibration_pass:
        print("✓ ALL THRESHOLDS PASSED")
        print(f"  Directional accuracy: {overall_accuracy:.2%} ≥ {DIRECTIONAL_THRESHOLD:.0%}")
        print(f"  Coverage: {total_coverage:.2%} ≥ {COVERAGE_THRESHOLD:.0%}")
        print(f"  Calibration: High ({avg_high_conf:.2%}) > Low ({avg_low_conf:.2%})")
        print()
        print("RECOMMENDATION: PROCEED TO PHASE 2 (Signal Hygiene)")
        verdict = "PASS"
    elif directional_pass and coverage_pass:
        print("✗ CALIBRATION FAILED")
        print(f"  Directional accuracy: {overall_accuracy:.2%} ≥ {DIRECTIONAL_THRESHOLD:.0%}")
        print(f"  Coverage: {total_coverage:.2%} ≥ {COVERAGE_THRESHOLD:.0%}")
        print(f"  Calibration: High ({avg_high_conf:.2%}) not > Low ({avg_low_conf:.2%})")
        print()
        print("RECOMMENDATION: FIX CONFIDENCE CALIBRATION BEFORE PROCEEDING")
        verdict = "CALIBRATION_FAIL"
    else:
        print("✗ THRESHOLDS NOT MET")
        if not directional_pass:
            print(f"  Directional accuracy: {overall_accuracy:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
        if not coverage_pass:
            print(f"  Coverage: {total_coverage:.2%} < {COVERAGE_THRESHOLD:.0%}")
        print()
        print("RECOMMENDATION: REVIEW IMPLEMENTATION OR ACCEPT LOWER ACCURACY")
        verdict = "FAIL"
    
    print()
    
    # Save results
    output = {
        'parameters': {
            'train_window_days': TRAIN_WINDOW_DAYS,
            'test_window_days': TEST_WINDOW_DAYS,
            'overlap_days': OVERLAP_DAYS,
        },
        'overall': {
            'total_predictions': total_predictions,
            'total_correct': total_correct,
            'directional_accuracy': overall_accuracy,
            'coverage': total_coverage,
            'passes_directional_threshold': directional_pass,
            'passes_coverage_threshold': coverage_pass,
        },
        'calibration': {
            'avg_high_conf_accuracy': avg_high_conf,
            'avg_low_conf_accuracy': avg_low_conf,
            'calibration_pass': calibration_pass,
        },
        'stations': {
            station: {
                'accuracy': r['accuracy'],
                'total_predictions': r['total_predictions'],
                'coverage': r['coverage'],
            }
            for station, r in all_results.items()
        },
        'verdict': verdict,
    }
    
    output_path = "/tmp/p1_walk_forward_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"Results saved to: {output_path}")
    print()
    
    return output


if __name__ == "__main__":
    result = main()
