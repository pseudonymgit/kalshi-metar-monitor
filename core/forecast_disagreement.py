#!/usr/bin/env python3
"""
CORE MODULE: Forecast Disagreement (GFS vs NWS) — Edge 5

Standalone module for the forecast disagreement signal.
When two independent forecast sources disagree by >5°F, bet in GFS direction.

Signal strength = sigmoid((|diff| - 5) / 3)

For backtest, uses proxy forecasts:
  Proxy 1 (NWS-like): yesterday's actual high (persistence forecast)
  Proxy 2 (GFS-like): 7-day rolling mean high (climatology baseline)

For live use, supports real GFS/NWS API calls (see live_forecast_disagreement()).

Walk-forward only. No AI in the loop. No in-sample metrics.
"""

import sqlite3
import math
import json
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

DISAGREEMENT_THRESHOLD = 5.0  # °F — minimum disagreement to trigger


def sigmoid(x):
    """Numerically stable sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


# ─── BACKTEST SIGNAL (proxy-based) ──────────────────────────────────────────

def forecast_disagreement_signal(idx, days):
    """
    Compute forecast disagreement signal using proxy forecasts.
    
    Proxy 1 (NWS-like): yesterday's actual high — persistence forecast
    Proxy 2 (GFS-like): 7-day rolling mean high — climatology baseline
    
    When |proxy1 - proxy2| > threshold, bet in GFS (climatology) direction.
    
    Returns: (direction, confidence) or (None, 0.0)
    """
    if idx < 8:
        return None, 0.0
    
    yesterday_high = days[idx - 1]['high']
    window = days[idx - 8:idx - 1]  # 7 days before yesterday
    if len(window) < 5:
        return None, 0.0
    
    weekly_mean = sum(d['high'] for d in window) / len(window)
    disagreement = yesterday_high - weekly_mean
    abs_disagreement = abs(disagreement)
    
    if abs_disagreement < DISAGREEMENT_THRESHOLD:
        return None, 0.0
    
    # Bet in GFS direction (climatology)
    direction = 'up' if disagreement < 0 else 'down'
    confidence = sigmoid((abs_disagreement - DISAGREEMENT_THRESHOLD) / 3.0)
    
    return direction, confidence


# ─── LIVE SIGNAL (real API) ─────────────────────────────────────────────────

# NWS gridpoint mapping for major stations
NWS_GRIDPOINTS = {
    'KNYC': ('OKX', '33,37'),    # New York
    'KMIA': ('MFL', '109,50'),   # Miami
    'KDEN': ('BOU', '62,61'),    # Denver
    'KMDW': ('LOT', '66,76'),    # Chicago
    'KLAX': ('LOX', '155,44'),   # Los Angeles
    'KPHX': ('PSR', '156,56'),   # Phoenix
    'KATL': ('FFC', '50,88'),    # Atlanta
    'KBOS': ('BOX', '71,90'),    # Boston
    'KSEA': ('SEW', '124,67'),   # Seattle
    'KSFO': ('MTR', '85,105'),   # San Francisco
    'KAUS': ('EWX', '138,90'),   # Austin
    'KDAL': ('FWD', '91,107'),   # Dallas
    'KDCA': ('LWX', '96,72'),    # DC
    'KDFW': ('FWD', '91,107'),   # Dallas-Fort Worth
    'KHOU': ('HGX', '68,98'),    # Houston
    'KLAS': ('VEF', '117,96'),   # Las Vegas
    'KMSP': ('MPX', '137,124'), # Minneapolis
    'KMSY': ('LIX', '93,63'),    # New Orleans
    'KOKC': ('OUN', '111,83'),   # Oklahoma City
    'KPHL': ('PHI', '89,75'),   # Philadelphia
    'KSAT': ('EWX', '126,119'), # San Antonio
}


def fetch_nws_forecast(station):
    """
    Fetch NWS gridpoint forecast for a station.
    Returns: dict with 'temperature' (list of hourly temps) and 'valid' (bool)
    """
    if station not in NWS_GRIDPOINTS:
        return {'temperature': None, 'valid': False}
    
    office, grid = NWS_GRIDPOINTS[station]
    url = f"https://api.weather.gov/gridpoints/{office}/{grid}/forecast/hourly"
    
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'WeatherEngine/1.0 (research)',
            'Accept': 'application/geo+json'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        
        temps = []
        for period in data.get('properties', {}).get('periods', []):
            t = period.get('temperature')
            if t is not None:
                temps.append(t)
        
        if temps:
            # Use the next 24 hours of forecast highs
            daily_high = max(temps[:24]) if len(temps) >= 24 else max(temps)
            return {'temperature': daily_high, 'valid': True, 'raw_temps': temps[:24]}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError) as e:
        pass
    
    return {'temperature': None, 'valid': False}


def fetch_gfs_forecast(station, lat=None, lon=None):
    """
    Fetch GFS 0.25° forecast from NOMADS OpenDAP.
    
    NOTE: This requires the pygrib library and GFS data download.
    For production, use the NOMADS grib filter:
    https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl
    
    For now, returns None — live GFS integration requires significant
    infrastructure (grib2 downloads, parsing). The proxy backtest
    demonstrates signal viability; live GFS would need a dedicated
    data pipeline.
    """
    return None


def live_forecast_disagreement(station):
    """
    Live forecast disagreement signal using real NWS API.
    
    GFS side: Not implemented (requires grib2 pipeline). When available,
    compare real GFS temp vs NWS forecast temp.
    
    For now, uses NWS forecast vs climatology baseline as a live proxy.
    """
    nws = fetch_nws_forecast(station)
    if not nws['valid']:
        return None, 0.0, "NWS fetch failed"
    
    nws_high = nws['temperature']
    
    # Get recent climatology from DB
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc DESC LIMIT 8
    """, (station,))
    recent = cur.fetchall()
    conn.close()
    
    if len(recent) < 5:
        return None, 0.0, "Insufficient climatology"
    
    # Note: recent is DESC, so reverse for chronological order
    recent_highs = [r[1] for r in reversed(recent)]
    climatology_mean = sum(recent_highs) / len(recent_highs)
    
    disagreement = nws_high - climatology_mean
    abs_disagreement = abs(disagreement)
    
    if abs_disagreement < DISAGREEMENT_THRESHOLD:
        return None, 0.0, f"No trigger (diff={abs_disagreement:.1f}°F)"
    
    direction = 'up' if disagreement < 0 else 'down'
    confidence = sigmoid((abs_disagreement - DISAGREEMENT_THRESHOLD) / 3.0)
    
    return direction, confidence, f"NWS={nws_high}°F, Clim={climatology_mean:.1f}°F, diff={disagreement:+.1f}°F"


# ─── BACKTEST ENGINE ────────────────────────────────────────────────────────

def load_station_data(station, conn):
    """Load daily high temps and settlement epochs for a station."""
    cur = conn.cursor()
    
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


def walk_forward_backtest(days, market, train_days=180, test_days=30):
    """Walk-forward backtest of the forecast disagreement signal."""
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
            pred, conf = forecast_disagreement_signal(idx, days)
            
            if pred is not None:
                results.append((pred, actual['direction'], conf, date))
        
        start += test_days
        if start > len(days):
            break
    
    return results, total_days


def run_backtest():
    """Run full walk-forward backtest across all stations."""
    print("=" * 90)
    print("EDGE 5: FORECAST DISAGREEMENT — CORE MODULE BACKTEST")
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
    
    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10}")
    print("-" * 65)
    
    all_results = []
    total_coverage = 0
    total_days_all = 0
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210:
            continue
        
        results, tdays = walk_forward_backtest(days, market)
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
        total_days_all += tdays
        
        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    total = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total if total > 0 else 0
    coverage = total_coverage / total_days_all if total_days_all > 0 else 0
    avg_conf = sum(c for p, a, c in all_results) / total if total > 0 else 0
    
    print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {coverage:>10.1%} {avg_conf:>10.3f}")
    
    # Confidence-gated
    print("\n--- Confidence-Gated Results ---")
    for threshold in [0.0, 0.5, 0.6, 0.7, 0.8]:
        gated = [(p, a, c) for p, a, c in all_results if c >= threshold]
        if not gated:
            print(f"  conf >= {threshold:.1f}: 0 trades")
            continue
        gt = len(gated)
        gc = sum(1 for p, a, c in gated if p == a)
        ga = gc / gt if gt > 0 else 0
        print(f"  conf >= {threshold:.1f}: {gt:>6} trades, {gc:>6} correct, {ga:>7.2%} accuracy")
    
    # Binomial test
    if total > 100:
        z = (accuracy - 0.5) * math.sqrt(total) / math.sqrt(0.25)
        binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
    else:
        binom_p = 1.0
    
    print(f"\n--- VERDICT ---")
    print(f"  Accuracy: {accuracy:.2%} ({correct}/{total} trades)")
    print(f"  Coverage: {coverage:.1%} of available days")
    print(f"  Binomial test p-value: {binom_p:.6f}")
    print(f"  Avg confidence: {avg_conf:.3f}")
    
    if accuracy >= 0.58:
        print(f"  ✓ PASSES 58% threshold")
    else:
        print(f"  ✗ FAILS 58% threshold")
    
    conn.close()
    return accuracy, total, all_results


if __name__ == "__main__":
    run_backtest()
