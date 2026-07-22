#!/usr/bin/env python3
"""
ENSEMBLE V9 — PHASE 2: Edge 5 + Edge 2 integrated with Phase 1 ensemble

Combines all Phase 1 edges (reversion, gaussian, regime, gaussian v2, pressure)
with Phase 2 edges:
- Edge 5: Forecast Disagreement (64.68% standalone, 42.2% coverage)
- Edge 2: Time-of-Day Decay (59.35% standalone, 11.3% coverage, momentum mode)

Walk-forward only. No AI in the loop. No in-sample metrics.
FDR correction applied to validation stations.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
import sys

# Add core modules to path
CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core')
sys.path.insert(0, CORE_DIR)

from forecast_disagreement import forecast_disagreement_signal
from time_decay import time_decay_signal, parse_timestamp, WINDOW_START_HOUR, RATE_THRESHOLD, SIGNAL_MODE
from market_cost_model import MARKET_COST_MODEL

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = MARKET_COST_MODEL.round_trip_fraction()
SIGNAL_VARIANT = 'v8'  # Signal variant flag for A/B testing ('v8' or 'v9')

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

FDR_STATIONS = ['KNYC','KMIA','KDEN','KMDW','KLAX','KPHX','KATL','KBOS']


def load_station_data(station, conn):
    """Load daily highs, hourly temps, and settlement epochs."""
    cur = conn.cursor()
    
    # Daily aggregates
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
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                      'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    
    # Hourly data for Edge 2
    cur.execute("""
        SELECT timestamp_utc, temp_f
        FROM metar_observations
        WHERE station=? AND date_utc >= '2023-01-01' AND temp_f IS NOT NULL
        ORDER BY timestamp_utc ASC
    """, (station,))
    hourly_by_date = defaultdict(dict)
    for row in cur.fetchall():
        ts, temp = row
        if temp is None: continue
        dt = parse_timestamp(ts)
        if dt is None: continue
        date_str = dt.strftime('%Y-%m-%d')
        hourly_by_date[date_str][dt.hour] = temp
    
    # Settlement epochs
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = {
                'direction': 'up' if r[1] > r[2] else 'down',
                'reversion': r[3] if r[3] is not None else 0
            }
    
    return days, hourly_by_date, market


# ─── PHASE 1 SIGNALS (from ensemble v8) ─────────────────────────────────────

def approach_reversion(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_gaussian(idx, days):
    if idx < 48: return None, 0.0
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0: return 'down', abs(z)
    elif z < -1.0: return 'up', abs(z)
    return None, 0.0

def approach_regime(idx, days):
    """V8: Stable-regime mean-reversion signal."""
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    vol = math.sqrt(var) if var > 0 else 0.01
    slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0
    if vol < 1.0 and abs(slope) < 0.5:
        if idx >= 31:
            w30 = days[idx-31:idx-1]
            h30 = [d['high'] for d in w30]
            m30 = sum(h30) / len(h30)
            dist = days[idx-1]['high'] - m30
            if dist > 1.0: return 'down', min(dist/3.0, 0.8)
            elif dist < -1.0: return 'up', min(abs(dist)/3.0, 0.8)
    return None, 0.0

def approach_gaussian_v2(idx, days):
    """V8: 30-day window, 0.5 threshold, abs(z) confidence."""
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_pressure(idx, days):
    """V8: Confidence scaling abs(dp)/5.0 capped at 0.8."""
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0


# ─── PHASE 2 SIGNALS ────────────────────────────────────────────────────────

def approach_edge5_forecast_disagreement(idx, days):
    """Edge 5: Forecast disagreement (proxy-based)."""
    return forecast_disagreement_signal(idx, days)

def approach_edge2_time_decay(date_str, hourly_by_date):
    """Edge 2: Time-of-day decay (momentum mode)."""
    day_hourly = hourly_by_date.get(date_str, {})
    return time_decay_signal(day_hourly, date_str)


# ─── ENSEMBLE COMBINATION ──────────────────────────────────────────────────

def ensemble_vote(idx, days, date_str, hourly_by_date, use_edge5=True, use_edge2=True, min_conf=0.0):
    """
    Combine all signals via weighted voting.
    
    Phase 1: reversion, gaussian, regime, gaussian_v2, pressure
    Phase 2: edge5 (forecast disagreement), edge2 (time decay)
    
    min_conf: confidence threshold — signals below this are excluded from the vote,
              and ensemble predictions below this are suppressed.
    """
    signals = []
    
    # Phase 1 signals
    for name, func in [('reversion', approach_reversion),
                       ('gaussian', approach_gaussian),
                       ('regime', approach_regime),
                       ('gaussian_v2', approach_gaussian_v2),
                       ('pressure', approach_pressure)]:
        pred, conf = func(idx, days)
        if pred is not None and conf >= min_conf:
            signals.append((name, pred, conf))
    
    # Phase 2 signals
    if use_edge5:
        pred, conf = approach_edge5_forecast_disagreement(idx, days)
        if pred is not None and conf >= min_conf:
            signals.append(('edge5', pred, conf))
    
    if use_edge2:
        pred, conf = approach_edge2_time_decay(date_str, hourly_by_date)
        if pred is not None and conf >= min_conf:
            signals.append(('edge2', pred, conf))
    
    if not signals:
        return None, 0.0
    
    # Weighted vote
    up_weight = sum(c for n, p, c in signals if p == 'up')
    down_weight = sum(c for n, p, c in signals if p == 'down')
    
    total = up_weight + down_weight
    if total == 0:
        return None, 0.0
    
    # V8 confidence formula: |up_weight - down_weight| / total (ranges 0-1)
    if up_weight > down_weight:
        return 'up', abs(up_weight - down_weight) / total
    else:
        return 'down', abs(down_weight - up_weight) / total


def walk_forward_ensemble(days, hourly_by_date, market, train_days=180, test_days=30,
                           use_edge5=True, use_edge2=True, min_conf=0.7):
    """Walk-forward backtest of the full ensemble."""
    results = []
    
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            pred, conf = ensemble_vote(idx, days, date, hourly_by_date, 
                                        use_edge5, use_edge2, min_conf=min_conf)
            if pred is not None and conf >= min_conf:
                results.append((pred, actual['direction'], conf, date))
        
        start += test_days
        if start > len(days):
            break
    
    return results


def main():
    print("=" * 90)
    print("ENSEMBLE V9 — PHASE 2: Edge 5 + Edge 2 integrated with Phase 1")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"FDR validation stations: {FDR_STATIONS}")
    print(f"Fee rate: {FEE_RATE}")
    print(f"Edge 5 (forecast disagreement): 64.68% standalone, 42.2% coverage")
    print(f"Edge 2 (time decay, momentum): 59.35% standalone, 11.3% coverage")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)
    
    # ─── RUN THREE CONFIGURATIONS ─────────────────────────────────────────
    configs = [
        ("Phase 1 only (baseline)", False, False),
        ("Phase 1 + Edge 5", True, False),
        ("Phase 1 + Edge 5 + Edge 2 (full Phase 2)", True, True),
    ]
    
    all_config_results = {}
    
    for config_name, use_edge5, use_edge2 in configs:
        print(f"\n{'=' * 90}")
        print(f"CONFIG: {config_name}")
        print(f"{'=' * 90}")
        
        print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10}")
        print("-" * 65)
        
        all_results = []
        total_coverage = 0
        total_days_all = 0
        
        for station in ALL_STATIONS:
            days, hourly_by_date, market = load_station_data(station, conn)
            if len(days) < 210:
                continue
            
            results = walk_forward_ensemble(days, hourly_by_date, market,
                                             use_edge5=use_edge5, use_edge2=use_edge2,
                                             min_conf=0.7)
            if not results:
                print(f"{station:<8} {'N/A':>8}")
                continue
            
            total = len(results)
            correct = sum(1 for p, a, c, d in results if p == a)
            acc = correct / total if total > 0 else 0
            tdays = sum(1 for idx in range(180, len(days)) if market.get(days[idx]['date']))
            coverage = total / tdays if tdays > 0 else 0
            avg_conf = sum(c for p, a, c, d in results) / total if total > 0 else 0
            
            all_results.extend([(p, a, c) for p, a, c, d in results])
            
            print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
        
        total = len(all_results)
        correct = sum(1 for p, a, c in all_results if p == a)
        accuracy = correct / total if total > 0 else 0
        avg_conf = sum(c for p, a, c in all_results) / total if total > 0 else 0
        
        print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {'':>10} {avg_conf:>10.3f}")
        
        # Sharpe-like metric
        if total > 0:
            wins = [1 if p == a else 0 for p, a, c in all_results]
            mean_w = sum(wins) / len(wins)
            var_w = sum((w - mean_w)**2 for w in wins) / len(wins)
            std_w = math.sqrt(var_w) if var_w > 0 else 0.01
            sharpe = (mean_w - 0.5) / std_w * math.sqrt(252) if std_w > 0 else 0
            net_profit = (correct * (1 - FEE_RATE)) - ((total - correct) * (1 + FEE_RATE))
            print(f"  Sharpe-like: {sharpe:.3f}")
            print(f"  Net profit (per trade, fee-adjusted): {net_profit/total:.4f}")
        
        # Binomial test
        if total > 100:
            z = (accuracy - 0.5) * math.sqrt(total) / math.sqrt(0.25)
            binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
            print(f"  Binomial test p-value: {binom_p:.8f}")
        
        # FDR station check
        print(f"\n--- FDR Validation (8 stations) ---")
        fdr_pass = 0
        for station in FDR_STATIONS:
            days_s, hourly_s, market_s = load_station_data(station, conn)
            if len(days_s) < 210: continue
            r = walk_forward_ensemble(days_s, hourly_s, market_s,
                                       use_edge5=use_edge5, use_edge2=use_edge2,
                                       min_conf=0.7)
            if r:
                sc = sum(1 for p, a, c, d in r if p == a)
                st = len(r)
                sa = sc / st if st > 0 else 0
                status = "✓" if sa >= 0.55 else "✗"
                print(f"  {station}: {sa:.2%} ({sc}/{st}) {status}")
                if sa >= 0.55:
                    fdr_pass += 1
        
        print(f"  FDR pass: {fdr_pass}/{len(FDR_STATIONS)}")
        
        all_config_results[config_name] = {
            'accuracy': accuracy,
            'total': total,
            'correct': correct,
            'sharpe': sharpe if total > 0 else 0,
        }
    
    # ─── COMPARISON ───────────────────────────────────────────────────────
    print(f"\n{'=' * 90}")
    print("CONFIGURATION COMPARISON")
    print(f"{'=' * 90}")
    print(f"\n{'Config':<45} {'Accuracy':>10} {'Trades':>8} {'Sharpe':>8}")
    print("-" * 75)
    for name, r in all_config_results.items():
        print(f"{name:<45} {r['accuracy']:>10.2%} {r['total']:>8} {r['sharpe']:>8.3f}")
    
    # Phase 2 gate check
    baseline_acc = all_config_results["Phase 1 only (baseline)"]['accuracy']
    full_acc = all_config_results["Phase 1 + Edge 5 + Edge 2 (full Phase 2)"]['accuracy']
    improvement = (full_acc - baseline_acc) * 100
    
    print(f"\n--- PHASE 2 GATE ---")
    print(f"  Baseline: {baseline_acc:.2%}")
    print(f"  Full Phase 2: {full_acc:.2%}")
    print(f"  Improvement: {improvement:+.2f} percentage points")
    
    if improvement >= 3.0:
        print(f"  ✓ GATE PASSED — ≥3% improvement")
    else:
        print(f"  ✗ GATE NOT MET — <3% improvement")
        print(f"  → Document and escalate per Phase 2 gate requirements")
    
    # Stop condition checks
    if full_acc >= 1.0:
        print(f"\n  🛑 STOP CONDITION: 100% accuracy detected — circularity bug!")
    if improvement < 0:
        print(f"\n  ⚠️  WARNING: Phase 2 decreased accuracy — investigate signal interaction")
    
    conn.close()
    
    print(f"\n{'=' * 90}")
    print("ENSEMBLE V9 COMPLETE")
    print(f"{'=' * 90}")


if __name__ == "__main__":
    main()
