#!/usr/bin/env python3
"""
Generate settlement epochs from METAR backfill data.

Takes the daily_stats table (per-station daily HIGH/LOW buckets) and creates
settlement_epochs with proper ground truth for backtesting:
- settlement_bucket: daily max temp bucket for HIGH market
- prior_settlement_bucket: previous day's max temp bucket
- reversion_occurred: 1 if current day max > prior day max, else 0
- terminal_state_reached: 1 if epoch completed, 0 if ongoing
"""

import sqlite3
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import math

# Add core module to path
script_dir = os.path.dirname(os.path.abspath(__file__))
workspace_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)


def get_temp_bucket(temp_f: float, market_type: str) -> int:
    """Convert temperature in Fahrenheit to a bucket integer."""
    # Round to nearest whole degree, then bucket (0-5, 6-10, 11-15, etc.)
    rounded = int(round(temp_f))
    if market_type == "HIGH":
        # For HIGH market: bucket = rounded temp
        return rounded
    else:
        # For LOW market: bucket = rounded temp
        return rounded


def generate_epochs_for_station(
    conn: sqlite3.Connection, 
    station: str,
    market_type: str,
    min_epoch_count: int = 30
) -> List[Dict[str, Any]]:
    """Generate settlement epochs for a single station/market_type."""
    cursor = conn.cursor()
    
    # Get daily stats for this station
    cursor.execute("""
        SELECT id, station, date_local, date_utc, 
               max_temp_f, min_temp_f, avg_temp_f, 
               observation_count, ingestion_timestamp_utc
        FROM daily_stats 
        WHERE station = ?
        ORDER BY date_local ASC
    """, (station,))
    
    daily_rows = cursor.fetchall()
    
    if len(daily_rows) < min_epoch_count:
        print(f"  {station}: Only {len(daily_rows)} days of data (need {min_epoch_count})")
        return []
    
    epochs = []
    
    for i, row in enumerate(daily_rows):
        epoch_id = row[0]
        station = row[1]
        date_local = row[2]
        date_utc = row[3]
        max_temp_f = row[4]
        min_temp_f = row[5]
        avg_temp_f = row[6]
        obs_count = row[7]
        ingestion_ts = row[8]
        
        # Determine bucket based on market type
        if market_type == "HIGH":
            current_bucket = get_temp_bucket(max_temp_f, "HIGH")
        else:
            current_bucket = get_temp_bucket(min_temp_f, "LOW")
        
        # Get prior bucket (previous day's max for HIGH, min for LOW)
        prior_bucket = None
        if i > 0:
            prior_row = daily_rows[i - 1]
            if market_type == "HIGH":
                prior_bucket = get_temp_bucket(prior_row[4], "HIGH")
            else:
                prior_bucket = get_temp_bucket(prior_row[5], "LOW")
        
        # Determine reversion: 1 if current > prior, 0 otherwise
        reversion_occurred = 1 if (prior_bucket is not None and current_bucket > prior_bucket) else 0
        
        # Terminal state: mark as closed (completed day)
        terminal_state_reached = 1
        
        epoch = {
            'station': station,
            'market_type': market_type,
            'local_trading_date': date_local,
            'date_utc': date_utc,
            'settlement_bucket': current_bucket,
            'prior_settlement_bucket': prior_bucket,
            'settlement_timestamp_utc': ingestion_ts,
            'epoch_status': 'closed',
            'epoch_close_reason': 'daily_completion',
            'epoch_close_timestamp_utc': ingestion_ts,
            'reversion_occurred': reversion_occurred,
            'first_reversion_timestamp_utc': ingestion_ts if reversion_occurred else None,
            'max_excursion_above_settlement': 0.0,
            'duration_at_or_above_settlement_seconds': 86400,  # 24 hours
            'duration_strictly_above_settlement_seconds': 0,
            'terminal_state_reached': terminal_state_reached,
            'settlement_transition_event_id': None,
            'last_transition_event_id': None,
            'last_transition_timestamp_utc': None,
            'last_transition_temp_f': None,
            'epoch_sequence': i + 1,
            'created_at_utc': datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        
        epochs.append(epoch)
    
    return epochs


def create_settlement_epochs_table(conn: sqlite3.Connection):
    """Create settlement_epochs table if it doesn't exist."""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settlement_epochs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            market_type TEXT,
            local_trading_date TEXT NOT NULL,
            date_utc TEXT,
            settlement_bucket INTEGER NOT NULL,
            prior_settlement_bucket INTEGER,
            settlement_timestamp_utc TEXT,
            settlement_jump_magnitude REAL DEFAULT 0.0,
            epoch_status TEXT DEFAULT 'closed',
            epoch_close_reason TEXT,
            epoch_close_timestamp_utc TEXT,
            reversion_occurred INTEGER DEFAULT 0,
            first_reversion_timestamp_utc TEXT,
            max_excursion_above_settlement REAL DEFAULT 0.0,
            duration_at_or_above_settlement_seconds INTEGER DEFAULT 86400,
            duration_strictly_above_settlement_seconds INTEGER DEFAULT 0,
            terminal_state_reached INTEGER DEFAULT 1,
            settlement_transition_event_id INTEGER,
            last_transition_event_id INTEGER,
            last_transition_timestamp_utc TEXT,
            last_transition_temp_f REAL,
            epoch_sequence INTEGER,
            created_at_utc TEXT,
            UNIQUE(station, market_type, local_trading_date)
        )
    """)
    
    # Create indexes
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_settlement_epoch_station 
        ON settlement_epochs(station)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_settlement_epoch_date 
        ON settlement_epochs(local_trading_date)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_settlement_epoch_station_date 
        ON settlement_epochs(station, local_trading_date)
    """)
    
    conn.commit()


def insert_epochs(conn: sqlite3.Connection, epochs: List[Dict[str, Any]]):
    """Insert settlement epochs into database."""
    cursor = conn.cursor()
    
    for epoch in epochs:
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO settlement_epochs (
                    station, market_type, local_trading_date, date_utc,
                    settlement_bucket, prior_settlement_bucket,
                    settlement_timestamp_utc, settlement_jump_magnitude,
                    epoch_status, epoch_close_reason, epoch_close_timestamp_utc,
                    reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement,
                    duration_at_or_above_settlement_seconds,
                    duration_strictly_above_settlement_seconds,
                    terminal_state_reached, settlement_transition_event_id,
                    last_transition_event_id, last_transition_timestamp_utc,
                    last_transition_temp_f, epoch_sequence, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                epoch['station'],
                epoch['market_type'],
                epoch['local_trading_date'],
                epoch['date_utc'],
                epoch['settlement_bucket'],
                epoch['prior_settlement_bucket'],
                epoch['settlement_timestamp_utc'],
                epoch.get('settlement_jump_magnitude', 0.0),
                epoch['epoch_status'],
                epoch['epoch_close_reason'],
                epoch['epoch_close_timestamp_utc'],
                epoch['reversion_occurred'],
                epoch['first_reversion_timestamp_utc'],
                epoch.get('max_excursion_above_settlement', 0.0),
                epoch.get('duration_at_or_above_settlement_seconds', 86400),
                epoch.get('duration_strictly_above_settlement_seconds', 0),
                epoch['terminal_state_reached'],
                epoch.get('settlement_transition_event_id'),
                epoch.get('last_transition_event_id'),
                epoch.get('last_transition_timestamp_utc'),
                epoch.get('last_transition_temp_f'),
                epoch['epoch_sequence'],
                epoch['created_at_utc'],
            ))
        except Exception as e:
            print(f"  Error inserting epoch for {epoch['station']} {epoch['local_trading_date']}: {e}")
    
    conn.commit()


def generate_settlement_epochs(
    db_path: str,
    stations: List[str] = None,
    market_types: List[str] = None,
    min_days: int = 30
) -> Dict[str, int]:
    """Generate settlement epochs from METAR backfill data."""
    
    print("=" * 60)
    print("SETTLEMENT EPOCH GENERATOR")
    print("=" * 60)
    print()
    print(f"Input database: {db_path}")
    print(f"Minimum days per station: {min_days}")
    print()
    
    conn = sqlite3.connect(db_path, timeout=30)
    
    try:
        # Create settlement_epochs table
        print("Creating settlement_epochs table...")
        create_settlement_epochs_table(conn)
        print("  ✓ Table ready")
        print()
        
        # Get stations from daily_stats
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT station FROM daily_stats ORDER BY station")
        all_stations = [row[0] for row in cursor.fetchall()]
        
        if not all_stations:
            print("ERROR: No daily_stats found in database!")
            return {}
        
        print(f"Found {len(all_stations)} stations: {', '.join(all_stations)}")
        print()
        
        # Filter if specified
        if stations:
            all_stations = [s for s in all_stations if s in stations]
        
        market_types = market_types or ["HIGH", "LOW"]
        
        total_epochs = 0
        station_counts = {}
        
        for station in all_stations:
            print(f"Processing {station}...")
            
            for market_type in market_types:
                epochs = generate_epochs_for_station(
                    conn, station, market_type, min_epoch_count=min_days
                )
                
                if epochs:
                    insert_epochs(conn, epochs)
                    station_counts[f"{station}_{market_type}"] = len(epochs)
                    total_epochs += len(epochs)
                    print(f"  ✓ {market_type}: {len(epochs)} epochs")
                else:
                    print(f"  ✗ {market_type}: insufficient data")
        
        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Total epochs generated: {total_epochs}")
        print()
        
        for key, count in sorted(station_counts.items()):
            print(f"  {key}: {count}")
        
        print()
        
        # Verify insertion
        cursor.execute("SELECT COUNT(*) FROM settlement_epochs")
        total_in_db = cursor.fetchone()[0]
        print(f"Epochs in database: {total_in_db}")
        
        # Show sample
        cursor.execute("""
            SELECT station, market_type, local_trading_date, 
                   settlement_bucket, prior_settlement_bucket, 
                   reversion_occurred
            FROM settlement_epochs
            ORDER BY local_trading_date DESC
            LIMIT 5
        """)
        print()
        print("Recent epochs:")
        for row in cursor.fetchall():
            print(f"  {row[0]} {row[1]} {row[2]}: bucket={row[3]}, prior={row[4]}, reversion={row[5]}")
        
        print()
        
        return station_counts
        
    finally:
        conn.close()


def main():
    """Main entry point."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found: {db_path}")
        print("Run backfill_metar.py first to populate data.")
        return
    
    # Generate epochs for all stations
    station_counts = generate_settlement_epochs(
        db_path,
        stations=None,  # All stations
        market_types=["HIGH", "LOW"],
        min_days=30
    )
    
    if station_counts:
        print()
        print("✓ Settlement epoch generation complete!")
    else:
        print()
        print("✗ No epochs generated. Check data availability.")


if __name__ == "__main__":
    main()
