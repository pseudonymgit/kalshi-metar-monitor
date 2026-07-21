#!/usr/bin/env python3
"""
Simple test script to validate that signal evaluation and trading works properly
before scaling up to combinatorial search.
"""

import sqlite3
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.dewpoint_modulator import modify_confidence_by_dewpoint

def validate_data_alignment(db_path: str):
    """Check if metar observation and settlement data can be properly aligned."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Pick a station to test with
    station = 'KATL'
    
    # Get a sample of observations and settlements to check date alignment
    cursor.execute('''
        SELECT mo.date_utc, mo.high, mo.low, se.settlement_bucket
        FROM (
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
            FROM metar_observations
            WHERE station = 'KATL' AND temp_f IS NOT NULL
            GROUP BY date_utc
            LIMIT 100
        ) as mo
        LEFT JOIN settlement_epochs se ON mo.date_utc = se.local_trading_date AND se.station = 'KATL'
        LIMIT 20
    ''')
    
    rows = cursor.fetchall()
    print(f"Date alignment check for {station}:")
    for row in rows:
        date_str, high, low, settlement = row
        print(f"   {date_str} | high: {high:.1f} | low: {low:.1f} | settlement: {settlement}")
    
    conn.close()


def test_single_signal_performance(db_path: str):
    """Test basic performance of a single signal."""
    # Load registry
    registry = create_signal_registry(db_path) 
    print(f"Available signals in registry: {list(registry.get_all_signals().keys())}")
    
    # Try a signal that should generally work
    signal = registry.get_signal('calendar_climatology')  
    
    if not signal:
        print("Calendar climatology signal not available!")
        return
    
    print(f"Testing signal: {signal.name}, min_lookback: {signal.min_lookback}")
    
    # Load test data for a single station
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    station = 'KATL'
    cursor.execute('''
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    ''', (station,))
    
    days = []
    for r in cursor.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
    conn.close()
    
    print(f"Loaded {len(days)} daily records for {station}")
    
    if len(days) < 365:  # Need enough for seasonal climatology 
        print("Insufficient data for calendar climatology signal")
        return
        
    # Load market results for backtesting
    market_conn = sqlite3.connect(db_path)
    cursor = market_conn.cursor()
    cursor.execute('''
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH'
        ORDER BY local_trading_date ASC
    ''', (station,))
    
    market_dict = {}
    prev = None
    for date_str, bucket in cursor.fetchall():
        if bucket is None:
            market_dict[date_str] = 'flat'
            prev = None
        else:
            if prev is not None:
                market_dict[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
            else:
                market_dict[date_str] = 'flat'
            prev = bucket
    market_conn.close()
    
    print(f"Loaded market directions for {len(market_dict)} dates")
    
    # Align weather and market data
    aligned_days = []
    for day_data in days:
        date_key = day_data['date'][:10]  # Extract date part (YYYY-MM-DD)
        if date_key in market_dict:
            entry = dict(day_data)
            entry['market_dir'] = market_dict[date_key]
            aligned_days.append(entry)
    
    print(f"Aligned weather and market data: {len(aligned_days)} records")
    
    # Test the signal performance
    if len(aligned_days) < signal.min_lookback + 10:  # Need enough data + lookback
        print(f"Not enough data for testing: need {signal.min_lookback + 10}, have {len(aligned_days)}")
        return
    
    # Test signal predictions  
    results = {
        'total': 0,
        'trades': 0, 
        'correct': 0,
        'incorrect': 0,
        'skipped': 0
    }
    
    print("Running signal evaluation...")
    
    for idx in range(signal.min_lookback, len(aligned_days)-1):  # -1 to predict on next day
        direction, confidence = signal.evaluate(idx, aligned_days)
        
        if direction is None or confidence == 0:
            results['skipped'] += 1
            continue
            
        actual_direction = aligned_days[idx+1].get('market_dir', 'flat')
        if actual_direction == 'flat':
            results['skipped'] += 1
            continue
            
        results['trades'] += 1
        results['total'] += 1
        
        if direction == actual_direction:
            results['correct'] += 1
        else:
            results['incorrect'] += 1
    
    print("Signal Test Results:")
    print(f"  Total: {results['total']}")
    print(f"  Trades: {results['trades']}")  
    print(f"  Correct: {results['correct']}")
    print(f"  Incorrect: {results['incorrect']}")
    print(f"  Skipped: {results['skipped']}")
    
    if results['trades'] > 0:
        accuracy = results['correct'] / results['trades']
        print(f"  Accuracy: {accuracy:.3f} ({results['correct']}/{results['trades']})")
    else:
        print("  Accuracy: 0.000 (no trades)")


if __name__ == '__main__':
    db_path = 'data/metar_backfill.db'
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)
        
    validate_data_alignment(db_path)
    print("\n")
    test_single_signal_performance(db_path)