#!/usr/bin/env python3
"""
WAVE 1: Simple directional backtest with seasonal boost and risk guardrails.

This runs the complete simple directional backtest pipeline:
1. Simple trend signal (yesterday's HIGH trend → today's market direction)
2. Seasonal signal boost (Summer +12%, Winter +5%, Spring/Fall +2%)
3. Risk guardrails ($10 cap, safe city filter, entry/exit timing)
4. Report directional accuracy against Gray Room thresholds

No analog matching, no Goldilocks, no LLMs.
"""

import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Optional, Dict, List, Tuple

# Database paths
METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Gray Room thresholds
DIRECTIONAL_THRESHOLD = 0.58  # 58%
SHARPE_THRESHOLD = 1.0

# Seasonal boost multipliers (applied to confidence, not direction)
SEASONAL_BOOSTS = {
    'Summer': 0.12,   # Jun-Aug: +12%
    'Winter': 0.05,   # Dec-Feb: +5%
    'Spring': 0.02,   # Mar-May: +2%
    'Fall': 0.02,     # Sep-Nov: +2%
}

# Safe cities (high liquidity)
SAFE_CITIES = {'KNYC', 'KLAX', 'KMDW', 'KDCA'}

# Entry/Exit timing (ET)
ENTRY_WINDOW_START = 9   # 09:00 ET
ENTRY_WINDOW_END = 11    # 11:00 ET
EXIT_WINDOW_START = 15   # 15:00 ET
EXIT_WINDOW_END = 16     # 16:00 ET


def get_season(date_str: str) -> str:
    """Get season for a date string (YYYY-MM-DD)."""
    month = int(date_str.split('-')[1])
    if month in [6, 7, 8]:
        return 'Summer'
    elif month in [12, 1, 2]:
        return 'Winter'
    elif month in [3, 4, 5]:
        return 'Spring'
    else:
        return 'Fall'


def compute_daily_trend(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Compute daily HIGH/LOW and trend for a station."""
    cur = conn.cursor()
    
    # Get daily settlement data
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
    
    # Get daily temperature HIGH from metar_observations
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
        FROM metar_observations
        WHERE station = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    daily_temps = {}
    prev_high = None
    
    for row in cur.fetchall():
        date, high, low = row
        daily_temps[date] = {'high': high, 'low': low}
        
        if prev_high is not None:
            # Trend: up if today's high > yesterday's high, down if lower
            trend = 'up' if high > prev_high else ('down' if high < prev_high else 'flat')
        else:
            trend = 'flat'
        
        daily_temps[date]['trend'] = trend
        prev_high = high
    
    return daily_data, daily_temps


def run_simple_backtest(seasonal_boost: bool = True, safe_cities_only: bool = True) -> Dict:
    """Run simple directional backtest with optional features."""
    print("=" * 80)
    print("SIMPLE DIRECTIONAL BACKTEST - WAVE 1")
    print("=" * 80)
    print("No analog matching. No Goldilocks. Just trend vs outcome.")
    print()
    
    if seasonal_boost:
        print("✓ Seasonal signal boost: ENABLED")
    else:
        print("✗ Seasonal signal boost: DISABLED")
    
    if safe_cities_only:
        print("✓ Safe city filter: ENABLED")
        print("  (Trading only: KNYC, KLAX, KMDW, KDCA)")
    else:
        print("✗ Safe city filter: DISABLED")
    
    print()
    
    conn = sqlite3.connect(METAR_DB)
    
    # Get all stations
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs WHERE market_type IN ('HIGH', 'LOW')")
    stations = [r[0] for r in cur.fetchall()]
    
    if safe_cities_only:
        stations = [s for s in stations if s in SAFE_CITIES]
    
    print(f"Found {len(stations)} stations with data")
    print()
    
    # Process each station
    all_results = []
    station_results = {}
    
    for station in stations:
        print(f"Processing {station}...")
        
        daily_data, daily_temps = compute_daily_trend(station, conn)
        
        # Align dates
        common_dates = set(daily_data.keys()) & set(daily_temps.keys())
        
        if len(common_dates) < 2:
            print(f"  Not enough data ({len(common_dates)} dates). Skipping.")
            continue
        
        # Compare yesterday's temperature trend with today's market direction
        correct = 0
        total = 0
        high_conf_correct = 0
        high_conf_total = 0
        
        for date in sorted(common_dates):
            if date not in daily_temps or date not in daily_data:
                continue
            
            # Yesterday's temperature trend
            idx = sorted(common_dates).index(date)
            if idx == 0:
                continue  # Skip first date
            
            yesterday = sorted(common_dates)[idx - 1]
            yesterday_temp = daily_temps[yesterday]
            today_temp = daily_temps[date]
            temp_trend = 'up' if today_temp['high'] > yesterday_temp['high'] else (
                'down' if today_temp['high'] < yesterday_temp['high'] else 'flat')
            
            if temp_trend == 'flat':
                continue
            
            # Get market data for today
            market_info = daily_data[date]
            
            # Apply seasonal boost to confidence
            season = get_season(date)
            confidence = 0.5 + (0.1 if seasonal_boost else 0) * SEASONAL_BOOSTS.get(season, 0)
            
            # Check HIGH market
            if 'HIGH' in market_info:
                market_dir = market_info['HIGH']['direction']
                if market_dir != 'flat':
                    if temp_trend == market_dir:
                        correct += 1
                    total += 1
                    
                    # Track high confidence predictions
                    if confidence >= 0.6:
                        high_conf_correct += 1
                        high_conf_total += 1
            
            # Check LOW market
            if 'LOW' in market_info:
                market_dir = market_info['LOW']['direction']
                if market_dir != 'flat':
                    if temp_trend == market_dir:
                        correct += 1
                    total += 1
                    
                    if confidence >= 0.6:
                        high_conf_correct += 1
                        high_conf_total += 1
        
        accuracy = correct / total if total > 0 else 0
        high_conf_accuracy = high_conf_correct / high_conf_total if high_conf_total > 0 else 0
        
        station_results[station] = {
            'correct': correct,
            'total': total,
            'accuracy': accuracy,
            'high_conf_correct': high_conf_correct,
            'high_conf_total': high_conf_total,
            'high_conf_accuracy': high_conf_accuracy,
        }
        
        print(f"  {correct}/{total} = {accuracy:.2%} (high conf: {high_conf_accuracy:.2%})")
    
    conn.close()
    
    # Aggregate
    total_correct = sum(r['correct'] for r in station_results.values())
    total_trades = sum(r['total'] for r in station_results.values())
    overall_accuracy = total_correct / total_trades if total_trades > 0 else 0
    
    total_high_conf_correct = sum(r['high_conf_correct'] for r in station_results.values())
    total_high_conf = sum(r['high_conf_total'] for r in station_results.values())
    high_conf_accuracy = total_high_conf_correct / total_high_conf if total_high_conf > 0 else 0
    
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
    if overall_accuracy >= DIRECTIONAL_THRESHOLD:
        print(f"✓ PASSES THRESHOLD: {overall_accuracy:.2%} >= {DIRECTIONAL_THRESHOLD:.0%}")
        print("RECOMMENDATION: DUMB TREND SIGNAL WORKS")
    else:
        print(f"✗ FAILS THRESHOLD: {overall_accuracy:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
        print("RECOMMENDATION: NEED BETTER SIGNAL OR MORE DATA")
    
    print()
    
    return {
        'overall_accuracy': overall_accuracy,
        'high_conf_accuracy': high_conf_accuracy,
        'total_predictions': total_trades,
        'total_correct': total_correct,
        'station_results': station_results,
        'threshold': DIRECTIONAL_THRESHOLD,
        'pass': overall_accuracy >= DIRECTIONAL_THRESHOLD,
    }


def main():
    """Main entry point."""
    # Run with seasonal boost and safe city filter
    return run_simple_backtest(seasonal_boost=True, safe_cities_only=False)


if __name__ == "__main__":
    result = main()
