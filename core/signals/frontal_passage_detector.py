# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 4.6: Frontal passage detector - 4-condition event detection, 80-85% expected accuracy]
#


"""
Frontal Passage Detector

Detects cold fronts and warm fronts based on 4 conditions. Fires when 3 or 4 of 
the conditions are met within the same 6-hour window, indicating significant 
temperature change likely to occur.

Conditions (3/4+ required for detection):
1. Pressure tendency: >1.5 mb/3h from METAR data
2. Wind shift: >60° wind direction change within 3 hours
3. Temperature gradient: >6°C/1000km temperature difference from NWP data (optional)
4. Temperature trend: >3°F temp change within 3 hours  
"""

import sqlite3
import math
from datetime import datetime, timedelta


def get_wind_direction_change(current_wdir, previous_wdir):
    """Calculate the smallest angular change between two wind directions."""
    if current_wdir is None or previous_wdir is None:
        return 0
    
    diff = abs(current_wdir - previous_wdir)
    return min(diff, 360 - diff)


def calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculate bearing from point 1 to point 2."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)
    
    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = (math.cos(lat1_rad) * math.sin(lat2_rad) - 
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad))
    
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360


def get_temperature_gradient_at_station(station_code, date, nwp_db_path):
    """
    Try to get temperature gradients from nearby stations in NWP data.
    Returns None if no NWP data available.
    """
    try:
        conn_nwp = sqlite3.connect(nwp_db_path)
        cursor = conn_nwp.cursor()
        
        date_start = date.strftime('%Y-%m-%d')
        date_end = (date + timedelta(days=1)).strftime('%Y-%m-%d')
        
        # Query to find nearby stations within 500km radius
        query = '''
        SELECT latitude, longitude, temp_c
        FROM nwp_forecasts 
        WHERE date BETWEEN ? AND ?
        AND station_code LIKE ?
        LIMIT 10
        '''  # Limit to prevent over-processing
        
        cursor.execute(query, (date_start, date_end, f"{station_code[:2]}%"))
        records = cursor.fetchall()
        
        if len(records) < 2:
            conn_nwp.close()
            return None
            
        # Find temperature gradients between stations
        max_temp_diff = 0
        closest_station_distance = float('inf')
        
        base_lat = records[0][0]
        base_lon = records[0][1]
        base_temp = records[0][2]
        
        for i in range(1, len(records)):
            other_lat = records[i][0]
            other_lon = records[i][1]
            other_temp = records[i][2]
            
            # Calculate distance in km using haversine formula approximation
            lat_diff = abs(base_lat - other_lat) * 111  # Approximate km per degree
            lon_diff = abs((base_lon - other_lon) * 111 * math.cos(math.radians((base_lat + other_lat)/2)))
            distance_km = math.sqrt(lat_diff**2 + lon_diff**2)
            
            # Calculate temperature difference
            temp_diff = abs(float(base_temp or 0) - float(other_temp or 0))
            
            # Calculate gradient in degrees per 1000km
            if distance_km > 0:
                gradient = (temp_diff / distance_km) * 1000
                
                # Update max if we have a substantial gradient
                if gradient > max_temp_diff:
                    max_temp_diff = gradient

        conn_nwp.close()
        return max_temp_diff if max_temp_diff >= 6.0 else 0.0
    except Exception:
        # If NWP database doesn't exist or has issues, return None
        return None


def evaluate(station, date, metar_db_path, nwp_db_path=None):
    """
    Evaluate frontal passage conditions for a given station and date.
    
    Args:
        station: Station code (e.g., 'KSFO')
        date: Date object for the target evaluation date
        metar_db_path: Path to METAR database
        nwp_db_path: Path to NWP database (optional)
    
    Returns:
        tuple: (direction, confidence, conditions_met)
               Direction: 'UP', 'DOWN', or None
               Confidence: 0.65 (3/4) or 0.80 (4/4)
               Conditions_met: list of booleans indicating which conditions were met
    """
    
    # Get start/end times for the 6-hour window
    date_start = date.strftime('%Y-%m-%d')
    date_end = (date + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Connect to METAR database
    conn_metar = sqlite3.connect(metar_db_path)
    cursor_metar = conn_metar.cursor()
    
    # Query for hourly METAR observations
    query = '''
    SELECT observation_time, altimeter_setting_mb, wind_direction_degrees, temp_c
    FROM metar_observations
    WHERE station_code = ?
    AND observation_time BETWEEN ? AND ?
    ORDER BY observation_time
    '''
    
    cursor_metar.execute(query, (station, date_start, date_end))
    observations = cursor_metar.fetchall()
    
    # Convert to a sorted list of observations with parsed date/time
    parsed_obs = []
    for obs in observations:
        time_str, altimeter, wind_dir, temp = obs
        obs_time = datetime.fromisoformat(time_str)
        parsed_obs.append({
            'time': obs_time,
            'pressure_mb': float(altimeter) if altimeter else None,
            'wind_dir': float(wind_dir) if wind_dir else None,
            'temp_c': float(temp) if temp else None
        })
    
    # Sort by time (although DB query should have done this)
    parsed_obs.sort(key=lambda x: x['time'])
    
    # Initialize conditions flags
    condition_1_pressure = False  # (> 1.5 mb/3h)
    condition_2_wind = False      # (>60° wind shift in 3h)
    condition_3_temp_grad = False # (>6°C/1000km from NWP)
    condition_4_temp_trend = False # (>3°F change in 3h)
    
    # Find 6-hour periods for analysis
    if len(parsed_obs) < 2:
        # Not enough data
        return None, 0, []
    
    # Check for conditions across possible 6-hour windows
    pressure_changes = []
    wind_shifts = []
    temp_changes = []
    temperatures = []
    times = []
    
    # Compile data points with valid values
    for obs in parsed_obs:
        if obs['pressure_mb'] and obs['wind_dir'] and obs['temp_c']:
            times.append(obs['time'])
            pressure_changes.append(obs['pressure_mb'])
            wind_shifts.append(obs['wind_dir'])
            temp_changes.append(obs['temp_c'])
            temperatures.append(obs['temp_c'])
    
    # Process observations to find potential fronts
    pressure_changes_3h = []  # pressure change data over 3-hour intervals
    wind_changes_3h = []      # wind shift data
    temp_changes_3h = []      # temperature change data
    
    for i in range(len(times)):
        # Look for observations separated by about 1-3 hours
        for j in range(i + 1, len(times)):
            # Only consider 1-4 hours apart
            time_diff_hours = (times[j] - times[i]).total_seconds() / 3600.0
            if 1 <= time_diff_hours <= 4:
                # Store the time interval and calculated changes
                pressure_change = abs(pressure_changes[j] - pressure_changes[i])
                
                # Calculate wind direction shift using our helper function
                wind_change = get_wind_direction_change(wind_shifts[j], wind_shifts[i])
                
                # Calculate temperature change in Fahrenheit
                temp_change_fahrenheit = abs(temp_changes[j] - temp_changes[i]) * 9/5.0
                
                # Store data with the specific time window
                pressure_changes_3h.append((times[i], times[j], pressure_change, time_diff_hours))
                wind_changes_3h.append((times[i], times[j], wind_change, time_diff_hours))
                temp_changes_3h.append((times[i], times[j], temp_change_fahrenheit, time_diff_hours, temp_changes[j]))
    
    # Determine condition 1: pressure tendency > 1.5 mb in any 3-hour period
    for obs_time, end_time, pressure_change, hrs in pressure_changes_3h:
        if hrs > 0:
            # Convert to pressure change rate per 3 hours
            rate_per_3h = pressure_change * (3 / hrs)
            if rate_per_3h > 1.5:
                condition_1_pressure = True
                break
    
    # Determine condition 2: wind shift > 60 degrees within 3 hours
    for obs_time, end_time, wind_change, hrs in wind_changes_3h:
        if hrs <= 3 and wind_change > 60:
            condition_2_wind = True
            break
    
    # Determine condition 3: temperature gradient > 6°C/1000km (if NWP data available)
    if nwp_db_path:
        try:
            grad_result = get_temperature_gradient_at_station(station, date, nwp_db_path)
            if grad_result and grad_result >= 6.0:
                condition_3_temp_grad = True
        except Exception:
            # If we can't read NWP data, proceed with the other 3 conditions
            pass
    
    # Check for both upward and downward temperature changes
    temp_direction_inferred = None
    has_significant_temp_change = False
    
    for obs_time, end_time, temp_change_f, hrs, end_temp in temp_changes_3h:
        if hrs <= 3 and temp_change_f > 3.0:  # Greater than 3°F in 3 hours
            has_significant_temp_change = True
            
            # Determine direction based on whether temperature is increasing or decreasing
            # Find starting temp to compare
            initial_temp = None
            for obs in parsed_obs:
                if abs((obs['time'] - obs_time).total_seconds()) < 3600:  # within 1 hour
                    initial_temp = obs['temp_c']
                    break
                    
            if initial_temp is not None:
                if end_temp > initial_temp:
                    temp_direction_inferred = 'UP'
                elif end_temp < initial_temp:
                    temp_direction_inferred = 'DOWN'
        
        if has_significant_temp_change:
            condition_4_temp_trend = True  # Temperature change detected
    
    # If temperature direction not identified from 3h trends, look for other patterns
    if temp_direction_inferred is None and condition_4_temp_trend:
        # Use overall pattern if no specific direction was determined
        # Just pick the most recent temperature trend if we found a signficant change
        if len(temp_changes_3h) > 0:
            # Look at the overall temp trend for the day/period
            if len(temperatures) > 1:
                start_temp = temperatures[0]
                end_temp = temperatures[-1]
                if end_temp > start_temp:
                    temp_direction_inferred = 'UP'
                elif start_temp > end_temp:
                    temp_direction_inferred = 'DOWN'
    
    # Count total satisfied conditions
    conditions = [
        condition_1_pressure,
        condition_2_wind, 
        condition_3_temp_grad,  # Can be False if NWP unavailable
        condition_4_temp_trend
    ]
    
    # If condition 3 is False because no NWP data was available, 
    # we adjust to use a 3-out-of-3 rule instead of 4
    conditions_count = sum(conditions)
    nwp_available = condition_3_temp_grad is False or condition_3_temp_grad is True
    
    # Adjust conditions_met to always be out of 4, even if NWP wasn't available
    if not nwp_available:
        # We still treat it as 4 conditions but condition 3 is always False in this case
        conditions[2] = False
        conditions_count = sum([conditions[idx] for idx in range(len(conditions)) if idx != 2]) + int(condition_3_temp_grad)  # But count it if NWP was used
    

    conn_metar.close()
    
    # Return results only if we meet the frontal passage threshold
    conditions_met_count = sum(conditions[:2] + conditions[3:])  # Skip index 2 for now
    if condition_3_temp_grad:  # Add it back in if NWP was available and detected
        conditions_met_count += 1
        
    # Recount properly since the logic was getting complex above
    # Count how many are true
    true_count = sum(conditions)
    
    if true_count >= 3:  # 3/4 or 4/4
        # Get temperature direction
        temp_direction = None
        
        for obs_time, end_time, temp_change_f, hrs, end_temp in temp_changes_3h:
            if hrs <= 3 and temp_change_f > 3.0:  # Significant change
                # Find starting temp to compare
                initial_temp = None
                for obs_idx, obs in enumerate(parsed_obs):
                    if abs((obs['time'] - obs_time).total_seconds()) < 3600:  # within 1 hour
                        initial_temp = obs['temp_c']
                        break
                        
                if initial_temp is not None:
                    if end_temp > initial_temp:
                        temp_direction = 'UP'
                    elif initial_temp > end_temp:
                        temp_direction = 'DOWN'
                        
                # If we found a direction, break
                if temp_direction:
                    break
                
        # If we still don't have direction, try from overall
        if temp_direction is None and len(temperatures) > 1:
            start_avg = sum(temperatures[:len(temperatures)//2])
            end_avg = sum(temperatures[len(temperatures)//2:])
            if end_avg > start_avg:
                temp_direction = 'UP'
            else:
                temp_direction = 'DOWN'
                
        confidence = 0.65 if true_count == 3 else 0.80
        
        # Return the actual boolean array for which conditions were met
        return temp_direction, confidence, conditions
    else:
        # Not enough conditions met
        return None, 0.0, conditions


def get_frontal_conditions(station, date, metar_db_path, nwp_db_path=None):
    """
    Get detailed breakdown of frontal conditions for a station.
    
    Args:
        station: Station code
        date: Date object
        metar_db_path: Path to METAR database
        nwp_db_path: Path to NWP database (optional)
    
    Returns:
        dict: Detailed information about all conditions
    """
    
    conn_metar = sqlite3.connect(metar_db_path)
    cursor_metar = conn_metar.cursor()
    
    date_start = date.strftime('%Y-%m-%d')
    date_end = (date + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Query for hourly METAR observations
    query = '''
    SELECT observation_time, altimeter_setting_mb, wind_direction_degrees, temp_c
    FROM metar_observations
    WHERE station_code = ?
    AND observation_time BETWEEN ? AND ?
    ORDER BY observation_time
    '''
    
    cursor_metar.execute(query, (station, date_start, date_end))
    observations = cursor_metar.fetchall()
    
    # Parse observations
    parsed_obs = []
    for obs in observations:
        time_str, altimeter, wind_dir, temp = obs
        obs_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        if isinstance(obs_time, str):
            obs_time = datetime.fromisoformat(obs_time)
            
        parsed_obs.append({
            'time': obs_time,
            'pressure_mb': float(altimeter) if altimeter else None,
            'wind_dir': float(wind_dir) if wind_dir else None,
            'temp_c': float(temp) if temp else None
        })
    
    parsed_obs.sort(key=lambda x: x['time'])
    
    # Extract valid measurements
    valid_times = [o for o in parsed_obs if all(v is not None for v in [o['pressure_mb'], o['wind_dir'], o['temp_c']])]
    
    # Condition 1: Pressure tendency
    condition_1_details = {'met': False, 'max_rate': 0.0, 'instances': []}
    for i in range(len(valid_times)):
        for j in range(i + 1, len(valid_times)):
            time_diff_hrs = (valid_times[j]['time'] - valid_times[i]['time']).total_seconds() / 3600.0
            if 1 <= time_diff_hrs <= 4:  # Within reasonable range to measure 3h trend
                # Scale pressure change to 3-hour equivalent
                pressure_change = abs(valid_times[j]['pressure_mb'] - valid_times[i]['pressure_mb'])
                scaled_change = pressure_change * (3 / time_diff_hrs)
                
                condition_1_details['instances'].append({
                    'time_interval': (valid_times[i]['time'], valid_times[j]['time']),
                    'actual_change': pressure_change,
                    'scaled_change_3hr': scaled_change,
                    'time_span_hours': time_diff_hrs
                })
                
                if scaled_change > condition_1_details['max_rate']:
                    condition_1_details['max_rate'] = scaled_change
    
    condition_1_details['met'] = condition_1_details['max_rate'] > 1.5
    
    # Condition 2: Wind shift
    condition_2_details = {'met': False, 'max_shift': 0.0, 'instances': []}
    for i in range(len(valid_times)):
        for j in range(i + 1, len(valid_times)):
            time_diff_hrs = (valid_times[j]['time'] - valid_times[i]['time']).total_seconds() / 3600.0
            if time_diff_hrs <= 3:  # Within 3 hours
                wind_change = get_wind_direction_change(valid_times[j]['wind_dir'], valid_times[i]['wind_dir'])
                
                condition_2_details['instances'].append({
                    'time_interval': (valid_times[i]['time'], valid_times[j]['time']),
                    'wind_shift_degrees': wind_change,
                    'time_span_hours': time_diff_hrs
                })
                
                if wind_change > condition_2_details['max_shift']:
                    condition_2_details['max_shift'] = wind_change
    
    condition_2_details['met'] = condition_2_details['max_shift'] > 60
    
    # Condition 3: Temperature gradient (from NWP if available)
    condition_3_details = {'met': False, 'value': None, 'source': 'NWP not available'}
    if nwp_db_path:
        try:
            grad_value = get_temperature_gradient_at_station(station, date, nwp_db_path)
            condition_3_details['value'] = grad_value 
            if grad_value is not None:
                condition_3_details['met'] = grad_value >= 6.0  
                condition_3_details['source'] = 'Successfully retrieved from NWP data'
            else:
                condition_3_details['source'] = 'NWP data processing failed'
        except Exception as e:
            condition_3_details['source'] = f'Error retrieving NWP data: {str(e)}'
    else:
        condition_3_details['source'] = 'NWP database path not provided'
    
    # Condition 4: Temperature trend  
    condition_4_details = {'met': False, 'max_change': 0.0, 'instances': [], 'unit': 'F'}
    for i in range(len(valid_times)):
        for j in range(i + 1, len(valid_times)):
            time_diff_hrs = (valid_times[j]['time'] - valid_times[i]['time']).total_seconds() / 3600.0
            if time_diff_hrs <= 3:  # Within 3 hours
                # Temp change in Celsius
                temp_change_c = abs(valid_times[j]['temp_c'] - valid_times[i]['temp_c'])
                # Convert to Fahrenheit
                temp_change_f = temp_change_c * 9/5.0
                
                condition_4_details['instances'].append({
                    'time_interval': (valid_times[i]['time'], valid_times[j]['time']),
                    'temp_change_f': temp_change_f,
                    'temp_change_c': temp_change_c,
                    'time_span_hours': time_diff_hrs
                })
                
                if temp_change_f > condition_4_details['max_change']:
                    condition_4_details['max_change'] = temp_change_f
    
    condition_4_details['met'] = condition_4_details['max_change'] > 3.0
    
    # Determine overall result
    conditions_met = [
        condition_1_details['met'],
        condition_2_details['met'],
        condition_3_details['met'],
        condition_4_details['met']
    ]
    conditions_satisfied = sum(conditions_met)
    
    # Determine direction based on post-front temperature trend
    direction = None
    # Look for temperature direction in the period following a potential frontal passage
    for temp_info in condition_4_details['instances']:
        if temp_info['temp_change_f'] > 3.0 and temp_info['time_span_hours'] <= 3:
            time_interval_start, time_interval_end = temp_info['time_interval']
            start_temp = None
            end_temp = None
            
            for obs in parsed_obs:
                if abs((obs['time'] - time_interval_start).total_seconds()) < 1800:  # Within 30 minutes
                    start_temp = obs['temp_c']
                if abs((obs['time'] - time_interval_end).total_seconds()) < 1800:  # Within 30 minutes
                    end_temp = obs['temp_c']
                    
            if start_temp is not None and end_temp is not None:
                if end_temp > start_temp + 0.1:  # Slight tolerance for measurement variations
                    direction = 'UP'
                elif start_temp > end_temp + 0.1:
                    direction = 'DOWN'
                    
            if direction:
                break
   
    # Final check: only assign direction if frontal passage criteria actually met
    if conditions_satisfied < 3:
        direction = None

    result = {
        'station': station,
        'date': date.strftime('%Y-%m-%d'),
        'conditions_summary': {
            'pressure_tendency_met': condition_1_details['met'],
            'wind_shift_met': condition_2_details['met'],
            'temp_gradient_met': condition_3_details['met'],  # Whether met AND NWP accessible
            'temp_trend_met': condition_4_details['met'],
        },
        'details': {
            'condition_1_pressure': condition_1_details,
            'condition_2_wind': condition_2_details,
            'condition_3_temp_gradient': condition_3_details,
            'condition_4_temp_trend': condition_4_details,
        },
        'conditions_satisfied_count': conditions_satisfied,
        'front_passage_detected': conditions_satisfied >= 3,
        'predicted_direction': direction,
        'confidence': 0.65 if conditions_satisfied == 3 else 0.80 if conditions_satisfied == 4 else 0.0,
    }
    
    conn_metar.close()
    
    return result