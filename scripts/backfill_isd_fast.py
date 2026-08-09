#!/usr/bin/env python3
"""
WAVE 1, STEP 1: Fast NOAA ISD-Lite Backfill

Batch download and parse ISD-Lite data.
"""

import os
import sqlite3
import ftplib
import gzip
import io
from datetime import datetime
import threading
from queue import Queue
import time

# Kalshi stations (USAF codes)
ALL_KALSHI_USAF_CODES = {
    '010010', '012530', '012570', '014720',
    '012980', '014830', '016750', '017220',
    '017590', '017880', '017910', '018030',
    '018550', '018670', '018760', '019210',
    '019750', '019840', '020070',
}

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

def parse_isd_lite_line(line):
    """Parse ISD-Lite format line."""
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
        
        temp_c = None
        temp_f = None
        if parts[4] not in ['-9999', '-99999']:
            try:
                temp_c = float(parts[4]) / 10.0
                temp_f = temp_c * 9/5 + 32
            except ValueError:
                pass
        
        dew_c = None
        dew_f = None
        if parts[5] not in ['-9999', '-99999']:
            try:
                dew_c = float(parts[5]) / 10.0
                dew_f = dew_c * 9/5 + 32
            except ValueError:
                pass
        
        slp_mb = None
        if parts[6] not in ['-9999', '-99999']:
            try:
                slp_mb = float(parts[6])
            except ValueError:
                pass
        
        wind_dir = None
        if parts[7] not in ['-9999', '-99999']:
            try:
                wind_dir = float(parts[7])
            except ValueError:
                pass
        
        wind_speed = None
        if len(parts) > 8 and parts[8] not in ['-9999', '-99999']:
            try:
                wind_speed = float(parts[8])
            except ValueError:
                pass
        
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
    except Exception:
        return None


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


def process_file(ftp_conn, usaf_code, year, local_dir, conn, lock):
    """Process a single ISD-Lite file."""
    local_file = os.path.join(local_dir, f'{usaf_code}_{year}.gz')
    
    try:
        # Download
        ftp_conn.cwd(f'pub/data/noaa/isd-lite/{year}')
        with open(local_file, 'wb') as f:
            ftp_conn.retrbinary(f'RETR {usaf_code}-99999-{year}.gz', f.write)
        
        # Parse
        records = 0
        with gzip.open(local_file, 'rt', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parsed = parse_isd_lite_line(line)
                if not parsed:
                    continue
                
                with lock:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT COUNT(*) FROM isd_lite_raw 
                        WHERE usaf = ? AND timestamp_utc = ?
                    """, (usaf_code, parsed['timestamp_utc']))
                    
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
        
        conn.commit()
        return records, True
    except Exception as e:
        print(f"  Error processing {usaf_code}: {e}")
        return 0, False
    finally:
        if os.path.exists(local_file):
            os.remove(local_file)


def main():
    print("=" * 80)
    print("NOAA ISD-LITE BACKFILL - FAST VERSION")
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
        
        # Filter to Kalshi stations
        kalshi_files = []
        for f in files:
            for code in ALL_KALSHI_USAF_CODES:
                if f.startswith(f"{code}-99999-{year}.gz"):
                    kalshi_files.append(code)
                    break
        
        print(f"  Kalshi station files: {len(kalshi_files)}")
        
        # Download and process
        total_records = 0
        stations_processed = set()
        
        # Create FTP connection for each thread
        for usaf_code in kalshi_files:
            print(f"  Processing {usaf_code}...")
            
            # Create FTP connection
            ftp = ftplib.FTP('ftp.ncei.noaa.gov')
            ftp.login('anonymous', 'anonymous@openclaw.ai')
            
            records, success = process_file(ftp, usaf_code, year, local_dir, conn, threading.Lock())
            
            ftp.quit()
            
            if success:
                total_records += records
                stations_processed.add(usaf_code)
                print(f"    Inserted {records} records")
        
        # Log
        log_cur.execute("""
            INSERT INTO isd_backfill_log (year, stations_downloaded, records_inserted, status, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            year,
            len(stations_processed),
            total_records,
            'complete' if total_records > 0 else 'empty',
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ))
        log_conn.commit()
        
        print(f"  Downloaded: {len(stations_processed)} stations")
        print(f"  Records inserted: {total_records}")
    
    # Summary
    cur.execute("SELECT COUNT(*) FROM isd_lite_raw")
    total_records = cur.fetchone()[0]
    
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total ISD-Lite records: {total_records}")
    print(f"Database: {isd_db_path}")
    
    cur.execute("""
        SELECT usaf, COUNT(*) as count 
        FROM isd_lite_raw 
        GROUP BY usaf 
        ORDER BY count DESC
    """)
    print("\nTop stations by record count:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]} records")
    
    conn.close()
    log_conn.close()
    print("\nBackfill complete!")


if __name__ == "__main__":
    main()
