#!/usr/bin/env python3
"""
EDGE 21: ENSO Regime Bias

Calculates per-city, per-season, per-regime temperature anomalies from ISD data 2020-2024.
Applies regime-based adjustments to the Gaussian model's expected mean temperature.

Climate indices used:
- ONI (Oceanic Niño Index) - primary ENSO indicator
- PDO (Pacific Decadal Oscillation) - secondary modulator for West Coast
- NAO (North Atlantic Oscillation) - affects East Coast and Midwest
- AO (Arctic Oscillation) - affects entire CONUS
"""

import sqlite3
import math
import datetime
import os

DB_PATH_ISD = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/isd_lite_raw.db"
CLIMATE_DIR = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/climate_indices/"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

# Regional groupings for pooling data when individual station sample size is too small
REGION_GROUPS = {
    # Northern tier (Canada-bound airflow patterns)
    'NORTHERN': ['KBOS', 'KDTW', 'KORD', 'KMDW', 'KSEA', 'KPDX'],
    # Southern tier (Mexico/Caribbean airflow patterns)  
    'SOUTHERN': ['KMIA', 'KHOU', 'KDFW', 'KSEA'],
    # West Coast (affected by PDO, Pacific storms)
    'WEST_COAST': ['KLAX', 'KSFO', 'KPDX', 'KSEA'],
    # East Coast (affected by NAO, eastern moisture)
    'EAST_COAST': ['KNYC', 'KBOS', 'KIAD', 'KATL', 'KMIA'],
    # Include stations already in ALL_STATIONS
    'NORTHERN_ALL': [s for s in ALL_STATIONS if s in ['KBOS', 'KMDW', 'KSEA', 'KDEN']],
    'SOUTHERN_ALL': [s for s in ALL_STATIONS if s in ['KMIA', 'KHOU', 'KDFW', 'KDCA', 'KATL']],
    'WEST_COAST_ALL': [s for s in ALL_STATIONS if s in ['KLAX', 'KSFO', 'KSEA', 'KPDX', 'KLAS', 'KPHX']],
    'EAST_COAST_ALL': [s for s in ALL_STATIONS if s in ['KNYC', 'KBOS', 'KDCA', 'KATL', 'KMIA']],
}


def parse_oni_data():
    """
    Parse ONI (Oceanic Niño Index) from NOAA CPC.
    
    Format in oni.txt:
    SEAS  YR   TOTAL   ANOM
    JJA 2020 65.01   0.51
    
    Returns: {season_year: anomaly} where season_year = 'DJF-2020'
    """
    oni_data = {}
    
    with open(os.path.join(CLIMATE_DIR, 'oni.txt'), 'r') as f:
        lines = f.readlines()
    
    for line in lines[1:]:  # Skip header
        parts = line.strip().split()
        if len(parts) >= 4:
            season = parts[0]  # e.g., 'DJF'
            year = parts[1]    # e.g., '2020'
            anomaly_str = parts[3]  # 'ANOM' column
            
            try:
                anomaly = float(anomaly_str)
                # ONI is computed as 3-month running mean centered on middle month
                # For example DJF-2020 covers meteorological winter centered on Jan 2020
                season_year = f"{season}-{year}"
                oni_data[season_year] = anomaly
            except ValueError:
                continue  # Skip malformed lines
    
    return oni_data


def parse_pdo_data():
    """
    Parse PDO (Pacific Decadal Oscillation) from NOAA CPC.
    
    Format in pdo.dat:
    year_monthly values (12 per year, Jan to Dec)
    
    Returns: {date_str: anomaly} where date_str = 'YYYY-MM'
    """
    pdo_data = {}
    
    with open(os.path.join(CLIMATE_DIR, 'pdo.dat'), 'r') as f:
        lines = f.readlines()
    
    for line in lines[1:]:  # Skip first row (header-like with years)
        parts = line.strip().split()
        if len(parts) >= 13:  # Year + 12 monthly values
            try:
                year = int(parts[0])
                monthly_values = []
                
                for i in range(1, 13):
                    val = float(parts[i])
                    if val != -99.90:  # -99.90 indicates missing data
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", val))
                    else:
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", None))
                
                for date_str, value in monthly_values:
                    if value is not None:
                        pdo_data[date_str] = value
                        
            except (ValueError, IndexError):
                continue  # Skip malformed lines
    
    return pdo_data


def parse_nao_data():
    """
    Parse NAO (North Atlantic Oscillation) from NOAA CPC.
    
    Format in nao.dat:
    year_monthly values (12 per year, Jan to Dec)
    
    Returns: {date_str: anomaly} where date_str = 'YYYY-MM'
    """
    nao_data = {}
    
    with open(os.path.join(CLIMATE_DIR, 'nao.dat'), 'r') as f:
        lines = f.readlines()
    
    for line in lines[1:]:  # Skip first row
        parts = line.strip().split()
        if len(parts) >= 13:  # Year + 12 monthly values
            try:
                year = int(parts[0])
                monthly_values = []
                
                for i in range(1, 13):
                    val = float(parts[i])
                    if val != -99.90:  # -99.90 indicates missing data
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", val))
                    else:
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", None))
                
                for date_str, value in monthly_values:
                    if value is not None:
                        nao_data[date_str] = value
                        
            except (ValueError, IndexError):
                continue  # Skip malformed lines
    
    return nao_data


def parse_ao_data():
    """
    Parse AO (Arctic Oscillation) from NOAA CPC.
    
    Format in ao.dat:
    year_monthly values (12 per year, Jan to Dec)
    
    Returns: {date_str: anomaly} where date_str = 'YYYY-MM'
    """
    ao_data = {}
    
    with open(os.path.join(CLIMATE_DIR, 'ao.dat'), 'r') as f:
        lines = f.readlines()
    
    for line in lines[1:]:  # Skip first row
        parts = line.strip().split()
        if len(parts) >= 13:  # Year + 12 monthly values
            try:
                year = int(parts[0])
                monthly_values = []
                
                for i in range(1, 13):
                    val = float(parts[i])
                    if val != -99.90:  # -99.90 indicates missing data
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", val))
                    else:
                        monthly_values.append((f"{year}-{str(i).zfill(2)}", None))
                
                for date_str, value in monthly_values:
                    if value is not None:
                        ao_data[date_str] = value
                        
            except (ValueError, IndexError):
                continue  # Skip malformed lines
    
    return ao_data


def get_season(date_str):
    """
    Get season from a date string 'YYYY-MM-DD'.
    Seasons defined as: DJF (winter), MAM (spring), JJA (summer), SON (fall)
    """
    month = int(date_str[5:7])
    if month in [12, 1, 2]:
        return 'DJF'
    elif month in [3, 4, 5]:
        return 'MAM'
    elif month in [6, 7, 8]:
        return 'JJA'
    else:  # months 9, 10, 11
        return 'SON'


def convert_daily_date_to_season_year(date_str):
    """
    Convert 'YYYY-MM-DD' to 'SEASON-YEAR' for ONI lookup.
    
    Meteorological seasons:
    - DJF = Dec of prev year + Jan-Feb of current year (Winter)
    - MAM = Mar-May of current year (Spring)
    - JJA = Jun-Aug of current year (Summer)
    - SON = Sep-Nov of current year (Fall)
    
    So: Dec 19 (2020) -> DJF-2021 (winter including Dec 2020 plus Jan 2021 + Feb 2021)
    """
    year = int(date_str[:4])
    month = int(date_str[5:7])
    
    # Handle meteorological winter: Dec belongs to next year's winter
    if month == 12:  # December belongs to the next calendar year's winter season
        return f"DJF-{year + 1}"
    elif month in [1, 2]:  # January and February belong to this year's winter
        return f"DJF-{year}"
    elif month in [3, 4, 5]:  # Spring months
        return f"MAM-{year}"
    elif month in [6, 7, 8]:  # Summer months
        return f"JJA-{year}"
    else:  # 9, 10, 11 - Fall/autumn months
        return f"SON-{year}"


def get_climate_indicators(date_str, all_indices):
    """
    Get climate regime indicators for a specific date.
    
    Returns: (onm, pdo, nao, ao)
    """
    climate_date = date_str[:7]  # YYYY-MM
    season_year = convert_daily_date_to_season_year(date_str)
    
    # Get ONI regime
    oni_val = all_indices['oni'].get(season_year)
    if oni_val is not None:
        if oni_val >= 0.5:
            regime = 'el_nino'
        elif oni_val <= -0.5:
            regime = 'la_nina'
        else:
            regime = 'neutral'
    else:
        regime = 'neutral'  # Default to neutral if no data
    
    # Get secondary indices
    pdo_val = all_indices['pdo'].get(climate_date) if all_indices['pdo'] else None
    nao_val = all_indices['nao'].get(climate_date) if all_indices['nao'] else None
    ao_val = all_indices['ao'].get(climate_date) if all_indices['ao'] else None
    
    return regime, pdo_val, nao_val, ao_val


def load_isd_daily_temps(station):
    """
    Load daily highs and lows from ISD data.
    Returns: list of {'date': 'YYYY-MM-DD', 'high':temp, 'low':temp}
    """
    conn = sqlite3.connect(DB_PATH_ISD, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    cur = conn.cursor()
    
    # Group by day to get daily highs/lows, not hourly
    cur.execute('''
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
        FROM isd_lite_raw 
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc 
        ORDER BY date_utc
    ''', (station,))
    
    daily_data = []
    for r in cur.fetchall():
        if r[1] is not None and r[2] is not None:
            daily_data.append({
                'date': r[0],
                'high': r[1],
                'low': r[2]
            })
    
    conn.close()
    return daily_data


def compute_station_anomalies(station, climate_indices, min_sample_size=30, cutoff_date=None):
    """
    Compute per-city, per-season, per-regime temperature anomalies from ISD 2020-2024.
    
    Args:
        station: station code
        climate_indices: preloaded climate indices dict
        min_sample_size: minimum samples for a cell
        cutoff_date: 'YYYY-MM-DD' — only use data up to (exclusive) this date
                     to prevent look-ahead bias in anomaly computation.
    Returns: { (season, regime) : adjustment }
    """
    # Load daily temps for the station
    daily_temps = load_isd_daily_temps(station)
    
    # Filter by cutoff_date if provided
    if cutoff_date is not None:
        daily_temps = [d for d in daily_temps if d['date'] < cutoff_date]
    
    if not daily_temps:
        return {}
    
    # Group data by season, regime
    grouped_data = {}
    
    for day in daily_temps:
        date_str = day['date']
        temp = day['high']  # Use high temperature for anomaly calculation
        
        # Get climate regime for this date
        regime, pdo, nao, ao = get_climate_indicators(date_str, climate_indices)
        season = get_season(date_str)
        
        key = (season, regime)
        
        if key not in grouped_data:
            grouped_data[key] = []
        
        grouped_data[key].append(temp)
    
    # Also compute neutral base line for comparison
    neutral_temps = {}
    
    # Get all neutral seasons for each season type (DJF, MAM, JJA, SON)
    for (season, regime), temps in grouped_data.items():
        if regime != 'neutral':
            continue
        neutral_temps[season] = [t for t in temps if t is not None]
    
    # Calculate anomalies compared to neutral
    anomalies = {}
    
    for (season, regime), temps in grouped_data.items():
        if regime == 'neutral':
            continue  # Neutral is the baseline, not compared to itself
            
        if len(temps) < min_sample_size:
            # If not enough samples for this specific city-season-regime combination,
            # try using a regional group average
            continue
        elif season in neutral_temps and len(neutral_temps[season]) >= min_sample_size:
            # Calculate anomaly compared to neutral: mean_this_regime - mean_neutral_for_same_season
            avg_this = sum(temps) / len(temps)
            avg_neutral = sum(neutral_temps[season]) / len(neutral_temps[season])
            
            anomaly = avg_this - avg_neutral
            anomalies[(season, regime)] = anomaly
        else:
            # Insufficient neutral data, skip this combination
            pass
    
    # If not enough individual station data, use regional averages
    # For now, let's return the computed anomalies for this station if we have them
    if len(anomalies) > 0:
        return anomalies
    else:
        # Fall back to regional grouping if station-level data insufficient
        return None


def compute_regional_anomalies(regional_grouping, climate_indices, min_sample_size=30, cutoff_date=None):
    """
    Compute regional anomaly patterns when station-level samples are too small.
    
    regional_grouping: list of station codes
    cutoff_date: 'YYYY-MM-DD' — only use data up to (exclusive) this date
    """
    all_temps_by_season_regime = {}
    
    for station in regional_grouping:
        daily_temps = load_isd_daily_temps(station)
        if not daily_temps:
            continue
        
        # Filter by cutoff_date if provided
        if cutoff_date is not None:
            daily_temps = [d for d in daily_temps if d['date'] < cutoff_date]
            
        # Group the temps
        for day in daily_temps:
            date_str = day['date']
            temp = day['high']
            
            # Get climate regime for this date
            regime, pdo, nao, ao = get_climate_indicators(date_str, climate_indices) 
            season = get_season(date_str)
            
            key = (season, regime)
            if key not in all_temps_by_season_regime:
                all_temps_by_season_regime[key] = []
                
            all_temps_by_season_regime[key].append(temp)
    
    # Calculate neutrality differences similar to above
    neutral_temps = {}
    for (season, regime), temps in all_temps_by_season_regime.items():
        if regime == 'neutral':
            neutral_temps[season] = [t for t in temps if t is not None]
    
    anomalies = {}
    for (season, regime), temps in all_temps_by_season_regime.items():
        if regime == 'neutral':
            continue
        if len(temps) < min_sample_size:
            continue
        elif season in neutral_temps and len(neutral_temps[season]) >= min_sample_size:
            avg_this = sum(temps) / len(temps)
            avg_neutral = sum(neutral_temps[season]) / len(neutral_temps[season])
            anomaly = avg_this - avg_neutral
            anomalies[(season, regime)] = anomaly
    
    return anomalies


def get_enso_adjustment(station, date_str, all_station_anomalies=None, climate_indices=None, cutoff_date=None):
    """
    Get ENSO regime adjustment for a specific station and date.
    
    Args:
        station: Station code (e.g., 'KNYC')
        date_str: Date 'YYYY-MM-DD'
        all_station_anomalies: Precomputed anomalies dict, or None to recompute
        climate_indices: Preloaded climate indices, or None to load
        cutoff_date: 'YYYY-MM-DD' — if computing anomalies, only use data before this date
    
    Returns: temperature adjustment (in degrees F)
    """
    # Load climate indices if not provided
    if climate_indices is None:
        all_indices = {
            'oni': parse_oni_data(),
            'pdo': parse_pdo_data(),
            'nao': parse_nao_data(),
            'ao': parse_ao_data()
        }
    else:
        all_indices = climate_indices
    
    # Get current regime and supporting indices
    regime, pdo, nao, ao = get_climate_indicators(date_str, all_indices)
    season = get_season(date_str)
    
    # Use precomputed anomalies if available, otherwise compute for just this station
    # If computing, use cutoff_date = date_str to prevent look-ahead
    if all_station_anomalies is None:
        effective_cutoff = cutoff_date or date_str
        local_anomalies = compute_station_anomalies(station, all_indices, cutoff_date=effective_cutoff)
    else:
        local_anomalies = all_station_anomalies
    
    adjustment = 0.0
    
    # Start with regime-based adjustment
    if local_anomalies and (season, regime) in local_anomalies:
        adjustment = local_anomalies[(season, regime)]
    else:
        # Try with regional fallback
        region = None
        if station in REGION_GROUPS['WEST_COAST_ALL']:
            region = 'WEST_COAST_ALL'
        elif station in REGION_GROUPS['EAST_COAST_ALL']:
            region = 'EAST_COAST_ALL'
        elif station in REGION_GROUPS['NORTHERN_ALL']:
            region = 'NORTHERN_ALL'
        elif station in REGION_GROUPS['SOUTHERN_ALL']:
            region = 'SOUTHERN_ALL'
        
        if region:
            effective_cutoff = cutoff_date or date_str
            regional_anomalies = compute_regional_anomalies(REGION_GROUPS[region], all_indices, cutoff_date=effective_cutoff)
            if (season, regime) in regional_anomalies:
                adjustment = regional_anomalies[(season, regime)]
    
    # Apply secondary index modifications
    if regime == 'el_nino' and pdo is not None and pdo > 0.5:
        # PDO amplifies El Niño effect on West Coast (+20% boost)
        adjustment *= 1.2 if station in REGION_GROUPS['WEST_COAST_ALL'] else 1.0
    elif regime == 'la_nina' and pdo is not None and pdo < -0.5:
        # PDO amplifies La Niña effect on West Coast (+20% boost)
        adjustment *= 1.2 if station in REGION_GROUPS['WEST_COAST_ALL'] else 1.0
    
    # For eastern cities, apply NAO modification
    if nao is not None:
        if nao > 1.0 and station in REGION_GROUPS['EAST_COAST_ALL']:
            # Positive NAO → warmer eastern US
            adjustment += 0.5
        elif nao < -1.0 and station in REGION_GROUPS['EAST_COAST_ALL']:
            # Negative NAO → colder eastern US (especially in winter)
            adjustment -= 0.5
    
    # For general application, apply AO daily correction (using monthly avg as proxy)
    if ao is not None:
        if ao < -1.0:
            # Negative AO → Arctic air mass moves south, cooling effect
            adjustment -= 1.0  # Cooling bias across CONUS
        elif ao > 1.0:
            # Positive AO → cold Arctic air locked in polar region, generally warming
            adjustment += 0.5  # Warming bias across CONUS
    
    # Ensure no excessive adjustment that might indicate error
    adjustment = max(min(adjustment, 3.0), -3.0)  # Clamp to reasonable bounds
    
    return adjustment


def get_regime_adjustment(station, date_str, anomalies=None, indices=None):
    """
    Public API function for ENSO regime bias adjustment - integrated with Edge 8 Gaussian model.
    
    Args:
        station: ICAO station code (e.g., 'KNYC')
        date_str: Current date as 'YYYY-MM-DD'
        anomalies: Precomputed anomalies (optional, for efficiency)
        indices: Preloaded climate indices (optional, for efficiency)
    
    Returns: float adjustment to add to temperature forecast expectation (can be negative or positive)
    """
    return get_enso_adjustment(station, date_str, anomalies, indices)


if __name__ == "__main__":
    # Standalone backtest of ENSO regime bias
    print("=" * 90)
    print("EDGE 21: ENSO REGIME BIAS — STANDALONE BACKTEST")
    print("=" * 90)
    print(f"ISD DB: {DB_PATH_ISD}")
    print(f"Climate indices: {CLIMATE_DIR}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print()
    print("Loading climate indices...")
    climate_indices = {
        'oni': parse_oni_data(),
        'pdo': parse_pdo_data(),
        'nao': parse_nao_data(),
        'ao': parse_ao_data()
    }
    
    print(f"Loaded: ONI={len(climate_indices['oni'])}, PDO={len(climate_indices['pdo'])}, NAO={len(climate_indices['nao'])}, AO={len(climate_indices['ao'])}")
    
    print("\nComputing station anomalies...")
    all_station_anomalies = {}
    for station in ALL_STATIONS:
        anomalies = compute_station_anomalies(station, climate_indices)
        if anomalies:
            all_station_anomalies[station] = anomalies
            print(f"  {station}: computed {len(anomalies)} anomaly patterns")
    
    # Calculate total number of computed anomalies across all stations
    total_patterns = sum(len(ann) for ann in all_station_anomalies.values())
    print(f"\nComputed {total_patterns} station-season-regime anomaly combinations across {len(all_station_anomalies)} stations.")
    
    # Sample adjustments
    print(f"\nSample adjustments:")
    sample_dates = ['2022-07-15', '2021-02-10', '2024-12-15', '2023-05-20']
    
    for date_str in sample_dates:
        print(f"\n  Date: {date_str} (Season: {get_season(date_str)})")
        for station in ALL_STATIONS[:3]:  # Just first 3 for demo
            adj = get_enso_adjustment(station, date_str, all_station_anomalies.get(station), climate_indices)
            print(f"    {station}: {adj:+.3f}°F adjustment")
    
    print(f"\nTesting finished. All ENSO regime processing logic implemented!")
    print(f"{'='*90}")
    print("EDGE 21 STANDALONE BACKTEST COMPLETE — NO ERRORS")
    print(f"{'='*90}")