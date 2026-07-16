#!/usr/bin/env python3
"""
Per-Station Skill Gating (P1.2-P1.3) — 2026-07-06

For each station × market (HIGH/LOW), computes:
1. Brier Skill Score (BSS) vs persistence baseline (today = yesterday)
2. BSS vs climatology baseline (15-day rolling mean)
3. Block bootstrap 95% CI (block size=5)
4. Trade/no-trade decision based on BSS > 0 against both baselines

Then runs the ensemble backtest restricted to skilled stations only
and compares accuracy: all-stations vs skilled-only.

Uses the 13-month METAR backfill for daily HIGH/LOW extraction.
No AI/ML in any loop.
"""

import sqlite3
import math
import random
import os
import numpy as np
from scipy.stats import binomtest
from collections import defaultdict

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = 0.05

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU',
                'KLAX','KMDW','KMIA','KMSP','KNYC','KPHL','KPHX',
                'KSEA','KSFO']


def load_daily_highs_lows(station, conn):
    """Extract daily HIGH/LOW from METAR observations."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if r[1] is None or r[2] is None: continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'pressure': r[3]})
    return days


def load_market_directions(station, conn, market_type='HIGH'):
    """Load settlement epoch directions."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type=? AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station, market_type))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = 'up' if r[1] > r[2] else 'down'
    return market


# ─── Signal approaches (same as ensemble) ─────────────────────────────────────


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

APPROACHES = [approach_gaussian, approach_gaussian_v2, approach_pressure]


# ─── Brier Skill Score ───────────────────────────────────────────────────────

def brier_score(preds, actuals):
    """Compute Brier score. preds and actuals are lists of probabilities."""
    if not preds: return 0.0
    return sum((p - a) ** 2 for p, a in zip(preds, actuals)) / len(preds)


def brier_skill_score(model_preds, model_actuals, baseline_preds, baseline_actuals):
    """BSS = 1 - BS_model / BS_baseline. Positive = model beats baseline."""
    bs_model = brier_score(model_preds, model_actuals)
    bs_baseline = brier_score(baseline_preds, baseline_actuals)
    if bs_baseline == 0: return 0.0
    return 1.0 - (bs_model / bs_baseline)


def block_bootstrap_ci(data, block_size=5, n_boot=2000, confidence=0.95):
    """Block bootstrap for time-series data. data is list of (pred_prob, outcome) pairs."""
    if len(data) < block_size: return (-1.0, 1.0)
    n = len(data)
    n_blocks = math.ceil(n / block_size)
    bss_samples = []
    for _ in range(n_boot):
        # Sample blocks with replacement
        sample = []
        for _ in range(n_blocks):
            start = random.randint(0, n - block_size)
            sample.extend(data[start:start + block_size])
        sample = sample[:n]  # trim to original length

        model_preds = [p for p, _ in sample]
        model_actuals = [a for _, a in sample]
        bs_model = sum((p - a) ** 2 for p, a in zip(model_preds, model_actuals)) / len(sample)

        # Persistence baseline: previous day's outcome as probability
        baseline_preds = []
        baseline_actuals = []
        for i in range(len(sample)):
            if i > 0:
                baseline_preds.append(sample[i-1][1])  # previous outcome
                baseline_actuals.append(sample[i][1])
            else:
                baseline_preds.append(0.5)
                baseline_actuals.append(sample[i][1])
        bs_baseline = sum((p - a) ** 2 for p, a in zip(baseline_preds, baseline_actuals)) / len(sample)

        if bs_baseline > 0:
            bss_samples.append(1.0 - (bs_model / bs_baseline))

    if not bss_samples: return (-1.0, 1.0)
    bss_samples.sort()
    lower = bss_samples[int((1 - confidence) / 2 * n_boot)]
    upper = bss_samples[int((1 + confidence) / 2 * n_boot)]
    return (lower, upper)


def compute_rolling_bss(days, market, station, market_type, window=30):
    """Compute 30-day rolling BSS for a station × market."""
    results = []
    # Build daily predictions and outcomes
    daily_data = []
    for idx in range(180, len(days)):
        date = days[idx]['date']
        actual = market.get(date)
        if actual is None: continue

        # Ensemble prediction
        predictions = {}
        for i, fn in enumerate(APPROACHES):
            pred, conf = fn(idx, days)
            if pred is not None and conf >= 0.5:
                predictions[f'a{i+1}'] = (pred, conf)

        if len(predictions) >= 2:
            wsum = sum((1 if p=='up' else -1) * c for _, (p, c) in predictions.items())
            aw = sum(c for _, (p, c) in predictions.items())
            if aw > 0:
                conf = abs(wsum) / aw
                if conf >= 0.7:
                    predicted = 'up' if wsum > 0 else 'down'
                    prob_up = conf if predicted == 'up' else 1.0 - conf
                    outcome = 1.0 if actual == 'up' else 0.0
                    daily_data.append({
                        'date': date,
                        'prob_up': prob_up,
                        'outcome': outcome,
                        'actual': actual
                    })
        elif len(predictions) == 1:
            pred, conf = list(predictions.values())[0]
            if conf >= 0.7:
                prob_up = conf if pred == 'up' else 1.0 - conf
                outcome = 1.0 if actual == 'up' else 0.0
                daily_data.append({
                    'date': date,
                    'prob_up': prob_up,
                    'outcome': outcome,
                    'actual': actual
                })

    # Compute rolling 30-day BSS
    for i in range(window, len(daily_data)):
        window_data = daily_data[i-window:i]
        model_preds = [d['prob_up'] for d in window_data]
        model_actuals = [d['outcome'] for d in window_data]

        # Persistence baseline: previous day's outcome
        pers_preds = [window_data[j-1]['outcome'] if j > 0 else 0.5 for j in range(len(window_data))]
        pers_actuals = model_actuals

        # Climatology baseline: 15-day rolling mean
        clim_preds = []
        clim_actuals = []
        for j in range(len(window_data)):
            if j >= 15:
                clim_mean = sum(d['outcome'] for d in window_data[j-15:j]) / 15
            else:
                clim_mean = sum(d['outcome'] for d in window_data[:max(j,1)]) / max(j,1)
            clim_preds.append(clim_mean)
            clim_actuals.append(window_data[j]['outcome'])

        bs_model = brier_score(model_preds, model_actuals)
        bs_pers = brier_score(pers_preds, pers_actuals)
        bs_clim = brier_score(clim_preds, clim_actuals)

        bss_pers = 1.0 - (bs_model / bs_pers) if bs_pers > 0 else 0.0
        bss_clim = 1.0 - (bs_model / bs_clim) if bs_clim > 0 else 0.0

        results.append({
            'date': daily_data[i]['date'],
            'bss_persistence': bss_pers,
            'bss_climatology': bss_clim
        })

    return results


def walk_forward_ensemble(days, market, min_conf=0.7, train_days=180, test_days=30):
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
                        results.append((predicted, actual, conf))
            elif len(predictions) == 1:
                pred, conf = list(predictions.values())[0]
                if conf >= min_conf:
                    results.append((pred, actual, conf))
        start += test_days
        if start > len(days): break
    return results


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


def main():
    print("=" * 90)
    print("PER-STATION SKILL GATING (P1.2-P1.3) — 2026-07-06")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print()

    random.seed(42)
    conn = sqlite3.connect(DB_PATH, timeout=60)

    # ─── PART 1: Per-station BSS ──────────────────────────────────────────
    print("=" * 90)
    print("PART 1: PER-STATION BRIER SKILL SCORE vs PERSISTENCE & CLIMATOLOGY")
    print("=" * 90)

    skill_table = []
    station_skill = {}

    for station in ALL_STATIONS:
        days = load_daily_highs_lows(station, conn)
        if len(days) < 210:
            print(f"  {station}: insufficient data ({len(days)} days)")
            continue

        for market_type in ['HIGH', 'LOW']:
            market = load_market_directions(station, conn, market_type)
            if len(market) < 50:
                continue

            # Compute rolling BSS
            rolling_bss = compute_rolling_bss(days, market, station, market_type, window=30)
            if not rolling_bss:
                continue

            # Latest 30-day BSS
            recent = rolling_bss[-30:] if len(rolling_bss) >= 30 else rolling_bss
            avg_bss_pers = sum(r['bss_persistence'] for r in recent) / len(recent)
            avg_bss_clim = sum(r['bss_climatology'] for r in recent) / len(recent)

            # Block bootstrap CI on the most recent window
            # Build (prob, outcome) pairs for bootstrap
            daily_data = []
            for idx in range(180, len(days)):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None: continue
                predictions = {}
                for i, fn in enumerate(APPROACHES):
                    pred, conf = fn(idx, days)
                    if pred is not None and conf >= 0.5:
                        predictions[f'a{i+1}'] = (pred, conf)
                if len(predictions) >= 2:
                    wsum = sum((1 if p=='up' else -1) * c for _, (p, c) in predictions.items())
                    aw = sum(c for _, (p, c) in predictions.items())
                    if aw > 0:
                        conf = abs(wsum) / aw
                        if conf >= 0.7:
                            predicted = 'up' if wsum > 0 else 'down'
                            prob_up = conf if predicted == 'up' else 1.0 - conf
                            outcome = 1.0 if actual == 'up' else 0.0
                            daily_data.append((prob_up, outcome))

            if len(daily_data) >= 30:
                ci_lower, ci_upper = block_bootstrap_ci(daily_data[-200:], block_size=5)
            else:
                ci_lower, ci_upper = -1.0, 1.0

            # Decision: trade only if BSS > 0 against both baselines
            trade_decision = 'TRADE' if (avg_bss_pers > 0 and avg_bss_clim > 0) else 'NO-TRADE'

            skill_table.append({
                'station': station,
                'market': market_type,
                'bss_persistence': avg_bss_pers,
                'bss_climatology': avg_bss_clim,
                'ci_lower': ci_lower,
                'ci_upper': ci_upper,
                'decision': trade_decision
            })

            station_skill[(station, market_type)] = trade_decision

            status = '✓ SKILLED' if trade_decision == 'TRADE' else '✗ UN SKILLED'
            print(f"  {station:<6} {market_type:<5}  BSS_pers={avg_bss_pers:+.4f}  BSS_clim={avg_bss_clim:+.4f}  CI=[{ci_lower:+.4f}, {ci_upper:+.4f}]  {status}")

    # ─── PART 2: Run ensemble on ALL vs SKILLED-only ──────────────────────
    print()
    print("=" * 90)
    print("PART 2: ALL-STATIONS vs SKILLED-ONLY ENSEMBLE")
    print("=" * 90)

    # Determine skilled stations (HIGH market only, since that's what ensemble uses)
    skilled_stations = [s for s in ALL_STATIONS if station_skill.get((s, 'HIGH'), 'NO-TRADE') == 'TRADE']
    unskilled_stations = [s for s in ALL_STATIONS if station_skill.get((s, 'HIGH'), 'NO-TRADE') == 'NO-TRADE']

    print(f"\n  Skilled stations (HIGH): {skilled_stations}")
    print(f"  Unskilled stations (HIGH): {unskilled_stations}")
    print(f"  Skilled: {len(skilled_stations)}/{len(ALL_STATIONS)} stations")

    # All-stations ensemble
    all_results = []
    for station in ALL_STATIONS:
        days = load_daily_highs_lows(station, conn)
        if len(days) < 210: continue
        market = load_market_directions(station, conn, 'HIGH')
        results = walk_forward_ensemble(days, market, min_conf=0.7)
        all_results.extend(results)

    all_total = len(all_results)
    all_correct = sum(1 for p, a, c in all_results if p == a)
    all_acc = all_correct / all_total if all_total > 0 else 0
    all_sharpe = compute_sharpe([(c, p==a) for p, a, c in all_results])
    all_dd = max_drawdown(all_results)

    # Skilled-only ensemble
    skilled_results = []
    for station in skilled_stations:
        days = load_daily_highs_lows(station, conn)
        if len(days) < 210: continue
        market = load_market_directions(station, conn, 'HIGH')
        results = walk_forward_ensemble(days, market, min_conf=0.7)
        skilled_results.extend(results)

    skilled_total = len(skilled_results)
    skilled_correct = sum(1 for p, a, c in skilled_results if p == a)
    skilled_acc = skilled_correct / skilled_total if skilled_total > 0 else 0
    skilled_sharpe = compute_sharpe([(c, p==a) for p, a, c in skilled_results])
    skilled_dd = max_drawdown(skilled_results)

    print(f"\n  {'Metric':<25} {'All Stations':>15} {'Skilled Only':>15} {'Delta':>10}")
    print(f"  {'-'*25} {'-'*15} {'-'*15} {'-'*10}")
    print(f"  {'Trade count':<25} {all_total:>15} {skilled_total:>15} {skilled_total - all_total:>+10}")
    print(f"  {'Accuracy':<25} {all_acc:>15.2%} {skilled_acc:>15.2%} {skilled_acc - all_acc:>+10.2%}")
    print(f"  {'Sharpe ratio':<25} {all_sharpe:>15.3f} {skilled_sharpe:>15.3f} {skilled_sharpe - all_sharpe:>+10.3f}")
    print(f"  {'Max drawdown':<25} {all_dd:>15.2%} {skilled_dd:>15.2%} {skilled_dd - all_dd:>+10.2%}")

    conn.close()

    # ─── WRITE REPORT ─────────────────────────────────────────────────────
    report_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, 'p1-station-skill-2026-07-06.md')

    with open(report_path, 'w') as f:
        f.write("# P1.2-P1.3 — Per-Station Skill Gating (2026-07-06)\n\n")
        f.write(f"**Date:** 2026-07-06\n")
        f.write(f"**Stations:** {len(ALL_STATIONS)} (post R4-1.2 purge)\n")
        f.write(f"**Method:** 30-day rolling Brier Skill Score vs persistence and climatology baselines\n")
        f.write(f"**Bootstrap:** Block bootstrap, block size=5, 2000 resamples, 95% CI\n")
        f.write(f"**Decision rule:** TRADE if BSS > 0 against both baselines, else NO-TRADE\n\n")

        f.write("## Trade Selection Table\n\n")
        f.write("| Station | Market | BSS (Persistence) | BSS (Climatology) | CI Lower | CI Upper | Decision |\n")
        f.write("|---------|--------|-------------------|-------------------|----------|----------|----------|\n")
        for entry in sorted(skill_table, key=lambda x: (x['station'], x['market'])):
            f.write(f"| {entry['station']} | {entry['market']} | {entry['bss_persistence']:+.4f} | {entry['bss_climatology']:+.4f} | {entry['ci_lower']:+.4f} | {entry['ci_upper']:+.4f} | {entry['decision']} |\n")

        f.write(f"\n## Skilled vs Unskilled Stations (HIGH market)\n\n")
        f.write(f"- **Skilled ({len(skilled_stations)}):** {', '.join(skilled_stations)}\n")
        f.write(f"- **Unskilled ({len(unskilled_stations)}):** {', '.join(unskilled_stations)}\n")

        f.write(f"\n## All-Stations vs Skilled-Only Comparison\n\n")
        f.write(f"| Metric | All Stations | Skilled Only | Delta |\n")
        f.write(f"|--------|-------------|-------------|-------|\n")
        f.write(f"| Trade count | {all_total} | {skilled_total} | {skilled_total - all_total:+d} |\n")
        f.write(f"| Accuracy | {all_acc:.2%} | {skilled_acc:.2%} | {skilled_acc - all_acc:+.2%} |\n")
        f.write(f"| Sharpe ratio | {all_sharpe:.3f} | {skilled_sharpe:.3f} | {skilled_sharpe - all_sharpe:+.3f} |\n")
        f.write(f"| Max drawdown | {all_dd:.2%} | {skilled_dd:.2%} | {skilled_dd - all_dd:+.2%} |\n")

        f.write(f"\n## Notes\n\n")
        f.write("- BSS > 0 means the model outperforms the baseline (persistence or climatology)\n")
        f.write("- Persistence baseline: today's direction = yesterday's direction\n")
        f.write("- Climatology baseline: 15-day rolling mean of outcomes\n")
        f.write("- Block bootstrap preserves time-series autocorrelation (block size=5)\n")
        f.write("- All metrics computed from real METAR backfill, no AI/ML in loop\n")

    print(f"\n📝 Report written to {report_path}")
    print("\n" + "=" * 90)
    print("PER-STATION SKILL GATING COMPLETE")
    print("=" * 90)


if __name__ == "__main__":
    main()
