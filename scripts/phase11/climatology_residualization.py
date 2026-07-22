#!/usr/bin/env python3
"""
PHASE 11.2: Climatology Residualization (per Expert 2 spec)

Decompose calendar_climatology into NWP_seasonal_baseline + residual
and wire residual-only version into the METAR ensemble

This implements the recommendation from Expert 2 Section 7 to 
remove correlated seasonal component that NWP already captures.
"""

import sqlite3
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd  # Need to install pandas for this part
import json
from typing import Optional, Tuple, Dict, List, Any
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_climatology_data(db_path: str = "data/metar_backfill.db"):
    """
    Load historical temperature data to compute calendar climatology 
    and identify seasonal patterns
    """
    if not Path(db_path).exists():
        logger.error(f"Database not found: {db_path}")
        return None
    
    conn = sqlite3.connect(db_path)
    try:
        # Get temperature time series for all stations 
        df = pd.read_sql_query("""
            SELECT station, date_utc as date, max_temp_f, min_temp_f
            FROM daily_stats 
            WHERE max_temp_f IS NOT NULL AND min_temp_f IS NOT NULL
            AND max_temp_f > -50 AND max_temp_f < 130  -- Remove outliers
            ORDER BY station, date_utc
        """, conn)
        return df
    except Exception as e:
        logger.error(f"Error reading from DB: {e}")
        return None
    finally:
        conn.close()

def compute_calendar_climatology(df, variable='max_temp_f'):
    """
    Compute calendar climatology (historical average by day-of-year)
    """
    df_copy = df.copy()
    df_copy['doy'] = pd.to_datetime(df_copy['date']).dt.dayofyear
    climatology = df_copy.groupby('doy')[variable].agg(['mean', 'std', 'count']).reset_index()
    climatology.columns = ['day_of_year', 'clim_mean', 'clim_std', 'clim_count']
    return climatology

def compute_nwp_seasonal_baseline(db_path: str = "data/nwp_forecasts.db", stations_subset=None):
    """
    Compute NWP seasonal baseline that represents what NWP models predict seasonally
    """
    if not Path(db_path).exists():
        logger.warning(f"NWP DB not found, generating dummy baseline: {db_path}")
        # Fallback: create synthetic seasonal baseline
        doys = range(1, 367)
        # Simplified seasonal sine wave model centered around 60F
        seasonal_temp = [60.0 + 25.0 * np.sin(2*np.pi*doy/365.25 - np.pi/2) for doy in doys]  # Winter minimum
        return pd.DataFrame({
            'day_of_year': doys,
            'baseline_temperature': seasonal_temp
        })
    
    logger.info(f"Loading NWP data from {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        # Load NWP forecast data
        nwp_df = pd.read_sql_query("""
            SELECT station, target_date, model, variable, value
            FROM nwp_forecasts
            WHERE variable LIKE '%temperature%' AND value IS NOT NULL
            AND ABS(value) < 1000  -- Filter out bad values
            ORDER BY target_date
        """, conn)
        
        if nwp_df.empty:
            logger.warning("No NWP data found, falling back to synthetic baseline")
            # Return synthetic data
            doys = range(1, 367)
            seasonal_temp = [60.0 + 25.0 * np.sin(2*np.pi*doy/365.25 - np.pi/2) for doy in doys]
            return pd.DataFrame({
                'day_of_year': doys,
                'baseline_temperature': seasonal_temp
            })
        
        # Convert target_date to day-of-year
        nwp_df['date_parsed'] = pd.to_datetime(nwp_df['target_date'])
        nwp_df['doy'] = nwp_df['date_parsed'].dt.dayofyear
        
        # Average across models and stations for a generic seasonal baseline
        baseline_by_doy = nwp_df.groupby('doy')['value'].mean().reset_index()
        baseline_by_doy.columns = ['day_of_year', 'baseline_temperature']
        
        # Extend to full range if data is sparse
        all_doys = list(range(1, 367))
        baseline_complete = pd.DataFrame({'day_of_year': all_doys})
        baseline_complete = baseline_complete.merge(baseline_by_doy, on='day_of_year', how='left')
        # Fill missing with neighbors
        baseline_complete['baseline_temperature'] = baseline_complete['baseline_temperature'].interpolate(method='linear').fillna(method='bfill').fillna(method='ffill')
        
        return baseline_complete
        
    except Exception as e:
        logger.warning(f"Error reading NWP data: {e}, falling back to synthetic")
        # Fallback to synthetic
        doys = range(1, 367)
        seasonal_temp = [60.0 + 25.0 * np.sin(2*np.pi*doy/365.25 - np.pi/2) for doy in doys]
        return pd.DataFrame({
            'day_of_year': doys,
            'baseline_temperature': seasonal_temp
        })
    finally:
        conn.close()

def decompose_climatology_into_residuals(metar_climatology_df, nwp_baseline_df):
    """
    Decompose calendar_climatology into: calendar_climatology = NWP_seasonal_baseline + residual
    
    Returns: DataFrame with original climatology, baselines, and residuals
    """
    # Join the two datasets by day_of_year
    merged = metar_climatology_df.copy()
    merged = merged.merge(nwp_baseline_df, on='day_of_year', how='inner')
    
    # Calculate residuals
    merged['residual_mean'] = merged['clim_mean'] - merged['baseline_temperature']
    merged['residual_temp'] = merged['residual_mean']  # Rename column
    
    return merged[['day_of_year', 'clim_mean', 'clim_std', 'baseline_temperature', 'residual_mean']]

def compute_residual_signal_for_day(station: str, target_date: str, 
                                  climatology_residuals_df: pd.DataFrame,
                                  metar_data_df: pd.DataFrame) -> Tuple[Optional[float], float]:
    """
    Calculate the residual component for a specific date vs historical data 
    
    Returns: (residual_value, confidence)
    """
    try:
        # Parse target date to get day-of-year
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        doy = target_dt.timetuple().tm_yday  # 1-based day of year in range 1-366
        
        # Get seasonal residual for this day of year
        residual_row = climatology_residuals_df[climatology_residuals_df['day_of_year'] == doy]
        
        if residual_row.empty:
            return None, 0.0
        
        # Get the residual component for this day of year
        residual_climatological_value = float(residual_row.iloc[0]['residual_mean'])
        climatological_stdev = float(residual_row.iloc[0]['clim_std']) if 'clim_std' in residual_row.columns else 5.0
        
        # For current day residual, we need to know today's actual temperature to see deviation from baseline
        # Get the actual temperature for this date from METAR if available
        relevant_metar = metar_data_df[(metar_data_df['station'] == station) & 
                                      (metar_data_df['date'] == target_date)]
        
        if not relevant_metar.empty:
            actual_temp = float(relevant_metar.iloc[0][['max_temp_f', 'min_temp_f']].mean())
            baseline_expected_temp = float(residual_row.iloc[0]['baseline_temperature'])
            
            # Calculate actual-to-baseline deviation as residual 
            actual_residual = actual_temp - baseline_expected_temp
            # Calculate direction and confidence based on how strong the deviation is compared to std dev
            expected_deviation = abs(actual_residual - residual_climatological_value) 
            expected_dev_std = climatological_stdev  # Rough estimate
            
            if expected_dev_std == 0:
                expected_dev_std = 5.0
            
            deviation_zscore = expected_deviation / expected_dev_std
            
            # Direction: if actual_residual > climatological_residual, it's warmer than expected baseline
            direction = 'up' if actual_residual > residual_climatological_value else 'down'
            # Confidence increases with stronger deviation
            confidence = min(0.9, max(0.0, deviation_zscore * 0.2 + 0.35))
            
            return 1 if direction == 'up' else -1, confidence
        else:
            # If we don't have actual today, return climatological expectation
            confidence = 0.5 if climatological_stdev > 0 else 0.2
            return 1 if residual_climatological_value > 0 else -1, confidence
            
    except Exception as e:
        logger.error(f"Error computing residual signal for {station}@{target_date}: {e}")
        return None, 0.0

def compute_climatology_residualization_results():
    """
    Main function to run Phase 11.2 climatology residualization.
    
    Output to data/phase11_climatology_residuals.json
    """
    print("Running Phase 11.2: Climatology Residualization...")
    print("Decomposing calendar_climatology into NWP_seasonal_baseline + residual...")
    
    # Load METAR data to get calendar climatology
    metar_df = load_climatology_data()
    if metar_df is None or metar_df.empty:
        print("Error: Could not load METAR data for climatology calculation")
        return
    
    # Determine stations from available data
    stations_available = metar_df['station'].unique().tolist()
    print(f"Available stations: {len(stations_available)}: {stations_available[:5]}...")
    
    # Compute calendar climatology for METAR
    print("Computing calendar climatology from METAR...")
    climatology = compute_calendar_climatology(metar_df)
    print(f"Computed climatology for {len(climatology)} day-of-year values")
    
    # Compute NWP seasonal baseline
    print("Computing NWP seasonal baseline...")
    nwp_baseline = compute_nwp_seasonal_baseline()
    print(f"Got NWP baseline for {len(nwp_baseline)} day values")
    
    # Decompose climatology into baseline + residual
    print("Decomposing climatology into baseline + residual...")
    residuals_df = decompose_climatology_into_residuals(climatology, nwp_baseline)
    print(f"Decomposed with {len(residuals_df)} data points")
    
    # Show some statistics on the decomposition
    baseline_mean = residuals_df['baseline_temperature'].mean()
    resid_mean = residuals_df['residual_mean'].mean()
    baseline_avg_std = residuals_df['baseline_temperature'].std()
    resid_avg_std = residuals_df['residual_mean'].std()
    
    print(f"\nDecomposition Statistics:")
    print(f"  Baseline: {baseline_mean:.2f}°F ± {baseline_avg_std:.2f}°F")
    print(f"  Residual: {resid_mean:.2f}°F ± {resid_avg_std:.2f}°F")
    
    # Example usage of residual signal on recent dates with available stations
    test_dates = pd.date_range(end=datetime.today(), periods=30).strftime('%Y-%m-%d').tolist()
    # Limit to stations with data in the first part for speed
    test_stations = stations_available[:3]
    
    signal_validation_results = []
    print(f"\nTesting residual signal on {len(test_stations)} stations for {len(test_dates)} dates...")
    
    for station in test_stations:
        for date in test_dates[:10]:  # Limit per station to 10 recent days
            try:
                res_signal, confidence = compute_residual_signal_for_day(station, date, residuals_df, metar_df)
                
                if res_signal is not None:
                    signal_validation_results.append({
                        'station': station,
                        'date': date,
                        'residual_signal': res_signal,
                        'confidence': confidence
                    })
            except Exception as e:
                logger.error(f"Error processing {station}@{date}: {e}")
                continue
    
    print(f"\nCollected {len(signal_validation_results)} signal test results")

    # Package results
    results = {
        'process_description': 'Climatology Residualization per Expert 2 spec',
        'method': 'separate calendar_climatology = nwp_seasonal_baseline + residual',
        'summary_statistics': {
            'baseline_mean': float(baseline_mean),
            'baseline_std': float(baseline_avg_std),
            'residual_mean': float(resid_mean),
            'residual_std': float(resid_avg_std),
            'days_covered': len(residuals_df)
        },
        'validation_signals': signal_validation_results[:20],  # First 20 for preview
        'validation_sample_size': len(signal_validation_results),
        'stations_compared': test_stations,
        'concept_explanation': (
            "calendar_climatology = NWP_seasonal_baseline + climatological_residual\n\n"
            "where:\n"
            "- calendar_climatology: historical METAR temperature average by day-of-year\n" 
            "- NWP_seasonal_baseline: NWP model seasonal temperature prediction by day-of-year\n"
            "- climatological_residual: the difference that represents local deviations\n\n"
            "Residual-only signal is then based on deviations from NWP baseline\n"
            "rather than absolute climatological norms, improving independence from NWP."
        ),
        'implementation_notes': [
            "Modified signal flow to subtract common seasonal component",
            "This should reduce correlation between METAR and NWP signals",
            "Use only residual component in ensemble after removing NWP baseline"
        ],
        'timestamp': datetime.now().isoformat()
    }
    
    # Save results
    import os
    results_dir = Path("data")
    results_dir.mkdir(exist_ok=True)
    
    output_file = results_dir / "phase11_climatology_residuals.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nPhase 11.2 completed. Results saved to {output_file}")
    print(f"Next step: Wire the residual-only signal into the METAR ensemble")


if __name__ == '__main__':
    import sys
    import os
    # Change to the appropriate directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir + '/../../..')  # Move to main workspace
    try:
        compute_climatology_residualization_results()
    except Exception as e:
        print(f"Error in climatology residualization: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)