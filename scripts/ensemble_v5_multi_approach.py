#!/usr/bin/env python3
"""
Ensemble v5 — Multi-Approach Aggregation for Same-Day HIGH Temperature Direction

6 independent approaches, aggregated via weighted voting, walk-forward validated.
3 stations: KMIA, KNYC, KDEN.

APPROACHES:
1. Simple Trend — today's HIGH vs yesterday's HIGH (baseline ~80%)
2. Reversion — deviation from 30-day rolling mean → bet reversion
3. Gaussian Model — 48h rolling μ/σ, PMF-based signal
4. Climate Persistence — 3-day momentum (not pure persistence)
5. Regime Strategy — pressure-based stability filter + trend
6. Forecast Disagreement — METAR proxy (temp deviation from seasonal normal)

No AI. No subagents. Just code.
"""

import sqlite3
import os
import sys
import math
import json
from collections import defaultdict
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "metar_backfill.db")
STATIONS = ["KMIA", "KNYC", "KDEN"]
WALK_FORWARD_TRAIN_MONTHS = 6
WALK_FORWARD_TEST_MONTHS = 1


# ─── DATA LOADING ───────────────────────────────────────────────────────────

def load_metar_daily(station, conn):
    """Load daily aggregated METAR data for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc,
               MAX(temp_f) as high,
               MIN(temp_f) as low,
               AVG(temp_f) as avg_temp,
               AVG(dewpoint_f) as avg_dewpoint,
               AVG(pressure_mb) as avg_pressure,
               AVG(wind_speed_kt) as avg_wind,
               AVG(wind_direction_deg) as avg_wind_dir
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    rows = cur.fetchall()
    data = {}
    for r in rows:
        data[r[0]] = {
            'high': r[1], 'low': r[2], 'avg_temp': r[3],
            'avg_dewpoint': r[4], 'avg_pressure': r[5],
            'avg_wind': r[6], 'avg_wind_dir': r[7]
        }
    return data


def load_settlement_data(station, conn):
    """Load settlement epoch data for HIGH market only."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    rows = cur.fetchall()
    data = {}
    for r in rows:
        direction = 'flat'
        if r[2] is not None:
            if r[1] > r[2]:
                direction = 'up'
            elif r[1] < r[2]:
                direction = 'down'
        data[r[0]] = {
            'bucket': r[1],
            'prior_bucket': r[2],
            'direction': direction
        }
    return data


# ─── APPROACH 1: SIMPLE TREND ────────────────────────────────────────────────

def approach_simple_trend(metar_data, dates, i):
    """
    Today's HIGH vs yesterday's HIGH.
    This is the ~80% baseline from June 30.
    """
    today = metar_data[dates[i]]
    yesterday = metar_data[dates[i-1]]
    
    if today['high'] > yesterday['high']:
        return 'up', 0.80
    elif today['high'] < yesterday['high']:
        return 'down', 0.80
    return 'flat', 0.0


# ─── APPROACH 2: REVERSION ───────────────────────────────────────────────────

def approach_reversion(metar_data, dates, i, window=30):
    """
    If today's HIGH deviates >2°F from 30-day rolling mean → bet reversion.
    """
    if i < window:
        return 'flat', 0.0
    
    # Compute 30-day rolling mean of HIGHs
    recent_highs = [metar_data[dates[j]]['high'] for j in range(i - window, i)]
    rolling_mean = sum(recent_highs) / len(recent_highs)
    
    # Compute rolling std
    variance = sum((h - rolling_mean) ** 2 for h in recent_highs) / len(recent_highs)
    rolling_std = math.sqrt(variance) if variance > 0 else 1.0
    
    today_high = metar_data[dates[i]]['high']
    deviation = today_high - rolling_mean
    z_score = deviation / rolling_std if rolling_std > 0 else 0
    
    if abs(z_score) > 1.5:
        direction = 'down' if z_score > 0 else 'up'
        confidence = min(abs(z_score) / 3.0, 0.95)
        return direction, confidence
    
    return 'flat', 0.0


# ─── APPROACH 3: GAUSSIAN MODEL ──────────────────────────────────────────────

def approach_gaussian(metar_data, dates, i, window=48):
    """
    48h rolling μ/σ. If today's HIGH is >2σ from rolling mean → bet reversion.
    """
    if i < window:
        return 'flat', 0.0
    
    recent_highs = [metar_data[dates[j]]['high'] for j in range(i - window, i)]
    mu = sum(recent_highs) / len(recent_highs)
    variance = sum((h - mu) ** 2 for h in recent_highs) / len(recent_highs)
    sigma = math.sqrt(variance) if variance > 0 else 1.0
    
    today_high = metar_data[dates[i]]['high']
    z = (today_high - mu) / sigma if sigma > 0 else 0
    
    if abs(z) > 2.0:
        direction = 'down' if z > 0 else 'up'
        confidence = min(abs(z) / 4.0, 0.90)
        return direction, confidence
    
    return 'flat', 0.0


# ─── APPROACH 4: CLIMATE PERSISTENCE (3-DAY MOMENTUM) ────────────────────────

def approach_persistence(metar_data, dates, i):
    """
    3-day momentum: if the 3-day moving average of HIGHs is rising, bet UP.
    """
    if i < 4:
        return 'flat', 0.0
    
    # 3-day MA
    ma3_today = (metar_data[dates[i]]['high'] + 
                 metar_data[dates[i-1]]['high'] + 
                 metar_data[dates[i-2]]['high']) / 3.0
    ma3_yesterday = (metar_data[dates[i-1]]['high'] + 
                     metar_data[dates[i-2]]['high'] + 
                     metar_data[dates[i-3]]['high']) / 3.0
    
    if ma3_today > ma3_yesterday:
        return 'up', 0.60
    elif ma3_today < ma3_yesterday:
        return 'down', 0.60
    return 'flat', 0.0


# ─── APPROACH 5: REGIME STRATEGY ─────────────────────────────────────────────

def approach_regime(metar_data, dates, i):
    """
    Pressure-based stability: |ΔP| < 3mb over 24h = stable regime.
    In stable: bet with 2-day trend. In unstable: bet reversion.
    """
    if i < 3:
        return 'flat', 0.0
    
    today_p = metar_data[dates[i]]['avg_pressure']
    yesterday_p = metar_data[dates[i-1]]['avg_pressure']
    delta_p = today_p - yesterday_p if today_p and yesterday_p else 0
    
    is_stable = abs(delta_p) < 3.0
    
    if is_stable:
        # Bet with 2-day trend
        if metar_data[dates[i]]['high'] > metar_data[dates[i-2]]['high']:
            return 'up', 0.65
        else:
            return 'down', 0.65
    else:
        # Unstable: bet reversion
        if metar_data[dates[i]]['high'] > metar_data[dates[i-1]]['high']:
            return 'down', 0.55
        else:
            return 'up', 0.55


# ─── APPROACH 6: FORECAST DISAGREEMENT (METAR PROXY) ─────────────────────────

def approach_forecast_disagreement(metar_data, dates, i, window=30):
    """
    METAR proxy for GFS vs NWS disagreement.
    If today's HIGH deviates >5°F from 30-day seasonal normal → bet reversion.
    This approximates what GFS vs NWS disagreement would capture.
    """
    if i < window:
        return 'flat', 0.0
    
    # 30-day rolling mean as "seasonal normal" proxy
    recent_highs = [metar_data[dates[j]]['high'] for j in range(i - window, i)]
    seasonal_normal = sum(recent_highs) / len(recent_highs)
    
    today_high = metar_data[dates[i]]['high']
    deviation = today_high - seasonal_normal
    
    if abs(deviation) > 5.0:
        direction = 'down' if deviation > 0 else 'up'
        # sigmoid((|diff| - 5) / 3)
        strength = 1.0 / (1.0 + math.exp(-(abs(deviation) - 5.0) / 3.0))
        confidence = min(strength, 0.85)
        return direction, confidence
    
    return 'flat', 0.0


# ─── AGGREGATION ─────────────────────────────────────────────────────────────

APPROACHES = [
    ("simple_trend", approach_simple_trend),
    ("reversion", approach_reversion),
    ("gaussian", approach_gaussian),
    ("persistence", approach_persistence),
    ("regime", approach_regime),
    ("forecast_disagreement", approach_forecast_disagreement),
]


def aggregate_votes(votes, weights):
    """
    Weighted majority vote.
    votes: list of (direction, confidence) tuples
    weights: dict of approach_name -> weight
    """
    wsum = 0.0
    total_weight = 0.0
    
    for name, direction, confidence in votes:
        if direction == 'flat':
            continue
        w = weights.get(name, 1.0) * confidence
        if direction == 'up':
            wsum += w
        else:
            wsum -= w
        total_weight += w
    
    if total_weight == 0:
        return 'flat', 0.0
    
    direction = 'up' if wsum > 0 else 'down'
    confidence = abs(wsum) / total_weight
    return direction, confidence


# ─── WALK-FORWARD BACKTEST ───────────────────────────────────────────────────

def run_walk_forward(station, conn):
    """Run walk-forward backtest for one station."""
    metar_data = load_metar_daily(station, conn)
    settle_data = load_settlement_data(station, conn)
    
    # Align dates
    dates = sorted(set(metar_data.keys()) & set(settle_data.keys()))
    if len(dates) < 60:
        return None
    
    # Convert dates to datetime for windowing
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    
    # Walk-forward windows
    all_predictions = []
    approach_results = {name: {'correct': 0, 'total': 0} for name, _ in APPROACHES}
    ensemble_results = {'correct': 0, 'total': 0}
    
    # Start from earliest possible (need warmup for rolling windows)
    start_idx = 60  # 60 days warmup
    
    # Walk-forward: train on 6 months, test on 1 month
    current_train_end = start_idx
    
    while current_train_end < len(dates):
        # Find test window end
        test_start = current_train_end
        test_end = min(test_start + 30, len(dates))  # ~1 month test
        
        if test_end - test_start < 5:
            break
        
        # Train weights on training window
        train_dates = dates[:current_train_end]
        approach_accuracies = {}
        
        for name, func in APPROACHES:
            correct = 0
            total = 0
            for j in range(60, len(train_dates)):  # skip warmup
                direction, confidence = func(metar_data, train_dates, j)
                if direction == 'flat':
                    continue
                actual = settle_data[train_dates[j]]['direction']
                if actual == 'flat':
                    continue
                if direction == actual:
                    correct += 1
                total += 1
            approach_accuracies[name] = correct / total if total > 0 else 0.5
        
        # Convert accuracies to weights (log-odds)
        weights = {}
        for name, acc in approach_accuracies.items():
            if acc > 0.50:
                weights[name] = math.log(acc / (1 - acc)) if acc < 1.0 else 5.0
            else:
                weights[name] = 0.0
        
        # Normalize weights
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: v / total_w for k, v in weights.items()}
        
        # Test on test window
        for j in range(test_start, test_end):
            if j < 60:
                continue
            
            actual = settle_data[dates[j]]['direction']
            if actual == 'flat':
                continue
            
            # Collect votes from each approach
            votes = []
            for name, func in APPROACHES:
                direction, confidence = func(metar_data, dates, j)
                if direction != 'flat':
                    votes.append((name, direction, confidence))
                    approach_results[name]['total'] += 1
                    if direction == actual:
                        approach_results[name]['correct'] += 1
            
            # Aggregate
            if len(votes) >= 2:  # Require at least 2 approaches to vote
                ensemble_dir, ensemble_conf = aggregate_votes(votes, weights)
                if ensemble_dir != 'flat':
                    ensemble_results['total'] += 1
                    if ensemble_dir == actual:
                        ensemble_results['correct'] += 1
                    all_predictions.append({
                        'date': dates[j],
                        'station': station,
                        'predicted': ensemble_dir,
                        'actual': actual,
                        'confidence': ensemble_conf,
                        'votes': len(votes)
                    })
        
        current_train_end = test_end
    
    return {
        'station': station,
        'ensemble': ensemble_results,
        'approaches': approach_results,
        'predictions': all_predictions
    }


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("ENSEMBLE v5 — MULTI-APPROACH AGGREGATION")
    print("Same-day HIGH temperature direction prediction")
    print("3 stations: KMIA, KNYC, KDEN")
    print("=" * 80)
    print()
    
    conn = sqlite3.connect(DB_PATH)
    
    all_station_results = {}
    total_correct = 0
    total_trades = 0
    
    for station in STATIONS:
        print(f"Processing {station}...")
        result = run_walk_forward(station, conn)
        
        if result is None:
            print(f"  Not enough data. Skipping.")
            continue
        
        all_station_results[station] = result
        
        # Print approach breakdown
        print(f"  Approach accuracies (walk-forward):")
        for name, acc_data in sorted(result['approaches'].items()):
            if acc_data['total'] > 0:
                acc = acc_data['correct'] / acc_data['total']
                print(f"    {name:<25s}: {acc_data['correct']:>5d}/{acc_data['total']:<5d} = {acc:.2%}")
        
        # Print ensemble
        e = result['ensemble']
        if e['total'] > 0:
            e_acc = e['correct'] / e['total']
            print(f"  ENSEMBLE: {e['correct']}/{e['total']} = {e_acc:.2%}")
            total_correct += e['correct']
            total_trades += e['total']
        print()
    
    conn.close()
    
    # ─── FINAL REPORT ─────────────────────────────────────────────────────
    print("=" * 80)
    print("FINAL REPORT")
    print("=" * 80)
    print()
    
    overall_accuracy = total_correct / total_trades if total_trades > 0 else 0
    
    print(f"Overall Ensemble Accuracy: {overall_accuracy:.2%} ({total_correct}/{total_trades})")
    print()
    
    # Per-station
    print("Per-Station Ensemble:")
    print(f"{'Station':<8} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("-" * 36)
    for station in STATIONS:
        if station in all_station_results:
            e = all_station_results[station]['ensemble']
            if e['total'] > 0:
                acc = e['correct'] / e['total']
                print(f"{station:<8} {e['correct']:>8} {e['total']:>8} {acc:>10.2%}")
    print()
    
    # Per-approach aggregate
    print("Per-Approach Aggregate (all stations):")
    agg_approaches = defaultdict(lambda: {'correct': 0, 'total': 0})
    for station, result in all_station_results.items():
        for name, data in result['approaches'].items():
            agg_approaches[name]['correct'] += data['correct']
            agg_approaches[name]['total'] += data['total']
    
    print(f"{'Approach':<25s} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("-" * 53)
    for name in sorted(agg_approaches.keys()):
        d = agg_approaches[name]
        if d['total'] > 0:
            acc = d['correct'] / d['total']
            print(f"{name:<25s} {d['correct']:>8} {d['total']:>8} {acc:>10.2%}")
    print()
    
    # Threshold check
    THRESHOLD = 0.58
    print("=" * 80)
    if overall_accuracy >= THRESHOLD:
        print(f"✓ PASSES THRESHOLD: {overall_accuracy:.2%} >= {THRESHOLD:.0%}")
    else:
        print(f"✗ FAILS THRESHOLD: {overall_accuracy:.2%} < {THRESHOLD:.0%}")
    print("=" * 80)
    
    return {
        'overall_accuracy': overall_accuracy,
        'total_trades': total_trades,
        'total_correct': total_correct,
        'station_results': all_station_results,
        'approach_aggregate': dict(agg_approaches)
    }


if __name__ == "__main__":
    result = main()
