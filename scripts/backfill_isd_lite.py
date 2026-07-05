#!/usr/bin/env python3
"""
WAVE 1, STEP 1: NOAA ISD-Lite Backfill

Download 5 years of ISD-Lite data from NOAA FTP, parse simplified format,
store in SQLite with proper indexing.

ISD-Lite format is simpler than full ISD - it's CSV-like with fixed columns.

Target: 5 years of data covering all Kalshi cities.
"""

import os
import re
import sqlite3
import ftplib
from datetime import datetime
import sys
import gzip

# ISD-Lite FTP path
ISD_LITE_FTP_BASE = "ftp://ftp.ncei.noaa.gov/pub/data/noaa/isd-lite/"

# Kalshi stations (all USAF codes for Kalshi markets)
# KXHIGHNY (KNYC=010010), KXHIGHLA (KLAX=012530), KXHIGHCHI (KMDW=012570), KXHIGHDFW (KDFW=014720)
# Plus other stations
ALL_KALSHI_USAF_CODES = {
    '010010', '012530', '012570', '014720',  # KXHIGH markets
    '012980', '014830', '016750', '017220',   # KBOS, KSFO, KMIA, KPHX
    '017590', '017880', '017910', '018030',   # KSAT, KMSY, KOKC, KATL
    '018550', '018670', '018760', '019210',   # KSEA, KMSP, KDAL, KDEN
    '019750', '019840', '020070',             # KPHL, KLAS, KDTW (for testing)
}


# SQLite schema
ISD_SCHEMA = """
CREATE TABLE IF NOT EXISTS isd_lite_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    usaf TEXT,
    wban TEXT,
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
    ceilling_ft REAL,
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


def parse_isd_lite_line(line):
    """
    Parse ISD-Lite format line.
    
    ISD-Lite format is space-separated:
    YYYY MM DD HH [TEMP] [DEW] [WIND_DIR] [WIND_SPEED] [SLP] [VIS]
    
    Example: 2024 01 01 00   -70  -130 10208   318    61 -9999 -9999 -9999
    """
    line = line.strip()
    if not line:
        return None
    
    parts = line.split()
    if len(parts) < 8:
        return None
    
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        
        dt = datetime(year, month, day, hour, 0)
        date_utc = dt.strftime("%Y-%m-%d")
        timestamp_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Temperature (tenths of degrees C)
        temp_c = None
        temp_f = None
        if parts[4] not in ['-9999', '-99999']:
            try:
                temp_c = float(parts[4]) / 10.0
                temp_f = temp_c * 9/5 + 32
            except ValueError:
                pass
        
        # Dew point (tenths of degrees C)
        dew_c = None
        dew_f = None
        if parts[5] not in ['-9999', '-99999']:
            try:
                dew_c = float(parts[5]) / 10.0
                dew_f = dew_c * 9/5 + 32
            except ValueError:
                pass
        
        # Sea level pressure (millibars, already in proper units)
        slp_mb = None
        if parts[6] not in ['-9999', '-99999']:
            try:
                slp_mb = float(parts[6])
            except ValueError:
                pass
        
        # Wind direction (degrees)
        wind_dir = None
        if parts[7] not in ['-9999', '-99999']:
            try:
                wind_dir = float(parts[7])
            except ValueError:
                pass
        
        # Wind speed (knots)
        wind_speed = None
        if len(parts) > 8 and parts[8] not in ['-9999', '-99999']:
            try:
                wind_speed = float(parts[8])
            except ValueError:
                pass
        
        # Visibility (miles)
        vis_mi = None
        if len(parts) > 9 and parts[9] not in ['-9999', '-99999']:
            try:
                vis_mi = float(parts[9])
            except ValueError:
                pass
        
        return {
            'date_utc': date_utc,
            'timestamp_utc': timestamp_utc,
            'temp_f': temp_f,
            'dew_f': dew_f,
            'wind_dir_deg': wind_dir,
            'wind_speed_kt': wind_speed,
            'pressure_mb': slp_mb,
            'sea_level_pressure_mb': slp_mb,
            'visibility_mi': vis_mi,
        }
    except Exception as e:
        return None


def get_isd_lite_files_for_year(year):
    """Get list of available ISD-Lite files for a year."""
    try:
        ftp = ftplib.FTP('ftp.ncei.noaa.gov')
        ftp.login('anonymous', 'anonymous@openclaw.ai')
        ftp.cwd(f'pub/data/noaa/isd-lite/{year}')
        
        files = []
        ftp.retrlines('NLST', lambda x: files.append(x))
        
        # Parse filenames - format: USAF-WBAN-YEAR.gz
        available = set()
        for f in files:
            match = re.match(r'^(\d{6})-(\d{5})-(\d{4})\.gz$', f)
            if match:
                usaf = match.group(1)
                if len(usaf) >= 4:
                    available.add(usaf)
        
        ftp.quit()
        return available
    except Exception as e:
        print(f"  FTP error: {e}")
        return set()


def download_and_process_year(year, local_dir, conn, log_conn):
    """Download and process ISD-Lite data for a year."""
    os.makedirs(local_dir, exist_ok=True)
    
    print(f"Processing year {year}...")
    
    # Get available files from FTP
    available = get_isd_lite_files_for_year(year)
    print(f"  Available files on FTP: {len(available)}")
    
    total_inserted = 0
    stations_processed = set()
    
    for usaf_code in available:
        # Check if this is a Kalshi station
        if usaf_code not in ALL_KALSHI_USAF_CODES:
            continue
        
        print(f"  Processing {usaf_code}...")
        
        # Download file
        local_file = os.path.join(local_dir, f'{usaf_code}_{year}.gz')
        
        try:
            ftp = ftplib.FTP('ftp.ncei.noaa.gov')
            ftp.login('anonymous', 'anonymous@openclaw.ai')
            ftp.cwd(f'pub/data/noaa/isd-lite/{year}')
            
            with open(local_file, 'wb') as f:
                ftp.retrbinary(f'RETR {usaf_code}-99999-{year}.gz', f.write)
            
            ftp.quit()
            
            # Process the file
            records = 0
            with gzip.open(local_file, 'rt', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    parsed = parse_isd_lite_line(line)
                    if not parsed:
                        continue
                    
                    # Check for duplicate
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT COUNT(*) FROM isd_lite_raw 
                        WHERE usaf = ? AND timestamp_utc = ?
                    """, (usaf_code, parsed['timestamp_utc']))
                    
                    if cur.fetchone()[0] > 0:
                        continue
                    
                    # Insert record
                    try:
                        cur.execute("""
                            INSERT INTO isd_lite_raw 
                            (station, usaf, date_utc, timestamp_utc, temp_f, dew_f, 
                             wind_dir_deg, wind_speed_kt, pressure_mb, 
                             sea_level_pressure_mb, visibility_mi, raw_line)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            usaf_code,
                            usaf_code,
                            parsed['date_utc'],
                            parsed['timestamp_utc'],
                            parsed.get('temp_f'),
                            parsed.get('dew_f'),
                            parsed.get('wind_dir_deg'),
                            parsed.get('wind_speed_kt'),
                            parsed.get('pressure_mb'),
                            parsed.get('sea_level_pressure_mb'),
                            parsed.get('visibility_mi'),
                            line.strip()[:2048],
                        ))
                        records += 1
                    except sqlite3.IntegrityError:
                        continue
                    except Exception as e:
                        pass
            
            conn.commit()
            total_inserted += records
            stations_processed.add(usaf_code)
            
            if records > 0:
                print(f"    Inserted {records} records")
            
            # Cleanup
            if os.path.exists(local_file):
                os.remove(local_file)
                
        except Exception as e:
            print(f"  Error: {e}")
            if os.path.exists(local_file):
                os.remove(local_file)
    
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
    print("NOAA ISD-LITE BACKFILL - WAVE 1, STEP 1")
    print("=" * 80)
    print()
    
    workspace_dir = "/home/node/.openclaw/workspace/prototypes/weather-engine-source"
    isd_db_path = os.path.join(workspace_dir, "data", "isd_lite_raw.db")
    log_db_path = os.path.join(workspace_dir, "data", "isd_log.db")
    
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
    
    target_years = [2021, 2022, 2023, 2024, 2025]
    local_dir = os.path.join(workspace_dir, "data", "isd_lite_downloads")
    os.makedirs(local_dir, exist_ok=True)
    
    for year in target_years:
        print(f"\n{'=' * 40}")
        print(f"Year {year}")
        print(f"{'=' * 40}")
        
        records = download_and_process_year(year, local_dir, isd_conn, log_conn)
        print(f"\nYear {year} complete: {records} records inserted")
    
    isd_cur.execute("SELECT COUNT(*) FROM isd_lite_raw")
    total_records = isd_cur.fetchone()[0]
    
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total ISD-Lite records: {total_records}")
    print(f"Database: {isd_db_path}")
    
    isd_cur.execute("""
        SELECT usaf, COUNT(*) as count 
        FROM isd_lite_raw 
        GROUP BY usaf 
        ORDER BY count DESC
    """)
    print("\nTop stations by record count:")
    for row in isd_cur.fetchall():
        print(f"  {row[0]}: {row[1]} records")
    
    isd_conn.close()
    log_conn.close()
    print("\nBackfill complete!")


if __name__ == "__main__":
    main()
