#!/usr/bin/env python3
"""
EDGE 2: Time-of-Day Decay — Market Microstructure Signal

Core signal: As settlement approaches, market prices adjust toward certainty.
If 2-4 hours to expiry AND decay rate < −1.8%/hour → TIME_DECAY_SHORT.

Translation: If within 2-4 hours of settlement (end of day), and temperature
certainty develops rapidly during this period (the "settlement value" becomes
clear quickly) at a rate faster than 1.8%/hour, consider it time decay.

However, since we don't have Kalshi market data (just METAR observations),
we'll simulate this concept by looking at temperature certainty in the afternoon.
When temperature patterns suggest "the day's high is becoming very clear very 
quickly," bet against that acceleration (expect time decay in a real market).

For the backtest: Look for afternoon periods (2-4 hours before assumed settlement)
where the high temperature for the day becomes very certain very rapidly based on
the observed temperature evolution pattern.

Walk-forward only. No AI in the loop. No in-sample metrics.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
from datetime import datetime, timedelta

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

TIME_TILL_SETTLE_HOURS = 2  # Look at time window: hours to settlement
RATE_THRESHOLD_PER_HOUR = 1.8  # degrees per hour threshold for our proxy


def load_hourly_data(station, conn):
    """Load hourly temperature data and settlement epochs for a station."""
    cur = conn.cursor()
    
    # Get hourly observations - need to process time series data
    cur.execute("""
        SELECT timestamp_utc, temp_f
        FROM metar_observations
        WHERE station=? AND date_utc >= '2023-01-01' AND temp_f IS NOT NULL
        ORDER BY timestamp_utc ASC
    """, (station,))
    
    # Group hourly observations by date
    hourly_by_date = defaultdict(list)
    for row in cur.fetchall():
        timestamp_str, temp = row
        if temp is not None:
            # Convert full timestamp to date
            # Format is YYYY-MM-DD HH:MM:SS, extract date and hour
            try:
                timestamp_obj = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                date_str = timestamp_obj.strftime('%Y-%m-%d')
                hour = timestamp_obj.hour
                hourly_by_date[date_str].append((hour, temp))
            except:
                continue
    
    # Settlement epochs
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        direction = 'up' if r[1] > r[2] else 'down'
        market[r[0]] = {'direction': direction, 'bucket': r[1], 'prior': r[2]}
    
    return hourly_by_date, market


def time_of_day_decay_signal(day_hourly, day_date):
    """
    Detect time-of-day decay effect within 2-4 hours of settlement.
    
    In this proxy approach: look for periods in the afternoon where temperature
    change rate dramatically increases near end of day, suggesting "certainty 
    formation rate" like rapid market price movements.
    
    When this is detected, bet against (think: mean reversion during rapid moves)
    """
    if not day_hourly or len(day_hourly) < 6:  # Need sufficient hourly data
        return None, 0.0

    # Sort hourly data chronologically
    hourly_sorted = sorted(day_hourly, key=lambda x: x[0])  # Sort by hour
    
    # Look in 2-4 hours before end of day (approximating settlement)
    last_hour = max(hourly_sorted[i][0] for i in range(len(hourly_sorted)))
    # Target time window: 2-4 hours before settlement (i.e. hours [14,16] if settle at 18:00)
    lookback_start_hour = max(last_hour - 6, 0)  # Look from 6 hours before end of day
    cutoff_start_hour = max(last_hour - 4, 0)    # Start of our target window (2h before settlement)
    cutoff_end_hour = max(last_hour - 2, 0)      # End of our target window (4h before settlement)
    
    # Extract data for target time period
    target_period = [h for h in hourly_sorted if cutoff_start_hour <= h[0] <= cutoff_end_hour]
    
    if len(target_period) < 3:  # Need at least 3 observations for trend calculation
        return None, 0.0
    
    # Calculate rate of temperature change in this window
    start_temp_index = 0
    end_temp_index = len(target_period) - 1
    start_temp = target_period[start_temp_index][1]
    end_temp = target_period[end_temp_index][1]
    
    duration = target_period[end_temp_index][0] - target_period[start_temp_index][0]
    if duration <= 0:
        return None, 0.0
    
    rate_of_change = (end_temp - start_temp) / duration 
    
    # The original signal was: if decay rate < -1.8%/hour -> TIME_DECAY_SHORT
    # For temperature: we're looking for very rapid change patterns that indicate certainty formation
    # Absolute large rate indicates fast motion -> possibly signaling time decay pattern
    if abs(rate_of_change) < RATE_THRESHOLD_PER_HOUR:  # Not reaching the threshold for rapid movement
        return None, 0.0
        
    # If temperature is rising rapidly (positive rate), it might suggest high confidence
    # in upward movement. In a market context, if we see this, "time decay" suggests
    # it won't go as high -> bet down
    # If temperature is dropping rapidly, bet up
    if rate_of_change > 0:
        direction = 'down'  # rapid heating -> expect it won't continue ("time decay")
    else:
        direction = 'up'    # rapid cooling -> expect it won't continue
    
    # Confidence is based on how much beyond threshold the rate is
    excess = abs(rate_of_change) - RATE_THRESHOLD_PER_HOUR
    confidence = min(excess / RATE_THRESHOLD_PER_HOUR, 0.9)  # Cap confidence
    
    return direction, confidence


def walk_forward_edge2_with_dates(days, hourly_by_date, market, train_days=180, test_days=30):
    """Walk-forward backtest of the time-of-day decay signal."""
    results = []
    coverage_days = 0
    total_days = 0
    
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            total_days += 1
            day_hourly = hourly_by_date.get(date, [])
            pred, conf = time_of_day_decay_signal(day_hourly, date)
            
            if pred is not None:
                coverage_days += 1
                results.append((pred, actual['direction'], conf, date))
        
        start += test_days
        if start > len(days):
            break
    
    return results, total_days


def main():
    print("=" * 90)
    print("EDGE 2: TIME-OF-DAY DECAY — MARKET MICROSTRUCTURE SIGNAL")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Rate threshold: {RATE_THRESHOLD_PER_HOUR}°F/hour")
    print(f"Time window: 2-4 hours before settlement (end-of-day)")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()
    print("  Signal: Look for rapid temperature change rates in late afternoon,")
    print("  indicating rapid 'certainty formation'. Bet against acceleration.")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)
    
    # ─── PART 1: Per-station accuracy ─────────────────────────────────────
    print("=" * 90)
    print("PART 1: PER-STATION ACCURACY")
    print("=" * 90)
    
    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10}")
    print("-" * 65)
    
    all_results = []
    total_coverage = 0
    total_days = 0
    
    for station in ALL_STATIONS:
        hourly_by_date, market = load_hourly_data(station, conn)
        
        # Load daily data to synchronize with hourly
        cur = conn.cursor()
        cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
            FROM metar_observations
            WHERE station=? AND date_utc >= '2023-01-01' AND temp_f IS NOT NULL
            GROUP BY date_utc ORDER BY date_utc ASC
        """, (station,))
        days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]): continue
            days.append({'date': r[0], 'high': r[1], 'low': r[2]})
            
        if len(days) < 210:
            continue
        
        results, tdays = walk_forward_edge2_with_dates(days, hourly_by_date, market)
        if not results:
            print(f"{station:<8} {'N/A':>8}")
            continue
        
        total = len(results)
        correct = sum(1 for p, a, c, d in results if p == a)
        acc = correct / total if total > 0 else 0
        coverage = total / tdays if tdays > 0 else 0
        avg_conf = sum(c for p, a, c, d in results) / total if total > 0 else 0
        
        all_results.extend([(p, a, c) for p, a, c, d in results])
        total_coverage += total
        total_days += tdays
        
        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    # Aggregate
    total = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total if total > 0 else 0
    coverage = total_coverage / total_days if total_days > 0 else 0
    avg_conf = sum(c for p, a, c in all_results) / total if total > 0 else 0
    
    print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    # ─── PART 2: Confidence-gated results ─────────────────────────────────
    print()
    print("=" * 90)
    print("PART 2: CONFIDENCE-GATED RESULTS")
    print("=" * 90)
    
    for threshold in [0.0, 0.5, 0.6, 0.7, 0.8]:
        gated = [(p, a, c) for p, a, c in all_results if c >= threshold]
        if not gated:
            print(f"  conf ≥ {threshold:.1f}: 0 trades")
            continue
        gt = len(gated)
        gc = sum(1 for p, a, c in gated if p == a)
        ga = gc / gt if gt > 0 else 0
        print(f"  conf ≥ {threshold:.1f}: {gt:>6} trades, {gc:>6} correct, {ga:>7.2%}")
    
    # ─── PART 3: Direction analysis ─────────────────────────────────────────
    print()
    print("=" * 90)
    print("PART 3: DIRECTION ANALYSIS")
    print("=" * 90)
    
    up_results = [(p, a, c) for p, a, c in all_results if p == 'up']
    down_results = [(p, a, c) for p, a, c in all_results if p == 'down']
    
    up_c = sum(1 for p, a, c in up_results if p == a)
    down_c = sum(1 for p, a, c in down_results if p == a)
    
    print(f"\n  UP predictions:   {len(up_results):>6} trades, {up_c:>6} correct, {up_c/len(up_results):>7.2%}" if up_results else "  UP predictions: 0 trades")
    print(f"  DOWN predictions: {len(down_results):>6} trades, {down_c:>6} correct, {down_c/len(down_results):>7.2%}" if down_results else "  DOWN predictions: 0 trades")
    
    # ─── VERDICT ──────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("EDGE 2 VERDICT")
    print("=" * 90)
    
    # Binomial test
    if total > 100:
        z = (accuracy - 0.5) * math.sqrt(total) / math.sqrt(0.25)
        binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
    else:
        binom_p = 1.0
    
    print(f"\n  Accuracy: {accuracy:.2%} ({correct}/{total} trades)")
    print(f"  Coverage: {coverage:.1%} of available days")
    print(f"  Binomial test p-value: {binom_p:.4f}")
    print(f"  Avg confidence: {avg_conf:.3f}")
    
    if total > 0:
        if accuracy >= 0.58:
            print(f"\n  ✓ PASSES 58% threshold — signal adds value to ensemble")
        else:
            print(f"\n  ✗ FAILS 58% threshold — signal does not add standalone value")
    else:
        print(f"\n  ⚠️ INSUFFICIENT DATA — signal did not trigger during backtest")
    
    if coverage < 0.05:
        print(f"  ⚠️  Low coverage ({coverage:.1%}) — signal triggers infrequently")
    
    print(f"\n  Note: Based on temperature momentum patterns in late afternoon.")
    print(f"  Rapid temperature changes may indicate accelerated 'certainty formation'.")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 2 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
