#!/usr/bin/env python3
"""
WAVE 1, STEP 1: NOAA ISD Backfill

Download 5 years of ISD data from NOAA FTP, parse fixed-width format,
store in SQLite with proper indexing.

Target: 5 years of data covering all 20 Kalshi cities.
"""

import os
import re
import sqlite3
import ftplib
from datetime import datetime, timedelta
from collections import defaultdict
import sys
import gzip
import shutil

# Kalshi cities: stations for KXHIGH and KXLOW markets
KALSHI_CITIES = {
    'KXHIGHNY': 'KNYC',  # New York
    'KXHIGHLA': 'KLAX',  # Los Angeles
    'KXHIGHCHI': 'KDCA', # Washington DC (proxies for Chicago area)
    'KXHIGHDFW': 'KDFW', # Dallas
    'KXLOWNY': 'KNYC',
    'KXLOWLA': 'KLAX',
    'KXLOWCHI': 'KDCA',
    'KXLOWDFW': 'KDFW',
}

# Alternative stations for other Kalshi cities
ALTERNATIVE_STATIONS = {
    'KBOS',  # Boston
    'KSFO',  # San Francisco
    'KMIA',  # Miami
    'KMDW',  # Chicago
    'KPHX',  # Phoenix
    'KSAT',  # San Antonio
    'KMSY',  # New Orleans
    'KOKC',  # Oklahoma City
    'KATL',  # Atlanta
    'KSEA',  # Seattle
    'KMSP',  # Minneapolis
    'KDAL',  # Dallas/Fort Worth
    'KDEN',  # Denver
    'KPHL',  # Philadelphia
    'KLAS',  # Las Vegas
}

ALL_KALSHI_STATIONS = set(KALSHI_CITIES.values()) | ALTERNATIVE_STATIONS

# ISD FTP path - NCEI ISD (International Surface Data)
ISD_FTP_BASE = "ftp://ftp.ncei.noaa.gov/pub/data/noaa/"

# SQLite schema
ISD_SCHEMA = """
CREATE TABLE IF NOT EXISTS isd_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    date_utc DATE NOT NULL,
    timestamp_utc TEXT NOT NULL,
    temp_f REAL,
    temp_c REAL,
    dew_f REAL,
    dew_c REAL,
    wind_dir_deg REAL,
    wind_speed_kt REAL,
    wind_gust_kt REAL,
    pressure_mb REAL,
    sea_level_pressure_mb REAL,
    visibility_mi REAL,
    ceiling_ft REAL,
    raw_line TEXT,
    created_at_utc TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_isd_station ON isd_raw(station);
CREATE INDEX IF NOT EXISTS idx_isd_date ON isd_raw(date_utc);
CREATE INDEX IF NOT EXISTS idx_isd_station_date ON isd_raw(station, date_utc);

CREATE TABLE IF NOT EXISTS isd_backfill_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    stations_downloaded INTEGER,
    records_inserted INTEGER,
    status TEXT,
    started_at TEXT,
    completed_at TEXT
);
"""


def parse_isd_line(line):
    """
    Parse ISD fixed-width format line.
    
    Format reference: https://www.ncei.noaa.gov/data/international-screening-dataset/doc/isd-format.pdf
    
    Key fields (positions are 1-indexed):
    - USAF: 1-6
    - WBAN: 7-12
    - DATE: 14-25 (YYYYMMDDHHMM)
    - TMP: 44-50, 52 (temperature, tenths of degrees C)
    - DEW: 60-66, 68 (dew point, tenths of degrees C)
    - SLP: 88-93, 95 (sea level pressure, tenths of millibars)
    - Wind: various positions
    """
    if len(line) < 100:  # Minimal valid line length
        return None
    
    try:
        usaf = line[0:6].strip()
        wban = line[7:12].strip()
        
        # Date/time: YYYYMMDDHHMM
        date_str = line[13:25].strip()
        if len(date_str) < 12:
            return None
        
        try:
            dt = datetime.strptime(date_str, "%Y%m%d%H%M")
            date_utc = dt.strftime("%Y-%m-%d")
            timestamp_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
        
        # Temperature (tenths of degrees C)
        tmp_f = None
        if len(line) > 52:
            tmp_part = line[43:52].strip()
            if tmp_part and tmp_part not in ['+9999', '9999']:
                try:
                    tmp_c = float(tmp_part) / 10.0
                    tmp_f = tmp_c * 9/5 + 32
                except ValueError:
                    tmp_f = None
        
        # Dew point (tenths of degrees C)
        dew_f = None
        if len(line) > 68:
            dew_part = line[59:68].strip()
            if dew_part and dew_part not in ['+9999', '9999']:
                try:
                    dew_c = float(dew_part) / 10.0
                    dew_f = dew_c * 9/5 + 32
                except ValueError:
                    dew_f = None
        
        # Sea level pressure (tenths of millibars)
        slp_mb = None
        if len(line) > 95:
            slp_part = line[87:95].strip()
            if slp_part and slp_part not in ['99999', '+99999']:
                try:
                    slp_mb = float(slp_part) / 10.0
                except ValueError:
                    slp_mb = None
        
        # Wind direction (degrees)
        wind_dir = None
        if len(line) > 46:
            wind_dir_part = line[31:36].strip()
            if wind_dir_part and wind_dir_part not in ['999', '888']:
                try:
                    wind_dir = float(wind_dir_part)
                except ValueError:
                    wind_dir = None
        
        # Wind speed (knots)
        wind_speed = None
        if len(line) > 46:
            wind_speed_part = line[37:42].strip()
            if wind_speed_part and wind_speed_part not in ['9999', '0000']:
                try:
                    wind_speed = float(wind_speed_part)
                except ValueError:
                    wind_speed = None
        
        # Wind gust (knots)
        wind_gust = None
        if len(line) > 52:
            wind_gust_part = line[43:48].strip()
            if wind_gust_part and wind_gust_part not in ['9999', '0000']:
                try:
                    wind_gust = float(wind_gust_part)
                except ValueError:
                    wind_gust = None
        
        # Visibility (miles)
        vis_mi = None
        if len(line) > 95:
            vis_part = line[82:87].strip()
            if vis_part and vis_part != '99999':
                try:
                    vis_mi = float(vis_part) / 1000.0  # Usually in thousandths of a mile
                except ValueError:
                    vis_mi = None
        
        return {
            'station': usaf + '-' + wban if wban else usaf,
            'usaf': usaf,
            'date_utc': date_utc,
            'timestamp_utc': timestamp_utc,
            'temp_f': tmp_f,
            'dew_f': dew_f,
            'pressure_mb': slp_mb,
            'wind_dir_deg': wind_dir,
            'wind_speed_kt': wind_speed,
            'wind_gust_kt': wind_gust,
            'visibility_mi': vis_mi,
            'raw_line': line.strip(),
        }
    except Exception as e:
        print(f"  Parse error: {e}")
        return None


def get_isd_files_for_year(year, stations):
    """
    Get list of available ISD files for a year from the NCEI FTP server.
    
    ISD files are stored as: /pub/data/noaa/{year}/{station}-{year}.gz
    """
    try:
        ftp = ftplib.FTP('ftp.ncei.noaa.gov')
        ftp.login('anonymous', 'anonymous@openclaw.ai')
        ftp.cwd(f'pub/data/noaa/{year}')
        
        files = []
        ftp.retrlines('LIST', lambda x: files.append(x))
        
        # Parse filenames - format is like: stationID-YYYY.gz or just stationID.gz
        available = set()
        for f in files:
            parts = f.split()[-1] if f.split() else ''
            # Try to extract station ID from filename
            match = re.match(r'^([A-Z0-9]+)-(\d{4})?\.gz$', parts)
            if match:
                station = match.group(1)
                # Validate it's a likely station code (5-6 chars, mostly letters)
                if len(station) >= 4:
                    available.add(station)
        
        ftp.quit()
        return available
    except Exception as e:
        print(f"  FTP error listing year {year}: {e}")
        return set()


def download_isd_file(year, station_id, local_dir):
    """Download and decompress a single ISD file."""
    remote_path = f'pub/data/noaa/{year}/{station_id}-{year}.gz'
    local_file = os.path.join(local_dir, f'{station_id}_{year}.gz')
    decompressed_file = os.path.join(local_dir, f'{station_id}_{year}.txt')
    
    try:
        ftp = ftplib.FTP('ftp.ncei.noaa.gov')
        ftp.login('anonymous', 'anonymous@openclaw.ai')
        ftp.cwd(f'pub/data/noaa/{year}')
        
        # Download gzipped file
        with open(local_file, 'wb') as f:
            ftp.retrbinary(f'RETR {station_id}-{year}.gz', f.write)
        
        ftp.quit()
        
        # Decompress
        with gzip.open(local_file, 'rb') as f_in:
            with open(decompressed_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        return decompressed_file
    except ftplib.error_perm as e:
        print(f"  FTP error downloading {station_id}: {e}")
        for f in [local_file, decompressed_file]:
            if os.path.exists(f):
                os.remove(f)
        return None
    except Exception as e:
        print(f"  Error downloading {station_id}: {e}")
        for f in [local_file, decompressed_file]:
            if os.path.exists(f):
                os.remove(f)
        return None


def process_isd_file(local_file, conn, stations):
    """Process a downloaded ISD file and insert records."""
    if not os.path.exists(local_file):
        return 0
    
    cur = conn.cursor()
    records = 0
    
    try:
        with open(local_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parsed = parse_isd_line(line.rstrip('\n\r'))
                if not parsed:
                    continue
                
                # Check if station matches our filter
                station = parsed.get('usaf', parsed.get('station', ''))
                if station not in stations:
                    # Try partial match - ISD codes may differ slightly
                    matched = False
                    for s in stations:
                        if station.startswith(s[:3]):  # First 3 chars match
                            matched = True
                            break
                    if not matched:
                        continue
                
                # Check if we already have this record
                cur.execute("""
                    SELECT COUNT(*) FROM isd_raw 
                    WHERE station = ? AND timestamp_utc = ?
                """, (station, parsed['timestamp_utc']))
                
                if cur.fetchone()[0] > 0:
                    continue  # Skip duplicate
                
                # Insert record
                try:
                    cur.execute("""
                        INSERT INTO isd_raw 
                        (station, date_utc, timestamp_utc, temp_f, dew_f, pressure_mb, 
                         wind_dir_deg, wind_speed_kt, wind_gust_kt, visibility_mi, raw_line)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        station,
                        parsed['date_utc'],
                        parsed['timestamp_utc'],
                        parsed.get('temp_f'),
                        parsed.get('dew_f'),
                        parsed.get('pressure_mb'),
                        parsed.get('wind_dir_deg'),
                        parsed.get('wind_speed_kt'),
                        parsed.get('wind_gust_kt'),
                        parsed.get('visibility_mi'),
                        parsed.get('raw_line', '')[:2048],
                    ))
                    records += 1
                except sqlite3.IntegrityError:
                    continue  # Duplicate, skip
                except Exception as e:
                    print(f"    Insert error: {e}")
        
        conn.commit()
        return records
    except Exception as e:
        print(f"  Error processing {local_file}: {e}")
        return 0


def download_and_process_year(year, local_dir, conn, stations, log_conn):
    """Download and process ISD data for a year."""
    os.makedirs(local_dir, exist_ok=True)
    
    print(f"Processing year {year}...")
    print(f"  Stations: {len(stations)}")
    
    # Get available files from FTP
    available = get_isd_files_for_year(year, stations)
    print(f"  Available files on FTP: {len(available)}")
    
    # Download and process
    total_inserted = 0
    stations_processed = set()
    
    for station_id in available:
        print(f"  Processing {station_id}...")
        
        # Try to download
        local_file = download_isd_file(year, station_id, local_dir)
        
        if local_file:
            records = process_isd_file(local_file, conn, stations)
            total_inserted += records
            stations_processed.add(station_id)
            
            if records > 0:
                print(f"    Inserted {records} records")
            
            # Cleanup
            for ext in ['.gz', '.txt']:
                fpath = os.path.join(local_dir, f'{station_id}_{year}{ext}')
                if os.path.exists(fpath):
                    os.remove(fpath)
    
    # Log the download
    cur = log_conn.cursor()
    cur.execute("""
        INSERT INTO isd_backfill_log (year, stations_downloaded, records_inserted, status, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        year,
        len(stations_processed),
        total_inserted,
        'complete' if total_inserted > 0 else 'empty',
        datetime.utcnow().isoformat(),
        datetime.utcnow().isoformat(),
    ))
    log_conn.commit()
    
    print(f"  Downloaded: {len(stations_processed)} stations")
    print(f"  Records inserted: {total_inserted}")
    
    return total_inserted


def main():
    """Main entry point."""
    print("=" * 80)
    print("NOAA ISD BACKFILL - WAVE 1, STEP 1")
    print("=" * 80)
    print()
    
    # Database paths
    workspace_dir = "/home/node/.openclaw/workspace/prototypes/weather-engine-source"
    isd_db_path = os.path.join(workspace_dir, "data", "isd_raw.db")
    log_db_path = os.path.join(workspace_dir, "data", "isd_log.db")
    
    # Create databases
    print("Setting up database schemas...")
    isd_conn = sqlite3.connect(isd_db_path, timeout=60)
    isd_cur = isd_conn.cursor()
    isd_cur.executescript(ISD_SCHEMA)
    isd_conn.commit()
    
    log_conn = sqlite3.connect(log_db_path, timeout=60)
    log_cur = log_conn.cursor()
    log_cur.execute("""
        CREATE TABLE IF NOT EXISTS isd_backfill_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            stations_downloaded INTEGER,
            records_inserted INTEGER,
            status TEXT,
            started_at TEXT,
            completed_at TEXT
        )
    """)
    log_conn.commit()
    
    # Target years: 5 years back from 2026
    target_years = [2021, 2022, 2023, 2024, 2025]
    
    # Local directory for downloads
    local_dir = os.path.join(workspace_dir, "data", "isd_downloads")
    os.makedirs(local_dir, exist_ok=True)
    
    # Process each year
    for year in target_years:
        print(f"\n{'=' * 40}")
        print(f"Year {year}")
        print(f"{'=' * 40}")
        
        records = download_and_process_year(
            year, local_dir, isd_conn, ALL_KALSHI_STATIONS, log_conn
        )
        
        print(f"\nYear {year} complete: {records} records inserted")
    
    # Summary
    isd_cur.execute("SELECT COUNT(*) FROM isd_raw")
    total_records = isd_cur.fetchone()[0]
    
    # Summary by station
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total ISD records: {total_records}")
    print(f"Database: {isd_db_path}")
    print()
    
    # Per-station breakdown
    isd_cur.execute("""
        SELECT station, COUNT(*) as count 
        FROM isd_raw 
        GROUP BY station 
        ORDER BY count DESC
        LIMIT 20
    """)
    print("Top stations by record count:")
    for row in isd_cur.fetchall():
        print(f"  {row[0]}: {row[1]} records")
    
    isd_conn.close()
    log_conn.close()
    
    print()
    print("Backfill complete!")


if __name__ == "__main__":
    main()
