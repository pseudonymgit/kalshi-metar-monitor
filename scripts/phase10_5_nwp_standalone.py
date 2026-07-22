#!/usr/bin/env python3
"""
STANDALONE NWP ANALOG SIGNAL EVALUATION (Phase 10.5)

Evaluate the NWP Analog signal performance using the existing, correct 
NwpAnalogSignal class on ALL available data, ALL 20 stations.

Requirements:
1. Evaluate ALL 20 stations, ALL available dates in the NWP DB (not just 5 stations)
2. For each station, for each date where NWP data exists:
   - Call NwpAnalogSignal.evaluate_nwp_analog(station, date)
   - Compare against METAR ground truth (next-day HIGH direction)
   - Record accuracy, confidence, trade count
3. Output: data/phase10_5_nwp_results.json with per-station and overall accuracy
4. Must use the CORRECT ground truth: compare prediction against actual 
   settlement direction (up/down for next day's HIGH)
5. Include confidence distribution analysis (how many predictions have 
   confidence > 0.55, > 0.60, > 0.65, etc.)

Previous broken script only used 5 stations randomly.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# Add the core directory to the Python path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the necessary modules
from core.signals.nwp_analog_signal import NwpAnalogSignal
from core.sqlite_utils import get_sqlite_connection

def get_available_nwp_dates_stations():
    """Get all available station-date combinations from the NWP database."""
    print("Fetching available NWP data...")
    
    db_path = Path(__file__).parent.parent / "data" / "nwp_forecasts.db"
    metar_db_path = Path(__file__).parent.parent / "data" / "metar_backfill.db"
    
    if not db_path.exists():
        raise FileNotFoundError(f"NWP database not found: {db_path}")
    
    conn = get_sqlite_connection(str(db_path))
    c = conn.cursor()
    
    # Get distinct station-date combinations from NWP data
    c.execute("""
        SELECT DISTINCT target_date, station
        FROM nwp_forecasts
        ORDER BY station, target_date
    """)
    
    forecast_dates_stations = c.fetchall()
    conn.close()
    
    print(f"Found {len(forecast_dates_stations)} unique station-date combinations in NWP data")
    
    # Filter based on which have actual METAR settlement data for the next day
    # to ensure we have ground truth labels for tomorrow's market direction
    metar_conn = get_sqlite_connection(str(metar_db_path))
    metar_c = metar_conn.cursor()
    
    results = []
    
    for target_date_str, station in forecast_dates_stations:
        # Check if there's settlement data for the next day at this station
        # (since NWP forecasts predict tomorrow's temperature direction)
        metar_c.execute("""
            SELECT settlement_bucket, prior_settlement_bucket 
            FROM settlement_epochs 
            WHERE station = ? AND local_trading_date = date(?, '+1 day')
            AND epoch_status = 'closed' AND settlement_bucket IS NOT NULL
            AND prior_settlement_bucket IS NOT NULL
        """, (station, target_date_str))
        
        settlement_data = metar_c.fetchone()
        if settlement_data:
            next_day_temp_change_direction = 1 if settlement_data[0] > settlement_data[1] else -1
            results.append((target_date_str, station, next_day_temp_change_direction))
    
    metar_conn.close()
    
    print(f"Found {len(results)} pairs with valid ground truth data")
    return results

def evaluate_nwp_analog_performance():
    """Evaluate NWP Analog signal on ALL available data."""
    
    # Create output directory if it doesn't exist
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Get the pairs to evaluate
    forecast_pairs = get_available_nwp_dates_stations()
    
    if not forecast_pairs:
        print("No evaluable station-date pairs found!")
        return
    
    print(f"Evaluating on {len(forecast_pairs)} station-date combinations...")
    
    # Initialize the NWP Analog Signal
    nwp_signal = NwpAnalogSignal()
    
    # Tracking variables
    all_predictions = []
    station_results = defaultdict(list)
    total_evaluated = 0
    total_correct = 0
    confidence_buckets = defaultdict(int)  # Track confidence distribution
    
    # Loop through each pair and evaluate
    for i, (target_date_str, station, expected_direction) in enumerate(forecast_pairs):
        if i % 100 == 0:
            print(f"Processing {i}/{len(forecast_pairs)}...")
        
        try:
            # Get analog signal result
            direction, confidence = nwp_signal.evaluate_nwp_analog(station, target_date_str)
            
            if direction is not None and confidence is not None:
                # Convert direction string ('up'/'down') to integer (1/-1)
                predicted_direction = 1 if direction == 'up' else -1
                
                # Calculate if prediction was correct
                is_correct = predicted_direction == expected_direction
                total_correct += 1 if is_correct else 0
                total_evaluated += 1
                
                # Log this prediction
                prediction_record = {
                    'forecast_date': target_date_str,
                    'station': station,
                    'predicted_direction': predicted_direction,
                    'expected_direction': expected_direction,
                    'confidence': confidence,
                    'is_correct': is_correct
                }
                all_predictions.append(prediction_record)
                
                # Store per-station results
                station_results[station].append({
                    'forecast_date': target_date_str,
                    'predicted_direction': predicted_direction,
                    'expected_direction': expected_direction,
                    'confidence': confidence,
                    'is_correct': is_correct
                })
                
                # Track confidence distribution
                confidence_buckets[round(confidence, 2)] += 1
                
        except Exception as e:
            print(f"Error evaluating forecast {target_date_str} for {station}: {e}")
            continue
    
    # Calculate overall and per-station performance
    overall_accuracy = total_correct / total_evaluated if total_evaluated > 0 else 0.0
    print(f"Overall performance: {total_correct}/{total_evaluated} = {overall_accuracy:.3f}")
    
    # Prepare station-specific results
    station_summary = {}
    for station, records in station_results.items():
        correct = sum(1 for r in records if r['is_correct'])
        count = len(records)
        accuracy = correct / count if count > 0 else 0.0
        avg_confidence = sum(r['confidence'] for r in records) / count if count > 0 else 0.0
        station_summary[station] = {
            'accuracy': accuracy,
            'count': count,
            'correct': correct,
            'avg_confidence': avg_confidence
        }
    
    # Prepare confidence distribution analysis
    confidence_levels = {
        'above_50': 0,
        'above_55': 0,
        'above_60': 0,
        'above_65': 0,
        'above_70': 0
    }
    
    total_preds = len(all_predictions)
    for pred in all_predictions:
        conf = pred['confidence']
        if conf >= 0.50:
            confidence_levels['above_50'] += 1
        if conf >= 0.55:
            confidence_levels['above_55'] += 1
        if conf >= 0.60:
            confidence_levels['above_60'] += 1
        if conf >= 0.65:
            confidence_levels['above_65'] += 1
        if conf >= 0.70:
            confidence_levels['above_70'] += 1
    
    # Prepare results dictionary
    results = {
        'evaluation_timestamp': datetime.utcnow().isoformat(),
        'total_evaluated': total_evaluated,
        'total_correct': total_correct,
        'overall_accuracy': overall_accuracy,
        'predictions_list': all_predictions,  # Full detail of all predictions
        'station_results': station_summary,
        'confidence_analysis': {
            'total_predictions': len(all_predictions),
            'confidence_distribution_raw': dict(confidence_buckets),
            'confidence_threshold_summary': {
                'fraction_above_50': confidence_levels['above_50'] / total_preds if total_preds > 0 else 0,
                'fraction_above_55': confidence_levels['above_55'] / total_preds if total_preds > 0 else 0,
                'fraction_above_60': confidence_levels['above_60'] / total_preds if total_preds > 0 else 0,
                'fraction_above_65': confidence_levels['above_65'] / total_preds if total_preds > 0 else 0,
                'fraction_above_70': confidence_levels['above_70'] / total_preds if total_preds > 0 else 0
            }
        },
        'stations_used': sorted(list(station_summary.keys())),
        'notes': 'Evaluates next-day HIGH temperature direction prediction accuracy'
    }
    
    # Write to output file
    output_file = output_dir / "phase10_5_nwp_results.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults written to {output_file}")
    print(f"Total evaluated: {total_evaluated}")
    print(f"Correct: {total_correct}")
    print(f"Overall Accuracy: {overall_accuracy:.3f}")
    print(f"Stations covered: {len(station_summary.keys())}")
    
    return results

if __name__ == "__main__":
    if not os.environ.get('NWP_ANALOG_ENABLED'):
        os.environ['NWP_ANALOG_ENABLED'] = '1'  # Enable the NWP analog feature
        
    evaluate_nwp_analog_performance()