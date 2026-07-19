#!/usr/bin/env python3
"""
PHASE 6 — TASK 6.3: Re-run Full Calibration Pipeline

Re-runs the calibration pipeline with the cleaned 7-signal set and Phase 0.5 fixes:
  - window_start enforcement in walk-forward
  - no-change zone 0.01
  - max_history cap
  - Fixed isotonic regression with min 200 samples per cell

Generates calibrated_pipeline_v3.pkl

Usage:
    python3 scripts/phase6_calibration_pipeline.py [--db-path ...] [--output ...]
"""

import sqlite3
import os
import sys
import argparse
import pickle
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import SignalRegistry
from core.calibration_pipeline import CalibrationPipeline
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PHASE6_SIGNALS = [
    'calendar_climatology', 'gaussian', 'goldilocks',
    'pressure_delta', 'wind_direction_shift', 'regime',
    'forecast_disagreement',
]
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian'}

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']


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
    """Load settlement epoch directions."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = ?
        ORDER BY local_trading_date ASC
    """, (station, market_type))
    rows = cur.fetchall()
    market = {}
    prev = None
    for date_str, bucket in rows:
        if bucket is None:
            market[date_str] = 'flat'
            prev = bucket; continue
        if prev is not None:
            market[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
        else:
            market[date_str] = 'flat'
        prev = bucket
    return market


def align_data(days: List[dict], market: Dict[str, str]) -> List[dict]:
    """Merge daily METAR data with settlement direction."""
    market_by_date = {}
    for row in market:
        market_by_date[row] = market[row] if isinstance(market, dict) else row['market_dir']
    aligned = []
    for d in days:
        date = d['date']
        dir_key = date[:10] if len(date) > 10 else date
        if dir_key in market_by_date:
            entry = dict(d)
            entry['market_dir'] = market_by_date[dir_key]
            aligned.append(entry)
    return aligned


def evaluate_signal(signal_obj, signal_name, idx, days):
    """Evaluate signal and apply dewpoint modulator for relevant signals."""
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp') if idx - 1 < len(days) else None
        dewpoint = days[idx - 1].get('dewpoint') if idx - 1 < len(days) else None
        confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)
    return direction, confidence


def main():
    parser = argparse.ArgumentParser(description='Phase 6 Calibration Pipeline')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/calibrated_pipeline_v3.pkl',
                        help='Output path for serialized CalibrationPipeline')
    parser.add_argument('--market-type', type=str, default='HIGH')
    parser.add_argument('--station', type=str, default=None)
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Load signals
    registry = SignalRegistry(db_path)
    all_signals = registry.get_all_signals()
    active_signals = {n: all_signals[n] for n in PHASE6_SIGNALS if n in all_signals}
    logger.info(f"Loaded {len(active_signals)} Phase 6 signals: {list(active_signals.keys())}")

    # Get stations
    conn = sqlite3.connect(db_path)
    if args.station:
        stations = [args.station.upper()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r[0] for r in cur.fetchall()]
    conn.close()
    logger.info(f"Processing {len(stations)} stations: {stations}")

    # Initialize pipeline with Phase 0.5 fixes
    signal_names = list(active_signals.keys())
    cp = CalibrationPipeline(
        signal_names, stations,
        max_history=2000, window_start=0
    )

    total_observations = 0
    per_city_stats = {}

    for station in stations:
        logger.info(f"\n{'='*50}")
        logger.info(f"Processing: {station}")
        
        conn = sqlite3.connect(db_path)
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
        station_days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]): continue
            station_days.append({
                'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
            })
        
        market = load_market_directions(conn, station, args.market_type)
        conn.close()

        if len(station_days) < 250:
            logger.info(f"  Skipping: {len(station_days)} days < 250")
            continue

        aligned = align_data(station_days, market)
        if len(aligned) < 50:
            logger.info(f"  Skipping: {len(aligned)} aligned < 50")
            continue

        logger.info(f"  Days: {len(aligned)}, market entries: {len(market)}")
        
        signal_trades_count = {}
        
        for signal_name, signal_obj in active_signals.items():
            min_lb = signal_obj.min_lookback
            signal_trades = 0
            
            for idx in range(min_lb, len(aligned)):
                direction, confidence = evaluate_signal(signal_obj, signal_name, idx, aligned)
                if direction is None or confidence <= 0:
                    continue
                
                actual = aligned[idx]['market_dir']
                if actual == 'flat':
                    continue
                
                # Normalize z-score based confidences to [0,1]
                norm_conf = min(1.0, confidence / 3.0) if confidence > 1.0 else confidence
                was_correct = direction == actual
                
                # Update calibration pipeline (Phase 0.5: auto-prune on window_start)
                cp.update(signal_name, station, norm_conf, was_correct)
                signal_trades += 1
                total_observations += 1
            
            signal_trades_count[signal_name] = signal_trades
            logger.info(f"  {signal_name:>25s}: {signal_trades:>6d} trades")
        
        per_city_stats[station] = {
            'total_days': len(aligned),
            'signal_trades': signal_trades_count,
        }

    logger.info(f"\n{'='*50}")
    logger.info(f"Total observations: {total_observations}")
    
    # Fit calibrators with Phase 0.5: window_start = 0, no-change zone = 0.01
    logger.info("\nFitting calibrators (Phase 0.5: max_history=2000, no-change=0.01)...")
    cp.refit()

    # Save
    output_path = args.output
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as f:
        pickle.dump(cp, f)
    logger.info(f"\nCalibrated pipeline saved to {output_path}")

    # Report
    print(f"\n{'='*60}")
    print("PHASE 6 — CALIBRATION PIPELINE SUMMARY")
    print(f"{'='*60}")
    print(f"Per-signal-per-city calibrators fitted: {len(cp.calibrators)}")
    print(f"Global signal fallbacks: {len(cp.fallback_calibrators)}")
    for sig_name in signal_names:
        if sig_name in cp.fallback_calibrators:
            print(f"  - {sig_name}: fallback fitted")
    print(f"Global calibrator: {'fitted' if cp.global_calibrator is not None else 'not fitted'}")
    print(f"Total history entries: {sum(len(v) for v in cp.history.values())}")
    print(f"Max history cap: {cp.max_history}")
    print(f"No-change zone: 0.01")

    # Per-city coverage
    print(f"\n--- Per-City Coverage ---")
    print(f"{'Station':<8} {'Days':>6} {'Signals':>30}")
    print("-"*44)
    for station in stations:
        if station in per_city_stats:
            stats = per_city_stats[station]
            sig_str = ', '.join(f"{k}:{v}" for k, v in sorted(stats['signal_trades'].items()))
            print(f"{station:<8} {stats['total_days']:>6}  {sig_str}")
        else:
            print(f"{station:<8} {'skipped':>6}")

    # Fallback usage
    print(f"\n--- Fallback Usage ---")
    print(f"Per-city calibrators (fitted): {len(cp.calibrators)}")
    print(f"Calibrator keys (sample): {list(cp.calibrators.keys())[:10]}")

    # Demo calibration
    print(f"\n--- Sample Calibrations (gaussian @ KNYC) ---")
    for raw_conf in [0.3, 0.5, 0.7, 0.9, 1.0]:
        calibrated = cp.calibrate('gaussian', 'KNYC', raw_conf)
        print(f"  raw={raw_conf:.2f} → calibrated={calibrated:.3f}")

    return cp


if __name__ == '__main__':
    start = time.time()
    cp = main()
    elapsed = time.time() - start
    logger.info(f"Runtime: {elapsed:.1f}s")