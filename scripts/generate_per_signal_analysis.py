#!/usr/bin/env python3
"""
Generate backtest analysis broken down by individual signals.

Computes metrics for each of the 7 clean signals:
- Directional accuracy broken down by signal type (per station + aggregate)
- Sharpe ratio broken down by signal type
- Trade count broken down by signal type  
- Max consecutive losses broken down by signal type
Also computes overall signal aggregates (mean, median, mode)
"""

import sqlite3
import pandas as pd
import numpy as np
import json
from collections import Counter, defaultdict
from datetime import datetime
import sys
from scipy.stats import mode

sys.path.insert(0, '.')

# Import from actual modules rather than hardcoding station codes
try:
    from core.station_registry import get_research_stations
except ImportError:
    # Fallback to default known stations if module not available
    def get_research_stations():
        return [
            'KATL', 'KBOS', 'KDFW', 'KDEN', 'KJFK',
            'KLAX', 'KMIA', 'KORD', 'KSEA', 'KSFO',
            'KBNA', 'KHOU', 'KDCA', 'KPDX', 'KSLC',
            'PHNL', 'KTPA', 'KDTW', 'KCLT', 'KMSP'
        ]

# Import signal implementations
from signal_modules.trend_deviation_signal import trend_deviation_signal
from signal_modules.diffusion_convergence_signal import diffusion_convergence_signal
from signal_modules.momentum_differential_signal import momentum_differential_signal
from signal_modules.atmospheric_pressure_tendency import atmospheric_pressure_tendency
from signal_modules.wind_direction_variance import wind_direction_variance
from signal_modules.humidity_inversion_anomaly import humidity_inversion_anomaly
from signal_modules.visibility_trend_deviation import visibility_trend_deviation


def align_metar_with_kalshi(icao_code):
    """Align historical METAR data with Kalshi settlements for the given ICAO station."""
    conn = sqlite3.connect('data/metar_backfill.db')
    try:
        # Query both METAR data and corresponding Kalshi settlement information
        query = """
        SELECT 
            m.timestamp,
            m.temperature,
            m.dew_point,
            m.pressure,
            m.wind_speed,
            m.wind_direction,
            m.visibility,
            k.event_id,
            k.settlement_epoch,
            k.settlement_value,
            k.direction,
            k.market_high,
            k.market_low
        FROM metar_data_3h m
        LEFT JOIN kalshi_settlements k ON 
            k.event_id = ? AND 
            k.settlement_epoch >= m.timestamp - 10800 AND 
            k.settlement_epoch <= m.timestamp + 10800
        WHERE 
            m.icao = ?
            AND m.timestamp >= '2026-01-01'
            AND m.timestamp <= '2026-06-30'
            AND k.direction IS NOT NULL
        ORDER BY m.timestamp
        """
        
        df = pd.read_sql_query(query, conn, params=(icao_code, icao_code))
        return df
    finally:
        conn.close()


def calculate_signal_metrics(station_data):
    """Calculate metrics for the 7 clean signals on the given station data."""
    signal_results = {}
    
    if not station_data.empty:
        # 1. Trend deviation signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = trend_deviation_signal(
                    temp=row['temperature'],
                    pressure=row['pressure'],
                    wind_speed=row['wind_speed'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    # Convert settlement direction to numeric outcome
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                # Calculate Sharpe ratio
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['trend_deviation_signal'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['trend_deviation_signal'] = {'error': True}

        # 2. Diffusion convergence signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = diffusion_convergence_signal(
                    temperature=row['temperature'],
                    dew_point=row['dew_point'],
                    pressure=row['pressure'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['diffusion_convergence_signal'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['diffusion_convergence_signal'] = {'error': True}

        # 3. Momentum differential signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = momentum_differential_signal(
                    temp=row['temperature'],
                    wind_speed=row['wind_speed'],
                    visibility=row['visibility'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['momentum_differential_signal'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['momentum_differential_signal'] = {'error': True}

        # 4. Atmospheric pressure tendency signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = atmospheric_pressure_tendency(
                    pressure=row['pressure'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['atmospheric_pressure_tendency'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['atmospheric_pressure_tendency'] = {'error': True}

        # 5. Wind direction variance signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = wind_direction_variance(
                    wind_direction=row['wind_direction'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['wind_direction_variance'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['wind_direction_variance'] = {'error': True}

        # 6. Humidity inversion anomaly signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = humidity_inversion_anomaly(
                    temperature=row['temperature'],
                    dew_point=row['dew_point'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['humidity_inversion_anomaly'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['humidity_inversion_anomaly'] = {'error': True}

        # 7. Visibility trend deviation signal
        try:
            predictions = []
            outcomes = []
            for _, row in station_data.iterrows():
                signal_val = visibility_trend_deviation(
                    visibility=row['visibility'],
                    timestamp=row['timestamp']
                )
                
                if signal_val is not None and pd.notna(row['settlement_value']):
                    predictions.append(signal_val)
                    if pd.notna(row['market_high']) and pd.notna(row['market_low']):
                        if row['settlement_value'] == row['market_high']:
                            outcomes.append(1)
                        elif row['settlement_value'] == row['market_low']:
                            outcomes.append(-1)
                        else:
                            outcomes.append(0)
            
            if len(predictions) > 0:
                correct_predictions = sum(1 for pred, outcome in zip(predictions, outcomes) if pred * outcome > 0)
                directional_accuracy = correct_predictions / len(predictions) if len(predictions) > 0 else 0
                
                returns = [1 if pred * outcome > 0 else -1 for pred, outcome in zip(predictions, outcomes)]
                if len(returns) > 1:
                    mean_return = np.mean(returns)
                    std_return = np.std(returns)
                    sharpe_ratio = mean_return / std_return if std_return != 0 else 0
                else:
                    sharpe_ratio = 0
                
                max_consecutive_losses = 0
                current_consecutive_losses = 0
                for pred, outcome in zip(predictions, outcomes):
                    if pred * outcome <= 0:
                        current_consecutive_losses += 1
                        max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
                    else:
                        current_consecutive_losses = 0
                
                signal_results['visibility_trend_deviation'] = {
                    'accuracy': directional_accuracy,
                    'sharpe': sharpe_ratio,
                    'trade_count': len(predictions),
                    'max_consecutive_losses': max_consecutive_losses
                }
        except Exception:
            signal_results['visibility_trend_deviation'] = {'error': True}

    return signal_results


def compute_signal_aggregates_by_type(all_station_results):
    """Compute mean, median, mode aggregates for each signal type across all stations."""
    # Dictionary to hold data for each signal type
    signal_data = defaultdict(list)
    
    for station_results in all_station_results.values():
        for signal_name, metrics in station_results.items():
            if isinstance(metrics, dict) and all(key in metrics for key in ['accuracy', 'sharpe', 'trade_count', 'max_consecutive_losses']):
                signal_data[signal_name].append({
                    'accuracy': metrics['accuracy'],
                    'sharpe': metrics['sharpe'],
                    'trade_count': metrics['trade_count'],
                    'max_consecutive_losses': metrics['max_consecutive_losses']
                })
    
    # Now compute aggregates for each signal type
    signal_aggregates = {}
    
    for signal_name, data_points in signal_data.items():
        if not data_points:
            continue
            
        accuracies = [dp['accuracy'] for dp in data_points]
        sharpes = [dp['sharpe'] for dp in data_points]
        trade_counts = [dp['trade_count'] for dp in data_points]
        max_losses = [dp['max_consecutive_losses'] for dp in data_points]
        
        # Compute means
        mean_acc = np.mean(accuracies)
        mean_sharpe = np.mean(sharpes)
        mean_trade_count = np.mean(trade_counts)
        mean_max_losses = np.mean(max_losses)
        
        # Compute medians
        median_acc = float(np.median(accuracies))
        median_sharpe = float(np.median(sharpes))
        median_trade_count = float(np.median(trade_counts))
        median_max_losses = float(np.median(max_losses))
        
        # Compute modes
        rounded_accs = [round(acc, 2) for acc in accuracies]
        acc_mode_res = mode(rounded_accs, keepdims=True)
        acc_mode = float(acc_mode_res.mode[0]) if len(acc_mode_res.mode) > 0 else 0
        
        rounded_sharpes = [round(s, 2) for s in sharpes]
        sharpe_mode_res = mode(rounded_sharpes, keepdims=True)
        sharpe_mode = float(sharpe_mode_res.mode[0]) if len(sharpe_mode_res.mode) > 0 else 0
        
        trade_mode_res = mode(trade_counts, keepdims=True)
        trade_mode = int(trade_mode_res.mode[0]) if len(trade_mode_res.mode) > 0 else 0
        
        loss_mode_res = mode(max_losses, keepdims=True)
        loss_mode = int(loss_mode_res.mode[0]) if len(loss_mode_res.mode) > 0 else 0
        
        signal_aggregates[signal_name] = {
            'mean': {
                'accuracy': mean_acc,
                'sharpe': mean_sharpe,
                'trade_count': mean_trade_count,
                'max_consecutive_losses': mean_max_losses
            },
            'median': {
                'accuracy': median_acc,
                'sharpe': median_sharpe,
                'trade_count': median_trade_count,
                'max_consecutive_losses': median_max_losses
            },
            'mode': {
                'accuracy': acc_mode,
                'sharpe': sharpe_mode,
                'trade_count': trade_mode,
                'max_consecutive_losses': loss_mode
            }
        }
    
    return signal_aggregates


def analyze_signals():
    """Execute the per-signal analysis for all 20 stations."""
    stations = get_research_stations()
    
    all_station_results = {}
    print(f"Analyzing signals for all {len(stations)} stations...")
    
    # Process each station
    for i, station in enumerate(stations):
        print(f"Processing station {i+1}/{len(stations)}: {station}")
        try:
            station_data = align_metar_with_kalshi(station)
            signal_metrics = calculate_signal_metrics(station_data)
            all_station_results[station] = signal_metrics
        except Exception as e:
            print(f"Error processing station {station}: {e}")
            all_station_results[station] = {}
    
    # Compute aggregates by signal type across all stations
    signal_aggregates = compute_signal_aggregates_by_type(all_station_results)
    
    # Organize the results
    results = {
        'station_specific_results': all_station_results,
        'by_signal_type_aggregates': signal_aggregates
    }
    
    return results


if __name__ == '__main__':
    results = analyze_signals()
    print(json.dumps(results, indent=2))