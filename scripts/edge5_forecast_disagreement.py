#!/usr/bin/env python3
"""
EDGE 5: Forecast Disagreement (GFS vs NWS) — Backtest Version

Core signal: When two independent forecast sources disagree by >5°F,
bet in the direction of GFS (model consensus).

Backtest approach:
  Since historical GFS/NWS forecast archives aren't available via free API
  for the backtest period, we use two documented proxies:
  
  Proxy 1 — "NWS-like" forecast: yesterday's actual high temperature.
    NWS persistence forecast is a common baseline. When NWS forecasters
    look at tomorrow, yesterday's actual is their starting point.
    
  Proxy 2 — "GFS-like" forecast: 7-day rolling mean high temperature.
    GFS model output tends to revert toward recent climatology. The 7-day
    mean captures the model's baseline temperature expectation.
    
  When |Proxy1 - Proxy2| > 5°F, bet in the direction of Proxy2 (GFS-like).
  Signal strength = sigmoid((|diff| - 5) / 3).
  
  If this proxy signal works, it's worth investing in real GFS/NWS data feeds.
  If it doesn't work with proxies, real data probably won't help much either.

Walk-forward only. No AI in the loop. No in-sample metrics.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
from datetime import datetime

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

DISAGREEMENT_THRESHOLD = 5.0  # °F — minimum disagreement to trigger


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def load_station_data(station, conn):
    """Load daily high temps and settlement epochs for a station."""
    cur = conn.cursor()
    
    # Daily highs
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                      'temp': r[4], 'pressure': r[5]})
    
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
    
    return days, market


def forecast_disagreement_signal(idx, days):
    """
    Compute forecast disagreement signal.
    
    Proxy 1 (NWS-like): yesterday's actual high
    Proxy 2 (GFS-like): 7-day rolling mean high
    
    Returns: (direction, confidence) or (None, 0.0)
    """
    if idx < 8:
        return None, 0.0
    
    yesterday_high = days[idx - 1]['high']
    
    # 7-day rolling mean (GFS-like proxy)
    window = days[idx - 8:idx - 1]  # 7 days before today
    weekly_mean = sum(d['high'] for d in window) / len(window)
    
    disagreement = yesterday_high - weekly_mean
    abs_disagreement = abs(disagreement)
    
    if abs_disagreement < DISAGREEMENT_THRESHOLD:
        return None, 0.0
    
    # Bet in the direction of the GFS-like forecast (weekly mean)
    # If yesterday was warmer than the weekly mean, expect reversion (DOWN)
    # If yesterday was cooler than the weekly mean, expect recovery (UP)
    if disagreement > 0:
        direction = 'down'  # yesterday was hot → expect reversion
    else:
        direction = 'up'    # yesterday was cool → expect recovery
    
    # Signal strength using sigmoid
    confidence = sigmoid((abs_disagreement - DISAGREEMENT_THRESHOLD) / 3.0)
    
    return direction, confidence


def walk_forward_edge5(days, market, train_days=180, test_days=30):
    """Walk-forward backtest of the forecast disagreement signal."""
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
            pred, conf = forecast_disagreement_signal(idx, days)
            
            if pred is not None:
                coverage_days += 1
                results.append((pred, actual['direction'], conf, date))
        
        start += test_days
        if start > len(days):
            break
    
    return results, total_days


def main():
    print("=" * 90)
    print("EDGE 5: FORECAST DISAGREEMENT (GFS vs NWS) — WALK-FORWARD BACKTEST")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Disagreement threshold: {DISAGREEMENT_THRESHOLD}°F")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()
    print("  Proxy 1 (NWS-like): yesterday's actual high (persistence forecast)")
    print("  Proxy 2 (GFS-like): 7-day rolling mean high (climatology baseline)")
    print("  Signal: bet in GFS direction when |diff| > 5°F")
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
        days, market = load_station_data(station, conn)
        if len(days) < 210:
            continue
        
        results, tdays = walk_forward_edge5(days, market)
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
        print(f"  conf ≥ {threshold:.1f}: {gt:>6} trades, {gc:>6} correct, {ga:>7.2%} accuracy")
    
    # ─── PART 3: Direction analysis ───────────────────────────────────────
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
    
    # ─── PART 4: Disagreement magnitude analysis ──────────────────────────
    print()
    print("=" * 90)
    print("PART 4: DISAGREEMENT MAGNITUDE ANALYSIS")
    print("=" * 90)
    
    buckets = [(5, 7), (7, 10), (10, 15), (15, 100)]
    print(f"\n  {'Range':>12} {'Trades':>8} {'Correct':>8} {'Accuracy':>10}")
    print("  " + "-" * 45)
    
    for lo, hi in buckets:
        bucket_results = []
        for station in ALL_STATIONS:
            days, market = load_station_data(station, conn)
            if len(days) < 210:
                continue
            for idx in range(180, len(days)):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None:
                    continue
                pred, conf = forecast_disagreement_signal(idx, days)
                if pred is None:
                    continue
                # Compute disagreement magnitude
                yesterday_high = days[idx - 1]['high']
                window = days[idx - 8:idx - 1]
                weekly_mean = sum(d['high'] for d in window) / len(window)
                disagreement = abs(yesterday_high - weekly_mean)
                if lo <= disagreement < hi:
                    bucket_results.append((pred, actual['direction']))
        
        if bucket_results:
            bc = sum(1 for p, a in bucket_results if p == a)
            bt = len(bucket_results)
            print(f"  {lo}-{hi}°F      {bt:>8} {bc:>8} {bc/bt:>10.2%}")
        else:
            print(f"  {lo}-{hi}°F      {'0':>8}")
    
    # ─── VERDICT ──────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("EDGE 5 VERDICT")
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
    
    if accuracy >= 0.58:
        print(f"\n  ✓ PASSES 58% threshold — signal adds value to ensemble")
    else:
        print(f"\n  ✗ FAILS 58% threshold — signal does not add standalone value")
    
    if coverage < 0.15:
        print(f"  ⚠️  Low coverage ({coverage:.1%}) — signal triggers infrequently")
    
    print(f"\n  Note: Uses proxy forecasts (persistence vs climatology).")
    print(f"  If signal works with proxies, real GFS/NWS data may improve it further.")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 5 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
