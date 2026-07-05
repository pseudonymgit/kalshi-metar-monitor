#!/usr/bin/env python3
"""
METAR Backfill - Missing Stations Only
Focuses on the 3 missing stations: KAUS, KDFW, KHOU
"""

import os
import sqlite3
import time
import urllib.request
import gzip
import io
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Missing stations with ISD USAF-WBAN station IDs
# These stations need to be added to the existing database
# Verified station IDs from NOAA FTP: 2021-06-30
STATION_LIST = [
    ("KAUS", "Austin", "722540-13904"),  # Updated WBAN
    ("KDFW", "Dallas DFW", "722590-03927"),  # DFW airport code
    ("KHOU", "Houston", "722430-12960"),  # Updated WBAN
]

# NOAA ISD-Lite HTTPS configuration
ISD_LITE_BASE_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-lite"
USER_AGENT = "OpenClaw-Weather-Engine/1.0 (dan@openclaw.ai)"

# Output paths
OUTPUT_DIR = Path(__file__).parent
DB_PATH = OUTPUT_DIR / "metar_backfill.db"
LOG_PATH = OUTPUT_DIR / "backfill_missing.log"
YEARS_TO_FETCH = list(range(2021, 2026))


def fetch_isd_lite_gzip(station_id: str, year: int) -> Optional[str]:
    """Fetch ISD-Lite gzip data for a station and year via HTTPS."""
    filename = f"{station_id}-{year}.gz"
    url = f"{ISD_LITE_BASE_URL}/{year}/{filename}"
    
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/gzip'
        })
        
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                gzip_data = response.read()
                with gzip.GzipFile(fileobj=io.BytesIO(gzip_data)) as gz:
                    return gz.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"HTTP Error fetching {url}: {e.code} {e.reason}", file=sys.stderr)
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
    
    return None


def parse_isd_lite_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a single ISD-Lite CSV line."""
    try:
        parts = line.strip().split()
        if len(parts) < 12:
            return None
        
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        
        temp_c = float(parts[4]) / 10.0
        if temp_c == -999.9:
            return None
        
        dew_c = float(parts[5]) / 10.0 if len(parts) > 5 else -999.9
        if dew_c == -999.9:
            dew_c = None
        
        pressure = float(parts[6]) / 10.0 if len(parts) > 6 else -999.9
        if pressure == -999.9:
            pressure = None
        
        wind_dir = int(parts[7]) if len(parts) > 7 else -9999
        if wind_dir == -9999:
            wind_dir = None
        
        wind_spd = float(parts[8]) / 10.0 if len(parts) > 8 else -999.9
        if wind_spd == -999.9:
            wind_spd = None
        
        # Convert to Fahrenheit
        temp_f = temp_c * 9.0 / 5.0 + 32.0
        dew_f = dew_c * 9.0 / 5.0 + 32.0 if dew_c is not None else None
        wind_kts = wind_spd * 1.94384 if wind_spd is not None else None
        
        try:
            dt = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
        except ValueError:
            return None
        
        date_utc = dt.strftime('%Y-%m-%d')
        timestamp_utc = dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
        
        return {
            'timestamp': timestamp_utc,
            'temp_f': temp_f,
            'temp_c': temp_c,
            'dewpoint_f': dew_f,
            'dewpoint_c': dew_c,
            'pressure_mb': pressure,
            'wind_direction_deg': wind_dir,
            'wind_speed_kt': wind_kts,
            'date_utc': date_utc,
        }
    except (ValueError, IndexError):
        return None


def process_observations(observations: List[Dict[str, Any]], conn: sqlite3.Connection, 
                         station: str) -> int:
    """Process observations and insert into database with daily aggregation."""
    inserted = 0
    daily_data = defaultdict(list)
    
    for obs in observations:
        date_utc = obs.get('date_utc', '')
        if not date_utc:
            continue
        daily_data[date_utc].append(obs)
    
    for date_utc, day_obs in daily_data.items():
        temps_f = [o.get('temp_f') for o in day_obs if o.get('temp_f') is not None]
        
        if not temps_f:
            continue
        
        max_temp = max(temps_f)
        min_temp = min(temps_f)
        avg_temp = sum(temps_f) / len(temps_f)
        
        timestamps = [o.get('timestamp') for o in day_obs]
        
        # Insert daily stat
        conn.execute("""
            INSERT OR REPLACE INTO daily_stats 
            (station, date_local, date_utc, max_temp_f, min_temp_f, avg_temp_f, 
             observation_count, first_observation_utc, last_observation_utc, ingestion_timestamp_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            station, date_utc, date_utc,
            round(max_temp, 1), round(min_temp, 1), round(avg_temp, 1),
            len(temps_f),
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps else None,
            datetime.now(timezone.utc).isoformat()
        ))
        
        inserted += 1
        
        for obs in day_obs:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO metar_observations 
                    (station, date_utc, timestamp_utc, temp_f, temp_c, dewpoint_f, dewpoint_c,
                     wind_direction_deg, wind_speed_kt, wind_gust_kt, pressure_mb, sea_level_pressure_mb,
                     visibility_mi, ceiling_ft, raw_metar, source, ingestion_timestamp_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    station, date_utc, obs.get('timestamp'),
                    obs.get('temp_f'), obs.get('temp_c'),
                    obs.get('dewpoint_f'), obs.get('dewpoint_c'),
                    obs.get('wind_direction_deg'), obs.get('wind_speed_kt'),
                    None, obs.get('pressure_mb'), None,
                    None, None, None, 'isd-lite',
                    datetime.now(timezone.utc).isoformat()
                ))
            except sqlite3.IntegrityError:
                pass
    
    return inserted


def main():
    print("=" * 70)
    print("METAR Backfill - Missing Stations Only (KAUS, KDFW, KHOU)")
    print("=" * 70)
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    log_file = open(LOG_PATH, 'a')
    
    try:
        total_records = 0
        
        for station_icao, city_name, station_id in STATION_LIST:
            print(f"\nProcessing {station_icao} ({city_name}) - Station ID: {station_id}")
            print(f"{'='*60}", file=log_file)
            print(f"Processing {station_icao} ({city_name}) - Station ID: {station_id}", file=log_file)
            
            # Verify station ID exists on NOAA FTP
            try:
                ftp = ftplib.FTP('ftp.ncei.noaa.gov')
                ftp.login('anonymous', 'anonymous@openclaw.ai')
                ftp.cwd('pub/data/noaa/isd-lite/2021')
                files = []
                ftp.retrlines('NLST', lambda x: files.append(x))
                ftp.quit()
                expected_file = f"{station_id}-2021.gz"
                if expected_file not in files:
                    # Try alternate year
                    print(f"  Warning: {expected_file} not found in 2021", file=log_file, flush=True)
            except Exception as e:
                print(f"  FTP check error: {e}", file=log_file, flush=True)
            
            for year in YEARS_TO_FETCH:
                print(f"  Fetching {year}...", file=log_file, flush=True)
                
                gzip_content = fetch_isd_lite_gzip(station_id, year)
                if gzip_content is None:
                    print(f"    No data for {year}", file=log_file, flush=True)
                    continue
                
                lines = gzip_content.split('\n')
                observations = []
                for line in lines:
                    if not line.strip():
                        continue
                    obs = parse_isd_lite_line(line)
                    if obs is not None:
                        observations.append(obs)
                
                print(f"    Fetched {len(observations)} observations for {year}", file=log_file, flush=True)
                
                if observations:
                    inserted = process_observations(observations, conn, station_icao)
                    total_records += inserted
                    print(f"    Processed {inserted} daily records", file=log_file, flush=True)
                
                time.sleep(0.5)  # Rate limiting
            
            print(f"\n{station_icao} complete!", file=log_file, flush=True)
        
        conn.commit()
        
        # Get final counts for new stations
        for station_icao, city_name, _ in STATION_LIST:
            stats = conn.execute("""
                SELECT COUNT(*) as count, MIN(date_utc) as first, MAX(date_utc) as last
                FROM daily_stats WHERE station = ?
            """, (station_icao,)).fetchone()
            
            total_obs = conn.execute("""
                SELECT COUNT(*) FROM metar_observations WHERE station = ?
            """, (station_icao,)).fetchone()[0]
            
            print(f"  {station_icao}: {stats[0]} daily records, {total_obs} total observations", file=log_file, flush=True)
            print(f"  {station_icao}: Date range {stats[1]} to {stats[2]}")
        
        # Update metadata
        conn.execute("""
            INSERT OR REPLACE INTO backfill_metadata (key, value, timestamp_utc)
            VALUES (?, ?, ?)
        """, ('backfill_missing_stations', datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        
        conn.commit()
        
        print("\n" + "=" * 70)
        print("Backfill Complete!")
        print("=" * 70)
        print(f"Total new records: {total_records}")
        print(f"Log: {LOG_PATH}")
        
    finally:
        if log_file:
            log_file.close()
        conn.close()


if __name__ == '__main__':
    main()
