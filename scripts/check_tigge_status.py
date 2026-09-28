#!/usr/bin/env python3
"""
Simple script to check if TIGGE data is available for specific recent dates.
"""
import os, sys, json, time, sqlite3
from datetime import datetime, date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = os.environ.get("ECDS_KEY", "")  # GS-C5: secret-free; read from env
DB_PATH = os.path.join(ROOT, "data", "tigge_archive.db")

def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)

def main():
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    
    # Check for missing dates
    missing_dates = ['2026-09-20', '2026-09-21']
    
    log(f"Checking for missing dates: {missing_dates}")
    
    for target_date in missing_dates:
        cursor = conn.execute("SELECT COUNT(*) FROM tigge_archive WHERE target_date = ?", (target_date,))
        existing_count = cursor.fetchone()[0]
        log(f"Date {target_date}: {existing_count} records")
        
        # If date is missing, note that we may need to wait for data availability
        if existing_count == 0:
            today = datetime.now().strftime('%Y-%m-%d')
            log(f"  Date {target_date} is missing. Note: TIGGE data may not be available for dates this recent.")
            log(f"  Today is {today}, and ECMWF TIGGE forecasts typically have a lag.")

    # Check if there are other recent dates that might be needed for a 39-day window
    cursor = conn.execute('''
        SELECT target_date, COUNT(*) as record_count
        FROM tigge_archive
        GROUP BY target_date
        ORDER BY target_date DESC
        LIMIT 45
    ''')
    recent_dates = cursor.fetchall()
    
    # Check if there are any incomplete dates in the most recent 45
    incomplete_dates = []
    for date_str, count in recent_dates:
        if count < 100:  # Less than expected 20 stations * 5 steps
            incomplete_dates.append((date_str, count))
    
    if incomplete_dates:
        log(f"\\nFound {len(incomplete_dates)} incomplete dates among most recent 45:")
        for date_str, count in incomplete_dates:
            log(f"  {date_str}: {count}/100 records")
    else:
        log(f"\\nAll of the most recent 45 dates are complete.")
    
    # Verify the total count of dates
    cursor = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive")
    total_dates = cursor.fetchone()[0]
    log(f"\\nTotal unique dates in database: {total_dates}")
    
    conn.close()
    log("\\nDatabase verification complete.")

if __name__ == "__main__":
    main()