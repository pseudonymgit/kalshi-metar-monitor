#!/usr/bin/env python3
"""
Simple directional backtest - no analog matching, no Goldilocks.

Computes daily temperature trend from METAR data and compares against
actual HIGH/LOW market outcomes.

Goal: Establish a baseline - can a dumb trend signal beat 58%?
"""

import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime

# Add core module to path
script_dir = os.path.dirname(os.path.abspath(__file__))
workspace_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)


def get_daily_high_low(station, conn):
    """Get daily high and low bucket values for a station from settlement_epochs."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, market_type, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type IN ('HIGH', 'LOW') AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC, market_type ASC
    """, (station,))
    
    # We expect two rows per day: one HIGH, one LOW
    daily_data = defaultdict(dict)
    for row in cur.fetchall():
        date, mt, bucket, prior = row
        daily_data[date][mt] = {
            'bucket': bucket,
            'prior': prior,
            'direction': 'up' if prior is not None and bucket > prior else ('down' if prior is not None and bucket < prior else 'flat')
        }
    
    return daily_data


def get_daily_temp_trend(station, conn):
    """Compute daily temperature trend from METAR observations."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, 
               MAX(temp_f) as high, 
               MIN(temp_f) as low,
               FIRST_VALUE(temp_f) OVER (PARTITION BY date_utc ORDER BY timestamp_utc) as first_temp,
               LAST_VALUE(temp_f) OVER (PARTITION BY date_utc ORDER BY timestamp_utc ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) as last_temp
        FROM metar_observations
        WHERE station = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    # SQLite doesn't support window functions in the same way, so we'll compute manually
    cur.execute("""
        SELECT date_utc, temp_f, timestamp_utc
        FROM metar_observations
        WHERE station = ?
        ORDER BY date_utc ASC, timestamp_utc ASC
    """, (station,))
    
    rows = cur.fetchall()
    
    # Group by date and compute daily high/low
    daily_temps = defaultdict(list)
    for date, temp, ts in rows:
        daily_temps[date].append(temp)
    
    daily_trend = {}
    prev_high = None
    prev_low = None
    
    for date in sorted(daily_temps.keys()):
        temps = daily_temps[date]
        day_high = max(temps)
        day_low = min(temps)
        
        if prev_high is not None:
            # Trend: up if today's high > yesterday's high, down if lower
            trend = 'up' if day_high > prev_high else ('down' if day_high < prev_high else 'flat')
        else:
            trend = 'flat'  # No prior data
        
        daily_trend[date] = {
            'high': day_high,
            'low': day_low,
            'trend': trend
        }
        
        prev_high = day_high
        prev_low = day_low
    
    return daily_trend


def run_simple_backtest(db_path):
    """Run simple directional backtest."""
    print("=" * 80)
    print("SIMPLE DIRECTIONAL BACKTEST")
    print("=" * 80)
    print("No analog matching. No Goldilocks. Just trend vs outcome.")
    print()
    
    conn = sqlite3.connect(db_path)
    
    # Get all stations
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs WHERE market_type IN ('HIGH', 'LOW')")
    stations = [r[0] for r in cur.fetchall()]
    
    print(f"Found {len(stations)} stations with HIGH/LOW markets")
    print()
    
    # Process each station
    all_results = []
    station_results = {}
    
    for station in stations:
        print(f"Processing {station}...")
        
        daily_data = get_daily_high_low(station, conn)
        daily_trend = get_daily_temp_trend(station, conn)
        
        # Align dates
        common_dates = set(daily_data.keys()) & set(daily_trend.keys())
        
        if len(common_dates) < 2:
            print(f"  Not enough data ({len(common_dates)} dates). Skipping.")
            continue
        
        # For each date, compare yesterday's trend with today's market direction
        correct = 0
        total = 0
        
        for date in sorted(common_dates):
            if date not in daily_trend or date not in daily_data:
                continue
            
            trend = daily_trend[date]['trend']
            market_data = daily_data[date]
            
            # Check HIGH market
            if 'HIGH' in market_data:
                market_dir = market_data['HIGH']['direction']
                if trend == market_dir and trend != 'flat':
                    correct += 1
                if trend != 'flat':
                    total += 1
            
            # Check LOW market
            if 'LOW' in market_data:
                market_dir = market_data['LOW']['direction']
                if trend == market_dir and trend != 'flat':
                    correct += 1
                if trend != 'flat':
                    total += 1
        
        accuracy = correct / total if total > 0 else 0
        station_results[station] = {
            'correct': correct,
            'total': total,
            'accuracy': accuracy
        }
        all_results.extend([(station, correct, total, accuracy)] * 1)  # Just tracking
        
        print(f"  {correct}/{total} = {accuracy:.2%}")
    
    conn.close()
    
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
    db_paths = [
        "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db",
    ]
    
    for db_path in db_paths:
        if os.path.exists(db_path):
            # Check if it has data
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM settlement_epochs")
                count = cur.fetchone()[0]
                conn.close()
                if count > 0:
                    print(f"Using database: {db_path} ({count} settlement epochs)")
                    break
            except:
                pass
    else:
        print("ERROR: No valid database found!")
        return None
    
    return run_simple_backtest(db_path)


if __name__ == "__main__":
    result = main()
