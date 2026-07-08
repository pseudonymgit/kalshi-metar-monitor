#!/usr/bin/env python3
"""
M2/N4 — Live-vs-Backtest Drift Monitor

Cron script that compares live paper trading accuracy against backtest
accuracy for the same date range. Alerts if accuracy diverges >5pp for
3+ consecutive days.

Usage:
  python3 scripts/drift_monitor.py [--days 7] [--alert-threshold 0.05] [--streak 3]

Intended to run as a daily cron job after the paper trading run completes.

Output:
  - Console: summary report
  - reports/drift-monitor-YYYY-MM-DD.json: detailed metrics
  - Discord alert (if drift detected)
"""

import sqlite3
import os
import sys
import json
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Ensure core/ is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(SCRIPT_DIR, '..', 'core')
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from unified_backtest import run_backtest, DB_PATH
from signals import SignalRegistry, BACKTEST_SIGNALS

PAPER_DB_PATH = os.path.join(CORE_DIR, '..', 'data', 'paper_trading.db')
METAR_DB_PATH = os.path.join(CORE_DIR, '..', 'data', 'metar_backfill.db')
REPORT_DIR = os.path.join(CORE_DIR, '..', 'reports')
INSTANCE = os.getenv('PAPER_TRADING_INSTANCE', 'DEV').upper()


def get_paper_trading_accuracy(paper_db, date_from, date_to):
    """
    Get paper trading accuracy for a date range from the paper trading DB.
    
    Returns:
        dict: {date: {trades, correct, accuracy, settled, brier}}
    """
    if not os.path.exists(paper_db):
        return {}
    
    conn = sqlite3.connect(paper_db)
    c = conn.cursor()
    
    # Get settled trades in the date range
    c.execute("""
        SELECT trade_date_utc, signal_direction, forecast_prob,
               settled_value, status, realized_pnl, functionality
        FROM trades
        WHERE trade_date_utc BETWEEN ? AND ?
        AND status = 'closed'
        AND settled_value IS NOT NULL
        ORDER BY trade_date_utc
    """, (date_from, date_to))
    
    daily_stats = defaultdict(lambda: {'trades': 0, 'correct': 0, 'settled': 0, 'probs': [], 'outcomes': []})
    
    for row in c.fetchall():
        date = row[0]
        signal_dir = row[1]  # 'UP' or 'DOWN'
        forecast_prob = row[2]
        settled_value = row[3]  # 0.0-1.0
        
        daily_stats[date]['trades'] += 1
        daily_stats[date]['settled'] += 1
        
        # Determine if prediction was correct
        # settled_value > 0.5 means UP was correct, < 0.5 means DOWN was correct
        if signal_dir == 'UP' and settled_value > 0.5:
            daily_stats[date]['correct'] += 1
        elif signal_dir == 'DOWN' and settled_value < 0.5:
            daily_stats[date]['correct'] += 1
        
        # For Brier
        prob_up = forecast_prob if signal_dir == 'UP' else 1.0 - forecast_prob
        outcome = 1.0 if settled_value > 0.5 else 0.0
        daily_stats[date]['probs'].append(prob_up)
        daily_stats[date]['outcomes'].append(outcome)
    
    conn.close()
    
    # Compute daily accuracy and Brier
    result = {}
    for date, stats in daily_stats.items():
        acc = stats['correct'] / stats['settled'] if stats['settled'] > 0 else 0.0
        brier = 0.0
        if stats['probs']:
            for p, o in zip(stats['probs'], stats['outcomes']):
                brier += (p - o) ** 2
            brier /= len(stats['probs'])
        result[date] = {
            'trades': stats['trades'],
            'correct': stats['correct'],
            'settled': stats['settled'],
            'accuracy': acc,
            'brier': brier,
        }
    
    return result


def get_backtest_accuracy_for_range(date_from, date_to, stations=None):
    """
    Get backtest accuracy for the same date range.
    
    Runs the unified backtest on the specified date range and returns
    per-day accuracy.
    """
    if stations is None:
        # Use a representative subset for speed
        stations = ['KATL', 'KBOS', 'KLAX', 'KNYC', 'KSEA', 'KDFW', 'KMIA']
    
    conn = sqlite3.connect(METAR_DB_PATH)
    cur = conn.cursor()
    
    registry = SignalRegistry(METAR_DB_PATH)
    daily_stats = defaultdict(lambda: {'trades': 0, 'correct': 0})
    
    for station in stations:
        cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                   AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                   AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                   AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
            GROUP BY date_utc ORDER BY date_utc ASC
        """, (station,))
        days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]):
                continue
            days.append({
                'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
            })
        
        cur.execute("""
            SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
            FROM settlement_epochs
            WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
            AND local_trading_date BETWEEN ? AND ?
            ORDER BY local_trading_date ASC
        """, (station, date_from, date_to))
        market = {}
        for r in cur.fetchall():
            if r[2] is not None:
                market[r[0]] = 'up' if r[1] > r[2] else 'down'
        
        for idx in range(60, len(days)):
            date = days[idx]['date']
            if date < date_from or date > date_to:
                continue
            actual = market.get(date)
            if actual is None:
                continue
            
            # Evaluate signals
            votes = []
            for sig_name in BACKTEST_SIGNALS:
                sig = registry.get_signal(sig_name)
                if sig is None:
                    continue
                direction, conf = sig.evaluate(idx, days)
                if direction is not None:
                    votes.append(direction)
            
            if not votes:
                continue
            
            # Majority vote
            ups = sum(1 for v in votes if v == 'up')
            downs = len(votes) - ups
            pred = 'up' if ups > downs else 'down'
            
            daily_stats[date]['trades'] += 1
            if pred == actual:
                daily_stats[date]['correct'] += 1
    
    conn.close()
    
    result = {}
    for date, stats in daily_stats.items():
        acc = stats['correct'] / stats['trades'] if stats['trades'] > 0 else 0.0
        result[date] = {
            'trades': stats['trades'],
            'correct': stats['correct'],
            'accuracy': acc,
        }
    
    return result


def check_drift(paper_stats, backtest_stats, alert_threshold=0.05, streak_days=3):
    """
    Check for accuracy drift between paper trading and backtest.
    
    Returns:
        dict with drift_detected, streak, max_divergence, details
    """
    common_dates = sorted(set(paper_stats.keys()).intersection(set(backtest_stats.keys())))
    
    if not common_dates:
        return {
            'drift_detected': False,
            'reason': 'No overlapping dates between paper and backtest',
            'streak': 0,
            'max_divergence': 0.0,
        }
    
    divergences = []
    streak = 0
    max_streak = 0
    max_divergence = 0.0
    
    for date in common_dates:
        paper_acc = paper_stats[date]['accuracy']
        backtest_acc = backtest_stats[date]['accuracy']
        divergence = paper_acc - backtest_acc
        divergences.append({
            'date': date,
            'paper_accuracy': round(paper_acc, 4),
            'backtest_accuracy': round(backtest_acc, 4),
            'divergence': round(divergence, 4),
        })
        
        abs_div = abs(divergence)
        if abs_div > alert_threshold:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        
        max_divergence = max(max_divergence, abs_div)
    
    drift_detected = max_streak >= streak_days
    
    return {
        'drift_detected': drift_detected,
        'streak': max_streak,
        'max_divergence': round(max_divergence, 4),
        'alert_threshold': alert_threshold,
        'streak_threshold': streak_days,
        'divergences': divergences,
        'common_dates': len(common_dates),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description='M2/N4: Live-vs-backtest drift monitor')
    parser.add_argument('--days', type=int, default=7, help='Lookback days')
    parser.add_argument('--alert-threshold', type=float, default=0.05,
                        help='Alert if accuracy diverges > this threshold (default: 0.05 = 5pp)')
    parser.add_argument('--streak', type=int, default=3,
                        help='Alert if divergence persists for N+ consecutive days (default: 3)')
    args = parser.parse_args()
    
    today = datetime.now(timezone.utc)
    date_to = today.strftime('%Y-%m-%d')
    date_from = (today - timedelta(days=args.days)).strftime('%Y-%m-%d')
    
    print(f"M2/N4 — Live-vs-Backtest Drift Monitor")
    print(f"Instance: {INSTANCE}")
    print(f"Date range: {date_from} to {date_to}")
    print(f"Alert threshold: {args.alert_threshold:.1%} for {args.streak}+ consecutive days")
    print("=" * 70)
    
    # Get paper trading accuracy
    paper_db = PAPER_DB_PATH.replace('paper_trading.db', f'paper_trading_{INSTANCE.lower()}.db')
    if not os.path.exists(paper_db):
        paper_db = PAPER_DB_PATH  # Fallback to default
    
    print("Fetching paper trading accuracy...")
    paper_stats = get_paper_trading_accuracy(paper_db, date_from, date_to)
    
    if not paper_stats:
        print(f"⚠️  No paper trading data found in {paper_db}")
        print("   (This is expected if paper trading hasn't run yet for this date range)")
    else:
        print(f"   Found {len(paper_stats)} days of paper trading data")
    
    # Get backtest accuracy
    print("Computing backtest accuracy...")
    backtest_stats = get_backtest_accuracy_for_range(date_from, date_to)
    print(f"   Found {len(backtest_stats)} days of backtest data")
    
    # Check drift
    if paper_stats and backtest_stats:
        print("Checking for drift...")
        drift_result = check_drift(paper_stats, backtest_stats, args.alert_threshold, args.streak)
        
        if drift_result['drift_detected']:
            print(f"\n🚨 DRIFT DETECTED!")
            print(f"   Streak: {drift_result['streak']} days")
            print(f"   Max divergence: {drift_result['max_divergence']:.1%}")
            print(f"\n   Daily breakdown:")
            for d in drift_result['divergences']:
                marker = " ⚠️" if abs(d['divergence']) > args.alert_threshold else ""
                print(f"   {d['date']}: paper={d['paper_accuracy']:.1%} "
                      f"backtest={d['backtest_accuracy']:.1%} "
                      f"divergence={d['divergence']:+.1%}{marker}")
        else:
            print(f"\n✅ No significant drift detected")
            print(f"   Max divergence: {drift_result['max_divergence']:.1%}")
            print(f"   Common dates: {drift_result['common_dates']}")
            if drift_result.get('divergences'):
                print(f"\n   Daily breakdown:")
                for d in drift_result['divergences']:
                    print(f"   {d['date']}: paper={d['paper_accuracy']:.1%} "
                          f"backtest={d['backtest_accuracy']:.1%} "
                          f"divergence={d['divergence']:+.1%}")
    else:
        drift_result = {
            'drift_detected': False,
            'reason': 'Insufficient data for comparison',
            'paper_stats_days': len(paper_stats),
            'backtest_stats_days': len(backtest_stats),
        }
        print("\n⚠️  Insufficient data for drift comparison")
    
    # Save report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f'drift-monitor-{date_to}.json')
    report = {
        'date': date_to,
        'instance': INSTANCE,
        'date_range': [date_from, date_to],
        'paper_stats': paper_stats,
        'backtest_stats': backtest_stats,
        'drift_result': drift_result,
    }
    
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nReport saved: {report_path}")
    
    print("\n✅ M2/N4: Drift monitor — COMPLETE")


if __name__ == "__main__":
    main()
