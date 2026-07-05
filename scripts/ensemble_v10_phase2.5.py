#!/usr/bin/env python3
"""
ENSEMBLE V10 — PHASE 1 + EDGE 23 (CLOUD-COVER MODULATION)

Combines all Phase 1 edges plus Edge 23:
- 5 approaches from Phase 1 (reversion, gaussian, regime, gaussian v2, pressure)
- Edge 23: Gaussian model with cloud-cover σ adjustment
- Cloud-cover adjustment based on ISD Lite sky condition codes
- Adjusts the Gaussian model's σ based on cloud cover fraction (clear → higher σ, overcast → lower σ)

Walk-forward only. No circularity. No AI in the loop. $0 data cost.
"""

import sqlite3
import math
import random
from collections import defaultdict

# Database paths
DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
ISD_LITE_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/isd_lite_raw.db"

FEE_RATE = 0.05

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

FDR_STATIONS = ['KNYC','KMIA','KDEN','KMDW','KLAX','KPHX','KATL','KBOS']

# ─── CLOUD COVER MODULE INTEGRATION ─────────────────────────────────────────

# ISD sky condition → cloud cover fraction mapping
SKY_CODE_TO_FRACTION = {
    0: 0.0,    # Clear
    1: 0.125,  # Few (rare in ISD-lite, usually 2)
    2: 0.125,  # Few
    3: 0.375,  # Scattered (rare)
    4: 0.375,  # Scattered
    5: 0.625,  # Broken (rare)
    6: 0.625,  # Broken
    7: 0.625,  # Broken (rare)
    8: 1.0,    # Overcast
    9: None,   # Missing/obscured
    10: None,  # Missing
    -9999: None,
    -1: None,  # Our sentinel for missing
}

# σ multiplier range — don't compress/expand too aggressively
SIGMA_MIN = 0.75   # Overcast: reduce σ by up to 25%
SIGMA_MAX = 1.35   # Clear: expand σ by up to 35%

def load_cloud_cover_data(isd_conn):
    """
    Load cloud cover data by station, aggregated from ISD-lite raw_line field.
    Returns dict: {station: {date_str: cloud_cover_fraction}}
    """
    cloud_data_by_station = {}
    
    for station in ALL_STATIONS:
        cur = isd_conn.cursor()
        cur.execute("""
            SELECT date_utc, raw_line FROM isd_lite_raw
            WHERE station=? AND raw_line IS NOT NULL
            ORDER BY date_utc ASC
        """, (station,))
        
        daily_cloud = defaultdict(list)
        for date_str, raw_line in cur.fetchall():
            parts = raw_line.split()
            if len(parts) < 10:
                continue
            sky_code_str = parts[9]
            try:
                sky_code = int(sky_code_str)
            except ValueError:
                continue
            
            fraction = SKY_CODE_TO_FRACTION.get(sky_code)
            if fraction is not None:
                daily_cloud[date_str].append(fraction)
        
        # Average hourly observations into daily cloud cover fraction
        station_cloud_data = {}
        for date_str, fractions in daily_cloud.items():
            if fractions:  # Only if there are any valid readings for this day
                avg_fraction = sum(fractions) / len(fractions)
                station_cloud_data[date_str] = avg_fraction
        
        cloud_data_by_station[station] = station_cloud_data
    
    return cloud_data_by_station


def get_cloud_cover_adjustment(date_str, cloud_data):
    """
    Get the σ multiplier for a given date using 3-day average cloud cover.
    
    Args:
        date_str: date in 'YYYY-MM-DD' format
        cloud_data: dict of {date: cloud_fraction} for this station
    
    Returns: σ multiplier (float)
    """
    from datetime import datetime, timedelta
    
    # Get cloud cover for target date and previous 2 days
    fractions = []
    for offset in range(0, 3):
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            check_date = (dt - timedelta(days=offset)).strftime('%Y-%m-%d')
            frac = cloud_data.get(check_date)
            if frac is not None:
                fractions.append(frac)
        except (ValueError, KeyError):
            continue
    
    if not fractions:
        return 1.0  # No data → no adjustment
    
    avg_fraction = sum(fractions) / len(fractions)
    # Linear interpolation: fraction=0 → SIGMA_MAX, fraction=1 → SIGMA_MIN
    multiplier = SIGMA_MAX + (SIGMA_MIN - SIGMA_MAX) * avg_fraction
    
    return multiplier


# ─── LOADING FUNCTIONS ──────────────────────────────────────────────────────

def load_station_data(station, conn, cloud_data):
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
        
        # Add cloud_cover_multiplier for this date (using idx-1 date to set up for next day)
        date_str = r[0]
        sigma_mult = get_cloud_cover_adjustment(date_str, cloud_data.get(station, {}))
        
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
            'sigma_mult': sigma_mult
        })
    
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


# ─── APPROACHES ─────────────────────────────────────────────────────────────

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

def approach_gaussian_cloud_adjusted(idx, days):
    """
    Gaussian model with cloud-cover σ adjustment (from Edge 23)
    """
    if idx < 48: return None, 0.0
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    
    # Apply cloud-cover σ modulation using sigma_mult from current day (idx-1)
    # The cloud_cover_multiplier for "today" influences the Gaussian signal for tomorrow's prediction
    if idx > 0 and 'sigma_mult' in days[idx-1]:
        std_adjusted = std * days[idx-1]['sigma_mult']
    else:
        std_adjusted = std  # No adjustment if cloud_cover_multiplier unavailable
    
    if std_adjusted <= 0:
        return None, 0.0
    
    z = (days[idx-1]['high'] - mean) / std_adjusted if std_adjusted > 0 else 0
    if z > 1.0: return 'down', abs(z)
    elif z < -1.0: return 'up', abs(z)
    return None, 0.0

def approach_persistence(idx, days):
    if idx < 2: return None, 0.0
    prev = days[idx-2]['high']
    curr = days[idx-1]['high']
    return ('up' if curr > prev else 'down'), 0.3

def approach_regime(idx, days):
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
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

def approach_gaussian_v2(idx, days):
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
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0

APPROACHES = [
    approach_reversion, 
    approach_gaussian_cloud_adjusted,  # Replaced gaussian with cloud-adjusted version
    approach_regime, 
    approach_gaussian_v2, 
    approach_pressure
]
APPROACH_NAMES = ["Reversion", "Gaussian(48d)+CC", "Regime", "Gaussian v2(30d)", "Pressure"]


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def binomial_test_p(correct, total, null_p=0.5):
    if total == 0: return 1.0
    p = correct / total
    if total > 100:
        z = (p - null_p) * math.sqrt(total) / math.sqrt(null_p * (1 - null_p))
        return min(2 * (1 - normal_cdf(abs(z))), 1.0)
    else:
        from math import comb
        expected = null_p * total
        p_val = 0
        for k in range(total + 1):
            prob = comb(total, k) * (null_p**k) * ((1-null_p)**(total-k))
            if abs(k - expected) >= abs(correct - expected):
                p_val += prob
        return min(p_val, 1.0)

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
    var = sum((v - mean)**2 for v in vals) / n
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


def walk_forward_ensemble_enhanced(days, station, market, min_conf=0.0, train_days=180, test_days=30):
    """
    Enhanced ensemble with cloud cover and ENSO regime adjustments.
    Unlike the original approach, this allows for external data adjustments.
    """  
    results = []
    start = train_days
    
    # Load cloud cover data for this station if needed (for cloud_cover_sigma_modulation)    
    try:
        conn_isd_lite = sqlite3.connect('/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/isd_lite_raw.db', timeout=10)
        cloud_data = cloud_module.load_cloud_cover(station, conn_isd_lite)
        conn_isd_lite.close()
    except:
        cloud_data = {}  # Fallback if no cloud cover data
    
    # Load climate indices (these are the same for all stations)
    try:
        climate_indices = {
            'oni': enso_module.parse_oni_data(),
            'pdo': enso_module.parse_pdo_data(),
            'nao': enso_module.parse_nao_data(),
            'ao': enso_module.parse_ao_data()
        }
    except:
        climate_indices = {}
    
    # Create station-specific anomalies 
    station_anomalies = {}
    try:
        if climate_indices:
            station_anomalies = enso_module.compute_station_anomalies(station, climate_indices)
    except:
        pass  # Use empty dict as fallback
    
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            
            # Apply Gaussian approach like normal to generate baseline signal
            predictions = {}
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf >= min_conf:
                    predictions[f'a{i+1}'] = (pred, conf)
            
            # Apply ENSO regime adjustment to the Gaussian model's mean
            # Specifically, we'll modify the approach_gaussian approach with ENSO adjustment
            # Since the Gaussian model uses mean/std to compute z-scores
            enso_applied = False
            if station_anomalies and climate_indices and idx >= 48:  # Need enough days for Gaussian
                try:
                    # Get ENSO adjustment
                    adjustment = enso_module.get_regime_adjustment(station, date, 
                        anomalies=station_anomalies, indices=climate_indices)
                    
                    # Re-calculate Gaussian effect with ENSO-adjusted mean
                    # Copy logic from Gaussian approach but with adjusted mean
                    window = days[idx-48:idx-1]
                    highs = [d['high'] for d in window if d['high'] is not None]
                    if len(highs) >= 10:  # minimum sample
                        mean = sum(highs) / len(highs)
                        var = sum((h-mean)**2 for h in highs) / len(highs)
                        std = math.sqrt(var) if var > 0 else 0.01
                        
                        # Apply ENSO adjustment to mean
                        adjusted_mean = mean + adjustment
                        z = (days[idx-1]['high'] - adjusted_mean) / std if std > 0 else 0
                        
                        # If z is significant, add as ENSO-augmented Gaussian signal
                        if abs(z) > 1.0:  # Using same threshold as original Gaussian
                            gauss_pred = 'down' if z > 1.0 else 'up'
                            gauss_conf = min(abs(z) / 2.0, 0.95)  # Same conf computation as v2
                            # Override or augment the main Gaussian if stronger
                            # Check if the ENSO adjustment creates a stronger signal
                            original_pred, original_conf = approach_gaussian(idx, days)
                            if original_conf < gauss_conf:  # New signal is stronger
                                # Replace or augment as appropriate
                                predictions['gauss_enso'] = (gauss_pred, gauss_conf)
                                enso_applied = True
                except:
                    pass  # If fails, just proceed with original signals
            
            # Apply cloud_cover sigma adjustment (this affects the confidence)
            cloud_sigma_mult = cloud_module.get_cloud_cover_adjustment(
                station, date, cloud_data) if cloud_data else 1.0
            
            if cloud_sigma_mult != 1.0 and enso_applied:
                # We can adjust the confidence for cloud cover effect
                # The idea is that cloud cover affects variance estimation
                # Adjust the confidence if sigma was affected significantly
                # For now, keep simple and let the sigma adjustment work
                pass
            
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


def walk_forward_ensemble(days, market, min_conf=0.0, train_days=180, test_days=30):
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


def main():
    print("=" * 90)
    print("ENSEMBLE V10 — PHASE 1 + EDGE 23: CLOUD-COVER MODULATION")
    print("=" * 90)
    print(f"METAR Database: {DB_PATH}")
    print(f"ISD-Lite Database: {ISD_LITE_DB}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Approaches: {len(APPROACHES)} (includes cloud-adjusted Gaussian)")
    print(f"Cloud σ multiplier: [{SIGMA_MIN}, {SIGMA_MAX}]")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Fee rate: {FEE_RATE:.0%}")
    print()
    
    # Open both connections
    metar_conn = sqlite3.connect(DB_PATH, timeout=60)
    isd_conn = sqlite3.connect(ISD_LITE_DB, timeout=60)
    
    # Load cloud data for all stations
    print("Loading cloud cover data from ISD Lite...")
    cloud_data = load_cloud_cover_data(isd_conn)
    print(f"Loaded cloud data for {sum(1 for v in cloud_data.values() if v)} stations with cloud observations")
    print()
    
    random.seed(42)
    
    # ─── PART 1: Per-station ensemble accuracy ─────────────────────────────
    print("=" * 90)
    print("PART 1: ENSEMBLE ACCURACY (21 stations, confidence ≥0.7)")
    print("=" * 90)
    
    station_results = {}
    
    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Sharpe':>8} {'MaxDD':>8} {'Binom p':>10}")
    print("-" * 70)
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, metar_conn, cloud_data)
        if len(days) < 210: continue
        results = walk_forward_ensemble(days, market, min_conf=0.7)
        if not results: continue
        
        station_results[station] = results
        total = len(results)
        correct = sum(1 for p, a, c in results if p == a)
        acc = correct / total
        sharpe = compute_sharpe([(c, p==a) for p, a, c in results])
        dd = max_drawdown(results)
        binom_p = binomial_test_p(correct, total)
        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {sharpe:>8.3f} {dd:>8.2%} {binom_p:>10.4f}")
    
    # Aggregate
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
    
    # ─── PART 2: FDR correction on 8 key stations (only reversion and gaussian standalones)
    print()
    print("=" * 90)
    print("PART 2: FDR CORRECTION (8 stations × selected signals only)")
    print("=" * 90)
    
    p_values = []
    labels = []
    
    for station in FDR_STATIONS:
        days, market = load_station_data(station, metar_conn, cloud_data)
        if len(days) < 210: continue
        
        # Get just reversion and the cloud-adjusted gaussian standalone for FDR test
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
        
        # Cloud-adjusted Gaussian standalone
        gau_results = []
        start = 180
        while start + 30 <= len(days):
            for idx in range(start, min(start + 30, len(days))):
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None: continue
                pred, conf = approach_gaussian_cloud_adjusted(idx, days)
                if pred:
                    gau_results.append((pred, actual['direction']))
            start += 30
            if start > len(days): break
        
        if gau_results:
            c = sum(1 for p, a in gau_results if p == a)
            t = len(gau_results)
            p_values.append(binomial_test_p(c, t))
            labels.append(f"{station} Gaussian+CC")
    
    bh_results = benjamini_hochberg(p_values, alpha=0.05)
    n_sig = sum(1 for _, _, r in bh_results if r)
    
    print(f"\n  Total hypotheses: {len(p_values)}")
    print(f"  Significant after FDR: {n_sig}")
    print(f"  FDR pass rate: {n_sig}/{len(p_values)} = {n_sig/len(p_values):.1%}")
    
    # ─── VERDICT ───────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("V10 VERDICT")
    print("=" * 90)
    
    win_rate = accuracy
    sharpe_net = compute_sharpe([(c, p==a) for p, a, c in all_results], fee_rate=FEE_RATE)
    
    print(f"\n  Ensemble accuracy: {accuracy:.2%}")
    print(f"  vs V8 baseline: 64.79%")
    print(f"  Δ from baseline: {(accuracy - 0.6479)*100:+.3f} pp")
    print()
    
    print(f"  Original V8 accuracy: 64.79%, Sharpe: 4.916")
    
    # Circularity check
    circularity = any(acc >= 0.99 for acc in [accuracy])  # More lenient check for circularity
    if circularity:
        print("\n  ⚠️  CIRCULARITY DETECTED — STOP AND FIX")
    
    if accuracy > 0.6479:
        print(f"\n  ✅ EDGE 23 INTEGRATION WINS: accuracy improved from V8 baseline")
    else:
        print(f"\n  ⚠️  EDGE 23 HAD NO EFFECT: similar accuracy to baseline")
    
    print(f"  Ensemble accuracy: {accuracy:.3%} ({'+' if accuracy > 0.6479 else ''}{(accuracy-0.6479)*100:+.3f}% from baseline)")
    print(f"  Trades: {total}")
    print(f"  Sharpe (net): {sharpe_net:.3f}")
    print(f"  FDR significant: {n_sig}/{len(p_values)}")
    print(f"  Binomial p-value: {binom_p:.4f}")
    
    metar_conn.close()
    isd_conn.close()
    
    print(f"\n{'='*90}")
    print("ENSEMBLE V10 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()