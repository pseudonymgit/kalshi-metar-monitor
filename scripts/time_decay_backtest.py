#!/usr/bin/env python3
"""
Time-Decay Weighted Signal Reliability Backtest (P1.5) — 2026-07-06

Tests the TimeDecaySignalManager from core/signal_fusion.py:
1. Tracks per-signal per-city performance with exponential forgetting (decay=0.9, window=30)
2. Adjusts confidence: adjusted_conf = sqrt(raw_conf * reliability)
3. Modifies LOP weighting by reliability

Compares baseline ensemble vs time-decay-weighted ensemble.
No AI/ML in any loop.
"""

import sqlite3
import math
import random
import os
import sys
import numpy as np
from scipy.stats import binomtest
from collections import defaultdict

# Add core to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, 'core'))

DB_PATH = os.path.join(REPO_ROOT, 'data', 'metar_backfill.db')
FEE_RATE = 0.05

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU',
                'KLAX','KMDW','KMIA','KMSP','KNYC','KPHL','KPHX',
                'KSEA','KSFO']

SIGNAL_NAMES = ['reversion', 'gaussian', 'regime', 'gaussian_v2', 'pressure']


def load_station_data(station, conn):
    cur = conn.cursor()
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
    return days, market


# ─── Signal approaches ───────────────────────────────────────────────────────

def approach_reversion(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
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
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0: return 'down', abs(z)
    elif z < -1.0: return 'up', abs(z)
    return None, 0.0

def approach_regime(idx, days):
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    vol = math.sqrt(var)
    slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0
    if idx >= 1:
        dtr = days[idx-1]['high'] - days[idx-1]['low']
    else:
        dtr = 10.0
    if dtr > 15.0: threshold = 1.0
    elif dtr < 8.0: threshold = 0.4
    else: threshold = 0.8
    if vol < 1.0 and abs(slope) < threshold:
        if idx >= 31:
            w30 = days[idx-31:idx-1]
            h30 = [d['high'] for d in w30]
            m30 = sum(h30) / len(h30)
            dist = days[idx-1]['high'] - m30
            if dist > 1.0:
                conf = min(dist/3.0, 0.8)
                if dtr < 8.0: conf *= 0.6
                return 'down', conf
            elif dist < -1.0:
                conf = min(abs(dist)/3.0, 0.8)
                if dtr < 8.0: conf *= 0.6
                return 'up', conf
    return None, 0.0

def approach_gaussian_v2(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_pressure(idx, days):
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0

APPROACHES = [approach_reversion, approach_gaussian, approach_regime,
              approach_gaussian_v2, approach_pressure]


# ─── Simple TimeDecaySignalManager (inline, no external deps) ────────────────

class SimpleTimeDecayManager:
    """Simplified inline version of TimeDecaySignalManager for backtesting."""
    def __init__(self, signal_names, decay_factor=0.9, window=30):
        self.signal_names = signal_names
        self.decay_factor = decay_factor
        self.window = window
        self.history = defaultdict(list)  # {(signal_idx, station): [(date_str, correct_bool), ...]}
    
    def update(self, signal_idx, station, date_str, correct):
        self.history[(signal_idx, station)].append((date_str, correct))
    
    def compute_reliability(self, signal_idx, station):
        key = (signal_idx, station)
        history = self.history.get(key, [])
        if not history:
            return 0.5
        recent = history[-self.window:]
        if not recent:
            return 0.5
        n = len(recent)
        # Minimum observation requirement for reliable reliability calculation
        MIN_OBS = 3
        if n < MIN_OBS:
            return 0.5  # Not enough data for reliable estimate
        weighted_sum = 0.0
        weight_sum = 0.0
        for i, (date, correct) in enumerate(recent):
            w = self.decay_factor ** (n - 1 - i)
            weighted_sum += w * (1.0 if correct else 0.0)
            weight_sum += w
        reliability = weighted_sum / weight_sum if weight_sum > 0 else 0.5
        # Floor to prevent extreme values with few observations
        # If n < 10, clamp to [0.3, 0.7] to avoid overconfidence
        if n < 10:
            reliability = max(0.3, min(0.7, reliability))
        return max(0.1, min(1.0, reliability))
    
    def adjust_confidence(self, signal_idx, station, raw_conf):
        reliability = self.compute_reliability(signal_idx, station)
        return math.sqrt(max(0.0, raw_conf) * reliability)
    
    def get_lop_weight(self, signal_idx, station):
        reliability = self.compute_reliability(signal_idx, station)
        if reliability <= 0.01:
            return 0.0
        return math.log(reliability / (1.0 - reliability)) if reliability < 0.99 else 4.6


# ─── Metrics ─────────────────────────────────────────────────────────────────

def compute_sharpe(returns, fee_rate=0.05):
    if not returns: return 0.0
    vals = []
    for conf, ok in returns:
        gross = 2 * conf if ok else -2 * conf
        fee = fee_rate * conf
        vals.append(gross - fee)
    n = len(vals)
    if n == 0: return 0.0
    mean = sum(vals) / n
    var = np.var(vals, ddof=1) if n > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    return mean / std if std > 0 else 0.0

def compute_brier(results):
    if not results: return 0.0
    total = 0.0
    for pred, actual, conf in results:
        prob_up = conf if pred == 'up' else 1.0 - conf
        outcome = 1.0 if actual == 'up' else 0.0
        total += (prob_up - outcome) ** 2
    return total / len(results)

def compute_ece(results, n_bins=10):
    if not results: return 0.0
    bins = [[] for _ in range(n_bins)]
    for pred, actual, conf in results:
        bin_idx = min(int(conf * n_bins), n_bins - 1)
        ok = (pred == actual)
        bins[bin_idx].append((conf, ok))
    ece = 0.0
    n = len(results)
    for b in bins:
        if not b: continue
        acc = sum(1 for _, ok in b if ok) / len(b)
        avg_conf = sum(c for c, _ in b) / len(b)
        ece += (len(b) / n) * abs(acc - avg_conf)
    return ece

def max_drawdown(results, initial=250.0, bet=10.0):
    bankroll = initial
    peak = bankroll
    max_dd = 0.0
    for pred, actual, conf in results:
        ok = (pred == actual)
        position = min(bet * conf, bankroll * 0.08)
        if ok: bankroll += position * 0.95
        else: bankroll -= position
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd

def binomial_test_p(correct, total, null_p=0.5):
    if total == 0: return 1.0
    expected = null_p * total
    if correct >= expected:
        return binomtest(correct, total, null_p, alternative='greater').pvalue
    else:
        return binomtest(correct, total, null_p, alternative='less').pvalue


# ─── Backtest: Baseline ──────────────────────────────────────────────────────

def walk_forward_baseline(days, market, min_conf=0.7, train_days=180, test_days=30):
    """Standard ensemble without time-decay weighting."""
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue

            predictions = {}
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf >= min_conf:
                    predictions[f'a{i+1}'] = (pred, conf)

            if len(predictions) >= 2:
                wsum = sum((1 if p=='up' else -1) * c for _, (p, c) in predictions.items())
                aw = sum(c for _, (p, c) in predictions.items())
                if aw > 0:
                    conf = abs(wsum) / aw
                    if conf >= min_conf:
                        predicted = 'up' if wsum > 0 else 'down'
                        results.append((predicted, actual['direction'], conf))
            elif len(predictions) == 1:
                pred, conf = list(predictions.values())[0]
                if conf >= min_conf:
                    results.append((pred, actual['direction'], conf))
        start += test_days
        if start > len(days): break
    return results


# ─── Backtest: Time-Decay Weighted ──────────────────────────────────────────

def walk_forward_time_decay(days, market, station, min_conf=0.7, train_days=180, test_days=30):
    """Ensemble with time-decay weighted signal reliability."""
    decay_manager = SimpleTimeDecayManager(SIGNAL_NAMES, decay_factor=0.9, window=30)
    
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue

            predictions = {}
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf >= min_conf:
                    # Apply time-decay adjusted confidence
                    adjusted_conf = decay_manager.adjust_confidence(i, station, conf)
                    if adjusted_conf >= min_conf:
                        predictions[f'a{i+1}'] = (pred, adjusted_conf, conf)  # store both

            if len(predictions) >= 2:
                # Use reliability-weighted LOP
                wsum = 0.0
                weight_total = 0.0
                for key, (p, adj_conf, raw_conf) in predictions.items():
                    sig_idx = int(key[1:]) - 1
                    lop_w = decay_manager.get_lop_weight(sig_idx, station)
                    # Use LOP weight times adjusted confidence
                    w = max(0.01, lop_w + 1.0) * adj_conf  # ensure positive weight
                    wsum += (1 if p == 'up' else -1) * w
                    weight_total += w
                
                if weight_total > 0:
                    conf = abs(wsum) / weight_total
                    if conf >= min_conf:
                        predicted = 'up' if wsum > 0 else 'down'
                        results.append((predicted, actual['direction'], conf))
                        
                        # Update reliability tracking
                        for key, (p, adj_conf, raw_conf) in predictions.items():
                            sig_idx = int(key[1:]) - 1
                            correct = (p == actual['direction'])
                            decay_manager.update(sig_idx, station, date, correct)
            elif len(predictions) == 1:
                pred, adj_conf, raw_conf = list(predictions.values())[0]
                if adj_conf >= min_conf:
                    results.append((pred, actual['direction'], adj_conf))
                    sig_idx = int(list(predictions.keys())[0][1:]) - 1
                    correct = (pred == actual['direction'])
                    decay_manager.update(sig_idx, station, date, correct)
        start += test_days
        if start > len(days): break
    return results, decay_manager


def main():
    print("=" * 90)
    print("TIME-DECAY WEIGHTED SIGNAL RELIABILITY BACKTEST (P1.5) — 2026-07-06")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Decay factor: 0.9, Window: 30 days")
    print(f"Confidence adjustment: sqrt(raw_conf * reliability)")
    print()

    random.seed(42)
    conn = sqlite3.connect(DB_PATH, timeout=60)

    # ─── PART 1: Per-station baseline vs time-decay ───────────────────────
    print("=" * 90)
    print("PART 1: PER-STATION BASELINE vs TIME-DECAY WEIGHTED")
    print("=" * 90)

    all_baseline_results = []
    all_decay_results = []
    station_comparison = []

    print(f"\n{'Station':<8} {'Base Tr':>8} {'Base Acc':>10} {'Decay Tr':>8} {'Decay Acc':>10} {'Delta':>8} {'Base Sh':>8} {'Dec Sh':>8}")
    print("-" * 80)

    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue

        # Baseline
        base_results = walk_forward_baseline(days, market, min_conf=0.7)
        # Time-decay
        decay_results, decay_mgr = walk_forward_time_decay(days, market, station, min_conf=0.7)

        all_baseline_results.extend(base_results)
        all_decay_results.extend(decay_results)

        base_total = len(base_results)
        base_correct = sum(1 for p, a, c in base_results if p == a)
        base_acc = base_correct / base_total if base_total > 0 else 0
        base_sharpe = compute_sharpe([(c, p==a) for p, a, c in base_results])

        decay_total = len(decay_results)
        decay_correct = sum(1 for p, a, c in decay_results if p == a)
        decay_acc = decay_correct / decay_total if decay_total > 0 else 0
        decay_sharpe = compute_sharpe([(c, p==a) for p, a, c in decay_results])

        delta = decay_acc - base_acc
        station_comparison.append({
            'station': station,
            'base_trades': base_total, 'base_acc': base_acc, 'base_sharpe': base_sharpe,
            'decay_trades': decay_total, 'decay_acc': decay_acc, 'decay_sharpe': decay_sharpe,
            'delta': delta
        })

        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "="
        print(f"{station:<8} {base_total:>8} {base_acc:>10.2%} {decay_total:>8} {decay_acc:>10.2%} {arrow}{abs(delta):>7.2%} {base_sharpe:>8.3f} {decay_sharpe:>8.3f}")

    # ─── PART 2: Aggregate comparison ─────────────────────────────────────
    print()
    print("=" * 90)
    print("PART 2: AGGREGATE COMPARISON")
    print("=" * 90)

    base_total = len(all_baseline_results)
    base_correct = sum(1 for p, a, c in all_baseline_results if p == a)
    base_acc = base_correct / base_total if base_total > 0 else 0
    base_sharpe = compute_sharpe([(c, p==a) for p, a, c in all_baseline_results])
    base_brier = compute_brier(all_baseline_results)
    base_ece = compute_ece(all_baseline_results)
    base_dd = max_drawdown(all_baseline_results)
    base_binom = binomial_test_p(base_correct, base_total)

    decay_total = len(all_decay_results)
    decay_correct = sum(1 for p, a, c in all_decay_results if p == a)
    decay_acc = decay_correct / decay_total if decay_total > 0 else 0
    decay_sharpe = compute_sharpe([(c, p==a) for p, a, c in all_decay_results])
    decay_brier = compute_brier(all_decay_results)
    decay_ece = compute_ece(all_decay_results)
    decay_dd = max_drawdown(all_decay_results)
    decay_binom = binomial_test_p(decay_correct, decay_total)

    print(f"\n  {'Metric':<25} {'Baseline':>15} {'Time-Decay':>15} {'Delta':>10}")
    print(f"  {'-'*25} {'-'*15} {'-'*15} {'-'*10}")
    print(f"  {'Trade count':<25} {base_total:>15} {decay_total:>15} {decay_total - base_total:>+10}")
    print(f"  {'Accuracy':<25} {base_acc:>15.2%} {decay_acc:>15.2%} {decay_acc - base_acc:>+10.2%}")
    print(f"  {'Sharpe ratio':<25} {base_sharpe:>15.3f} {decay_sharpe:>15.3f} {decay_sharpe - base_sharpe:>+10.3f}")
    print(f"  {'Brier score':<25} {base_brier:>15.4f} {decay_brier:>15.4f} {decay_brier - base_brier:>+10.4f}")
    print(f"  {'ECE':<25} {base_ece:>15.4f} {decay_ece:>15.4f} {decay_ece - base_ece:>+10.4f}")
    print(f"  {'Max drawdown':<25} {base_dd:>15.2%} {decay_dd:>15.2%} {decay_dd - base_dd:>+10.2%}")
    print(f"  {'Binomial p':<25} {base_binom:>15.4f} {decay_binom:>15.4f} {decay_binom - base_binom:>+10.4f}")

    conn.close()

    # ─── WRITE REPORT ─────────────────────────────────────────────────────
    report_dir = os.path.join(REPO_ROOT, 'reports')
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, 'p1-time-decay-2026-07-06.md')

    with open(report_path, 'w') as f:
        f.write("# P1.5 — Time-Decay Weighted Signal Reliability (2026-07-06)\n\n")
        f.write(f"**Date:** 2026-07-06\n")
        f.write(f"**Stations:** {len(ALL_STATIONS)} (post R4-1.2 purge)\n")
        f.write(f"**Method:** Exponential forgetting (decay=0.9, window=30 days)\n")
        f.write(f"**Confidence adjustment:** adjusted_conf = sqrt(raw_conf * reliability)\n")
        f.write(f"**LOP weighting:** reliability-weighted log-odds\n\n")

        f.write("## Aggregate Comparison\n\n")
        f.write("| Metric | Baseline | Time-Decay | Delta |\n")
        f.write("|--------|----------|------------|-------|\n")
        f.write(f"| Trade count | {base_total} | {decay_total} | {decay_total - base_total:+d} |\n")
        f.write(f"| Accuracy | {base_acc:.2%} | {decay_acc:.2%} | {decay_acc - base_acc:+.2%} |\n")
        f.write(f"| Sharpe ratio | {base_sharpe:.3f} | {decay_sharpe:.3f} | {decay_sharpe - base_sharpe:+.3f} |\n")
        f.write(f"| Brier score | {base_brier:.4f} | {decay_brier:.4f} | {decay_brier - base_brier:+.4f} |\n")
        f.write(f"| ECE | {base_ece:.4f} | {decay_ece:.4f} | {decay_ece - base_ece:+.4f} |\n")
        f.write(f"| Max drawdown | {base_dd:.2%} | {decay_dd:.2%} | {decay_dd - base_dd:+.2%} |\n")
        f.write(f"| Binomial p | {base_binom:.4f} | {decay_binom:.4f} | {decay_binom - base_binom:+.4f} |\n")

        f.write("\n## Per-Station Comparison\n\n")
        f.write("| Station | Base Trades | Base Acc | Base Sharpe | Decay Trades | Decay Acc | Decay Sharpe | Delta |\n")
        f.write("|---------|-------------|----------|-------------|-------------|-----------|-------------|-------|\n")
        for entry in station_comparison:
            f.write(f"| {entry['station']} | {entry['base_trades']} | {entry['base_acc']:.2%} | {entry['base_sharpe']:.3f} | {entry['decay_trades']} | {entry['decay_acc']:.2%} | {entry['decay_sharpe']:.3f} | {entry['delta']:+.2%} |\n")

        f.write("\n## How It Works\n\n")
        f.write("### TimeDecaySignalManager\n\n")
        f.write("1. **Tracking**: Records per-signal per-station prediction outcomes with timestamps\n")
        f.write("2. **Reliability**: Computes exponentially weighted recent accuracy:\n")
        f.write("   - `reliability = Σ(decay^(t-i) * correct_i) / Σ(decay^(t-i))`\n")
        f.write("   - Most recent predictions get highest weight (decay=0.9)\n")
        f.write("   - Window of 30 days ensures adaptivity to regime changes\n")
        f.write("3. **Confidence adjustment**: `adjusted_conf = sqrt(raw_conf * reliability)`\n")
        f.write("   - Geometric mean of raw confidence and reliability\n")
        f.write("   - Penalizes overconfident signals with poor recent track record\n")
        f.write("4. **LOP weighting**: Signals with higher reliability get proportionally more weight\n")
        f.write("   via `log(reliability / (1 - reliability))` weighting in the opinion pool\n")

        f.write("\n## Notes\n\n")
        f.write("- All metrics computed from real METAR backfill data\n")
        f.write("- Walk-forward: 180-day train / 30-day test\n")
        f.write("- No AI/ML model calls in any loop\n")
        f.write("- TimeDecaySignalManager class added to core/signal_fusion.py\n")

    print(f"\n📝 Report written to {report_path}")
    print("\n" + "=" * 90)
    print("TIME-DECAY BACKTEST COMPLETE")
    print("=" * 90)


if __name__ == "__main__":
    main()
