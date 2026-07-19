#!/usr/bin/env python3
"""
Calibration Pipeline Wrapper — wires CalibrationPipeline into the 8-signal registry.

For each (signal, city) pair: runs historical backtest, collects (raw_conf, was_correct)
observations, then fits isotonic calibrators with hierarchical fallback.

Usage:
    python3 scripts/run_calibration_pipeline.py --db-path data/metar_backfill.db
    python3 scripts/run_calibration_pipeline.py --station KNYC  # single station for testing
"""

import sqlite3
import os
import sys
import argparse
import pickle
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals import SignalRegistry
from core.calibration_pipeline import CalibrationPipeline


def load_daily_data(conn: sqlite3.Connection) -> List[dict]:
    """Load daily aggregated METAR data ordered by date."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """)
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
        })
    return days


def load_market_directions(conn: sqlite3.Connection, station: str, market_type: str = 'HIGH') -> Dict[str, str]:
    """Load settlement epoch directions as ground truth.

    Returns dict of date -> 'up'/'down'/'flat' for the given market type.
    Direction is: current day settlement vs previous day settlement.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = ?
        ORDER BY local_trading_date ASC
    """, (station, market_type))
    
    rows = cur.fetchall()
    market = {}
    prev_bucket = None
    for date_str, bucket in rows:
        if bucket is None:
            market[date_str] = 'flat'
            prev_bucket = bucket
            continue
        if prev_bucket is not None:
            if bucket > prev_bucket:
                market[date_str] = 'up'
            elif bucket < prev_bucket:
                market[date_str] = 'down'
            else:
                market[date_str] = 'flat'
        else:
            market[date_str] = 'flat'  # first day, no direction
        prev_bucket = bucket
    return market


def align_data(days: List[dict], market: Dict[str, str]) -> List[dict]:
    """Merge daily METAR data with settlement direction."""
    market_by_date = {}
    for row in market:
        # Convert market key to date_utc format used in days
        market_by_date[row] = market[row] if isinstance(market, dict) else row['market_dir']
    
    aligned = []
    for d in days:
        date = d['date']
        # Try both date formats (with/without time)
        dir_key = date[:10] if len(date) > 10 else date
        if dir_key in market_by_date:
            entry = dict(d)
            entry['market_dir'] = market_by_date[dir_key]
            aligned.append(entry)
    return aligned


def main():
    parser = argparse.ArgumentParser(description='Calibrate all weather engine signals')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR SQLite database')
    parser.add_argument('--station', type=str, default=None,
                        help='Single station to test (default: all)')
    parser.add_argument('--output', type=str, default='data/calibrated_pipeline.pkl',
                        help='Output path for serialized CalibrationPipeline')
    parser.add_argument('--market-type', type=str, default='HIGH',
                        help='Market type to calibrate against (HIGH/LOW)')
    parser.add_argument('--min-days', type=int, default=250,
                        help='Minimum days of data required for a station')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    # Initialize signal registry
    registry = SignalRegistry(db_path)
    all_signals = registry.get_all_signals()
    
    # Desired signals: exclude DB-based (wind_direction_shift, nwp_analog) that don't implement evaluate(idx, days)
    desired_signals = [
        'persistence', 'simple_trend', 'gaussian', 'gaussian_v2',
        'pressure_delta', 'goldilocks'
    ]
    active_signals = {k: v for k, v in all_signals.items() if k in desired_signals}
    print(f"Loaded {len(active_signals)} signals: {list(active_signals.keys())}")
    
    # Get stations
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if args.station:
        all_stations = [args.station.upper()]
    else:
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        all_stations = [r[0] for r in cur.fetchall()]
    conn.close()
    
    print(f"Testing on {len(all_stations)} stations: {all_stations}")
    
    # Initialize calibration pipeline
    signal_names = list(active_signals.keys())
    cp = CalibrationPipeline(signal_names, all_stations)
    
    # Per signal/station tracking
    total_observations = 0
    
    for station in all_stations:
        print(f"\n{'='*60}")
        print(f"Processing station: {station}")
        print(f"{'='*60}")
        
        conn = sqlite3.connect(db_path)
        days = load_daily_data(conn)
        # Filter for this station
        local_cur = conn.cursor()
        local_cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                   AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                   AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                   AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
            GROUP BY date_utc ORDER BY date_utc ASC
        """, (station,))
        station_days = []
        for r in local_cur.fetchall():
            if any(v is None for v in r[1:]):
                continue
            station_days.append({
                'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
            })
        
        market = load_market_directions(conn, station, args.market_type)
        conn.close()
        
        if len(station_days) < args.min_days:
            print(f"  Skipping: insufficient data ({len(station_days)} days)")
            continue
        
        aligned = align_data(station_days, market)
        if len(aligned) < 50:
            print(f"  Skipping: insufficient aligned data ({len(aligned)} days)")
            continue
        
        print(f"  Days: {len(aligned)}, market entries: {len(market)}")
        
        # Evaluate each signal on each day
        for signal_name, signal_obj in active_signals.items():
            min_lb = signal_obj.min_lookback
            signal_trades = 0
            
            for idx in range(min_lb, len(aligned)):
                direction, confidence = signal_obj.evaluate(idx, aligned)
                
                if direction is None or confidence <= 0:
                    continue
                
                # Ground truth
                actual = aligned[idx]['market_dir']
                if actual == 'flat':
                    continue
                
                # Normalize z-score based confidences to [0,1] before calibration
                # Gaussian/gaussian_v2/goldilocks return abs(z_score) which can be 1000+
                # Cap at 3.0 (3 sigma) and normalize
                norm_conf = min(1.0, confidence / 3.0) if confidence > 1.0 else confidence
                was_correct = direction == actual
                cp.update(signal_name, station, norm_conf, was_correct)
                signal_trades += 1
                total_observations += 1
            
            print(f"  {signal_name:>20}: {signal_trades:>6} trades")
    
    print(f"\n{'='*60}")
    print(f"Total observations collected: {total_observations}")
    print(f"{'='*60}")
    
    # Fit calibrators
    print("\nFitting calibrators...")
    cp.refit()
    
    # Save calibrated pipeline
    output_path = args.output
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as f:
        pickle.dump(cp, f)
    print(f"\nCalibrated pipeline saved to {output_path}")
    
    # Print calibration summary
    print(f"\n{'='*60}")
    print(f"Calibration Summary")
    print(f"{'='*60}")
    print(f"Per-signal-per-city calibrators: {len(cp.calibrators)}")
    print(f"Global signal fallbacks: {len(cp.fallback_calibrators)}")
    print(f"Global calibrator: {'fitted' if cp.global_calibrator is not None else 'not fitted (insufficient data)'}")
    print(f"Total history entries: {sum(len(v) for v in cp.history.values())}")
    
    # Demo calibration for a test sample
    print(f"\n{'='*60}")
    print(f"Sample Calibrations (gaussian@KNYC)")
    print(f"{'='*60}")
    for raw_conf in [0.3, 0.5, 0.7, 0.9, 1.0]:
        calibrated = cp.calibrate('gaussian', 'KNYC', raw_conf)
        print(f"  raw={raw_conf:.2f} → calibrated={calibrated:.3f}")


if __name__ == '__main__':
    main()