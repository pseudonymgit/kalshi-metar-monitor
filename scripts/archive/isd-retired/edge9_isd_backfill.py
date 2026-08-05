#!/usr/bin/env python3
"""
EDGE 9: NOAA ISD Backfill (US stations)

Downloads ISD Lite data from NOAA for 21 Kalshi cities.
5 years (2020-2024). Uses curl for downloads with proper timeouts.

Standalone Python. No AI in the loop.
"""

import sqlite3
import os
import sys
import subprocess
import gzip
import io
from datetime import datetime

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/isd_lite_raw.db"
YEARS = [2020, 2021, 2022, 2023, 2024]
BASE_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-lite"

ISD_MAP = {
    'KATL': ('722190', '13874'), 'KAUS': ('722530', '13958'),
    'KBOS': ('725090', '14739'), 'KDAL': ('722590', '03927'),
    'KDCA': ('724050', '13743'), 'KDEN': ('725650', '03017'),
    'KDFW': ('722590', '03928'), 'KHOU': ('722440', '12918'),
    'KLAS': ('723860', '23169'), 'KLAX': ('722950', '23174'),
    'KMDW': ('725340', '14819'), 'KMIA': ('722020', '12839'),
    'KMSP': ('726580', '14922'), 'KMSY': ('722310', '12916'),
    'KNYC': ('725030', '14732'), 'KOKC': ('723530', '13967'),
    'KPHL': ('724080', '13739'), 'KPHX': ('722780', '23183'),
    'KSAT': ('722530', '12921'), 'KSEA': ('727930', '24233'),
    'KSFO': ('724940', '23234'),
}


def download_isd_lite_curl(usaf, wban, year):
    """Download ISD Lite file using curl. Returns text content or None."""
    filename = f"{usaf}-{wban}-{year}.gz"
    url = f"{BASE_URL}/{year}/{filename}"
    tmpfile = f"/tmp/isd_{usaf}_{wban}_{year}.gz"
    
    try:
        result = subprocess.run(
            ['curl', '-sL', '--connect-timeout', '15', '--max-time', '120', '-o', tmpfile, url],
            capture_output=True, timeout=130
        )
        
        if result.returncode != 0:
            return None
        
        if not os.path.exists(tmpfile) or os.path.getsize(tmpfile) < 100:
            return None
        
        with open(tmpfile, 'rb') as f:
            data = f.read()
        
        os.unlink(tmpfile)
        
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            text = f.read().decode('ascii', errors='replace')
        
        return text.splitlines()
    except Exception:
        if os.path.exists(tmpfile):
            os.unlink(tmpfile)
        return None


def parse_isd_lite_line(line, station, usaf, wban):
    """Parse ISD Lite CSV line."""
    parts = line.strip().split()
    if len(parts) < 9:
        return None
    
    try:
        year, month, day, hour = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        
        temp_c = int(parts[4]) / 10.0 if parts[4] != '-9999' else None
        dew_c = int(parts[5]) / 10.0 if parts[5] != '-9999' else None
        slp = int(parts[6]) / 10.0 if parts[6] != '-9999' else None
        wind_dir = int(parts[7]) if parts[7] != '-9999' else None
        wind_spd = int(parts[8]) / 10.0 if parts[8] != '-9999' else None
        
        temp_f = (temp_c * 9/5 + 32) if temp_c is not None else None
        dew_f = (dew_c * 9/5 + 32) if dew_c is not None else None
        wind_kt = wind_spd * 1.94384 if wind_spd is not None else None
        
        date_utc = f"{year:04d}-{month:02d}-{day:02d}"
        timestamp_utc = f"{date_utc} {hour:02d}:00:00"
        
        return (station, usaf, wban, date_utc, timestamp_utc,
                temp_f, temp_c, dew_f, dew_c, wind_dir, wind_kt, None,
                slp, slp, None, None, line.strip(), datetime.utcnow().isoformat())
    except (ValueError, IndexError):
        return None


def main():
    print("=" * 90)
    print("EDGE 9: NOAA ISD BACKFILL (US STATIONS)")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Years: {YEARS}")
    print(f"Stations: {len(ISD_MAP)}")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cur = conn.cursor()
    
    # Schema already exists, check for data
    existing = cur.execute("SELECT COUNT(*) FROM isd_lite_raw").fetchone()[0]
    print(f"Existing ISD records: {existing}")
    
    if existing > 0:
        print("ISD data already exists. Skipping backfill.")
        for station in sorted(ISD_MAP.keys()):
            count = cur.execute("SELECT COUNT(*) FROM isd_lite_raw WHERE station=?", (station,)).fetchone()[0]
            if count > 0:
                dmin = cur.execute("SELECT MIN(date_utc), MAX(date_utc) FROM isd_lite_raw WHERE station=?", (station,)).fetchone()
                print(f"  {station}: {count} records ({dmin[0]} to {dmin[1]})")
        conn.close()
        return
    
    total_records = 0
    total_stations = 0
    
    for year in YEARS:
        print(f"\n── Year {year} ──")
        year_records = 0
        year_stations = 0
        
        for station, (usaf, wban) in sorted(ISD_MAP.items()):
            sys.stdout.write(f"  {station} ({usaf}-{wban})... ")
            sys.stdout.flush()
            
            lines = download_isd_lite_curl(usaf, wban, year)
            
            if lines is None:
                print("NOT FOUND")
                continue
            
            rows = []
            for line in lines:
                parsed = parse_isd_lite_line(line, station, usaf, wban)
                if parsed:
                    rows.append(parsed)
            
            if not rows:
                print("NO VALID DATA")
                continue
            
            # Insert in batches
            batch_size = 5000
            inserted = 0
            for i in range(0, len(rows), batch_size):
                cur.executemany("""
                    INSERT INTO isd_lite_raw 
                    (station, usaf, wban, date_utc, timestamp_utc, temp_f, temp_c, dew_f, dew_c,
                     wind_dir_deg, wind_speed_kt, wind_gust_kt, pressure_mb, sea_level_pressure_mb,
                     visibility_mi, ceilling_ft, raw_line, created_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows[i:i+batch_size])
                inserted += len(rows[i:i+batch_size])
            conn.commit()
            
            year_records += inserted
            year_stations += 1
            total_records += inserted
            total_stations += 1
            print(f"{inserted:>8} records")
        
        print(f"  Year {year}: {year_records} records from {year_stations} stations")
    
    # Final summary
    print()
    print("=" * 90)
    print("ISD BACKFILL COMPLETE")
    print("=" * 90)
    
    final_count = cur.execute("SELECT COUNT(*) FROM isd_lite_raw").fetchone()[0]
    print(f"\n  Total records: {final_count}")
    
    print(f"\n  {'Station':<8} {'Records':>10} {'Date Range':>25}")
    print("  " + "-" * 45)
    
    for station in sorted(ISD_MAP.keys()):
        count = cur.execute("SELECT COUNT(*) FROM isd_lite_raw WHERE station=?", (station,)).fetchone()[0]
        if count > 0:
            drange = cur.execute("SELECT MIN(date_utc), MAX(date_utc) FROM isd_lite_raw WHERE station=?", (station,)).fetchone()
            print(f"  {station:<8} {count:>10} {drange[0]} to {drange[1]}")
        else:
            print(f"  {station:<8} {0:>10} NO DATA")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 9 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
