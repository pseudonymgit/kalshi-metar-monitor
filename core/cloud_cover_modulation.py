#!/usr/bin/env python3
"""
EDGE 23: Cloud-Cover Modulation

Adjusts the Gaussian model's σ (variance) based on METAR cloud cover observations.
- Overcast → compress σ (temperature distribution is narrower)
- Clear → expand σ (temperature distribution is wider)

Data source: WeatherAPI hourly.cloud field (cloud cover % 0-100)
  Replaces the previous ISD-lite sky condition code approach with direct,
  more granular cloud cover percentage. Aggregated to daily fraction.

Aggregates hourly observations into daily cloud-cover fraction, then uses it
as a σ multiplier on Edge 8's Gaussian model.

Walk-forward only. No AI in the loop. $0 data cost.
"""

import sqlite3
import math
from collections import defaultdict
from datetime import datetime
import os
import sys

# Database paths
METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
WEATHERAPI_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/weatherapi_archive.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

# WeatherAPI cloud cover % → fraction (cloud is already 0-100)
# No sky-code mapping needed — WeatherAPI provides direct cloud cover %

# σ multiplier range — don't compress/expand too aggressively
SIGMA_MIN = 0.75   # Overcast: reduce σ by up to 25%
SIGMA_MAX = 1.35   # Clear: expand σ by up to 35%
# Neutral point: cloud_cover ≈ 0.5 → multiplier ≈ 1.0


def load_cloud_cover(station, conn_weatherapi):
    """
    Load daily cloud cover fraction from WeatherAPI hourly data.
    
    WeatherAPI provides direct cloud cover % (0-100) per hour.
    Aggregated to a daily average, normalized to 0.0-1.0.
    
    Returns: dict of {date_str: cloud_cover_fraction}
    """
    cur = conn_weatherapi.cursor()
    cur.execute("""
        SELECT date, cloud FROM hourly
        WHERE station=? AND cloud IS NOT NULL
        ORDER BY date ASC
    """, (station,))
    
    daily_cloud = defaultdict(list)
    for date_str, cloud_pct in cur.fetchall():
        # cloud_pct is 0-100; convert to fraction 0.0-1.0
        fraction = cloud_pct / 100.0
        daily_cloud[date_str].append(fraction)
    
    # Average hourly observations into daily cloud cover fraction
    result = {}
    for date_str, fractions in daily_cloud.items():
        result[date_str] = sum(fractions) / len(fractions)
    
    return result


def cloud_cover_to_sigma_multiplier(cloud_fraction):
    """
    Convert cloud cover fraction (0.0 to 1.0) to a σ multiplier.
    
    Clear (0.0): expand σ → multiplier > 1.0 (more variance)
    Overcast (1.0): compress σ → multiplier < 1.0 (less variance)
    
    Linear mapping from [0.0, 1.0] → [SIGMA_MAX, SIGMA_MIN]
    with a smooth transition through 0.5 → 1.0 (neutral)
    
    Returns: float multiplier
    """
    if cloud_fraction is None:
        return 1.0  # No data → no adjustment
    
    # Linear interpolation: fraction=0 → SIGMA_MAX, fraction=1 → SIGMA_MIN
    # fraction=0.5 → (SIGMA_MAX + SIGMA_MIN) / 2
    multiplier = SIGMA_MAX + (SIGMA_MIN - SIGMA_MAX) * cloud_fraction
    
    return multiplier


def get_cloud_cover_adjustment(station, date_str, cloud_data):
    """
    Get the σ multiplier for a given station and date.
    
    Uses a 3-day average cloud cover to smooth out single-day anomalies.
    
    Args:
        station: station code (e.g., 'KNYC')
        date_str: date in 'YYYY-MM-DD' format
        cloud_data: dict of {date: cloud_fraction} for this station
    
    Returns: σ multiplier (float), typically 0.75 to 1.35
    """
    # Get cloud cover for target date and previous 2 days
    fractions = []
    for offset in range(0, 3):
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            from datetime import timedelta
            check_date = (dt - timedelta(days=offset)).strftime('%Y-%m-%d')
            frac = cloud_data.get(check_date)
            if frac is not None:
                fractions.append(frac)
        except (ValueError, KeyError):
            continue
    
    if not fractions:
        return 1.0  # No data → no adjustment
    
    avg_fraction = sum(fractions) / len(fractions)
    return cloud_cover_to_sigma_multiplier(avg_fraction)


# ─── ENSEMBLE SIGNAL INTERFACE ──────────────────────────────────────────────

def cloud_cover_sigma_adjustment(idx, days, cloud_data):
    """
    Compute σ multiplier for the Gaussian model at a given index.
    
    This is NOT a directional signal — it modifies the Gaussian model's
    variance parameter. It returns a multiplier to apply to σ.
    
    Args:
        idx: current day index in days array
        days: list of daily data dicts
        cloud_data: dict of {date: cloud_fraction} for this station
    
    Returns: σ multiplier (float), typically 0.75 to 1.35
    """
    if idx < 1:
        return 1.0
    
    date_str = days[idx]['date']
    return get_cloud_cover_adjustment('', date_str, cloud_data)


# ─── BACKTEST ENGINE ────────────────────────────────────────────────────────

def load_station_data(station, conn_metar, conn_weatherapi):
    """Load daily highs, cloud cover, and settlement epochs for a station."""
    cur = conn_metar.cursor()
    
    # Daily aggregates from metar_backfill.db
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(temp_f) as temp, AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2],
                      'temp': r[3], 'pressure': r[4]})
    
    # Cloud cover from weatherapi_archive.db (replaces ISD-lite)
    cloud_data = load_cloud_cover(station, conn_weatherapi)
    
    # Settlement epochs
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        market[r[0]] = {
            'direction': 'up' if r[1] > r[2] else 'down',
            'reversion': r[3] if r[3] is not None else 0
        }
    
    return days, cloud_data, market


def gaussian_signal_with_cloud(idx, days, cloud_data, use_cloud_adjustment=True):
    """
    Edge 8 Gaussian model with optional cloud-cover σ modulation.
    
    Returns: (direction, confidence, sigma_multiplier)
    """
    if idx < 48:
        return None, 0.0, 1.0
    
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h - mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    
    # Apply cloud-cover σ modulation
    if use_cloud_adjustment:
        sigma_mult = cloud_cover_sigma_adjustment(idx, days, cloud_data)
        std_adjusted = std * sigma_mult
    else:
        sigma_mult = 1.0
        std_adjusted = std
    
    if std_adjusted <= 0:
        return None, 0.0, sigma_mult
    
    z = (days[idx-1]['high'] - mean) / std_adjusted
    
    # Gaussian signal: z > 1.0 → down, z < -1.0 → up
    if z > 1.0:
        return 'down', min(abs(z) / 2.0, 0.95), sigma_mult
    elif z < -1.0:
        return 'up', min(abs(z) / 2.0, 0.95), sigma_mult
    return None, 0.0, sigma_mult


def walk_forward_backtest(days, cloud_data, market, train_days=180, test_days=30,
                           use_cloud_adjustment=True):
    """Walk-forward backtest comparing Gaussian with/without cloud adjustment."""
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
            pred, conf, sigma_mult = gaussian_signal_with_cloud(
                idx, days, cloud_data, use_cloud_adjustment)
            
            if pred is not None:
                results.append((pred, actual['direction'], conf, date, sigma_mult))
        
        start += test_days
        if start > len(days):
            break
    
    return results, total_days


def run_backtest():
    """Run full walk-forward backtest: Gaussian baseline vs Gaussian + cloud adjustment."""
    print("=" * 90)
    print("EDGE 23: CLOUD-COVER MODULATION — BACKTEST")
    print("=" * 90)
    print(f"METAR DB: {METAR_DB}")
    print(f"WeatherAPI DB: {WEATHERAPI_DB}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"σ multiplier range: [{SIGMA_MIN}, {SIGMA_MAX}]")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()
    print("  Cloud cover codes: CLR=0.0, FEW=0.125, SCT=0.375, BKN=0.625, OVC=1.0")
    print("  Clear → expand σ (more variance), Overcast → compress σ (less variance)")
    print("  3-day average cloud cover used for smoothing")
    print()
    
    conn_metar = sqlite3.connect(METAR_DB, timeout=60)
    conn_metar.execute("PRAGMA journal_mode=WAL;")
    conn_metar.execute("PRAGMA busy_timeout=5000;")
    conn_weatherapi = sqlite3.connect(WEATHERAPI_DB, timeout=60)
    conn_weatherapi.execute("PRAGMA journal_mode=WAL;")
    conn_weatherapi.execute("PRAGMA busy_timeout=5000;")
    
    # Run both configurations
    configs = [
        ("Gaussian baseline (no cloud adj)", False),
        ("Gaussian + cloud-cover σ adj", True),
    ]
    
    all_config_results = {}
    
    for config_name, use_cloud in configs:
        print(f"\n{'=' * 90}")
        print(f"CONFIG: {config_name}")
        print(f"{'=' * 90}")
        
        print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10} {'Avg σ_mult':>10}")
        print("-" * 75)
        
        all_results = []
        total_coverage = 0
        total_days_all = 0
        
        for station in ALL_STATIONS:
            days, cloud_data, market = load_station_data(station, conn_metar, conn_weatherapi)
            if len(days) < 210:
                continue
            
            results, tdays = walk_forward_backtest(days, cloud_data, market,
                                                    use_cloud_adjustment=use_cloud)
            if not results:
                print(f"{station:<8} {'N/A':>8}")
                continue
            
            total = len(results)
            correct = sum(1 for p, a, c, d, s in results if p == a)
            acc = correct / total if total > 0 else 0
            tdays_count = sum(1 for idx in range(180, len(days)) if market.get(days[idx]['date']))
            coverage = total / tdays_count if tdays_count > 0 else 0
            avg_conf = sum(c for p, a, c, d, s in results) / total if total > 0 else 0
            avg_sigma = sum(s for p, a, c, d, s in results) / total if total > 0 else 0
            
            all_results.extend([(p, a, c) for p, a, c, d, s in results])
            total_coverage += total
            total_days_all += tdays_count
            
            print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f} {avg_sigma:>10.3f}")
        
        total = len(all_results)
        correct = sum(1 for p, a, c in all_results if p == a)
        accuracy = correct / total if total > 0 else 0
        
        print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%}")
        
        # Binomial test
        if total > 100:
            z = (accuracy - 0.5) * math.sqrt(total) / math.sqrt(0.25)
            binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
            print(f"  Binomial test p-value: {binom_p:.8f}")
        
        all_config_results[config_name] = {
            'accuracy': accuracy,
            'total': total,
            'correct': correct,
        }
    
    # ─── COMPARISON ───────────────────────────────────────────────────────
    print(f"\n{'=' * 90}")
    print("EDGE 23 COMPARISON: GAUSSIAN BASELINE vs CLOUD-ADJUSTED")
    print(f"{'=' * 90}")
    
    baseline = all_config_results["Gaussian baseline (no cloud adj)"]
    adjusted = all_config_results["Gaussian + cloud-cover σ adj"]
    
    print(f"\n{'Config':<45} {'Accuracy':>10} {'Trades':>8}")
    print("-" * 65)
    print(f"{'Gaussian baseline':<45} {baseline['accuracy']:>10.2%} {baseline['total']:>8}")
    print(f"{'Gaussian + cloud-cover σ adj':<45} {adjusted['accuracy']:>10.2%} {adjusted['total']:>8}")
    
    improvement = (adjusted['accuracy'] - baseline['accuracy']) * 100
    print(f"\n  Improvement: {improvement:+.2f} percentage points")
    
    if improvement > 0:
        print(f"  ✓ Cloud-cover adjustment IMPROVES accuracy")
    else:
        print(f"  ✗ Cloud-cover adjustment does NOT improve accuracy")
    
    # Analyze extreme cloud cover days
    print(f"\n--- Extreme Cloud Cover Day Analysis ---")
    conn_metar.close()
    conn_weatherapi.close()
    
    # Re-open for extreme analysis
    conn_metar = sqlite3.connect(METAR_DB, timeout=60)
    conn_metar.execute("PRAGMA journal_mode=WAL;")
    conn_metar.execute("PRAGMA busy_timeout=5000;")
    conn_weatherapi = sqlite3.connect(WEATHERAPI_DB, timeout=60)
    conn_weatherapi.execute("PRAGMA journal_mode=WAL;")
    conn_weatherapi.execute("PRAGMA busy_timeout=5000;")
    
    extreme_clear = {'correct': 0, 'total': 0}
    extreme_overcast = {'correct': 0, 'total': 0}
    moderate = {'correct': 0, 'total': 0}
    
    for station in ALL_STATIONS:
        days, cloud_data, market = load_station_data(station, conn_metar, conn_weatherapi)
        if len(days) < 210:
            continue
        
        results, _ = walk_forward_backtest(days, cloud_data, market, use_cloud_adjustment=True)
        
        for pred, actual, conf, date, sigma_mult in results:
            # Get the cloud fraction for this date
            frac = None
            from datetime import timedelta
            try:
                dt = datetime.strptime(date, '%Y-%m-%d')
                fracs = []
                for offset in range(3):
                    check_date = (dt - timedelta(days=offset)).strftime('%Y-%m-%d')
                    f = cloud_data.get(check_date)
                    if f is not None:
                        fracs.append(f)
                if fracs:
                    frac = sum(fracs) / len(fracs)
            except (ValueError, KeyError):
                pass
            
            correct = (pred == actual)
            
            if frac is not None:
                if frac <= 0.15:  # Clear
                    extreme_clear['total'] += 1
                    if correct:
                        extreme_clear['correct'] += 1
                elif frac >= 0.85:  # Overcast
                    extreme_overcast['total'] += 1
                    if correct:
                        extreme_overcast['correct'] += 1
                else:
                    moderate['total'] += 1
                    if correct:
                        moderate['correct'] += 1
    
    print(f"\n  Extreme Clear (frac ≤ 0.15):   {extreme_clear['correct']}/{extreme_clear['total']} = {extreme_clear['correct']/extreme_clear['total']:.2%}" if extreme_clear['total'] > 0 else f"  Extreme Clear: 0 trades")
    print(f"  Moderate (0.15 < frac < 0.85): {moderate['correct']}/{moderate['total']} = {moderate['correct']/moderate['total']:.2%}" if moderate['total'] > 0 else f"  Moderate: 0 trades")
    print(f"  Extreme Overcast (frac ≥ 0.85): {extreme_overcast['correct']}/{extreme_overcast['total']} = {extreme_overcast['correct']/extreme_overcast['total']:.2%}" if extreme_overcast['total'] > 0 else f"  Extreme Overcast: 0 trades")
    
    conn_metar.close()
    conn_weatherapi.close()
    
    print(f"\n{'=' * 90}")
    print("EDGE 23 BACKTEST COMPLETE")
    print(f"{'=' * 90}")
    
    return all_config_results


if __name__ == "__main__":
    run_backtest()
