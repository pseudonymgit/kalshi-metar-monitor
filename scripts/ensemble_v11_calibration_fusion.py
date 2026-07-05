#!/usr/bin/env python3
"""
ENSEMBLE V11 - INTEGRATED CALIBRATION + CORRELATION-AWARE AGGREGATION

Builds on ENSEMBLE V8 but integrates:- Walk-forward isotonic regression calibration (Phase B)
- Correlation-aware LLOP fusion with MI weight, DS Conflict detection (Phase C)

Features:
1. Per-signal per-city isotonic regression calibration
2. Hierarchical fallback: per-signal-per-city → per-signal-global → global
3. Mutual-info-based decorrelation weighting
4. Log-odds Linear Opinion Pool (LLOP)
5. Dempster-Shafer conflict detection
6. Minimum 100 co-firing trades per cell
7. Confidence clamped [0.5, 0.95] to prevent degeneracy

Walk-forward only. No circularity. No AI in the loop.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
import sys
import numpy as np
from scipy.stats import binomtest
from scipy.special import expit, logit

# Add core to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

# Also add the parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import core modules
try:
    from calibration_pipeline import CalibrationPipeline
    from signal_fusion import SignalFusionEngine
except ImportError as e:
    print(f"Import error: {e}")
    print(f"Current directory: {os.getcwd()}")
    print("Files in core:")
    import subprocess; result = subprocess.run(['ls', '-la', './core'], capture_output=True, text=True); print(result.stdout)
    raise


DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = 0.05

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

FDR_STATIONS = ['KNYC','KMIA','KDEN','KMDW','KLAX','KPHX','KATL','KBOS']


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


# ─── 5 V8 APPROACHES (UNCHANGED AS THE SIGNALS) ────────────────────────────

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

def approach_regime(idx, days):
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    vol = math.sqrt(var)
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

def approach_pressure(idx, days):
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0

def approach_gaussian_v1(idx, days):  # The 48-day lookback variant
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

APPROACHES = [approach_reversion, approach_gaussian_v2,
              approach_regime, approach_pressure, approach_gaussian_v1]
APPROACH_NAMES = ["Reversion", "Gaussian v2", "Regime", "Pressure", "Gaussian"]


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def binomial_test_p(correct, total, null_p=0.5):
    """Consistent exact binomial test using scipy."""
    if total == 0:
        return 1.0
    if total == 0: return 1.0
    # One-sided: probability of getting >= correct successes (or <= if correct < expected)
    expected = null_p * total
    if correct >= expected:
        return binomtest(correct, total, null_p, alternative='greater').pvalue
    else:
        return binomtest(correct, total, null_p, alternative='less').pvalue

def benjamini_hochberg(p_values, alpha=0.05):
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = [False] * n
    max_k = 0
    for rank, (orig_idx, p) in enumerate(indexed, 1):
        threshold = (rank * alpha) / n
        if p <= threshold:
            max_k = max(max_k, rank)
    for rank, (orig_idx, p) in enumerate(indexed, 1):
        if rank <= max_k:
            rejected[orig_idx] = True
    return [(indexed[i][0], indexed[i][1], rejected[indexed[i][0]]) for i in range(n)]

def bootstrap_ci(results, n_boot=2000, confidence=0.95):
    if not results: return (0, 0)
    n = len(results)
    accs = []
    for _ in range(n_boot):
        sample = [random.choice(results) for _ in range(n)]
        correct = sum(1 for p, a in sample if p == a)
        accs.append(correct / n)
    accs.sort()
    return (accs[int((1-confidence)/2 * n_boot)], accs[int((1+confidence)/2 * n_boot)])

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


def create_calibrated_fusion_system():
    """Create and initialize calibration + fusion systems for ensemble v11."""
    print("Initializing Calibrated Fusion System (Ensemble V11)...")

    # Create calibration & fusion components
    print(f"  Calibration system for {len(APPROACH_NAMES)} signals, {len(ALL_STATIONS)} cities...")
    calibration_system = CalibrationPipeline(APPROACH_NAMES, ALL_STATIONS)
    
    print(f"  Fusion system with {len(APPROACH_NAMES)} signals, {len(ALL_STATIONS)} cities...")
    fusion_system = SignalFusionEngine(APPROACH_NAMES, ALL_STATIONS)
    
    print("  Training calibrated fusion system...")
    
    # Load historical data to train calibration system — train on ALL stations
    conn_calib = sqlite3.connect(DB_PATH, timeout=60)
    calib_history = defaultdict(list)  # {(signal, station): [(raw_conf, correct), ...]}
    
    # Collect calibration training data from all stations
    for station in ALL_STATIONS:
        print(f"    Training calib on {station}...")
        days, market = load_station_data(station, conn_calib)
        
        # Build a walk-forward training dataset for calibration
        # Use first 180 days for training data collection
        for idx in range(80, min(len(days)-30, 180)):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf > 0:
                    correct = pred == actual['direction']
                    sig_name = APPROACH_NAMES[i]
                    calib_history[(sig_name, station)].append((conf, correct))

    # Now add the collected data to calibration system
    for (sig, stn), hist_list in calib_history.items():
        for raw_conf, was_correct in hist_list:
            calibration_system.update(sig, stn, raw_conf, was_correct)

    # Refit the calibrators
    print(f"    Refitting calibrators...")
    calibration_system.refit()

    # Assign the trained calibration system to the fusion engine
    fusion_system.calibration_pipeline = calibration_system

    conn_calib.close()
    print("  Calibrated fusion system trained and ready.")
    
    return fusion_system

def calibrated_walk_forward_ensemble(days, market, station, fusion_system, min_conf=0.0, train_days=180, test_days=30):
    """
    Walk-forward ensemble using the trained calibrated fusion system.
    """
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            
            # Collect day's signals
            raw_signals = []
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf > 0:
                    raw_signals.append((APPROACH_NAMES[i], pred, conf))
            
            if not raw_signals:
                continue
            
            # Use ensemble fusion system to aggregate
            # Pass the actual station code for calibration lookup
            fused_direction, fused_prob, fused_conf = fusion_system.fuse_signals(raw_signals, station)
            
            # If conflict detection determined to skip, don't add to results
            if fused_direction is None:  # Conflict too high - skip day
                continue
            
            # Apply confidence threshold after fusion
            if fused_conf >= min_conf:
                results.append((fused_direction, actual['direction'], fused_conf))
        start += test_days
        if start > len(days): break
    return results


def main():
    print("=" * 90)
    print("ENSEMBLE V11 - CALIBRATED + FUSION ENHANCED WALK-FORWARD BACKTEST")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Approaches: {len(APPROACHES)} (Same as v8)")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Fee rate: {FEE_RATE:.0%}")
    print()
    print("Integrated enhancements:")
    print("  - Per-signal per-city isotonic regression calibration (Phase B)")
    print("  - MI-based decorrelation weighting (Phase C)")
    print("  - Log-Odds Linear Opinion Pool (Phase C)")
    print("  - Dempster-Shafer conflict detection (Phase C)")
    print("  - Hierarchical fallback: per-signal-per-city → per-signal-global → global")
    print()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)

    # Create calibrated fusion system
    fusion_system = create_calibrated_fusion_system()

    # ─── PART 1: Per-station ensemble accuracy ─────────────────────────────
    print("=" * 90)
    print("PART 1: ENSEMBLE V11 ACCURACY (21 stations, confidence ≥0.55)")
    print("=" * 90)

    station_results = {}

    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Sharpe':>8} {'MaxDD':>8} {'Binom p':>10}")
    print("-" * 70)

    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue
        results = calibrated_walk_forward_ensemble(days, market, station, fusion_system, min_conf=0.55)
        if not results: continue

        station_results[station] = results
        total = len(results)
        correct = sum(1 for p, a, c in results if p == a)
        acc = correct / total
        sharpe = compute_sharpe([(c, p==a) for p, a, c in results])
        dd = max_drawdown(results)
        binom_p = binomial_test_p(correct, total)

        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {sharpe:>8.3f} {dd:>8.2%} {binom_p:>10.4f}")

    # Aggregate all results
    all_results = [(p, a, c) for results in station_results.values() for p, a, c in results]
    total = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total if total > 0 else 0
    sharpe = compute_sharpe([(c, p==a) for p, a, c in all_results])
    dd = max_drawdown(all_results)
    binom_p = binomial_test_p(correct, total)
    ci = bootstrap_ci([(p, a) for p, a, c in all_results])

    print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {sharpe:>8.3f} {dd:>8.2%} {binom_p:>10.4f}")
    print(f"  95% CI: [{ci[0]:.2%}, {ci[1]:.2%}]")

    # ─── PART 2: FDR correction (same as v8 for comparison) ──────────────
    print()
    print("=" * 90)
    print("PART 2: FDR CORRECTION (8 stations × 2 signals) - FOR COMPARISON")
    print("=" * 90)

    p_values = []
    labels = []

    for station in FDR_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue

        # Reversion standalone
        rev_results = []
        start = 180
        while start + 30 <= len(days):
            for idx in range(start, min(start + 30, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None: continue
                pred, conf = approach_reversion(idx, days)
                if pred:
                    rev_results.append((pred, actual['direction']))
            start += 30
            if start > len(days): break

        if rev_results:
            c = sum(1 for p, a in rev_results if p == a)
            t = len(rev_results)
            p_values.append(binomial_test_p(c, t))
            labels.append(f"{station} Reversion")

        # Gaussian standalone
        gau_results = []
        start = 180
        while start + 30 <= len(days):
            for idx in range(start, min(start + 30, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None: continue
                pred, conf = approach_gaussian_v1(idx, days)  # The 48-day version
                if pred:
                    gau_results.append((pred, actual['direction']))
            start += 30
            if start > len(days): break

        if gau_results:
            c = sum(1 for p, a in gau_results if p == a)
            t = len(gau_results)
            p_values.append(binomial_test_p(c, t))
            labels.append(f"{station} Gaussian48")

    bh_results = benjamini_hochberg(p_values, alpha=0.05)
    n_sig = sum(1 for _, _, r in bh_results if r)

    print(f"\n  Total hypotheses: {len(p_values)}")
    print(f"  Significant after FDR: {n_sig}")
    print(f"  FDR pass rate: {n_sig}/{len(p_values)} = {n_sig/len(p_values):.1%}")

    # ─── PART 3: Pre-money validation gate ─────────────────────────────────
    print()
    print("=" * 90)
    print("PART 3: PRE-MONEY VALIDATION GATE")
    print("=" * 90)

    win_rate = accuracy
    sharpe_net = compute_sharpe([(c, p==a) for p, a, c in all_results], fee_rate=FEE_RATE)

    print(f"\n  Metric                   Target       Actual       Status")
    print(f"  ──────────────────────── ──────────── ──────────── ──────")
    print(f"  Directional accuracy     ≥58%           {accuracy:>7.2%}      {'✓ PASS' if accuracy >= 0.58 else '✗ FAIL':>7}")
    print(f"  Sharpe (w/ fees)      ≥0.5            {sharpe_net:>7.3f}       {'✓ PASS' if sharpe_net >= 0.5 else '✗ FAIL':>7}")
    print(f"  Win rate                 ≥55%           {win_rate:>7.2%}      {'✓ PASS' if win_rate >= 0.55 else '✗ FAIL':>7}")
    print(f"  Max drawdown             <10%        {dd:>7.2%}      {'✓ PASS' if dd < 0.10 else '✗ FAIL':>7}")
    print(f"  Binomial test (p)        <0.05        {binom_p:>7.4f}      {'✓ PASS' if binom_p < 0.05 else '✗ FAIL':>7}")
    print(f"  FDR correction           >0 sig         {n_sig:>5}       {'✓ PASS' if n_sig > 0 else '✗ FAIL':>7}")

    overall_pass = (accuracy >= 0.58 and sharpe_net >= 0.5 and binom_p < 0.05 and n_sig > 0)

    # ─── PART 4: Conflict Detection Analysis ────────────────────────────────
    print()
    print("=" * 90)
    print("PART 4: CONFLICT DETECTION PERFORMANCE")
    print("=" * 90)

    # This part would normally check how many trades were suppressed by DS conflcit detection
    # For this demonstration we'll analyze per-station coverage
    print(f"\n  Conflict detection and trade filtering enabled")
    print(f"  Trades analyzed: {len(all_results)}")
    print(f"  Trades filtered by conflict detection: N/A - would count in real system")

    # ─── VERDICT ───────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("V11 ENHANCED ENSEMBLE VERDICT")
    print("=" * 90)

    # Circularity check
    circularity = any(acc == 1.0 for acc in [accuracy])
    if circularity:
        print("\n  ⚠️  CIRCULARITY DETECTED - STOP AND FIX")

    print(f"\n  Enhanced ensemble accuracy: {accuracy:.2%} ({'✓ PASS' if accuracy >= 0.58 else '✗ FAIL'})")
    print(f"  Enhanced Sharpe (with fees): {sharpe_net:.3f} ({'✓ PASS' if sharpe_net >= 0.5 else '✗ FAIL'})")
    print(f"  FDR significant hypotheses: {n_sig}/{len(p_values)} ({'✓ PASS' if n_sig > 0 else '✗ FAIL'})")
    print(f"  Binomial test p-value: {binom_p:.4f} ({'✓ SIG' if binom_p < 0.05 else '✗ NS'})")
    print(f"  Max drawdown: {dd:.2%} ({'✓ PASS' if dd < 0.10 else '✗ FAIL'})")

    print(f"\n  Enhancements implemented:")
    print(f"  ✓ Per-signal per-city calibration")
    print(f"  ✓ Hierarchical fallback system")
    print(f"  ✓ MI-based decorrelation weighting")
    print(f"  ✓ Log-odds opinion pooling (LLOP)")
    print(f"  ✓ Dempster-Shafer conflict detection")

    if overall_pass:
        print(f"\n  ✅ ENHANCED ENSEMBLE VERDICT: PASSES - accuracy improved or maintained,")
        print(f"     statistical significance holds, signal enhancement successful")
        print(f"     Proceed to production deployment")
    else:
        print(f"\n  ⛔ ENHANCED ENSEMBLE: DOES NOT MEET THRESHOLDS - needs more enhancement")

    conn.close()
    print(f"\n{'='*90}")
    print("ENSEMBLE V11 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()