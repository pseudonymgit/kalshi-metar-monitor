#!/usr/bin/env python3
"""
Experiment: Signal Freshness Weighting

Weight signals by their age based on half-life principles before fusing them.
NWP half-life ~6h, METAR half-life ~2h, but different signals have different update frequencies.

Half-lives (per Gray Room R6 analysis):
- nwp_direct: 6 hours
- temperature_advection: 6 hours
- forecast_disagreement: 2 hours (METAR-based)
- pressure_delta: 2 hours
- calendar_climatology: 24 hours
- gaussian: 2 hours
- frontal_detector: 1 hour
- persistence: 0 hours (always fresh)
- wind_direction_shift: 2 hours
"""

import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import math

def calculate_freshness_weight(elapsed_hours, half_life_hours):
    """
    Calculate freshness weight using exponential decay based on half-life.
    
    Args:
        elapsed_hours (float): Time since signal observation in hours
        half_life_hours (float): Half-life of the signal in hours
    
    Returns:
        float: Weight between 0 and 1
    """
    if half_life_hours <= 0:
        return 1.0  # Always fresh
    
    return math.exp(-math.log(2) * elapsed_hours / half_life_hours)

def run_signal_freshness_experiment():
    # Connect to database
    conn = sqlite3.connect('data/weather_data.db')
    
# Skip the first large query as needed columns don't exist - load and continue with different approach
    
    # Load data for analysis
    query = """
    SELECT 
        s.station,
        s.target_date,
        s.signal_name,
        s.signal_value,
        s.signal_timestamp,
        r.settlement_value
    FROM signals s
    JOIN settlements r ON s.station = r.station AND s.target_date = r.target_date
    WHERE s.signal_name IN ('nwp_direct', 'temperature_advection', 'forecast_disagreement', 
                          'pressure_delta', 'calendar_climatology', 'gaussian', 
                          'frontal_detector', 'persistence', 'wind_direction_shift')
    """
    
    df = pd.read_sql_query(query, conn)
    
    # Add time difference column for signals that have timestamps. Using target_date to calculate age from signal_timestamp.
    # Convert timestamps, filling in default when invalid
    df['signal_dt'] = pd.to_datetime(df['signal_timestamp'], format='mixed', errors='coerce')  
    df['target_dt'] = pd.to_datetime(df['target_date'])
    
    # Calculate how long ago the signal was generated relative to target date 
    td_diff = df['target_dt'] - df['signal_dt']
    df['elapsed_hours'] = td_diff.dt.total_seconds() / 3600  
    
    # Handle possible NaT values from conversion issues, fill with default 24 hours
    df['elapsed_hours'] = df['elapsed_hours'].fillna(24.0)
    # Ensure that only positive or zero elapsed times are processed (signals can't be made in the future)
    df = df[df['elapsed_hours'] >= 0]
    
    # Define half-lives for each signal type
    half_lives = {
        'nwp_direct': 6.0,
        'temperature_advection': 6.0,
        'forecast_disagreement': 2.0,
        'pressure_delta': 2.0,
        'calendar_climatology': 24.0,
        'gaussian': 2.0,
        'frontal_detector': 1.0,
        'persistence': 0.0,  # Always fresh since it uses most recent METAR
        'wind_direction_shift': 2.0
    }
    
    # Calculate weights
    df['half_life'] = df['signal_name'].map(half_lives)
    df['freshness_weight'] = df.apply(
        lambda row: calculate_freshness_weight(row['elapsed_hours'], row['half_life']), axis=1
    )
    
    # Now run the same analysis comparing with/without weights
    # Group by station, target_date, and ensemble_id to get weighted and unweighted results
    signals_df = pd.read_sql_query("""
        SELECT 
            station, 
            target_date, 
            ensemble_id,
            signal_name,
            CAST(signal_value AS REAL) as signal_value,
            signal_timestamp
        FROM signals 
        WHERE signal_name IN ('nwp_direct', 'temperature_advection', 'forecast_disagreement', 
                              'pressure_delta', 'calendar_climatology', 'gaussian', 
                              'frontal_detector', 'persistence', 'wind_direction_shift')
    """, conn)
    
    settlements_df = pd.read_sql_query("""
        SELECT station, target_date, settlement_value 
        FROM settlements
        WHERE settlement_value IS NOT NULL
    """, conn)
    
    # Join signals with settlements
    combined_df = signals_df.merge(settlements_df, on=['station', 'target_date'], how='inner')
    
    # Calculate elapsed hours if timestamps are available
    if 'signal_timestamp' in combined_df.columns:
        # Attempt to convert signal_timestamp and calculate elapsed time
        combined_df['signal_dt'] = pd.to_datetime(combined_df['signal_timestamp'], format='mixed', errors='coerce')
        # Use a fixed reference time for signal age calculation if needed
        combined_df['settlement_dt'] = pd.to_datetime(combined_df['target_date'])
        # Calculate elapsed time in hours from signal to target date
        elapsed_td = pd.to_timedelta(combined_df['settlement_dt'].dt.date - combined_df['signal_dt'].dt.date)
        combined_df['elapsed_hours'] = elapsed_td.dt.days * 24  # Convert days to hours
        # If signal_dt is NaT or invalid, set a default elapsed time (like 24 hours)
        combined_df.loc[combined_df['signal_dt'].isna(), 'elapsed_hours'] = 24.0
    else:
        # If no signal_timestamp, assume a default elapsed time of 24 hours
        combined_df['elapsed_hours'] = 24.0
    
    # Calculate weights
    combined_df['half_life'] = combined_df['signal_name'].map(half_lives)
    combined_df['freshness_weight'] = combined_df.apply(
        lambda row: calculate_freshness_weight(row['elapsed_hours'], row['half_life']), axis=1
    )
    
    # Group signals by station, target_date, and ensemble_id to calculate weighted average
    weighted_signals = []
    unweighted_signals = []
    
    grouped = combined_df.groupby(['station', 'target_date', 'ensemble_id'])
    
    for name, group in grouped:
        station, target_date, ensemble_id = name
        
        # Calculate unweighted average (simple mean)
        unweighted_avg = group['signal_value'].mean()
        settlement = group['settlement_value'].iloc[0]
        
        unweighted_signals.append({
            'station': station,
            'target_date': target_date,
            'ensemble_id': ensemble_id,
            'predicted_value': unweighted_avg,
            'settlement_value': settlement
        })
        
        # Calculate weighted average
        if len(group) > 0:
            try:
                weights = group['freshness_weight']
                values = group['signal_value']
                
                # Avoid division by zero if all weights are 0
                weight_sum = weights.sum()
                if weight_sum == 0:
                    weighted_avg = values.mean()  # Fallback to unweighted mean
                else:
                    weighted_avg = (weights * values).sum() / weight_sum
                
                weighted_signals.append({
                    'station': station,
                    'target_date': target_date,
                    'ensemble_id': ensemble_id,
                    'predicted_value': weighted_avg,
                    'settlement_value': settlement
                })
            except Exception:
                # Error occurred, fallback to unweighted mean
                weighted_signals.append({
                    'station': station,
                    'target_date': target_date,
                    'ensemble_id': ensemble_id,
                    'predicted_value': unweighted_avg,
                    'settlement_value': settlement
                })
    
    # Convert to DataFrames
    df_unweighted = pd.DataFrame(unweighted_signals)
    df_weighted = pd.DataFrame(weighted_signals)
    
    # Create walk-forward validation setup similar to other experiments
    def evaluate_accuracy(df_subset):
        """Calculate various performance metrics"""
        if df_subset.empty or df_subset['predicted_value'].isnull().all():
            return {'accuracy': 0, 'mae': float('inf'), 'rmse': float('inf'), 'count': 0}
        
        df_subset = df_subset.dropna(subset=['predicted_value', 'settlement_value'])
        
        if len(df_subset) == 0:
            return {'accuracy': 0, 'mae': float('inf'), 'rmse': float('inf'), 'count': 0}
        
        predicted = df_subset['predicted_value']
        actual = df_subset['settlement_value']
        
        # Accuracy for direction prediction
        prediction_direction = np.sign(predicted - 32)  # Above/below freezing prediction
        actual_direction = np.sign(actual - 32)
        accuracy = (prediction_direction == actual_direction).mean()
        
        # Calculate error metrics
        mae = abs(predicted - actual).mean()
        rmse = np.sqrt(((predicted - actual) ** 2).mean())
        
        return {
            'accuracy': accuracy,
            'mae': mae,
            'rmse': rmse,
            'count': len(df_subset)
        }
    
    # Split data chronologically for walk-forward validation
    df_unweighted['target_date_dt'] = pd.to_datetime(df_unweighted['target_date'])
    df_weighted['target_date_dt'] = pd.to_datetime(df_weighted['target_date'])
    
    # Simple approach: sort by date, use last 30 days for testing, first n-30 for training
    max_date = df_unweighted['target_date_dt'].max()
    min_test_date = max_date - pd.Timedelta(days=30)
    
    unweighted_train = df_unweighted[df_unweighted['target_date_dt'] < min_test_date]
    unweighted_test = df_unweighted[df_unweighted['target_date_dt'] >= min_test_date]
    
    weighted_train = df_weighted[df_weighted['target_date_dt'] < min_test_date]
    weighted_test = df_weighted[df_weighted['target_date_dt'] >= min_test_date]
    
    # Evaluate both approaches on test set
    unweighted_metrics = evaluate_accuracy(unweighted_test)
    weighted_metrics = evaluate_accuracy(weighted_test)
    
    # Calculate improvements (positive means weighting helps)
    results = {
        'overview': {
            'half_lives_used': half_lives,
            'training_period': f"{unweighted_train['target_date_dt'].min()} to {unweighted_train['target_date_dt'].max()}", 
            'test_period': f"{unweighted_test['target_date_dt'].min()} to {unweighted_test['target_date_dt'].max()}",
            'total_test_samples': len(unweighted_test),
            'total_combinations_evaluated': len(unweighted_test['ensemble_id'].unique())
        },
        'unweighted_performance': {
            'accuracy': round(unweighted_metrics['accuracy'], 4),
            'mae': round(unweighted_metrics['mae'], 4),
            'rmse': round(unweighted_metrics['rmse'], 4),
            'trade_count': unweighted_metrics['count']
        },
        'weighted_performance': {
            'accuracy': round(weighted_metrics['accuracy'], 4),
            'mae': round(weighted_metrics['mae'], 4),
            'rmse': round(weighted_metrics['rmse'], 4),
            'trade_count': weighted_metrics['count']
        },
        'improvement_vs_unweighted': {
            'accuracy_change': round(weighted_metrics['accuracy'] - unweighted_metrics['accuracy'], 4),
            'mae_change': round(weighted_metrics['mae'] - unweighted_metrics['mae'], 4),
            'rmse_change': round(weighted_metrics['rmse'] - unweighted_metrics['rmse'], 4),
            'trade_count_change': weighted_metrics['count'] - unweighted_metrics['count']
        }
    }
    
    # Also find the top 5 combinations by performance
    top_combinations = []
    for ensemble_id in df_unweighted['ensemble_id'].unique()[:5]:  # Top 5
        ensemble_unweighted = df_unweighted[df_unweighted['ensemble_id'] == ensemble_id]
        ensemble_weighted = df_weighted[df_weighted['ensemble_id'] == ensemble_id]
        
        subset_unweighted = ensemble_unweighted[
            ensemble_unweighted['target_date_dt'] >= min_test_date
        ]
        subset_weighted = ensemble_weighted[
            ensemble_weighted['target_date_dt'] >= min_test_date
        ]
        
        uw_metrics = evaluate_accuracy(subset_unweighted)
        w_metrics = evaluate_accuracy(subset_weighted)
        
        if uw_metrics['count'] > 0 and w_metrics['count'] > 0:  # Only include if there are test samples
            top_combinations.append({
                'ensemble_id': ensemble_id,
                'unweighted_accuracy': round(uw_metrics['accuracy'], 4),
                'weighted_accuracy': round(w_metrics['accuracy'], 4),
                'accuracy_improvement': round(w_metrics['accuracy'] - uw_metrics['accuracy'], 4),
                'trade_count_unweighted': uw_metrics['count'],
                'trade_count_weighted': w_metrics['count'],
                'trade_count_change': w_metrics['count'] - uw_metrics['count']
            })
    
    results['top_5_combinations_comparison'] = sorted(
        top_combinations, 
        key=lambda x: x['accuracy_improvement'], 
        reverse=True
    )[:5]
    
    conn.close()
    
    return results


if __name__ == "__main__":
    print("Running signal freshness experiment...")
    results = run_signal_freshness_experiment()
    
    # Save results to data directory
    with open('data/experiment_signal_freshness.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print("Experiment complete!")
    print(f"Results saved to data/experiment_signal_freshness.json")
    
    print("\nSummary:")
    print(f"Accuracy improvement: {results['improvement_vs_unweighted']['accuracy_change']:.4f}")
    print(f"MAE change: {results['improvement_vs_unweighted']['mae_change']:.4f}")
    print(f"Trade count change: {results['improvement_vs_unweighted']['trade_count_change']}")
    
    print("\nTop 5 combinations by improvement:")
    for i, combo in enumerate(results['top_5_combinations_comparison'][:5]):
        print(f"  {i+1}. Ensemble {combo['ensemble_id']}: "
              f"{combo['accuracy_improvement']:+.4f} accuracy, "
              f"{combo['trade_count_change']:+d} trades")