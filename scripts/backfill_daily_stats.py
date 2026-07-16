#!/usr/bin/env python3
"""
Script to backfill missing daily_stats entries by aggregating data from metar_observations
where daily stats are missing for dates present in NWP data.
"""
import sqlite3
from datetime import datetime
import sys


def backfill_daily_stats():
    # Connect to both DBs
    metar_conn = sqlite3.connect('data/metar_backfill.db')
    nwp_conn = sqlite3.connect('data/nwp_forecasts.db')
    
    metar_cursor = metar_conn.cursor()
    nwp_cursor = nwp_conn.cursor()
    
    # Get the NWP date range
    nwp_cursor.execute('SELECT MIN(target_date), MAX(target_date) FROM nwp_forecasts')
    nwp_min_date, nwp_max_date = nwp_cursor.fetchone()
    print(f"NWP date range: {nwp_min_date} to {nwp_max_date}")
    
    # Get stations that appear in NWP data
    nwp_cursor.execute('SELECT DISTINCT station FROM nwp_forecasts ORDER BY station')
    nwp_stations = [row[0] for row in nwp_cursor.fetchall()]
    print(f"NWP stations: {nwp_stations}")
    
    # Find NWP forecast dates that are missing from daily_stats
    print("Finding missing daily_stats entries...")
    missing_entries = []
    
    for station in nwp_stations:
        # Get dates from NWP forecasts for this station
        nwp_cursor.execute('''SELECT DISTINCT target_date FROM nwp_forecasts 
                              WHERE station = ? AND target_date BETWEEN ? AND ? 
                              ORDER BY target_date''', (station, nwp_min_date, nwp_max_date))
        nwp_dates = [row[0] for row in nwp_cursor.fetchall()]
        
        # Get dates that already exist in daily_stats
        metar_cursor.execute('''SELECT DISTINCT date_utc FROM daily_stats 
                                WHERE station = ? AND date_utc BETWEEN ? AND ? 
                                ORDER BY date_utc''', (station, nwp_min_date, nwp_max_date))
        existing_dates = [row[0] for row in metar_cursor.fetchall()]
        
        # Find missing dates
        missing_dates = set(nwp_dates) - set(existing_dates)
        
        for date in missing_dates:
            missing_entries.append((station, date))
    
    print(f"Total missing entries: {len(missing_entries)}")
    
    # Process in batches to prevent memory issues
    batch_size = 1000
    total_backfilled = 0
    
    for i in range(0, len(missing_entries), batch_size):
        batch = missing_entries[i:i + batch_size]
        print(f"Processing batch {i//batch_size + 1}: {len(batch)} missing entries")
        
        for station, date in batch:
            # Check metar_observations for data on this date for this station
            metar_cursor.execute('''
                SELECT MIN(temp_f), MAX(temp_f), AVG(temp_f), COUNT(*),
                       MIN(timestamp_utc), MAX(timestamp_utc)
                FROM metar_observations 
                WHERE station = ? AND date_utc = ?
            ''', (station, date))
            
            min_temp, max_temp, avg_temp, obs_count, first_time, last_time = metar_cursor.fetchone()
            
            if obs_count > 0 and min_temp is not None and max_temp is not None:
                # Insert the computed daily stats
                metar_cursor.execute('''
                    INSERT OR IGNORE INTO daily_stats 
                    (station, date_local, date_utc, max_temp_f, min_temp_f, avg_temp_f, observation_count,
                     first_observation_utc, last_observation_utc, ingestion_timestamp_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    station,  # station
                    date,     # date_local - using UTC date for local too (simplification)
                    date,     # date_utc
                    max_temp, # max_temp_f
                    min_temp, # min_temp_f
                    avg_temp, # avg_temp_f
                    obs_count, # observation_count
                    first_time, # first_observation_utc
                    last_time   # last_observation_utc
                ))
                total_backfilled += 1
        
        # Commit after each batch
        metar_conn.commit()
        print(f"  Batch committed. Backfilled {total_backfilled} total entries so far.")
    
    print(f"\nBackfill completed! Total entries backfilled: {total_backfilled}")
    print(f"Original missing: {len(missing_entries)}")
    
    # Close connections
    metar_conn.close()
    nwp_conn.close()


if __name__ == '__main__':
    backfill_daily_stats()