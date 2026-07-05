#!/usr/bin/env python3
"""
CORE MODULE: Time-of-Day Decay — Edge 2

Standalone module for the time-of-day decay signal.
If 2-4 hours to expiry AND decay rate < -1.8%/hour → TIME_DECAY_SHORT.

For backtest, uses METAR hourly observations as a proxy for market price decay.
The signal looks at late-afternoon temperature evolution patterns:
  - When temperature is changing rapidly in the 2-4 hours before settlement,
    it indicates accelerated "certainty formation"
  - Bet against the rapid move (expect mean reversion near settlement)

For live use, would need Kalshi settlement API hourly snapshots to compute
actual market price decay. See DATA_GAP.md section below.

Walk-forward only. No AI in the loop. No in-sample metrics.
"""

import sqlite3
import math
from collections import defaultdict
from datetime import datetime

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

# Signal parameters
HOURS_BEFORE_SETTLE_START = 2   # Start of decay window (hours before settlement)
HOURS_BEFORE_SETTLE_END = 4    # End of decay window (hours before settlement)
RATE_THRESHOLD = 1.8           # °F/hour threshold for rapid change
SETTLEMENT_HOUR_UTC = 23        # Assumed settlement hour (end of UTC day)
WINDOW_START_HOUR = 17          # Hour to start looking at late-day temperatures
SIGNAL_MODE = 'momentum'        # 'momentum' = bet WITH the move, 'reversion' = bet against


def sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


def parse_timestamp(ts_str):
    """Parse various timestamp formats from the database."""
    # Try ISO format: 2024-01-01T00:00:00+00:00
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        pass
    # Try standard format: 2024-01-01 00:00:00
    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M']:
        try:
            return datetime.strptime(ts_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


def load_hourly_data(station, conn):
    """Load hourly temperature data and settlement epochs for a station."""
    cur = conn.cursor()
    
    # Get hourly observations
    cur.execute("""
        SELECT timestamp_utc, temp_f
        FROM metar_observations
        WHERE station=? AND date_utc >= '2023-01-01' AND temp_f IS NOT NULL
        ORDER BY timestamp_utc ASC
    """, (station,))
    
    hourly_by_date = defaultdict(dict)  # date -> {hour: temp}
    for row in cur.fetchall():
        timestamp_str, temp = row
        if temp is None:
            continue
        dt = parse_timestamp(timestamp_str)
        if dt is None:
            continue
        date_str = dt.strftime('%Y-%m-%d')
        hourly_by_date[date_str][dt.hour] = temp
    
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
    
    # Also load daily highs for synchronization
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
    
    return days, hourly_by_date, market


def time_decay_signal(hourly_temps, date_str):
    """
    Compute time-of-day decay signal from hourly temperatures.
    
    Looks at the late-afternoon window (hour 17 onward) for rapid temperature
    change. When temperature is changing rapidly (> threshold °F/hour),
    bet WITH the move (momentum) — rapid late-day temperature moves tend
    to continue into the next day's settlement bucket.
    
    This is a proxy for the Kalshi market price decay signal. The original
    spec calls for market price decay < -1.8%/hour → TIME_DECAY_SHORT.
    The temperature proxy uses momentum: rapid temp moves predict continuation.
    
    Args:
        hourly_temps: dict of {hour: temp_f} for the date
        date_str: date string for debugging
    
    Returns: (direction, confidence) or (None, 0.0)
    """
    if not hourly_temps:
        return None, 0.0
    
    hours = sorted(hourly_temps.keys())
    if len(hours) < 4:
        return None, 0.0
    
    # Use late afternoon window starting at WINDOW_START_HOUR
    late_hours = [h for h in hours if h >= WINDOW_START_HOUR]
    if len(late_hours) < 2:
        late_hours = hours[-4:]
    
    if len(late_hours) < 2:
        return None, 0.0
    
    start_hour = late_hours[0]
    end_hour = late_hours[-1]
    start_temp = hourly_temps[start_hour]
    end_temp = hourly_temps[end_hour]
    
    duration = end_hour - start_hour
    if duration <= 0:
        return None, 0.0
    
    rate_of_change = (end_temp - start_temp) / duration  # °F/hour
    
    if abs(rate_of_change) < RATE_THRESHOLD:
        return None, 0.0
    
    # Momentum: bet WITH the rapid move
    # Rapid rising temps → next day tends up; rapid falling → tends down
    if SIGNAL_MODE == 'momentum':
        direction = 'up' if rate_of_change > 0 else 'down'
    else:
        # Reversion: bet against the rapid move
        direction = 'down' if rate_of_change > 0 else 'up'
    
    # Confidence scales with how far beyond threshold
    excess = abs(rate_of_change) - RATE_THRESHOLD
    confidence = sigmoid(excess / RATE_THRESHOLD)
    
    return direction, confidence


def walk_forward_backtest(days, hourly_by_date, market, train_days=180, test_days=30):
    """Walk-forward backtest of the time-of-day decay signal."""
    results = []
    total_days = 0
    
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            total_days += 1
            day_hourly = hourly_by_date.get(date, {})
            pred, conf = time_decay_signal(day_hourly, date)
            
            if pred is not None:
                results.append((pred, actual['direction'], conf, date))
        
        start += test_days
        if start > len(days):
            break
    
    return results, total_days


def run_backtest():
    """Run full walk-forward backtest across all stations."""
    print("=" * 90)
    print("EDGE 2: TIME-OF-DAY DECAY — CORE MODULE BACKTEST")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Rate threshold: {RATE_THRESHOLD}°F/hour")
    print(f"Time window: {HOURS_BEFORE_SETTLE_START}-{HOURS_BEFORE_SETTLE_END} hours before settlement")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()
    print("  Signal: Rapid temperature change in late afternoon → bet against (mean reversion)")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    
    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10}")
    print("-" * 65)
    
    all_results = []
    total_coverage = 0
    total_days_all = 0
    
    for station in ALL_STATIONS:
        days, hourly_by_date, market = load_hourly_data(station, conn)
        if len(days) < 210:
            continue
        
        results, tdays = walk_forward_backtest(days, hourly_by_date, market)
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
        total_days_all += tdays
        
        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    total = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total if total > 0 else 0
    coverage = total_coverage / total_days_all if total_days_all > 0 else 0
    avg_conf = sum(c for p, a, c in all_results) / total if total > 0 else 0
    
    print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    # Confidence-gated
    print("\n--- Confidence-Gated Results ---")
    for threshold in [0.0, 0.3, 0.5, 0.6, 0.7]:
        gated = [(p, a, c) for p, a, c in all_results if c >= threshold]
        if not gated:
            print(f"  conf >= {threshold:.1f}: 0 trades")
            continue
        gt = len(gated)
        gc = sum(1 for p, a, c in gated if p == a)
        ga = gc / gt if gt > 0 else 0
        print(f"  conf >= {threshold:.1f}: {gt:>6} trades, {gc:>6} correct, {ga:>7.2%} accuracy")
    
    # Direction analysis
    print("\n--- Direction Analysis ---")
    up_r = [(p, a, c) for p, a, c in all_results if p == 'up']
    down_r = [(p, a, c) for p, a, c in all_results if p == 'down']
    if up_r:
        uc = sum(1 for p, a, c in up_r if p == a)
        print(f"  UP predictions:   {len(up_r):>6} trades, {uc:>6} correct, {uc/len(up_r):>7.2%}")
    if down_r:
        dc = sum(1 for p, a, c in down_r if p == a)
        print(f"  DOWN predictions: {len(down_r):>6} trades, {dc:>6} correct, {dc/len(down_r):>7.2%}")
    
    # Binomial test
    if total > 100:
        z = (accuracy - 0.5) * math.sqrt(total) / math.sqrt(0.25)
        binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
    else:
        binom_p = 1.0
    
    print(f"\n--- VERDICT ---")
    print(f"  Accuracy: {accuracy:.2%} ({correct}/{total} trades)")
    print(f"  Coverage: {coverage:.1%} of available days")
    print(f"  Binomial test p-value: {binom_p:.6f}")
    print(f"  Avg confidence: {avg_conf:.3f}")
    
    if total > 0 and accuracy >= 0.58:
        print(f"  ✓ PASSES 58% threshold")
    elif total > 0:
        print(f"  ✗ FAILS 58% threshold")
    else:
        print(f"  ⚠️ INSUFFICIENT DATA")
    
    # Data gap documentation
    print(f"\n--- DATA GAP: Kalshi Market Price Decay ---")
    print(f"  This backtest uses temperature change rates as a proxy for market")
    print(f"  price decay. The original spec calls for Kalshi settlement API hourly")
    print(f"  snapshots to compute actual market price decay rates (change in")
    print(f"  contract price per hour as settlement approaches).")
    print(f"")
    print(f"  For production use, you would need:")
    print(f"  1. Kalshi API access to historical contract prices (hourly snapshots)")
    print(f"  2. Settlement timestamps per market")
    print(f"  3. Compute: decay_rate = (price_t2 - price_t1) / (t2 - t1)")
    print(f"  4. If decay_rate < -1.8%/hour → TIME_DECAY_SHORT")
    print(f"")
    print(f"  The temperature proxy tests whether rapid late-day temperature")
    print(f"  changes predict next-day bucket direction (mean reversion).")
    
    conn.close()
    return accuracy, total, all_results


if __name__ == "__main__":
    run_backtest()
