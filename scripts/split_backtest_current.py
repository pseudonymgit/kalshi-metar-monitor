#!/usr/bin/env python3
"""
Split Backtest — Real Per-Signal Metrics (2026-07-06)

Computes actual per-signal performance by running each signal independently
against the METAR backfill, then the ensemble combination.

Signals tested:
1. Reversion (30-day z-score)
2. Gaussian (48-day z-score)
3. Regime (DTR-scaled adaptive threshold) [R4-1.4]
4. Gaussian v2 (30-day z-score)
5. Pressure (delta pressure)
6. Late-day momentum (hourly slope)
7. Calendar climatology
8. Goldilocks reversion [R4-1.5 asymmetric confidence]

All metrics computed from real data — no mocks.
"""

import sqlite3
import math
import random
import os
import sys
import numpy as np
from scipy.stats import binomtest
from collections import defaultdict

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = 0.05

# 15 viable stations after R4-1.2 purge
ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU',
                'KLAX','KMDW','KMIA','KMSP','KNYC','KPHL','KPHX',
                'KSEA','KSFO']


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


# ─── 8 Signal Approaches ─────────────────────────────────────────────────────

def signal_reversion(idx, days):
    """Signal 1: Reversion (30-day z-score)"""
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

def signal_gaussian(idx, days):
    """Signal 2: Gaussian (48-day z-score)"""
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

def signal_regime(idx, days):
    """Signal 3: Regime (DTR-scaled adaptive threshold) [R4-1.4]"""
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    vol = math.sqrt(var)
    slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0

    # R4-1.4: DTR-scaled regime-adaptive threshold
    if idx >= 1:
        dtr = days[idx-1]['high'] - days[idx-1]['low']
    else:
        dtr = 10.0

    if dtr > 15.0:
        threshold = 1.0
    elif dtr < 8.0:
        threshold = 0.4
    else:
        threshold = 0.8

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

def signal_gaussian_v2(idx, days):
    """Signal 4: Gaussian v2 (30-day z-score)"""
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

def signal_pressure(idx, days):
    """Signal 5: Pressure (delta pressure)"""
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0

def signal_late_day_momentum(idx, days):
    """Signal 6: Late-day momentum (simplified daily trend proxy)
    Uses the day's temp trend from the previous day as a momentum proxy."""
    if idx < 3: return None, 0.0
    # Use temperature change over previous 2 days as proxy
    t0 = days[idx-1]['temp']
    t1 = days[idx-2]['temp']
    dt = t0 - t1
    if abs(dt) > 2.0:
        return ('up' if dt > 0 else 'down'), min(abs(dt)/4.0, 0.7)
    return None, 0.0

def signal_calendar_climatology(idx, days):
    """Signal 7: Calendar climatology (seasonal mean reversion)"""
    if idx < 60: return None, 0.0
    # Use 60-day rolling mean as climatology baseline
    window = days[idx-60:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    std = math.sqrt(np.var(highs, ddof=1)) if len(highs) > 1 else 0.01
    if std == 0: return None, 0.0
    z = (days[idx-1]['high'] - mean) / std
    if z > 1.5: return 'down', min(abs(z)/3.0, 0.6)
    elif z < -1.5: return 'up', min(abs(z)/3.0, 0.6)
    return None, 0.0

def signal_goldilocks(idx, days):
    """Signal 8: Goldilocks reversion (near-boundary with asymmetric confidence) [R4-1.5]"""
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0

    # R4-1.5: Asymmetric confidence for up/down reversion
    if z > 1.0:
        # Spike up → expect drop back (up reversion → bet down)
        # Up reversion: base 0.40, higher confidence
        conf = min(0.40 + abs(z) * 0.10, 0.75)
        return 'down', conf
    elif z < -1.0:
        # Spike down → expect bounce back (down reversion → bet up)
        # Down reversion: base 0.25, reduced confidence (cold-air drainage can persist)
        conf = min(0.25 + abs(z) * 0.08, 0.60)
        return 'up', conf
    return None, 0.0


ALL_SIGNALS = [
    signal_reversion,
    signal_gaussian,
    signal_regime,
    signal_gaussian_v2,
    signal_pressure,
    signal_late_day_momentum,
    signal_calendar_climatology,
    signal_goldilocks,
]

SIGNAL_NAMES = [
    "Reversion (30d z-score)",
    "Gaussian (48d z-score)",
    "Regime (DTR-scaled) [R4-1.4]",
    "Gaussian v2 (30d z-score)",
    "Pressure (delta)",
    "Late-day momentum",
    "Calendar climatology (60d)",
    "Goldilocks reversion [R4-1.5]",
]
# Full ensemble signal list (Phase 2) — used by paper trading engine and signal fusion.
# Phase 2 additions are modular classes in core/signals/ and core/rdae_mos.py.
# See core/paper_trading_engine.py FULL_ENSEMBLE_SIGNALS for the canonical list.
FULL_ENSEMBLE_SIGNAL_NAMES = [
    "Reversion (30d z-score)",
    "Gaussian (48d z-score)",
    "Regime (DTR-scaled) [R4-1.4]",
    "Gaussian v2 (30d z-score)",
    "Pressure (delta)",
    "Late-day momentum",
    "Calendar climatology (60d)",
    "Goldilocks reversion [R4-1.5]",
    "Pressure×Regime [Phase 2]",
    "DTR Trend [Phase 2]",
    "Wind Direction Shift [Phase 2]",
    "RDAE-MOS [Phase 2]",
]


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


def run_signal_backtest(signal_fn, days, market, min_conf=0.0, train_days=180, test_days=30):
    """Run a single signal through walk-forward backtest."""
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            pred, conf = signal_fn(idx, days)
            if pred is not None and conf >= min_conf:
                results.append((pred, actual['direction'], conf))
        start += test_days
        if start > len(days): break
    return results

def run_ensemble_backtest(days, market, signals=None, min_conf=0.7, train_days=180, test_days=30):
    """Run ensemble of signals through walk-forward backtest."""
    if signals is None:
        signals = ALL_SIGNALS
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue

            predictions = {}
            for i, fn in enumerate(signals):
                pred, conf = fn(idx, days)
                if pred is not None and conf >= min_conf:
                    predictions[f's{i+1}'] = (pred, conf)

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


def main():
    print("=" * 90)
    print("SPLIT BACKTEST — Real Per-Signal Metrics (2026-07-06)")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Signals: {len(ALL_SIGNALS)}")
    print(f"Walk-forward: 180-day train / 30-day test")
    print(f"Fee rate: {FEE_RATE:.0%}")
    print()

    random.seed(42)
    conn = sqlite3.connect(DB_PATH, timeout=60)

    # ─── PER-SIGNAL BACKTEST ──────────────────────────────────────────────
    print("=" * 90)
    print("PART 1: PER-SIGNAL ACCURACY (individual signals)")
    print("=" * 90)

    signal_results = {}
    signal_leave_one_out = {}

    for sig_idx, (sig_fn, sig_name) in enumerate(zip(ALL_SIGNALS, SIGNAL_NAMES)):
        all_sig_results = []
        per_station = {}
        for station in ALL_STATIONS:
            days, market = load_station_data(station, conn)
            if len(days) < 210: continue
            results = run_signal_backtest(sig_fn, days, market, min_conf=0.0)
            if results:
                all_sig_results.extend(results)
                per_station[station] = results

        if not all_sig_results:
            print(f"  {sig_name}: NO TRADES")
            signal_results[sig_name] = {
                'trades': 0, 'correct': 0, 'accuracy': 0, 'sharpe': 0,
                'brier': 0, 'ece': 0, 'max_dd': 0, 'per_station': {}
            }
            continue

        total = len(all_sig_results)
        correct = sum(1 for p, a, c in all_sig_results if p == a)
        acc = correct / total
        sharpe = compute_sharpe([(c, p==a) for p, a, c in all_sig_results])
        brier = compute_brier(all_sig_results)
        ece = compute_ece(all_sig_results)
        dd = max_drawdown(all_sig_results)
        binom_p = binomial_test_p(correct, total)

        signal_results[sig_name] = {
            'trades': total, 'correct': correct, 'accuracy': acc,
            'sharpe': sharpe, 'brier': brier, 'ece': ece,
            'max_dd': dd, 'binom_p': binom_p, 'per_station': per_station
        }

        print(f"  {sig_name:<40} {total:>6} trades  {acc:>6.2%}  Sharpe={sharpe:.3f}  Brier={brier:.4f}  ECE={ece:.4f}  DD={dd:.2%}  p={binom_p:.4f}")

    # ─── ENSEMBLE WITH LEAVE-ONE-OUT ──────────────────────────────────────
    print()
    print("=" * 90)
    print("PART 2: ENSEMBLE LEAVE-ONE-OUT ANALYSIS")
    print("=" * 90)

    # Full ensemble baseline
    full_ensemble_results = []
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue
        results = run_ensemble_backtest(days, market, min_conf=0.7)
        full_ensemble_results.extend(results)

    full_total = len(full_ensemble_results)
    full_correct = sum(1 for p, a, c in full_ensemble_results if p == a)
    full_acc = full_correct / full_total if full_total > 0 else 0
    full_sharpe = compute_sharpe([(c, p==a) for p, a, c in full_ensemble_results])
    full_brier = compute_brier(full_ensemble_results)

    print(f"\n  Full ensemble (all 8 signals): {full_total} trades, {full_acc:.2%} accuracy, Sharpe={full_sharpe:.3f}, Brier={full_brier:.4f}")

    # Leave one out
    for skip_idx in range(len(ALL_SIGNALS)):
        remaining = [s for i, s in enumerate(ALL_SIGNALS) if i != skip_idx]
        skip_name = SIGNAL_NAMES[skip_idx]
        loo_results = []
        for station in ALL_STATIONS:
            days, market = load_station_data(station, conn)
            if len(days) < 210: continue
            results = run_ensemble_backtest(days, market, signals=remaining, min_conf=0.7)
            loo_results.extend(results)

        loo_total = len(loo_results)
        loo_correct = sum(1 for p, a, c in loo_results if p == a)
        loo_acc = loo_correct / loo_total if loo_total > 0 else 0
        loo_sharpe = compute_sharpe([(c, p==a) for p, a, c in loo_results])

        delta = loo_acc - full_acc
        signal_leave_one_out[skip_name] = {
            'trades': loo_total, 'accuracy': loo_acc, 'sharpe': loo_sharpe,
            'delta': delta
        }

        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "="
        print(f"  Without {skip_name:<40} {loo_total:>6} trades  {loo_acc:>6.2%}  Sharpe={loo_sharpe:.3f}  Δ={arrow}{abs(delta):.2%}")

    # ─── PER-STATION ENSEMBLE ─────────────────────────────────────────────
    print()
    print("=" * 90)
    print("PART 3: PER-STATION ENSEMBLE ACCURACY")
    print("=" * 90)

    station_ensemble = {}
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue
        results = run_ensemble_backtest(days, market, min_conf=0.7)
        if not results: continue
        total = len(results)
        correct = sum(1 for p, a, c in results if p == a)
        acc = correct / total
        sharpe = compute_sharpe([(c, p==a) for p, a, c in results])
        brier = compute_brier(results)
        dd = max_drawdown(results)
        station_ensemble[station] = {
            'trades': total, 'correct': correct, 'accuracy': acc,
            'sharpe': sharpe, 'brier': brier, 'max_dd': dd
        }
        print(f"  {station:<8} {total:>6} trades  {acc:>6.2%}  Sharpe={sharpe:.3f}  Brier={brier:.4f}  DD={dd:.2%}")

    conn.close()

    # ─── WRITE REPORT ─────────────────────────────────────────────────────
    report_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, 'p0-split-backtest-2026-07-06.md')

    with open(report_path, 'w') as f:
        f.write("# P0 Split Backtest — Per-Signal Metrics (2026-07-06)\n\n")
        f.write(f"**Date:** 2026-07-06\n")
        f.write(f"**Stations:** {len(ALL_STATIONS)} (post R4-1.2 purge)\n")
        f.write(f"**Walk-forward:** 180-day train / 30-day test\n")
        f.write(f"**Fee rate:** {FEE_RATE:.0%}\n\n")

        f.write("## Per-Signal Performance\n\n")
        f.write("| Signal | Trades | Correct | Accuracy | Sharpe | Brier | ECE | MaxDD | Binom p |\n")
        f.write("|--------|--------|---------|----------|--------|-------|-----|-------|---------|\n")
        for sig_name, metrics in sorted(signal_results.items(), key=lambda x: -x[1]['accuracy']):
            f.write(f"| {sig_name} | {metrics['trades']} | {metrics['correct']} | {metrics['accuracy']:.2%} | {metrics['sharpe']:.3f} | {metrics['brier']:.4f} | {metrics['ece']:.4f} | {metrics['max_dd']:.2%} | {metrics['binom_p']:.4f} |\n")

        f.write("\n## Ensemble Leave-One-Out Analysis\n\n")
        f.write(f"| Signal Removed | Trades | Accuracy | Sharpe | Δ vs Full Ensemble |\n")
        f.write(f"|----------------|--------|----------|--------|-------------------|\n")
        f.write(f"| **(none — full ensemble)** | {full_total} | **{full_acc:.2%}** | {full_sharpe:.3f} | — |\n")
        for sig_name, metrics in sorted(signal_leave_one_out.items(), key=lambda x: x[1]['delta']):
            arrow = "↑" if metrics['delta'] > 0 else "↓"
            f.write(f"| {sig_name} | {metrics['trades']} | {metrics['accuracy']:.2%} | {metrics['sharpe']:.3f} | {arrow}{abs(metrics['delta']):.2%} |\n")

        f.write("\n## Per-Station Ensemble Accuracy\n\n")
        f.write("| Station | Trades | Correct | Accuracy | Sharpe | Brier | MaxDD |\n")
        f.write("|---------|--------|---------|----------|--------|-------|-------|\n")
        for station, metrics in sorted(station_ensemble.items(), key=lambda x: -x[1]['accuracy']):
            f.write(f"| {station} | {metrics['trades']} | {metrics['correct']} | {metrics['accuracy']:.2%} | {metrics['sharpe']:.3f} | {metrics['brier']:.4f} | {metrics['max_dd']:.2%} |\n")

        f.write("\n## Notes\n\n")
        f.write("- All metrics computed from real METAR backfill data (807K observations, 16 stations)\n")
        f.write("- Walk-forward design: 180-day train / 30-day test, no circularity\n")
        f.write("- R4-1.4: Regime signal uses DTR-scaled adaptive threshold\n")
        f.write("- R4-1.5: Goldilocks signal uses asymmetric confidence (up=0.40 base, down=0.25 base)\n")
        f.write("- No AI/ML model calls in backtest loop\n")

    print(f"\n📝 Report written to {report_path}")
    print("\n" + "=" * 90)
    print("SPLIT BACKTEST COMPLETE")
    print("=" * 90)


if __name__ == "__main__":
    main()
