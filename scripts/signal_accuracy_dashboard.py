#!/usr/bin/env python3
"""
SIGNAL ACCURACY DASHBOARD

Comprehensive accuracy breakdown for weather engine signals.
Runs each of the 5 v8 signals independently and computes detailed metrics
for signal and station performance identification.
"""

import sqlite3
import math
import random
import json
import sys
from collections import defaultdict
import os
import numpy as np
from scipy.stats import binomtest


# Database path
DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Station list from station_registry
ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

def load_station_data(station, conn):
    """Copied from ensemble_v8 for consistency."""
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
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                      'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = {
                'direction': 'up' if r[1] > r[2] else 'down',
                'reversion': r[3] if r[3] is not None else 0
            }
    return days, market


def sigmoid(x):
    """Numerical stability sigmoid function."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


# ─── ORIGINAL 5 V8 SIGNALS ───────────────────────────────────────────────────


def approach_gaussian(idx, days):
    if idx < 48: return None, 0.0
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0: return 'down', abs(z)
    elif z < -1.0: return 'up', abs(z)
    return None, 0.0


def approach_gaussian_v2(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_pressure(idx, days):
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0

APPROACHES = [approach_gaussian,
              approach_gaussian_v2, approach_pressure]
APPROACH_NAMES = ["Gaussian(48d)", "Gaussian v2(30d)", "Pressure"]


def binomial_test_p(correct, total, null_p=0.5):
    """Exact binomial test."""
    if total == 0:
        return 1.0
    # One-sided: probability of getting >= correct successes (or <= if correct < expected)
    expected = null_p * total
    if correct >= expected:
        return binomtest(correct, total, null_p, alternative='greater').pvalue
    else:
        return binomtest(correct, total, null_p, alternative='less').pvalue


def compute_brier_score(trials):
    """
    Compute Brier score: mean squared error of probability forecast.
    trials: list of (prediction_direction, actual_direction, confidence_value)
    """
    if not trials:
        return float('nan'), 0.0, 0.0  # brier, correct_pct, total
    
    squared_errors = []
    correct_count = 0
    for pred_dir, actual_dir, conf in trials:
        # Convert prediction to probability for the outcome that occurred
        prob_prediction = conf if pred_dir == 'up' else (1 - conf)  # prob of outcome if 'up' predicted
        prob_prediction = 1 - conf if pred_dir == 'down' else prob_prediction  # prob of outcome if 'down' predicted
        
        # Determine what actually happened (as binary event that 'up' occurred)
        if actual_dir == 'up':
            actual_prob = 1.0  # up occurred
            prob_error = (prob_prediction - 1.0) ** 2
        else:  # actual_dir == 'down'
            actual_prob = 0.0  # up did not occur
            prob_error = (prob_prediction - 0.0) ** 2
            
        squared_errors.append(prob_error)
        
        # Also count how many times we got the direction right
        if pred_dir == actual_dir:
            correct_count += 1
    
    brier = sum(squared_errors) / len(squared_errors) if squared_errors else float('nan')
    correct_pct = correct_count / len(trials) if trials else 0.0
    return brier, correct_pct, len(trials)


def analyze_single_signal_approach(approach_fn, approach_name, station, conn):
    """
    Analyzes a single signal approach on a single station.
    Returns detailed metrics as tuple.
    """
    days, market = load_station_data(station, conn)
    if len(days) < 180:  # Need enough data for walk-forward
        return None
    
    # Perform walk-forward validation for this signal approach
    results = []
    start_idx = 180  # Training period
    test_days = 30
    total_days_available = len(days)
    
    while start_idx < total_days_available:
        test_stop = min(start_idx + test_days, total_days_available)
        for actual_idx in range(start_idx, test_stop):
            day_date = days[actual_idx]['date']
            actual_direction = market.get(day_date)
            if actual_direction is None:
                continue
                
            # Run this signal approach
            pred, conf = approach_fn(actual_idx, days)
            if pred is not None and conf > 0.0:  # Any confidence level triggers
                results.append((pred, actual_direction['direction'], conf))
        
        start_idx += test_days
    
    if not results:
        return None
    
    # Compute metrics
    total_trades = len(results)
    correct_predictions = sum(1 for pred, actual, conf in results if pred == actual)
    accuracy = correct_predictions / total_trades if total_trades > 0 else 0
    
    # Calculate coverage (% of possible periods when signal fired)
    if len(days) > 180:
        possible_days = len(days) - 180
        total_observed = 0
        for i in range(180, len(days)):
            day_date = days[i]['date']
            if day_date in market:  # Actual day had corresponding market data
                total_observed += 1
        
        # For this specific signal, check actual days where signal could fire
        coverage_trials = 0
        for i in range(180, len(days)):
            day_date = days[i]['date']
            if day_date in market:  # Day exists in market data
                pred, conf = approach_fn(i, days)
                if pred is not None and conf > 0.0:  # Signal fired
                    coverage_trials += 1
        coverage = coverage_trials / total_observed if total_observed > 0 else 0.0
    else:
        coverage = 0.0
    
    # Brier score
    brier, brier_accuracy, brier_total = compute_brier_score(results)
    
    # Average confidence when correct vs wrong
    correct_confs = [conf for pred, actual, conf in results if pred == actual]
    wrong_confs = [conf for pred, actual, conf in results if pred != actual]
    
    avg_conf_correct = sum(correct_confs) / len(correct_confs) if correct_confs else 0.0
    avg_conf_wrong = sum(wrong_confs) / len(wrong_confs) if wrong_confs else 0.0
    
    # Binomial p-value
    binomial_p = binomial_test_p(correct_predictions, total_trades)
    
    return {
        'approach_name': approach_name,
        'station': station,
        'accuracy': accuracy,
        'total_trades': total_trades,
        'coverage': coverage,
        'brier_score': brier,
        'avg_confidence_correct': avg_conf_correct,
        'avg_confidence_wrong': avg_conf_wrong,
        'binomial_p': binomial_p,
        'results': results  # For potential cross-signal analysis
    }


def run_individual_signal_analysis():
    """Run individual signal analysis for all signals and stations."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    
    signal_station_metrics = {}
    all_signal_data = {}
    
    print("=" * 120)
    print("SIGNAL ACCURACY DASHBOARD — Individual Signal Performance Review")
    print("=" * 120)
    
    # Loop through all signals
    for approach_idx, (approach_fn, approach_name) in enumerate(zip(APPROACHES, APPROACH_NAMES)):
        print(f"\n{approach_name} ({approach_idx + 1}/{len(APPROACHES)}) Analysis")
        print("-" * 120)
        print(f"{'Station':<8} {'Accuracy':>10} {'Trades':>8} {'Coverage':>10} {'Brier':>8} {'Conf_Corr':>10} {'Conf_Wrong':>10} {'Binomial_p':>12}")
        print("-" * 120)
        
        for station in ALL_STATIONS:
            result = analyze_single_signal_approach(approach_fn, approach_name, station, conn)
            if result:
                signal_station_metrics[(approach_name, station)] = result
                all_signal_data[f"{approach_name}:{station}"] = result['results']
                
                print(f"{station:<8} {result['accuracy']:>10.3f} {result['total_trades']:>8} {result['coverage']:>10.3f} "
                      f"{result['brier_score'] if not math.isnan(result['brier_score']) else 0.0:>8.3f} "
                      f"{result['avg_confidence_correct']:>10.3f} {result['avg_confidence_wrong']:>10.3f} "
                      f"{result['binomial_p']:>12.4f}")
            else:
                print(f"{station:<8} {'No data':>10} {'0':>8} {'0.000':>10} {'0.000':>8} {'0.000':>10} {'0.000':>10} {'1.0000':>12}")
    
    conn.close()
    
    # Identify deadweight signals (<55% accuracy OR <10% coverage)
    print("\n\nDEADWEIGHT SIGNAL EVALUATION")
    print("=" * 80)
    deadweight_signals = set()
    for (approach_name, station), metrics in signal_station_metrics.items():
        if metrics['accuracy'] < 0.55 or metrics['coverage'] < 0.10:
            deadweight_signals.add(approach_name)
            print(f"  {approach_name} @ {station} — ACC: {metrics['accuracy']:.3f}, COV: {metrics['coverage']:.3f} — DISQUALIFIED")
    
    # Identify untradeable stations (using ensemble method for each station)
    print("\n\nUNTRADEABLE STATIONS EVALUATION")
    print("=" * 80)
    untradeable_stations = set()
    
    # Ensemble performance per station
    ensemble_by_station = run_ensemble_per_station_analysis()
    for station, metrics in ensemble_by_station.items():
        if metrics['accuracy'] < 0.58:
            untradeable_stations.add(station)
            print(f"  {station} — Ensemble ACC: {metrics['accuracy']:.3f} — UNTRADEABLE")
    
    # Summary
    print("\n\nSUMMARY")
    print("=" * 80)
    print(f"Deadweight signals: {deadweight_signals}")
    print(f"Untradeable stations: {untradeable_stations}")
    
    # Most productive signals (highest average accuracy)
    print("\nProductive Signal Ranking (Avg Accuracy):")
    signal_averages = {}
    for approach_name in APPROACH_NAMES:
        relevant_results = [signal_station_metrics[k] for k in signal_station_metrics 
                           if k[0] == approach_name and signal_station_metrics[k]['total_trades'] > 0]
        if relevant_results:
            avg_acc = sum(r['accuracy'] for r in relevant_results) / len(relevant_results)
            signal_averages[approach_name] = avg_acc
            print(f"  {approach_name}: {avg_acc:.3f}")
    
    # Best performing stations
    print("\nBest Performing Stations (Avg Accuracy across all signals):")
    station_averages = {}
    for station in ALL_STATIONS:
        relevant_results = [signal_station_metrics[k] for k in signal_station_metrics 
                           if k[1] == station and signal_station_metrics[k]['total_trades'] > 0]
        if relevant_results:
            avg_acc = sum(r['accuracy'] for r in relevant_results) / len(relevant_results)
            station_averages[station] = avg_acc
    
    sorted_stations = sorted(station_averages.items(), key=lambda x: x[1], reverse=True)
    for station, avg_acc in sorted_stations[:5]:  # Top 5
        print(f"  {station}: {avg_acc:.3f}")
    
    return signal_station_metrics, deadweight_signals, untradeable_stations, ensemble_by_station


def run_ensemble_per_station_analysis():
    """Analyze ensemble performance separately for each station."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    
    station_results = {}
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210:  # Need data for training
            continue
        
        # Walk-forward ensemble for this single station
        results = []
        start = 180  # Training period
        test_days = 30
        total_days = len(days)
        
        while start + test_days <= total_days:
            for idx in range(start, min(start + test_days, total_days)):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None: continue
                
                # Ensemble voting
                predictions = {}
                for i, fn in enumerate(APPROACHES):
                    pred, conf = fn(idx, days)
                    if pred is not None and conf > 0.0:  # Any signal firing counts
                        predictions[f'a{i+1}'] = (pred, conf)
                
                if len(predictions) >= 1:  # At least one signal to determine direction
                    # Simple ensemble: weight votes by confidence
                    up_sum = sum(conf for pred, conf in predictions.values() if pred == 'up')
                    down_sum = sum(conf for pred, conf in predictions.values() if pred == 'down')
                    
                    if (up_sum + down_sum) > 0:
                        if up_sum > down_sum:
                            ensemble_pred = 'up'
                            ensemble_conf = up_sum / (up_sum + down_sum)
                        else:
                            ensemble_pred = 'down'
                            ensemble_conf = down_sum / (up_sum + down_sum) if (up_sum + down_sum) > 0 else 0
                        
                        results.append((ensemble_pred, actual['direction'], ensemble_conf))
            
            start += test_days
            if start > total_days: break
    
        if results:  # Only count if station has results
            total = len(results)
            correct = sum(1 for p, a, c in results if p == a)
            acc = correct / total
            binom_p = binomial_test_p(correct, total)
            station_results[station] = {
                'accuracy': acc,
                'total_trades': total,
                'correct': correct,
                'binomial_p': binom_p,
                'results': results
            }
    
    conn.close()
    return station_results


def main():
    print("Weather Engine: Signal Accuracy Dashboard")
    print("=" * 80)
    
    signal_metrics, deadweight_signals, untradeable_stations, ensemble_results = run_individual_signal_analysis()
    
    # Prepare report data
    report_data = {
        'timestamp': '2026-07-02',
        'summary': {
            'total_stations': len(ALL_STATIONS),
            'total_signals': len(APPROACH_NAMES),
            'deadweight_signals': list(deadweight_signals),
            'untradeable_stations': list(untradeable_stations)
        },
        'signal_station_performance': {}
    }
    
    for (sig, st), metrics in signal_metrics.items():
        key = f"{sig}:{st}"
        report_data['signal_station_performance'][key] = {
            'station': st,
            'signal': sig,
            'accuracy': metrics['accuracy'],
            'total_trades': metrics['total_trades'],
            'coverage': metrics['coverage'],
            'brier_score': metrics['brier_score'] if not math.isnan(metrics['brier_score']) else 0.0,
            'avg_conf_correct': metrics['avg_confidence_correct'],
            'avg_conf_wrong': metrics['avg_confidence_wrong'],
            'binomial_p': metrics['binomial_p']
        }
        
        report_data['signal_station_performance'][key]['is_deadweight'] = (
            metrics['accuracy'] < 0.55 or metrics['coverage'] < 0.10
        )
    
    # Station-level ensemble results
    report_data['station_ensemble_performance'] = {}
    for station, perf in ensemble_results.items():
        report_data['station_ensemble_performance'][station] = {
            'accuracy': perf['accuracy'],
            'total_trades': perf['total_trades'],
            'correct': perf['correct'],
            'binomial_p': perf['binomial_p'],
            'is_untradeable': perf['accuracy'] < 0.58
        }
    
    # Save JSON report
    os.makedirs('data', exist_ok=True)
    with open('data/signal_accuracy_report.json', 'w') as f:
        json.dump(report_data, f, indent=2)
    
    print(f"\nComplete! Report saved to data/signal_accuracy_report.json")
    
    # Key findings summary
    print("\nKEY FINDINGS:")
    print("-" * 30)
    if deadweight_signals:
        print(f"🔴 DEADWEIGHT SIGNALS: {deadweight_signals}")
    if untradeable_stations:
        print(f"🟡 UNTRADEABLE STATIONS: {untradeable_stations}")


if __name__ == "__main__":
    main()