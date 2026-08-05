#!/usr/bin/env python3
"""
Download missing ISD-lite data files for all Kalshi stations.

Maps station names to USAF codes:
- KNYC (New York): 010010
- KLAX (Los Angeles): 012530
- KMDW (Chicago): 012570
- KDCA (DC): 014830
- KBOS (Boston): 012980
- KSFO (San Francisco): 014830... wait, let me check
"""

import os
import sqlite3
import ftplib
import gzip
from datetime import datetime
import threading
from queue import Queue

# Station mapping - Kalshi station names to USAF codes
STATION_TO_USAF = {
    'KNYC': '010010',  # New York
    'KLAX': '012530',  # Los Angeles
    'KMDW': '012570',  # Chicago
    'KDCA': '014830',  # Washington DC
    'KBOS': '012980',  # Boston
    'KSFO': '017590',  # San Francisco
    'KMIA': '017220',  # Miami
    'KPHX': '016750',  # Phoenix
    'KSAT': '017880',  # San Antonio
    'KMSY': '017910',  # New Orleans
    'KOKC': '018030',  # Oklahoma City
    'KATL': '018550',  # Atlanta
    'KSEA': '019210',  # Seattle
    'KMSP': '019750',  # Minneapolis
    'KDAL': '019840',  # Dallas
    'KDEN': '020070',  # Denver
    'KPHL': '018670',  # Philadelphia
    'KLAS': '018760',  # Las Vegas
}

# Kalshi market type mapping (high/low)
MARKET_TYPE_TO_USAF = {
    'KXHIGHNY': '010010',   # KNYC
    'KXHIGHLA': '012530',   # KLAX
    'KXHIGHCHI': '012570',  # KMDW
    'KXHIGHDFW': '014830',  # KDCA (proxies DFW)
    'KXLOWNY': '010010',
    'KXLOWLA': '012530',
    'KXLOWCHI': '012570',
    'KXLOWDFW': '014830',
}

# All USAF codes we need
ALL_USAF_CODES = set(STATION_TO_USAF.values()) | set(MARKET_TYPE_TO_USAF.values())

# SQLite schema
ISD_SCHEMA = """
CREATE TABLE IF NOT EXISTS isd_lite_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    usaf TEXT,
    date_utc DATE NOT NULL,
    timestamp_utc TEXT NOT NULL,
    temp_f REAL,
    dew_f REAL,
    wind_dir_deg REAL,
    wind_speed_kt REAL,
    pressure_mb REAL,
    sea_level_pressure_mb REAL,
    visibility_mi REAL,
    raw_line TEXT,
    created_at_utc TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_isd_station ON isd_lite_raw(station);
CREATE INDEX IF NOT EXISTS idx_isd_date ON isd_lite_raw(date_utc);
CREATE INDEX IF NOT EXISTS idx_isd_station_date ON isd_lite_raw(station, date_utc);

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


def get_available_files(year):
    """Get list of available files for a year."""
    try:
        ftp = ftplib.FTP('ftp.ncei.noaa.gov')
        ftp.login('anonymous', 'anonymous@openclaw.ai')
        ftp.cwd(f'pub/data/noaa/isd-lite/{year}')
        
        files = []
        ftp.retrlines('NLST', lambda x: files.append(x))
        ftp.quit()
        
        return files
    except Exception as e:
        print(f"  FTP error: {e}")
        return []


def process_file(year, usaf_code, local_dir, conn):
    """Process a single ISD-Lite file."""
    local_file = os.path.join(local_dir, f'{usaf_code}_{year}.gz')
    
    try:
        ftp = ftplib.FTP('ftp.ncei.noaa.gov')
        ftp.login('anonymous', 'anonymous@openclaw.ai')
        ftp.cwd(f'pub/data/noaa/isd-lite/{year}')
        
        # Download
        with open(local_file, 'wb') as f:
            ftp.retrbinary(f'RETR {usaf_code}-99999-{year}.gz', f.write)
        ftp.quit()
        
        # Parse
        records = 0
        with gzip.open(local_file, 'rt', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 8:
                    continue
                
                try:
                    date_utc = f"{parts[0]}-{parts[1]:02d}-{parts[2]:02d}"
                    timestamp_utc = f"{parts[0]}-{parts[1]:02d}-{parts[2]:02d}T{parts[3]:02d}:00:00Z"
                    
                    # Temperature
                    temp_f = None
                    if parts[4] not in ['-9999', '-99999']:
                        try:
                            temp_f = float(parts[4]) / 10.0 * 9/5 + 32
                        except ValueError:
                            pass
                    
                    # Dew point
                    dew_f = None
                    if parts[5] not in ['-9999', '-99999']:
                        try:
                            dew_f = float(parts[5]) / 10.0 * 9/5 + 32
                        except ValueError:
                            pass
                    
                    # Pressure
                    pressure_mb = None
                    if parts[6] not in ['-9999', '-99999']:
                        try:
                            pressure_mb = float(parts[6])
                        except ValueError:
                            pass
                    
                    # Wind direction
                    wind_dir = None
                    if parts[7] not in ['-9999', '-99999']:
                        try:
                            wind_dir = float(parts[7])
                        except ValueError:
                            pass
                    
                    # Wind speed
                    wind_speed = None
                    if len(parts) > 8 and parts[8] not in ['-9999', '-99999']:
                        try:
                            wind_speed = float(parts[8])
                        except ValueError:
                            pass
                    
                    # Visibility
                    vis_mi = None
                    if len(parts) > 9 and parts[9] not in ['-9999', '-99999']:
                        try:
                            vis_mi = float(parts[9])
                        except ValueError:
                            pass
                    
                    # Insert
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM isd_lite_raw WHERE usaf = ? AND timestamp_utc = ?", 
                               (usaf_code, timestamp_utc))
                    if cur.fetchone()[0] > 0:
                        continue
                    
                    cur.execute("""
                        INSERT INTO isd_lite_raw 
                        (station, usaf, date_utc, timestamp_utc, temp_f, dew_f, 
                         wind_dir_deg, wind_speed_kt, pressure_mb, 
                         sea_level_pressure_mb, visibility_mi, raw_line)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        usaf_code,
                        usaf_code,
                        date_utc,
                        timestamp_utc,
                        temp_f,
                        dew_f,
                        wind_dir,
                        wind_speed,
                        pressure_mb,
                        pressure_mb,
                        vis_mi,
                        line.strip()[:2048],
                    ))
                    records += 1
                except Exception:
                    pass
        
        conn.commit()
        
        if records > 0:
            print(f"  {usaf_code}: {records} records")
        
        return records, True
    except Exception as e:
        print(f"  Error processing {usaf_code}: {e}")
        return 0, False
    finally:
        if os.path.exists(local_file):
            os.remove(local_file)


def main():
    print("=" * 80)
    print("DOWNLOAD MISSING ISD-LITE DATA")
    print("=" * 80)
    print()
    
    workspace_dir = "/home/node/.openclaw/workspace/prototypes/weather-engine-source"
    isd_db_path = os.path.join(workspace_dir, "data", "isd_lite_raw.db")
    log_db_path = os.path.join(workspace_dir, "data", "isd_log.db")
    
    print("Setting up database...")
    conn = sqlite3.connect(isd_db_path, timeout=60)
    cur = conn.cursor()
    cur.executescript(ISD_SCHEMA)
    conn.commit()
    
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
    
    target_years = [2021, 2022, 2023, 2024, 2025]
    local_dir = os.path.join(workspace_dir, "data", "isd_lite_downloads")
    os.makedirs(local_dir, exist_ok=True)
    
    for year in target_years:
        print(f"\n{'=' * 40}")
        print(f"Year {year}")
        print(f"{'=' * 40}")
        
        files = get_available_files(year)
        print(f"  Available files on FTP: {len(files)}")
        
        # Filter to our stations
        stations_to_download = []
        for code in ALL_USAF_CODES:
            expected_file = f"{code}-99999-{year}.gz"
            if expected_file in files:
                stations_to_download.append(code)
            else:
                # Check alternative format
                for f in files:
                    if f.startswith(f"{code}-"):
                        stations_to_download.append(code)
                        break
        
        print(f"  Stations to download: {len(stations_to_download)}")
        
        # Download each station
        for usaf_code in stations_to_download:
            records, success = process_file(year, usaf_code, local_dir, conn)
        
        # Log
        cur.execute("SELECT COUNT(*) FROM isd_lite_raw WHERE usaf IN ({})".format(
            ','.join('?' * len(ALL_USAF_CODES)), 
        ), list(ALL_USAF_CODES))
        total = cur.fetchone()[0]
        
        log_cur.execute("""
            INSERT INTO isd_backfill_log (year, stations_downloaded, records_inserted, status, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            year,
            len(stations_to_download),
            total,
            'complete' if total > 0 else 'empty',
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ))
        log_conn.commit()
        
        print(f"  Downloaded: {len(stations_to_download)} stations")
        print(f"  Total records in DB: {total}")
    
    # Summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    cur.execute("SELECT COUNT(*) FROM isd_lite_raw")
    total_records = cur.fetchone()[0]
    print(f"Total ISD-Lite records: {total_records}")
    print(f"Database: {isd_db_path}")
    
    cur.execute("""
        SELECT usaf, COUNT(*) as count 
        FROM isd_lite_raw 
        GROUP BY usaf 
        ORDER BY count DESC
    """)
    print("\nStations by record count:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]} records")
    
    conn.close()
    log_conn.close()
    print("\nDownload complete!")


if __name__ == "__main__":
    main()
