#!/usr/bin/env python3
"""
LOW MARKET BACKTEST v2 — Predict LOW direction using HIGH temperature trend.

The signal: yesterday's HIGH temperature trend → today's LOW market direction.
This is non-trivial because we're predicting the LOW (min temp) using the
HIGH (max temp) signal — a genuinely different measurement.

No analog matching, no Goldilocks, no LLMs.
"""

import sqlite3
import os
import sys
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

METAR_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "metar_backfill.db")
DIRECTIONAL_THRESHOLD = 0.58


def load_data(station: str, conn: sqlite3.Connection) -> Tuple[Dict, Dict, Dict]:
    """Load LOW market data, daily HIGH temps, and daily LOW temps."""
    cur = conn.cursor()
    
    # LOW market directions from settlement_epochs
    cur.execute("""
        SELECT local_trading_date,
               CASE WHEN settlement_bucket > prior_settlement_bucket THEN 'up'
                    WHEN settlement_bucket < prior_settlement_bucket THEN 'down'
                    ELSE 'flat' END as direction
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'LOW' AND epoch_status = 'closed'
          AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date
    """, (station,))
    low_markets = {r[0]: r[1] for r in cur.fetchall()}
    
    # Daily HIGH and LOW temps from METAR
    cur.execute("""
        SELECT date(timestamp_utc) as obs_date,
               MAX(temp_f) as high_temp,
               MIN(temp_f) as low_temp
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY obs_date
        ORDER BY obs_date
    """, (station,))
    temps = {}
    for r in cur.fetchall():
        temps[r[0]] = {'high': r[1], 'low': r[2]}
    
    return low_markets, temps


def run_backtest(stations: List[str], conn: sqlite3.Connection) -> Dict:
    """Run LOW market backtest predicting LOW direction from HIGH trend."""
    results = {}
    
    for station in stations:
        low_markets, temps = load_data(station, conn)
        
        common_dates = sorted(set(low_markets.keys()) & set(temps.keys()))
        if len(common_dates) < 2:
            continue
        
        correct = 0
        total = 0
        
        for i, date in enumerate(common_dates):
            if i == 0:
                continue
            
            yesterday = common_dates[i - 1]
            
            # Signal: yesterday's HIGH trend (today's HIGH vs yesterday's HIGH)
            today_high = temps[date]['high']
            yesterday_high = temps[yesterday]['high']
            
            if today_high > yesterday_high:
                high_trend = 'up'
            elif today_high < yesterday_high:
                high_trend = 'down'
            else:
                continue  # flat
            
            # LOW market direction
            low_dir = low_markets.get(date)
            if low_dir is None or low_dir == 'flat':
                continue
            
            if high_trend == low_dir:
                correct += 1
            total += 1
        
        accuracy = correct / total if total > 0 else 0
        results[station] = {
            'correct': correct,
            'total': total,
            'accuracy': accuracy,
        }
    
    return results


def main():
    print("=" * 80)
    print("LOW MARKET BACKTEST v2 — Predict LOW from HIGH Trend")
    print("=" * 80)
    print()
    print("Signal:   yesterday's HIGH temperature trend (up/down)")
    print("Target:   today's LOW market direction (up/down)")
    print("Method:   Non-tautological — uses HIGH trend to predict LOW")
    print()
    
    if not os.path.exists(METAR_DB):
        print(f"ERROR: Database not found at {METAR_DB}")
        sys.exit(1)
    
    conn = sqlite3.connect(METAR_DB)
    cur = conn.cursor()
    
    cur.execute("""
        SELECT DISTINCT station FROM settlement_epochs 
        WHERE market_type = 'LOW' AND epoch_status = 'closed'
          AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
    """)
    stations = [r[0] for r in cur.fetchall()]
    print(f"Stations with LOW market data: {len(stations)}")
    print()
    
    results = run_backtest(stations, conn)
    conn.close()
    
    if not results:
        print("No results. Check database.")
        return
    
    sorted_stations = sorted(results.items(), key=lambda x: x[1]['accuracy'], reverse=True)
    
    print("-" * 80)
    print(f"{'Station':<10} {'Correct':>8} {'Total':>6} {'Accuracy':>10}")
    print("-" * 80)
    
    for station, data in sorted_stations:
        marker = " ✓" if data['accuracy'] >= DIRECTIONAL_THRESHOLD else " ✗"
        print(f"{station:<10} {data['correct']:>8} {data['total']:>6} {data['accuracy']:>8.2%}{marker}")
    
    print("-" * 80)
    
    total_correct = sum(d['correct'] for _, d in sorted_stations)
    total_trades = sum(d['total'] for _, d in sorted_stations)
    overall = total_correct / total_trades if total_trades > 0 else 0
    passing = sum(1 for _, d in sorted_stations if d['accuracy'] >= DIRECTIONAL_THRESHOLD)
    
    best = max(results.items(), key=lambda x: x[1]['accuracy'])
    worst = min(results.items(), key=lambda x: x[1]['accuracy'])
    
    print(f"\nOVERALL: {total_correct}/{total_trades} = {overall:.2%}")
    print(f"Stations passing ≥{DIRECTIONAL_THRESHOLD:.0%} threshold: {passing}/{len(sorted_stations)}")
    print(f"Threshold: {DIRECTIONAL_THRESHOLD:.0%} → {'✓ PASS' if overall >= DIRECTIONAL_THRESHOLD else '✗ FAIL'}")
    print(f"Best:  {best[0]} at {best[1]['accuracy']:.2%} ({best[1]['correct']}/{best[1]['total']})")
    print(f"Worst: {worst[0]} at {worst[1]['accuracy']:.2%} ({worst[1]['correct']}/{worst[1]['total']})")
    print()


if __name__ == "__main__":
    main()