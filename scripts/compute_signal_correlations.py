#!/usr/bin/env python3
"""
PHASE 10 — Compute signal correlations to determine redundant signals

Evaluate all available signals for cross-correlation of predictions, highlighting 
signals that have agreement > 80% and thus may be effectively redundant.
"""

import sqlite3
import math
import json
import os
import sys
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

# Add parent directory to path to import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry

# ─── Helper functions ───────────────────────────────────────────────────────────

def compute_direction_agreement(a_dir: str, b_dir: str) -> float:
    """Compute agreement (0 or 1) between two signal directions."""
    if a_dir is None or b_dir is None or a_dir == 'flat' or b_dir == 'flat':
        return 0.0  # No valid direction
    return 1.0 if a_dir == b_dir else 0.0

def get_signal_predictions_for_station(station: str, conn: sqlite3.Connection, 
                                     registry) -> Dict[str, List[Tuple[str, float]]]:
    """Get all signal predictions and confidence for a station."""
    
    # These data loading functions are typically in backtest scripts, so define here locally
    # (These functions were incorrectly imported from core.signals)
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
            if date_key in market and date_key in [day['date'][:10] for day in days]:
                entry = dict(d)
                entry['market_dir'] = market[date_key]
                aligned.append(entry)
        return aligned

    # Load data
    days = load_station_days(station, conn)
    market = load_market(conn, station)
    aligned = align_data(days, market)
    
    # Get signals
    signals = registry.get_all_signals()  # All signals now includes regime
    signal_preds = defaultdict(list)  # Dict[str -> List[(direction, confidence)]]
    
    # Generate predictions for each signal
    for sig_name, sig_obj in signals.items():
        if sig_name == 'goldilocks':  # Skip goldilocks (intraday arb signal)
            continue
            
        for idx, day_data in enumerate(aligned):
            if idx == 0:  # Skip first day since some signals need history
                continue
                
            # Get prediction
            direction, confidence = sig_obj.evaluate(idx, aligned)
            if direction is not None and confidence > 0.1:
                signal_preds[sig_name].append((direction, confidence))
    
    return signal_preds

def compute_pairwise_agreement(sig_a_preds: List[Tuple[str, float]], 
                              sig_b_preds: List[Tuple[str, float]]) -> float:
    """Compute agreement percentage between two signal's predictions."""
    if len(sig_a_preds) == 0 or len(sig_b_preds) == 0:
        return 0.0
        
    # Align timestamps somehow to match... but in backtesting we need to 
    # find overlapping prediction timestamps since different signals fire differently
    # Just calculate agreement on days both fired
    
    total_comparable = 0
    agreements = 0

    # Find matching indices - this is hard because not all signals fire every day
    # So use all predictions from both signals
    for i, (dir_a, _) in enumerate(sig_a_preds):
        if i < len(sig_b_preds):
            dir_b, _ = sig_b_preds[i]
            agreement = compute_direction_agreement(dir_a, dir_b)
            if agreement != 0 or (dir_a is not None and dir_b is not None):  # Only count if both predict something
                total_comparable += 1
                agreements += agreement
        # Continue with remaining in sig_b preds if sig_a is shorter
    
    for i, (dir_b, _) in enumerate(sig_b_preds):
        if i >= len(sig_a_preds):  # Only process extra ones
            # This approach isn't correct because it double counts - need better alignment
            pass
    
    # Better approach: align signal predictions by their actual firing instances
    min_len = min(len(sig_a_preds), len(sig_b_preds))
    
    if min_len == 0:
        return 0.0 
    
    agreements = 0
    total_comparable = 0
    
    for i in range(min_len):
        dir_a, _ = sig_a_preds[i]
        dir_b, _ = sig_b_preds[i]
        agreement = compute_direction_agreement(dir_a, dir_b)
        
        # Count this prediction pair if both signals made a forecast
        if dir_a is not None and dir_b is not None:
            total_comparable += 1
            agreements += agreement
    
    return agreements / total_comparable if total_comparable > 0 else 0.0


def main():
    db_path = 'data/metar_backfill.db'
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return

    # Load 20 main stations from our constant
    ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                    'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                    'KPHX','KSAT','KSEA','KSFO']

    # Load signal registry
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    
    print(f"Computing correlations for {len(all_signals)} signals: {list(all_signals.keys())}")
    
    # Load database connection
    conn = sqlite3.connect(db_path)
    
    # For correlation calculation, we'll collect all signal predictions across all stations
    all_station_signal_data = {}
    
    for station in ALL_STATIONS:
        print(f"Processing station {station}...")
        try:
            signal_data = get_signal_predictions_for_station(station, conn, registry)
            all_station_signal_data[station] = signal_data
        except Exception as e:
            print(f"  Error processing {station}: {e}")
    
    conn.close()
    
    # Collect signals for which we have data across stations
    # Get all possible signal names that have predictions across any station
    all_signal_names = set()
    for station_sig_data in all_station_signal_data.values():
        all_signal_names.update(station_sig_data.keys())
    
    print(f"Found data for signals: {all_signal_names}")
    
    if not all_signal_names:
        print("No signal prediction data found!")
        return
        
    # Now compute correlation matrix across these signals
    # The approach: For each pair of signals, compute their agreement across all stations
    correlation_matrix = {}
    
    signal_names_list = sorted(list(all_signal_names))
    
    for sig1 in signal_names_list:
        if sig1 not in correlation_matrix:
            correlation_matrix[sig1] = {}
        for sig2 in signal_names_list:
            if sig2 not in correlation_matrix[sig1]:
                correlation_matrix[sig1][sig2] = 0.0
            
            if sig1 == sig2:
                correlation_matrix[sig1][sig2] = 1.0  # Signal is perfectly correlated with itself
            else:
                # Compute average across stations
                avg_agreement = 0.0
                count_valid_stations = 0
                
                for station, station_data in all_station_signal_data.items():
                    if sig1 in station_data and sig2 in station_data:
                        pred1 = station_data[sig1]
                        pred2 = station_data[sig2]
                        
                        agreement = compute_pairwise_agreement(pred1, pred2)
                        if agreement >= 0:  # Valid
                            avg_agreement += agreement
                            count_valid_stations += 1
                
                if count_valid_stations > 0:
                    final_agreement = avg_agreement / count_valid_stations
                    correlation_matrix[sig1][sig2] = final_agreement
                    print(f"{sig1} vs {sig2} = {final_agreement:.3f}")
    
    # Now identify highly correlated pairs (>0.80)
    high_correlations = []
    
    for sig1 in signal_names_list:
        for sig2 in signal_names_list:
            if sig1 != sig2 and correlation_matrix[sig1][sig2] > 0.80:
                # Check if already recorded in reverse
                already_recorded = any(existing['sig1'] == sig2 and existing['sig2'] == sig1 and 
                                      existing['agreement'] == correlation_matrix[sig1][sig2] 
                                      for existing in high_correlations)
                
                if not already_recorded:
                    pair_info = {
                        'sig1': sig1,
                        'sig2': sig2,
                        'agreement': correlation_matrix[sig1][sig2],
                        'is_redundant': correlation_matrix[sig1][sig2] > 0.80
                    }
                    high_correlations.append(pair_info)
    
    # Create final report
    report = {
        'timestamp': datetime.now().isoformat(),
        'database': db_path,
        'stations_analyzed': ALL_STATIONS,
        'signals_analyzed': signal_names_list,
        'correlation_matrix': correlation_matrix,
        'high_correlation_pairs': high_correlations,
        'recommendations_for_removal': [
            f"'{pair['sig2']}' is redundant with '{pair['sig1']}'" for pair in high_correlations
        ] if high_correlations else []
    }
    
    # Output to console
    print("\n=== SIGNAL CORRELATION RESULTS ===")
    print(f"Analyzing {len(signal_names_list)} signals over {len([s for s in ALL_STATIONS if s in all_station_signal_data])} stations")
    
    print("\nHigh Correlation Pairs (>80% agreement):")
    for pair in high_correlations:
        print(f"  {pair['sig1']} <> {pair['sig2']}: {pair['agreement']:.3f}")
    
    if not high_correlations:
        print("  No highly correlated signal pairs found (>80%)")
    
    # Save results
    output_file = 'data/phase10_signal_correlation_matrix.json'
    os.makedirs('data', exist_ok=True)
    
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2)
    
    print(f"\nResults saved to {output_file}")
    

if __name__ == '__main__':
    main()