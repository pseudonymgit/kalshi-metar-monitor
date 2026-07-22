#!/usr/bin/env python3
"""
Populate weather_data.db with relevant data copied from the existing databases.
This prepares the database for the signal freshness and ECMWF backfill experiments.
"""

import sqlite3
import pandas as pd
import os
from datetime import datetime

def create_weather_database():
    """Create weather_data.db with the required schema."""
    if os.path.exists('data/weather_data.db'):
        os.remove('data/weather_data.db')
    
    # Connect to new weather database
    weather_conn = sqlite3.connect('data/weather_data.db')
    
    # Create the schema by executing the SQL file
    with open('scripts/setup_weather_data.sql', 'r') as f:
        schema_sql = f.read()
    
    weather_conn.executescript(schema_sql)
    print("Schema created successfully.")
    weather_conn.close()

def populate_from_existing_databases():
    """Copy relevant data from existing databases to weather_data.db"""
    
    weather_conn = sqlite3.connect('data/weather_data.db')
    
    print("Populating from nwp_forecasts.db...")
    nwp_conn = sqlite3.connect('data/nwp_forecasts.db')
    
    # Copy nwp forecasts
    nwp_df = pd.read_sql_query("SELECT * FROM nwp_forecasts LIMIT 10000", nwp_conn)  # Limit to avoid huge datasets
    nwp_df.to_sql('nwp_forecasts', weather_conn, if_exists='replace', index=False)
    print(f"Copied {len(nwp_df)} nwp forecasts.")
    
    nwp_conn.close()
    
    print("Populating from metar_backfill.db...")
    metar_conn = sqlite3.connect('data/metar_backfill.db')
    
    # Copy settlement epochs
    try:
        settlements_df = pd.read_sql_query("SELECT * FROM settlement_epochs", metar_conn)
        settlements_df.to_sql('settlement_epochs', weather_conn, if_exists='replace', index=False)
        print(f"Copied {len(settlements_df)} settlement epochs.")
    except Exception as e:
        print(f"No settlement_epochs found: {e}")
    
    # Copy metar observations (limited sample)
    try:
        metar_df = pd.read_sql_query("SELECT * FROM metar_observations LIMIT 10000", metar_conn)
        metar_df.to_sql('metar_observations', weather_conn, if_exists='replace', index=False)
        print(f"Copied {len(metar_df)} metar observations.")
    except Exception as e:
        print(f"No metar_observations found: {e}")
    
    # Copy daily stats (if needed)
    try:
        daily_stats_df = pd.read_sql_query("SELECT * FROM daily_stats LIMIT 10000", metar_conn)
        daily_stats_df.to_sql('daily_stats', weather_conn, if_exists='replace', index=False)
        print(f"Copied {len(daily_stats_df)} daily stats records.")
    except Exception as e:
        print(f"No daily_stats found or error copying: {e}")
    
    # Copy six hour aggregates
    try:
        agg_df = pd.read_sql_query("SELECT * FROM six_hour_aggregates", metar_conn)
        agg_df.to_sql('six_hour_aggregates', weather_conn, if_exists='replace', index=False)
        print(f"Copied {len(agg_df)} six-hour aggregate records.")
    except Exception as e:
        print(f"No six_hour_aggregates found or error copying: {e}")
    
    metar_conn.close()
    
    print("Populating from forecast_disagreement_live.db...")
    forecast_conn = sqlite3.connect('data/forecast_disagreement_live.db')
    
    # Copy daily forecast snapshots with signal data
    try:
        forecast_df = pd.read_sql_query("SELECT * FROM daily_forecast_snapshots", forecast_conn)
        forecast_df.to_sql('daily_forecast_snapshots', weather_conn, if_exists='replace', index=False)
        print(f"Copied {len(forecast_df)} daily forecast snapshots with signal data.")
    except Exception as e:
        print(f"No daily_forecast_snapshots found or error copying: {e}")
    
    forecast_conn.close()
    
    # Synthesize settlements from available actual values in daily_forecast_snapshots
    print("Synthesizing settlements from daily_forecast_snapshots...")
    try:
        # Extract settlement data from daily_forecast_snapshots for analysis
        forecast_conn = sqlite3.connect('data/forecast_disagreement_live.db')
        settlements_synthesis = pd.read_sql_query("""
            SELECT DISTINCT
                station,
                collection_date as target_date,
                actual_high_f as settlement_value
            FROM daily_forecast_snapshots
            WHERE actual_high_f IS NOT NULL
            ORDER BY station, target_date
        """, forecast_conn)
        forecast_conn.close()
        
        # Remove duplicates (keep first occurrence) when multiple entries for the same day/station
        settlements_synthesis = settlements_synthesis.drop_duplicates(subset=['station', 'target_date'])
        
        # Store settlements in our custom settlements table
        settlements_synthesis.to_sql('settlements', weather_conn, if_exists='replace', index=False)
        print(f"Synthesized {len(settlements_synthesis)} settlement records.")
    except Exception as e:
        print(f"Error synthesizing settlements: {e}")
    
    # Try also from settlement_epochs if we can extract the right field
    print("Also synthesizing from settlement_epochs...")
    try:
        metar_conn = sqlite3.connect('data/metar_backfill.db')
        settlements_synthesis2 = pd.read_sql_query("""
            SELECT 
                station,
                local_trading_date as target_date,
                settlement_bucket as settlement_value
            FROM settlement_epochs
            WHERE settlement_value IS NOT NULL AND local_trading_date IS NOT NULL
            ORDER BY station, target_date
        """, metar_conn)
        metar_conn.close()
        
        # Remove duplicates and add to settlements if they're missing
        settlements_synthesis2 = settlements_synthesis2.drop_duplicates(subset=['station', 'target_date'])
        
        # Get existing settlements
        existing = pd.read_sql_query("SELECT station, target_date FROM settlements", weather_conn)
        # Only add those not already in the settlements table
        new_settlements = settlements_synthesis2.merge(
            existing, 
            on=['station', 'target_date'], 
            how='left', 
            indicator=True
        ).query("_merge == 'left_only'").drop('_merge', axis=1)
        
        if len(new_settlements) > 0:
            new_settlements.to_sql('settlements', weather_conn, if_exists='append', index=False)
            print(f"Added {len(new_settlements)} additional settlement records from settlement_epochs.")
        
    except Exception as e:
        print(f"Error processing settlement_epochs: {e}")
    
    weather_conn.close()
    print("Database population complete!")

def create_synthetic_signals():
    """Create synthetic signal data based on the NWP forecast and other available data"""
    print("Creating synthetic signals...")
    
    conn = sqlite3.connect('data/weather_data.db')
    
    # Create signal data based on nwp_forecasts and daily_forecast_snapshots that include signal directions
    print("Generating synthetic signals from forecast_disagreement data...")
    
    # Generate signal data from daily_forecast_snapshots that contains signal_direction and other signal-like fields
    query = """
    SELECT 
        station,
        collection_date as target_date,
        'forecast_disagreement' as signal_name,
        disagreement_f as signal_value,
        collection_timestamp_utc as signal_timestamp
    FROM daily_forecast_snapshots
    WHERE disagreement_f IS NOT NULL
    UNION ALL
    SELECT 
        station,
        collection_date as target_date,
        'signal_confidence' as signal_name,
        signal_confidence as signal_value,
        collection_timestamp_utc as signal_timestamp
    FROM daily_forecast_snapshots
    WHERE signal_confidence IS NOT NULL
    UNION ALL
    SELECT 
        station,
        collection_date as target_date,
        'ensemble_confidence' as signal_name,
        ensemble_confidence as signal_value,
        collection_timestamp_utc as signal_timestamp
    FROM daily_forecast_snapshots
    WHERE ensemble_confidence IS NOT NULL
    """
    
    synthetic_signals = pd.read_sql_query(query, conn)
    synthetic_signals['ensemble_id'] = 'disagreement_ensemble'
    
    # Add NWP-based signals from nwp_forecasts
    nwp_signals_query = """
    SELECT 
        station,
        target_date,
        model || '_' || variable as signal_name,
        value as signal_value,
        datetime('now') as signal_timestamp
    FROM nwp_forecasts
    WHERE model IS NOT NULL AND variable IS NOT NULL AND value IS NOT NULL
    LIMIT 50000
    """
    
    nwp_signals = pd.read_sql_query(nwp_signals_query, conn)
    nwp_signals['ensemble_id'] = nwp_signals['signal_name']
    nwp_signals['signal_timestamp'] = '2025-01-01 00:00:00'  # Placeholder
    
    # Combine and save
    all_signals = pd.concat([synthetic_signals[['station', 'target_date', 'signal_name', 'signal_value', 'signal_timestamp', 'ensemble_id']], 
                            nwp_signals[['station', 'target_date', 'signal_name', 'signal_value', 'signal_timestamp', 'ensemble_id']]], 
                           ignore_index=True)
    
    # Add some common signal types for demonstration
    # Create some temperature advection based signals from NWP data
    temp_adv_signals = nwp_signals[nwp_signals['signal_name'].str.contains('temperature', case=False, na=False)].copy()
    temp_adv_signals['signal_name'] = 'temperature_advection'
    temp_adv_signals['signal_timestamp'] = '2025-01-01 00:00:00'
    
    # Create some pressure delta signals
    pres_signals = nwp_signals[nwp_signals['signal_name'].str.contains('pressure', case=False, na=False)].copy()
    pres_signals['signal_name'] = 'pressure_delta'
    pres_signals['signal_timestamp'] = '2025-01-01 00:00:00'
    
    all_signals = pd.concat([all_signals, temp_adv_signals, pres_signals], ignore_index=True)
    
    # Filter for only the signals we want to test with (ones mentioned in the requirements)
    valid_signals = [
        'nwp_direct', 'temperature_advection', 'forecast_disagreement', 
        'pressure_delta', 'calendar_climatology', 'gaussian', 
        'frontal_detector', 'persistence', 'wind_direction_shift'
    ]
    
    # Map NWP variables to the signal names used in the requirements
    # Add placeholder records for signals that don't exist yet
    signal_mappings = {
        'gfs_temperature_2m_max': 'nwp_direct',
        'gfs_temperature_2m_min': 'nwp_direct',
        'ecmwf_temperature_2m_max': 'nwp_direct',
        'ecmwf_temperature_2m_min': 'nwp_direct',
        'gfs_surface_pressure': 'pressure_delta',
        'gfs_precipitation_sum': 'gaussian',  # placeholder for now
        'gfs_wind_u_component_10m': 'wind_direction_shift',
        'gfs_wind_v_component_10m': 'wind_direction_shift',
        'ecmwf_surface_pressure': 'pressure_delta',
        'ecmwf_precipitation_sum': 'gaussian',
        'ecmwf_wind_u_component_10m': 'wind_direction_shift',
        'ecmwf_wind_v_component_10m': 'wind_direction_shift'
    }
    
    for orig_name, mapped_name in signal_mappings.items():
        subset = all_signals[all_signals['signal_name'] == orig_name].copy()
        if len(subset) > 0:
            subset['signal_name'] = mapped_name
            all_signals = pd.concat([all_signals, subset], ignore_index=True)
    
    all_signals = all_signals[all_signals['signal_name'].isin(valid_signals)]
    
    all_signals.to_sql('signals', conn, if_exists='replace', index=False)
    
    print(f"Created {len(all_signals)} synthetic signals across {all_signals['signal_name'].nunique()} signal types.")
    
    conn.close()
    print("Synthetic signal creation complete!")


if __name__ == "__main__":
    print("Setting up weather_data.db...")
    create_weather_database()
    populate_from_existing_databases()
    create_synthetic_signals()
    print("Setup complete! The weather_data.db is ready for experiments.")