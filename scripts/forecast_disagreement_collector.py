#!/usr/bin/env python3
"""
FORECAST DISAGREEMENT — LIVE DATA COLLECTOR

Daily collection of GFS and NWS forecasts for 21 Kalshi stations.
Stores raw forecasts, disagreement signal, ensemble prediction, and
(back-filled later) actual settlement outcome.

Runs via cron daily at 7am ET (before Kalshi market settlement).

Resilient: retries on API failures, logs everything, never crashes.
"""

import sqlite3
import json
import urllib.request
import urllib.error
import math
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/forecast_disagreement_live.db"
LOG_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/logs/forecast_disagreement_collector.log"
METAR_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Station coordinates (ICAO → lat, lon)
STATIONS = {
    'KATL': (33.6407, -84.4277), 'KAUS': (30.1945, -97.6699),
    'KBOS': (42.3656, -71.0096), 'KDAL': (32.8471, -96.8517),
    'KDCA': (38.8512, -77.0402), 'KDEN': (39.8561, -104.6737),
    'KDFW': (32.8998, -97.0403), 'KHOU': (29.6454, -95.2789),
    'KLAS': (36.0840, -115.1537), 'KLAX': (33.9425, -118.4081),
    'KMDW': (41.7868, -87.7522), 'KMIA': (25.7959, -80.2870),
    'KMSP': (44.8848, -93.2223), 'KMSY': (29.9934, -90.2580),
    'KNYC': (40.7829, -73.9654), 'KOKC': (35.3931, -97.6007),
    'KPHL': (39.8744, -75.2424), 'KPHX': (33.4342, -112.0116),
    'KSAT': (29.5337, -98.4698), 'KSEA': (47.4502, -122.3088),
    'KSFO': (37.6213, -122.3790),
}

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
API_TIMEOUT = 20  # seconds
DISAGREEMENT_THRESHOLD = 5.0  # °F

# ─── LOGGING ─────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ─── DATABASE SETUP ─────────────────────────────────────────────────────────

def setup_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_forecast_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_date TEXT NOT NULL,          -- Date of the forecast target (YYYY-MM-DD)
            collection_timestamp_utc TEXT NOT NULL,  -- When we collected it
            station TEXT NOT NULL,
            nws_office TEXT,
            nws_grid_x INTEGER,
            nws_grid_y INTEGER,
            nws_forecast_high_f REAL,
            nws_forecast_low_f REAL,
            nws_raw_json TEXT,                      -- Full NWS forecast JSON
            gfs_forecast_high_f REAL,
            gfs_forecast_low_f REAL,
            gfs_raw_json TEXT,                      -- Full GFS response JSON
            disagreement_f REAL,                    -- |GFS_high - NWS_high|
            signal_direction TEXT,                  -- 'up', 'down', or NULL
            signal_confidence REAL,
            ensemble_prediction TEXT,               -- 'up', 'down', or NULL
            ensemble_confidence REAL,
            actual_high_f REAL,                     -- Back-filled from METAR
            actual_direction TEXT,                  -- 'up' or 'down' (vs prior day)
            actual_settlement_bucket INTEGER,
            actual_prior_bucket INTEGER,
            status TEXT DEFAULT 'forecast_collected', -- forecast_collected → settled
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collection_date, station)
        )
    """)
    
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_snap_date 
        ON daily_forecast_snapshots(collection_date)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_snap_station 
        ON daily_forecast_snapshots(station)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_snap_status 
        ON daily_forecast_snapshots(status)
    """)
    
    # Collection run log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp_utc TEXT NOT NULL,
            stations_attempted INTEGER,
            stations_succeeded INTEGER,
            stations_failed INTEGER,
            errors TEXT,
            duration_seconds REAL,
            status TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    log.info(f"Database ready: {DB_PATH}")

# ─── API CALLS ───────────────────────────────────────────────────────────────

def api_get_json(url, headers=None):
    """Fetch JSON from URL with retries."""
    if headers is None:
        headers = {'User-Agent': 'OpenClaw-Weather-Engine/1.0 (research)'}
    
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # Not found, don't retry
            log.warning(f"  Attempt {attempt+1}/{MAX_RETRIES}: HTTP {e.code} for {url[:80]}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
        except Exception as e:
            log.warning(f"  Attempt {attempt+1}/{MAX_RETRIES}: {e} for {url[:80]}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None

def get_nws_gridpoint(lat, lon):
    """Get NWS forecast office and gridpoint for a location."""
    url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    data = api_get_json(url)
    if data and 'properties' in data:
        props = data['properties']
        return props.get('gridId'), props.get('gridX'), props.get('gridY')
    return None, None, None

def get_nws_forecast(office, x, y):
    """Get NWS daily forecast from gridpoint endpoint."""
    url = f"https://api.weather.gov/gridpoints/{office}/{x},{y}/forecast"
    data = api_get_json(url)
    if not data or 'properties' not in data:
        return None, None, None
    
    # Extract daily highs/lows from forecast periods
    forecasts = {}  # date → {high, low}
    for period in data['properties']['periods']:
        start = period.get('startTime', '')
        date = start[:10]  # YYYY-MM-DD
        temp = period.get('temperature')
        is_day = period.get('isDaytime', False)
        
        if date not in forecasts:
            forecasts[date] = {}
        
        if is_day:
            forecasts[date]['high'] = temp
        else:
            forecasts[date]['low'] = temp
    
    # Get today's and tomorrow's forecast
    today = datetime.utcnow().strftime('%Y-%m-%d')
    
    # Find the first daytime period (today or next available)
    today_high = None
    today_low = None
    for date, vals in sorted(forecasts.items()):
        if 'high' in vals:
            today_high = vals['high']
            today_low = vals.get('low')
            break
    
    return today_high, today_low, json.dumps(data)

def get_gfs_forecast(lat, lon):
    """Get GFS forecast from Open-Meteo (accessing NOMADS GFS 0.25°)."""
    # Request today's forecast
    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&timezone=America/New_York"
           f"&models=gfs_seamless"
           f"&forecast_days=3")
    
    data = api_get_json(url)
    if not data or 'daily' not in data:
        return None, None, None
    
    daily = data['daily']
    dates = daily.get('time', [])
    highs = daily.get('temperature_2m_max', [])  # Celsius
    lows = daily.get('temperature_2m_min', [])
    
    if not dates or not highs:
        return None, None, None
    
    # First entry is today
    high_c = highs[0]
    low_c = lows[0] if lows else None
    
    high_f = round(high_c * 9/5 + 32, 1) if high_c is not None else None
    low_f = round(low_c * 9/5 + 32, 1) if low_c is not None else None
    
    return high_f, low_f, json.dumps(data)

# ─── SIGNAL COMPUTATION ──────────────────────────────────────────────────────

def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x)) if abs(x) < 100 else (1.0 if x > 0 else 0.0)

def compute_disagreement_signal(gfs_high, nws_high):
    """Compute forecast disagreement signal per Gray Room spec."""
    if gfs_high is None or nws_high is None:
        return None, 0.0, 0.0
    
    disagreement = abs(gfs_high - nws_high)
    
    if disagreement < DISAGREEMENT_THRESHOLD:
        return None, 0.0, disagreement
    
    # Bet in GFS direction
    direction = 'up' if gfs_high > nws_high else 'down'
    confidence = sigmoid((disagreement - DISAGREEMENT_THRESHOLD) / 3.0)
    
    return direction, confidence, disagreement

def compute_ensemble_prediction(station, target_date):
    """Compute ensemble prediction using existing METAR-based signals."""
    try:
        conn = sqlite3.connect(METAR_DB_PATH, timeout=10)
        cur = conn.cursor()
        
        # Get daily highs for the last 50 days
        cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station=? AND temp_f IS NOT NULL
            AND date_utc <= ?
            GROUP BY date_utc
            ORDER BY date_utc DESC
            LIMIT 50
        """, (station, target_date))
        rows = cur.fetchall()
        conn.close()
        
        if len(rows) < 48:
            return None, 0.0
        
        # Build days array (reverse to chronological)
        days = []
        for r in reversed(rows):
            if r[1] is not None:
                days.append({'date': r[0], 'high': r[1], 'pressure': r[2]})
        
        if len(days) < 48:
            return None, 0.0
        
        idx = len(days) - 1  # Today is the last day
        
        # Run 4 approaches (reversion, gaussian, gaussian_v2, pressure)
        predictions = {}
        
        # Reversion (30-day)
        if idx >= 31:
            window = days[idx-31:idx-1]
            highs = [d['high'] for d in window]
            mean = sum(highs) / len(highs)
            var = sum((h-mean)**2 for h in highs) / len(highs)
            std = math.sqrt(var) if var > 0 else 0.01
            z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
            if z > 0.5:
                predictions['reversion'] = ('down', abs(z))
            elif z < -0.5:
                predictions['reversion'] = ('up', abs(z))
        
        # Gaussian (48-day)
        if idx >= 48:
            window = days[idx-48:idx-1]
            highs = [d['high'] for d in window]
            mean = sum(highs) / len(highs)
            var = sum((h-mean)**2 for h in highs) / len(highs)
            std = math.sqrt(var) if var > 0 else 0.01
            z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
            if z > 1.0:
                predictions['gaussian'] = ('down', abs(z))
            elif z < -1.0:
                predictions['gaussian'] = ('up', abs(z))
        
        # Gaussian v2 (30-day, lower threshold)
        if idx >= 31:
            window = days[idx-31:idx-1]
            highs = [d['high'] for d in window]
            mean = sum(highs) / len(highs)
            var = sum((h-mean)**2 for h in highs) / len(highs)
            std = math.sqrt(var) if var > 0 else 0.01
            z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
            if z > 0.5:
                predictions['gaussian_v2'] = ('down', abs(z))
            elif z < -0.5:
                predictions['gaussian_v2'] = ('up', abs(z))
        
        # Pressure
        if idx >= 2 and days[idx-1]['pressure'] and days[idx-2]['pressure']:
            dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
            if abs(dp) > 2.0:
                predictions['pressure'] = (('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8))
        
        # Weighted vote
        if len(predictions) >= 2:
            wsum = sum((1 if p=='up' else -1) * c for _, (p, c) in predictions.items())
            aw = sum(c for _, (p, c) in predictions.items())
            if aw > 0:
                conf = abs(wsum) / aw
                if conf >= 0.7:
                    return ('up' if wsum > 0 else 'down'), conf
        elif len(predictions) == 1:
            pred, conf = list(predictions.values())[0]
            if conf >= 0.7:
                return pred, conf
        
        return None, 0.0
    except Exception as e:
        log.warning(f"  Ensemble prediction error for {station}: {e}")
        return None, 0.0

# ─── BACKFILL ACTUAL OUTCOMES ────────────────────────────────────────────────

def backfill_actuals():
    """Update previous days' records with actual METAR observations."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        metar_conn = sqlite3.connect(METAR_DB_PATH, timeout=10)
        cur = conn.cursor()
        mcur = metar_conn.cursor()
        
        # Find records that need actuals (older than today, status still forecast_collected)
        cur.execute("""
            SELECT id, collection_date, station 
            FROM daily_forecast_snapshots 
            WHERE status = 'forecast_collected'
            AND collection_date < date('now')
            ORDER BY collection_date DESC
            LIMIT 50
        """)
        
        records = cur.fetchall()
        if not records:
            return 0
        
        updated = 0
        for rec_id, date, station in records:
            # Get actual high from METAR
            mcur.execute("""
                SELECT MAX(temp_f) as high
                FROM metar_observations
                WHERE station=? AND date_utc=? AND temp_f IS NOT NULL
            """, (station, date))
            metar_row = mcur.fetchone()
            
            if not metar_row or metar_row[0] is None:
                continue
            
            actual_high = metar_row[0]
            
            # Get settlement bucket from epochs
            mcur.execute("""
                SELECT settlement_bucket, prior_settlement_bucket
                FROM settlement_epochs
                WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
                AND local_trading_date=?
            """, (station, date))
            epoch_row = mcur.fetchone()
            
            actual_bucket = epoch_row[0] if epoch_row else None
            prior_bucket = epoch_row[1] if epoch_row else None
            actual_direction = None
            if actual_bucket is not None and prior_bucket is not None:
                actual_direction = 'up' if actual_bucket > prior_bucket else 'down'
            
            cur.execute("""
                UPDATE daily_forecast_snapshots
                SET actual_high_f = ?,
                    actual_settlement_bucket = ?,
                    actual_prior_bucket = ?,
                    actual_direction = ?,
                    status = 'settled'
                WHERE id = ?
            """, (actual_high, actual_bucket, prior_bucket, actual_direction, rec_id))
            updated += 1
        
        conn.commit()
        conn.close()
        metar_conn.close()
        
        if updated > 0:
            log.info(f"Backfilled {updated} records with actual outcomes")
        return updated
    except Exception as e:
        log.error(f"Backfill error: {e}")
        return 0

# ─── MAIN COLLECTION ────────────────────────────────────────────────────────

def collect_daily():
    """Main daily collection routine."""
    run_start = datetime.utcnow()
    today = run_start.strftime('%Y-%m-%d')
    timestamp = run_start.isoformat()
    
    log.info("=" * 70)
    log.info(f"DAILY COLLECTION START — {today} ({timestamp})")
    log.info("=" * 70)
    
    setup_database()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cur = conn.cursor()
    
    succeeded = 0
    failed = 0
    errors = []
    signals_triggered = 0
    
    for station, (lat, lon) in sorted(STATIONS.items()):
        station_start = time.time()
        log.info(f"  {station} ({lat:.4f}, {lon:.4f})...")
        
        try:
            # 1. Get NWS gridpoint (cached if we've done it before)
            cur.execute("""
                SELECT nws_office, nws_grid_x, nws_grid_y 
                FROM daily_forecast_snapshots 
                WHERE station=? AND nws_office IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """, (station,))
            cached = cur.fetchone()
            
            if cached:
                office, gx, gy = cached
            else:
                office, gx, gy = get_nws_gridpoint(lat, lon)
                if office is None:
                    raise Exception(f"Failed to get NWS gridpoint for {station}")
                log.info(f"    NWS gridpoint: {office}/{gx},{gy}")
            
            # 2. Get NWS forecast
            nws_high, nws_low, nws_raw = get_nws_forecast(office, gx, gy)
            if nws_high is None:
                raise Exception(f"NWS forecast returned no data for {station}")
            
            # 3. Get GFS forecast
            gfs_high, gfs_low, gfs_raw = get_gfs_forecast(lat, lon)
            if gfs_high is None:
                raise Exception(f"GFS forecast returned no data for {station}")
            
            # 4. Compute disagreement signal
            signal_dir, signal_conf, disagreement = compute_disagreement_signal(gfs_high, nws_high)
            
            # 5. Compute ensemble prediction (METAR-based)
            ensemble_dir, ensemble_conf = compute_ensemble_prediction(station, today)
            
            # 6. Store everything
            cur.execute("""
                INSERT OR REPLACE INTO daily_forecast_snapshots
                (collection_date, collection_timestamp_utc, station,
                 nws_office, nws_grid_x, nws_grid_y,
                 nws_forecast_high_f, nws_forecast_low_f, nws_raw_json,
                 gfs_forecast_high_f, gfs_forecast_low_f, gfs_raw_json,
                 disagreement_f, signal_direction, signal_confidence,
                 ensemble_prediction, ensemble_confidence,
                 status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (today, timestamp, station,
                  office, gx, gy,
                  nws_high, nws_low, nws_raw,
                  gfs_high, gfs_low, gfs_raw,
                  disagreement, signal_dir, signal_conf,
                  ensemble_dir, ensemble_conf,
                  'forecast_collected'))
            
            conn.commit()
            succeeded += 1
            
            status_parts = [f"NWS={nws_high}°F", f"GFS={gfs_high}°F", f"diff={disagreement:.1f}°F"]
            if signal_dir:
                status_parts.append(f"Signal={signal_dir}({signal_conf:.2f})")
                signals_triggered += 1
            if ensemble_dir:
                status_parts.append(f"Ensemble={ensemble_dir}({ensemble_conf:.2f})")
            
            elapsed = time.time() - station_start
            log.info(f"    {' | '.join(status_parts)} ({elapsed:.1f}s)")
            
            # Rate limit: 1 second between stations
            time.sleep(1)
            
        except Exception as e:
            failed += 1
            errors.append(f"{station}: {e}")
            log.error(f"    FAILED: {e}")
    
    # Backfill actuals for previous days
    backfill_count = backfill_actuals()
    
    # Summary
    run_end = datetime.utcnow()
    duration = (run_end - run_start).total_seconds()
    
    cur.execute("""
        INSERT INTO collection_runs
        (run_timestamp_utc, stations_attempted, stations_succeeded, stations_failed,
         errors, duration_seconds, status)
        VALUES (?,?,?,?,?,?,?)
    """, (timestamp, len(STATIONS), succeeded, failed,
          json.dumps(errors) if errors else None,
          duration, 'complete' if failed == 0 else 'partial'))
    conn.commit()
    conn.close()
    
    log.info("=" * 70)
    log.info(f"COLLECTION COMPLETE — {succeeded}/{len(STATIONS)} succeeded, {failed} failed")
    log.info(f"  Signals triggered: {signals_triggered}/{succeeded}")
    log.info(f"  Backfilled actuals: {backfill_count}")
    log.info(f"  Duration: {duration:.1f}s")
    if errors:
        log.warning(f"  Errors: {errors}")
    log.info("=" * 70)
    
    return succeeded, failed, signals_triggered


if __name__ == "__main__":
    collect_daily()
