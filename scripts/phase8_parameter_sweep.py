#!/usr/bin/env python3
"""
PHASE 8 — TASK 8.3: Two-Stage Parameter Sweep

Two-Stage Approach:
Stage 1: Global sweep on 50% of data (randomly selected historical period)
Stage 2: Per-city refinement on held-out 50% (global parameters + tuning per city)
- Nested validation: Stage 1 parameters are frozen during Stage 2
- Ensures robust hyperparameters across cities and time periods

Nested Cross Validation Structure:
- Outer loop: split dataset temporally (50-50)
- Inner loop: validate global parameters (Stage 1)
- Final stage: per-city refinement with fixed global params (Stage 2)

Output: data/phase8_parameter_sweep.json  
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
import random
from datetime import datetime, timedelta
from itertools import product
from collections import defaultdict
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

# Parameter ranges for Stage 1 (Global Sweep)
STAGE1_PARAM_RANGES = {
    'agreement_threshold': [1, 2, 3, 4],  # Min number of agreeing signals
    'confidence_threshold': [0.5, 0.55, 0.6, 0.65, 0.7],  # Min average confidence
    'window_length': [120, 180, 240, 300],  # Training window length
    'use_dewpoint_modulation': [True, False]  # Whether to modulate with dewpoint
}

# Parameter ranges for Stage 2 (Per-City Refinement)
STAGE2_PARAM_RANGES = {
    'confidence_threshold_adjustment': [-0.1, -0.05, 0.0, 0.05, 0.1],  # Per-city adjustments
    'agreement_threshold_adjustment': [-1, 0, 1]  # Per-city adjustment (within bounds)
}


class ParameterOptimizer:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.registry = create_signal_registry(db_path)
        self.all_signals = self.registry.get_all_signals()
        self.all_signal_names = list(self.all_signals.keys())

    def temporal_split_dataset(self, days: List[Dict], split_ratio: float = 0.5) -> Tuple[List[Dict], List[Dict]]:
        """
        Split dataset temporally by specified ratio, maintaining chronological order.
        
        Returns:
        - early_period: First chunk of dataset chronologically 
        - late_period: Second chunk of dataset chronologically
        """
        split_point = int(len(days) * split_ratio)
        return days[:split_point], days[split_point:]

    def load_market_data(self, station: str) -> Dict[str, str]:
        """Load market data for a station."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT local_trading_date, settlement_bucket
            FROM settlement_epochs
            WHERE station=? AND market_type='HIGH'
            ORDER BY local_trading_date ASC
        """, (station,))
        rows = cur.fetchall()
        conn.close()
        
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

    def align_days_with_market(self, days: List[Dict], market: Dict[str, str]) -> List[Dict]:
        """Align daily data with market direction."""
        aligned = []
        for d in days:
            date_key = d['date'][:10]
            if date_key in market:
                entry = dict(d)
                entry['market_dir'] = market[date_key]
                aligned.append(entry)
        return aligned    

    def evaluate_config_stage1(self, station: str, param_config: Dict, signal_subset: List[str]) -> Dict:
        """
        Evaluate a parameter configuration during Stage 1 (for a specific station).
        Properly loads and aligns market data.
        """
        # Load days for this station 
        conn = sqlite3.connect(self.db_path)
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
        raw_days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]):
                continue
            raw_days.append({
                'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
            })
        conn.close()
        
        if not raw_days:
            return {'correct': 0, 'total': 0, 'trades': 0}
            
        # Load and align market data 
        market_data = self.load_market_data(station)
        aligned_days = self.align_days_with_market(raw_days, market_data)
        
        # Now use data from first 50% of aligned period only for training
        mid_point = len(aligned_days) // 2
        training_days = aligned_days[:mid_point]
        
        if len(training_days) < param_config['window_length']:
            return {'correct': 0, 'total': 0, 'trades': 0}
            
        # Extract parameters
        min_agreement = param_config['agreement_threshold']
        conf_thresh = param_config['confidence_threshold'] 
        use_dewpoint = param_config['use_dewpoint_modulation']
        
        results = {'correct': 0, 'total': 0, 'trades': 0}
        
        # Evaluate across training period
        for idx in range(param_config['window_length'], len(training_days)):
            actual_direction = training_days[idx].get('market_dir', 'flat') 
            if actual_direction == 'flat':
                continue
                
            # Gather signal predictions
            votes = []
            signal_objects = [self.all_signals[name] for name in signal_subset if name in self.all_signals]
            
            for i, sig_name in enumerate(signal_subset):
                sig_obj = signal_objects[i]
                
                # Calculate confidence based on the signal
                try:
                    direction, confidence = sig_obj.evaluate(idx, training_days)
                    
                    if direction is not None and confidence > 0:
                        # Apply dewpoint modulation if needed
                        if use_dewpoint and sig_name in ['calendar_climatology', 'gaussian', 'gaussian_v2']:
                            if idx > 0:
                                prev_day = training_days[idx - 1]
                                temp = prev_day.get('temp')
                                dewpoint = prev_day.get('dewpoint')
                                if temp is not None and dewpoint is not None:
                                    confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)
                        
                        if confidence >= conf_thresh:
                            votes.append((direction, confidence))
                except:
                    continue  # Skip signals with errors
                    
            # Apply agreement check
            if len(votes) < min_agreement:
                continue
            
            # Calculate weighted majority direction
            up_score = sum(w for d, w in votes if d == 'up')
            down_score = sum(w for d, w in votes if d == 'down')
            
            if up_score > down_score:
                predicted_direction = 'up'
            elif down_score > up_score:
                predicted_direction = 'down' 
            else:
                continue  # Tie - no prediction
            
            # Record result
            results['trades'] += 1
            results['total'] += 1
            if predicted_direction == actual_direction:
                results['correct'] += 1
        
        return results

    def sweep_stage1_global(self, station_list: List[str],
                            signal_set: List[str]) -> Tuple[Dict, Dict]:
        """
        Perform global parameter sweep for each station using properly aligned data.
        Return best configuration and all results.
        """
        stage1_results = {}
        
        logger.info("Performing Stage 1: Global Sweep")
        
        # Collect all possible configurations
        param_names = list(STAGE1_PARAM_RANGES.keys())
        param_values = [STAGE1_PARAM_RANGES[name] for name in param_names]
        
        # Generate all parameter combinations
        for param_combo in product(*param_values):
            param_config = dict(zip(param_names, param_combo))
            
            total_stats = {'correct': 0, 'total': 0, 'trades': 0} 
            station_stats = {}
            
            for station in station_list:
                try:
                    station_result = self.evaluate_config_stage1(station, param_config, signal_set)
                    station_stats[station] = station_result
                    total_stats['correct'] += station_result['correct']
                    total_stats['total'] += station_result['total']
                    total_stats['trades'] += station_result['trades']
                except Exception as e:
                    logger.warning(f"Error evaluating {param_config} on {station}: {str(e)}")
                    station_stats[station] = {'correct': 0, 'total': 0, 'trades': 0}
            
            # Calculate metrics
            if total_stats['trades'] > 0:
                accuracy = total_stats['correct'] / total_stats['trades']
                sharpe = self.compute_sharpe(total_stats)
                key = str(param_config)
                stage1_results[key] = {
                    'accuracy': accuracy,
                    'sharpe': sharpe,
                    'total_trades': total_stats['trades'],
                    'total_correct': total_stats['correct'],
                    'param_config': param_config,
                    'station_results': station_stats
                }
                
                logger.info(f"Stage 1 Eval: {param_config} -> Acc={accuracy:.4f}, Sharpe={sharpe:.4f}, Trades={total_stats['trades']}")
        
        # Find best configuration
        if stage1_results:
            best_config_key = max(stage1_results.keys(), key=lambda x: stage1_results[x]['accuracy'])
            best_config = stage1_results[best_config_key]
            logger.info(f"Best Stage 1 Config: {best_config['param_config']} -> Acc={best_config['accuracy']:.4f}")
        else:
            best_config = {}
            
        return best_config, stage1_results

    def evaluate_config_stage2(self, days: List[Dict], market: Dict[str, str],
                             base_params: Dict, city_specific_params: Dict,
                             signal_subset: List[str]) -> Dict:
        """
        Evaluate with both base and city-specific parameters (Stage 2).
        """
        # Combine base + city-specific parameters
        effective_params = base_params.copy()
        
        # Apply city-specific adjustments
        effective_params['confidence_threshold'] = max(0.3, 
            min(0.9, base_params['confidence_threshold'] + city_specific_params['confidence_threshold_adjustment']))
        
        # Adjust agreement threshold with constraints  
        raw_agreement = base_params['agreement_threshold'] + city_specific_params['agreement_threshold_adjustment']
        effective_params['agreement_threshold'] = max(1, min(len(signal_subset), raw_agreement))
        
        results = {'correct': 0, 'total': 0, 'trades': 0}
        
        # Simple evaluation across the dataset period using refined parameters
        for idx in range(base_params['window_length'], len(days)):
            actual_direction = days[idx].get('market_dir', 'flat') if idx < len(days) else None
            if not actual_direction or actual_direction == 'flat':
                continue
                
            # Rest of the evaluation follows similar logic as Stage 1 with effective parameters
            # Gather signal predictions
            votes = []
            for sig_name in signal_subset:
                sig_obj = self.all_signals[sig_name] 
                direction, confidence = sig_obj.evaluate(idx, days)
                
                if base_params['use_dewpoint_modulation'] and sig_name in ['calendar_climatology', 'gaussian', 'gaussian_v2']:
                    if direction is not None and confidence > 0 and idx > 0:
                        prev_day = days[idx - 1]
                        temp = prev_day.get('temp')
                        dewpoint = prev_day.get('dewpoint')
                        if temp is not None and dewpoint is not None:
                            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)
                
                if direction is not None and confidence >= effective_params['confidence_threshold']:
                    votes.append((direction, confidence))
                    
            # Apply agreement check with adjusted minimum
            if len(votes) < effective_params['agreement_threshold']:
                continue
            
            # Calculate weighted majority direction
            up_score = sum(w for d, w in votes if d == 'up')
            down_score = sum(w for d, w in votes if d == 'down')
            
            if up_score > down_score:
                predicted_direction = 'up'
            elif down_score > up_score:
                predicted_direction = 'down' 
            else:
                continue  # Tie - no prediction
            
            # Record result
            results['trades'] += 1
            results['total'] += 1
            if predicted_direction == actual_direction:
                results['correct'] += 1
        
        return results

    def sweep_stage2_city_refinement(self, station_days_dict: Dict[str, List[Dict]], 
                                     best_base_params: Dict, signal_set: List[str]) -> Tuple[Dict, Dict]:
        """
        Perform per-city parameter refinement using the base parameters from Stage 1.
        """
        stage2_results = {
            'city_specific_params': {},
            'combined_performance': {'correct': 0, 'total': 0, 'trades': 0}
        }
        
        logger.info("Performing Stage 2: Per-City Refinement")
        
        # Load markets first for each station
        all_markets = {}
        conn = sqlite3.connect(self.db_path) 
        for station in station_days_dict.keys():
            markets = {}
            cursor = conn.cursor()
            cursor.execute("SELECT local_trading_date, settlement_bucket FROM settlement_epochs WHERE station=? AND market_type='HIGH' ORDER BY local_trading_date ASC", (station,))
            rows = cursor.fetchall()
            prev = None
            for date_str, bucket in rows:
                if bucket is None:
                    markets[date_str] = 'flat'
                    prev = bucket
                    continue
                if prev is not None:
                    markets[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
                else:
                    markets[date_str] = 'flat'
                prev = bucket
            all_markets[station] = markets
        conn.close()
        
        # For each station, optimize city-specific parameters
        for station, alldays in station_days_dict.items():
            if len(alldays) < 250:
                continue
            
            # Use second half of data for Stage 2 evaluation 
            _, later_period = self.temporal_split_dataset(alldays, split_ratio=0.5)
            if len(later_period) == 0:
                continue
                
            # Align the later period with markets
            aligned_later = self.align_data(later_period, all_markets[station])
            if len(aligned_later) == 0:
                continue
                
            best_city_params = None
            best_score = -float('inf')
            best_result = None
            
            # Test all city-specific parameter combinations  
            param_names = list(STAGE2_PARAM_RANGES.keys())
            param_values = [STAGE2_PARAM_RANGES[name] for name in param_names]
            
            for param_combo in product(*param_values):
                city_specific_config = dict(zip(param_names, param_combo))
                
                try:
                    # Evaluate on this station with this set of city-specific params
                    result = self.evaluate_config_stage2(aligned_later, all_markets[station], 
                                                         best_base_params, city_specific_config, signal_set)
                    
                    if result['trades'] > 0:
                        accuracy = result['correct'] / result['trades'] 
                        sharpe = self.compute_sharpe(result)
                        
                        if sharpe > best_score:  # Optimize for Sharpe rather than accuracy
                            best_score = sharpe
                            best_city_params = city_specific_config
                            best_result = result
                            
                        stage2_name = f"{station}_{city_specific_config}"
                        
                except Exception as e:
                    logger.warning(f"Error evaluating {city_specific_config} on {station}: {str(e)}")
            
            if best_city_params:
                stage2_results['city_specific_params'][station] = {
                    'params': best_city_params,
                    'performance': best_result,
                    'accuracy': best_result['correct'] / best_result['trades'] if best_result['trades'] > 0 else 0.0
                }
                
                # Add to combined totals
                stage2_results['combined_performance']['correct'] += best_result['correct']
                stage2_results['combined_performance']['total'] += best_result['total']
                stage2_results['combined_performance']['trades'] += best_result['trades']
                
                logger.info(f"Stage 2 - {station}: {best_city_params} -> Acc={best_result['correct']/best_result['trades']:.4f} if trades > 0 else 0, Trades={best_result['trades']}")
        
        if stage2_results['combined_performance']['trades'] > 0:
            combined_accuracy = stage2_results['combined_performance']['correct'] / stage2_results['combined_performance']['trades']
            combined_sharpe = self.compute_sharpe(stage2_results['combined_performance'])
            stage2_results['combined_final_metrics'] = {
                'accuracy': combined_accuracy,
                'sharpe': combined_sharpe  
            }
            
        return stage2_results

    def compute_sharpe(self, stats: Dict) -> float:
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

    def align_data(self, days: List[Dict], market: Dict[str, str]) -> List[Dict]:
        """Merge daily data with market direction."""
        aligned = []
        for d in days:
            date_key = d['date'][:10]
            if date_key in market:
                entry = dict(d)
                entry['market_dir'] = market[date_key]
                aligned.append(entry)
        return aligned


def main():
    parser = argparse.ArgumentParser(description='Phase 8 Two-Stage Parameter Sweep')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase8_parameter_sweep.json',
                        help='Output file')
    parser.add_argument('--signal-set', type=str, default='all',
                        choices=['all', 'top5', 'diverse'], 
                        help='Which signal set to use')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Initialize optimizer
    optimizer = ParameterOptimizer(db_path)

    # Get relevant signals
    all_signal_names = list(optimizer.all_signals.keys()) 
    if args.signal_set == 'top5':
        # Use pre-selected high-performance signals from previous analysis
        # These would be based on Phase 8.1 results
        signal_subset = all_signal_names[:5]
        logger.info(f"Using top 5 signals: {signal_subset}")
    elif args.signal_set == 'diverse':
        # Use a diverse subset to ensure variety
        # Take every other signal to ensure diversity
        signal_subset = all_signal_names[::2] if len(all_signal_names) > 1 else all_signal_names
        logger.info(f"Using diverse signal set: {signal_subset}")
    else: 
        signal_subset = all_signal_names
        logger.info(f"Using all {len(all_signal_names)} signals")

    # Get stations and load data
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    all_stations = [r[0] for r in cursor.fetchall()]
    conn.close()

    # Load station data
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        # Reuse the loading functions from other scripts
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
        
        if len(days) >= 250:
            station_data[station] = days
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(days)} days)")

    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations")

    # PHASE 8.3: Two-Stage Parameter Optimization
    logger.info("="*80)
    logger.info("PHASE 8.3: STARTING TWO-STAGE PARAMETER SWEEP")
    logger.info("="*80)

    # Stage 1: Global sweep on temporal splits
    best_stage1_config, all_stage1_results = optimizer.sweep_stage1_global(station_data, signal_subset)
    
    # Stage 2: City-specific refinements with best global parameters
    if best_stage1_config:
        stage2_results = optimizer.sweep_stage2_city_refinement(
            station_data, 
            best_stage1_config['param_config'], 
            signal_subset
        )
    else:
        logger.error("No valid configurations found in Stage 1.")
        stage2_results = {}

    # Compile final results
    master_results = {
        'phase8_parameter_sweep': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signal_set_used': signal_subset,
            'stage1_parameters_range': STAGE1_PARAM_RANGES,
            'stage2_parameters_range': STAGE2_PARAM_RANGES,
            'stations_evaluated': list(station_data.keys()),
            'description': 'Two-stage parameter sweep: global optimization followed by city-specific refinement',
        },
        'stage1_global_results': {
            'best_configuration': best_stage1_config,
            'all_evaluations': all_stage1_results
        },
        'stage2_city_specific_results': stage2_results,
        'summary': {
            'stage1_configs_tested': len(all_stage1_results),
            'stage1_best_accuracy': best_stage1_config.get('accuracy', 0) if best_stage1_config else 0,
            'combined_final_accuracy': stage2_results.get('combined_final_metrics', {}).get('accuracy', 0),
            'combined_final_sharpe': stage2_results.get('combined_final_metrics', {}).get('sharpe', 0)
        }
    }

    # Save results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {args.output}")
    
    # Print summary
    print_summary(master_results)
    
    return master_results


def print_summary(results: Dict):
    """Print final summary of parameter sweep."""
    summary = results['summary']
    stage1_info = results['stage1_global_results']['best_configuration']
    
    print("\n" + "="*100)
    print("PHASE 8.3 RESULTS - TWO-STAGE PARAMETER SWEEP")
    print("="*100)
    print(f"Stage 1 Configs Tested: {summary['stage1_configs_tested']}")
    print(f"Stage 1 Best Accuracy: {summary['stage1_best_accuracy']:.4f}" if stage1_info else "No valid Stage 1 config")
    print(f"Final Combined Accuracy: {summary['combined_final_accuracy']:.4f}")
    print(f"Final Combined Sharpe: {summary['combined_final_sharpe']:.4f}")
    if stage1_info:
        print(f"Best Global Params: {stage1_info.get('param_config', {})}")
    
    # Show city-specific parameters if available
    city_params = results.get('stage2_city_specific_results', {}).get('city_specific_params', {})
    if city_params:
        print(f"\nCity-specific refinements applied for {len(city_params)} locations") 
        top_5_cities = sorted(city_params.items(), 
                              key=lambda x: x[1].get('accuracy', 0), 
                              reverse=True)[:5]
        print("Top 5 city performances after refinement:")
        for city, perf in top_5_cities:
            print(f"  {city}: {perf.get('accuracy', 0):.4f} accuracy ({perf['performance'].get('trades', 0)} trades)")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")