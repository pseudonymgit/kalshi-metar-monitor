#!/usr/bin/env python3
"""
SIGNAL: Diurnal Temperature Range (DTR) Trend

Calculates 3-day change in diurnal temperature range (DTR = max_temp - min_temp).
- If DTR trend > 2°F → up (indicating warming trend)
- If DTR trend < -2°F → down (indicating cooling trend)

DTR captures surface energy balance and provides leading indicators of atmospheric state changes.
"""

import sqlite3
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple, List
import os


class DTRTrendSignal:
    """
    DTR (Diurnal Temperature Range) Trend Detector
    Based on 3-day change in max-min temperature difference
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Threshold for significant trend
        self.dtr_threshold = 2.0  # degrees F

    def get_historical_daily_temps(self, station: str, date_str: str, days_lookback: int = 4) -> List[Tuple[str, float, float]]:
        """
        Retrieve historical daily min/max temperatures for the given station.
        
        Returns: List of (date, max_temp, min_temp) tuples
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        start_date = self._subtract_days(date_str, days_lookback)
        
        cursor.execute("""
            SELECT date_utc, MAX(temp_f) as max_temp, MIN(temp_f) as min_temp
            FROM metar_observations
            WHERE station = ? 
            AND date_utc BETWEEN ? AND ?
            AND temp_f IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc DESC
        """, (station, start_date, date_str))
        
        results = []
        for row in cursor.fetchall():
            date_val, max_temp, min_temp = row
            if max_temp is not None and min_temp is not None:
                results.append((date_val, float(max_temp), float(min_temp)))
        
        conn.close()
        return results

    def calculate_dtr_trend(self, dtr_history: List[Tuple[str, float]], days: int = 3) -> Optional[float]:
        """
        Calculate the trend in DTR over the specified number of days.
        
        Args:
            dtr_history: List of (date, dtr_value) in reverse chronological order
            days: Number of days to look back for trend calculation
            
        Returns:
            The 3-day difference in DTR as a trend value
        """
        if len(dtr_history) < days + 1:
            return None

        # Ensure data is sorted chronologically (oldest first)
        sorted_history = sorted(dtr_history[:days+1], key=lambda x: x[0])
        
        # Calculate the change from old DTR to recent DTR
        earliest_dtr = sorted_history[0][1]  # oldest
        recent_dtr = sorted_history[-1][1]  # newest 
        
        return recent_dtr - earliest_dtr  # DTR trend

    def generate_signal(self, station: str, date_str: str) -> Optional[Tuple[str, float]]:
        """
        Generate the DTR trend signal.

        Args:
            station: Station code (e.g., 'KATL')
            date_str: Date in 'YYYY-MM-DD' format

        Returns:
            (direction, confidence) or None if no signal
                direction: 'up' if DTR trend > threshold (warming trend indicators), 'down' if <-threshold (cooling)
                confidence: Based on magnitude of trend
        """
        # Get historical daily temperatures
        daily_temps = self.get_historical_daily_temps(station, date_str, days_lookback=5)
        
        if len(daily_temps) < 4:  # Need at least 4 days to calculate trend over 3 days
            return None
            
        # Calculate DTR (Diurnal Temperature Range) for each day
        dtr_list = [(date, max_temp - min_temp) for date, max_temp, min_temp in daily_temps]
        
        # Calculate the 3-day DTR trend
        dtr_trend = self.calculate_dtr_trend(dtr_list, days=3)
        
        if dtr_trend is None or abs(dtr_trend) < self.dtr_threshold:
            return None
            
        # Determine direction and confidence based on DTR trend
        if dtr_trend > self.dtr_threshold:
            # Increasing DTR suggests changing weather pattern, increased heating potential
            direction = 'up'  # Indicates expectation of temperatures going up
            # Confidence is proportional to the strength of the trend relative to threshold
            intensity_multiplier = min(1.5, abs(dtr_trend) / self.dtr_threshold)
            confidence = 0.45 + (0.25 * intensity_multiplier)  # Starting from 45% + up to 37.5% boost
        elif dtr_trend < -self.dtr_threshold:
            # Decreasing DTR suggests decreasing amplitude, possible cooling influence
            direction = 'down'  # Indicates expectation of temperatures going down
            intensity_multiplier = min(1.5, abs(dtr_trend) / self.dtr_threshold)
            confidence = 0.45 + (0.25 * intensity_multiplier)  # Same logic for downward trend
        else:
            return None
            
        # Clamp confidence to reasonable range
        confidence = min(0.90, max(0.50, confidence))
        
        return direction, confidence

    def _subtract_days(self, date_str: str, days: int) -> str:
        """Helper method to subtract days from a date string."""
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        new_dt = dt - timedelta(days=days)
        return new_dt.strftime('%Y-%m-%d')


# Test the DTR trend signal
def test_dtr_signal():
    """Test function to validate the DTR trend signal."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    # Use a test date and station
    station = 'KATL'
    date_str = '2025-06-15'  # Use a date within the data range
    
    dtr_signal = DTRTrendSignal(db_path)
    result = dtr_signal.generate_signal(station, date_str)
    
    if result:
        direction, confidence = result
        print(f"DTRTrendSignal - Date: {date_str}, Station: {station}")
        print(f"  Direction: {direction}, Confidence: {confidence:.3f}")
    else:
        print(f"DTRTrendSignal - No signal generated for {date_str}, {station}")


if __name__ == "__main__":
    test_dtr_signal()