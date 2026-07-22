import math
from datetime import datetime, timedelta
from ..sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection


def modulate_confidence(station, date, metar_db_path, confidence):
    """
    Apply dewpoint depression-based confidence modulation.
    
    Dewpoint depression (DPD = temperature - dewpoint) indicates humidity level:
    - High DPD (>15°F) suggests clear skies → confidence boost 
    - Low DPD (<5°F) suggests cloudy conditions → confidence penalty
    - Moderate DPD (5-15°F) → no change
    
    Args:
        station (str): Station identifier
        date (datetime): Target date for observation
        metar_db_path (str): Path to METAR database
        confidence (float): Original confidence value
        
    Returns:
        float: Modulated confidence value
    """
    # Get most recent METAR observation within last 6 hours
    dpd_value = _get_most_recent_dpd(station, date, metar_db_path)
    
    if dpd_value is None:
        # If no data available, return original confidence
        return confidence
    
    # Apply modulation based on DPD value
    if dpd_value > 15.0:  # Clear conditions
        modulated_confidence = confidence * 1.2
        # Cap at 0.95 to maintain sanity
        return min(modulated_confidence, 0.95)
    elif dpd_value < 5.0:  # Cloudy conditions
        modulated_confidence = confidence * 0.8
        return modulated_confidence
    else:  # Moderate conditions - no change
        return confidence


def _get_most_recent_dpd(station, target_date, metar_db_path):
    """
    Retrieve the most recent dewpoint depression within 6 hours of target date.
    
    Args:
        station (str): Station identifier
        target_date (datetime): Target date
        metar_db_path (str): Path to METAR database
        
    Returns:
        float or None: Dewpoint depression value (temp - dewpoint), or None if insufficient data
    """
    # Define time range: 6 hours before target date to target date
    start_time = target_date - timedelta(hours=6)
    
    try:
        conn = get_sqlite_connection(metar_db_path)
        cursor = conn.cursor()
        
        # Query for most recent valid METAR record from given station within time range
        query = """
            SELECT temperature_f, dewpoint_f 
            FROM observations 
            WHERE station_id = ? 
                AND observation_time BETWEEN ? AND ?
                AND temperature_f IS NOT NULL 
                AND dewpoint_f IS NOT NULL
            ORDER BY observation_time DESC 
            LIMIT 1
        """
        
        cursor.execute(
            query, 
            (station, start_time.replace(tzinfo=None), target_date.replace(tzinfo=None))
        )
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            temp_f, dewpoint_f = result
            if temp_f is not None and dewpoint_f is not None:
                dpd = temp_f - dewpoint_f
                return dpd
                
    except Exception as e:
        print(f"Warning: Error retrieving DPD for station {station}: {e}")
        pass
    
    # Return None if no valid data found
    return None