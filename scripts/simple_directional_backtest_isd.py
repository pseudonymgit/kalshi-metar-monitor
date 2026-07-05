#!/usr/bin/env python3
"""
WAVE 1: Simple directional backtest - ISD-lite to market data.

Maps ISD-lite USAF codes to market station names and computes directional accuracy.
"""

import sqlite3
import os
import sys
from collections import defaultdict

# Database paths
ISD_LITE_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/isd_lite_raw.db"
MARKET_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# USAF to station name mapping
USAF_TO_STATION = {
    '010010': 'KNYC',  # New York
    '012530': 'KLAX',  # Los Angeles
    '012570': 'KMDW',  # Chicago
    '014830': 'KDCA',  # Washington DC
}

# Which USAF codes to process
USAF_CODES = list(USAF_TO_STATION.keys())


def get_daily_high_low_from_market_db(station, conn):
    """Get daily high and low bucket values for a station from settlement_epochs."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, market_type, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type IN ('HIGH', 'LOW') AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC, market_type ASC
    """, (station,))
    
    daily_data = defaultdict(dict)
    for row in cur.fetchall():
        date, mt, bucket, prior = row
        if prior is not None:
            direction = 'up' if bucket > prior else ('down' if bucket < prior else 'flat')
        else:
            direction = 'flat'
        
        daily_data[date][mt] = {
            'bucket': bucket,
            'prior': prior,
            'direction': direction
        }
    
    return daily_data


def get_daily_temp_trend_from_isd_lite(usaf_code, conn):
    """Get daily temperature trend from ISD-lite data."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
        FROM isd_lite_raw
        WHERE usaf = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (usaf_code,))
    
    daily_temps = {}
    prev_high = None
    
    for row in cur.fetchall():
        date, high, low = row
        daily_temps[date] = {'high': high, 'low': low}
        
        if prev_high is not None:
            trend = 'up' if high > prev_high else ('down' if high < prev_high else 'flat')
            daily_temps[date]['trend'] = trend
        else:
            daily_temps[date]['trend'] = 'flat'
        
        prev_high = high
    
    return daily_temps


def run_simple_backtest():
    """Run simple directional backtest."""
    print("=" * 80)
    print("SIMPLE DIRECTIONAL BACKTEST - WAVE 1")
    print("=" * 80)
    print("Using ISD-lite temperature data to predict market direction.")
    print()
    
    isd_conn = sqlite3.connect(ISD_LITE_DB)
    market_conn = sqlite3.connect(MARKET_DB)
    
    print(f"Found {len(USAF_CODES)} stations to process")
    print()
    
    station_results = {}
    
    for usaf_code in USAF_CODES:
        station_name = USAF_TO_STATION[usaf_code]
        print(f"Processing {station_name} (USAF: {usaf_code})...")
        
        market_data = get_daily_high_low_from_market_db(station_name, market_conn)
        temp_data = get_daily_temp_trend_from_isd_lite(usaf_code, isd_conn)
        
        # Align dates
        common_dates = set(market_data.keys()) & set(temp_data.keys())
        
        if len(common_dates) < 2:
            print(f"  Not enough data ({len(common_dates)} dates). Skipping.")
            continue
        
        print(f"  Found {len(common_dates)} common dates")
        
        # Compare yesterday's trend with today's market direction
        correct = 0
        total = 0
        
        for date in sorted(common_dates):
            if date not in temp_data or date not in market_data:
                continue
            
            trend = temp_data[date]['trend']
            market_info = market_data[date]
            
            # Check HIGH market
            if 'HIGH' in market_info:
                market_dir = market_info['HIGH']['direction']
                if trend == market_dir and trend != 'flat':
                    correct += 1
                if trend != 'flat':
                    total += 1
            
            # Check LOW market
            if 'LOW' in market_info:
                market_dir = market_info['LOW']['direction']
                if trend == market_dir and trend != 'flat':
                    correct += 1
                if trend != 'flat':
                    total += 1
        
        accuracy = correct / total if total > 0 else 0
        station_results[station_name] = {
            'correct': correct,
            'total': total,
            'accuracy': accuracy
        }
        
        print(f"  {correct}/{total} = {accuracy:.2%}")
    
    isd_conn.close()
    market_conn.close()
    
    # Aggregate
    total_correct = sum(r['correct'] for r in station_results.values())
    total_trades = sum(r['total'] for r in station_results.values())
    overall_accuracy = total_correct / total_trades if total_trades > 0 else 0
    
    # Print results
    print()
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print()
    
    print("OVERALL")
    print("-" * 40)
    print(f"Total predictions: {total_trades}")
    print(f"Correct:           {total_correct}")
    print(f"Accuracy:          {overall_accuracy:.2%}")
    print()
    
    print("STATION BREAKDOWN")
    print("-" * 40)
    print(f"{'Station':<8} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("-" * 36)
    for station in sorted(station_results.keys()):
        r = station_results[station]
        print(f"{station:<8} {r['correct']:>8} {r['total']:>8} {r['accuracy']:>10.2%}")
    
    print()
    print("=" * 80)
    
    # Gray Room thresholds
    DIRECTIONAL_THRESHOLD = 0.58  # 58%
    
    if overall_accuracy >= DIRECTIONAL_THRESHOLD:
        print(f"✓ PASSES THRESHOLD: {overall_accuracy:.2%} >= {DIRECTIONAL_THRESHOLD:.0%}")
        print("RECOMMENDATION: DUMB TREND SIGNAL WORKS")
    else:
        print(f"✗ FAILS THRESHOLD: {overall_accuracy:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
        print("RECOMMENDATION: NEED BETTER SIGNAL OR MORE DATA")
    
    print()
    
    return {
        'overall_accuracy': overall_accuracy,
        'total_predictions': total_trades,
        'total_correct': total_correct,
        'station_results': station_results,
        'threshold': DIRECTIONAL_THRESHOLD,
        'pass': overall_accuracy >= DIRECTIONAL_THRESHOLD
    }


def main():
    """Main entry point."""
    return run_simple_backtest()


if __name__ == "__main__":
    result = main()
