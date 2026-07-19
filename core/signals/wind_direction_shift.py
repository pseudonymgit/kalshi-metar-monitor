#!/usr/bin/env python3
"""
SIGNAL: Wind Direction Shift

Detects significant circular differences in wind direction with moderate wind speeds (>10kt).
Based on the principle that changing wind directions can indicate approaching weather fronts
and thus temperature changes.

Implements:
- Circular difference Δθ > 45° 
- Wind speed > 10kt threshold
- Temperature implications based on compass direction (North = colder air, South = warmer air)
"""

import sqlite3
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
import os

from .base_signal import BaseSignal, validate_signal


class WindDirectionShiftSignal(BaseSignal):
    """
    Wind Direction Shift Signal
    Detects significant shifts in wind direction which can precede temperature changes.
    """
    
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        # Parameters for signal detection
        self.angle_threshold = 45.0  # Degrees
        self.wind_speed_threshold = 10.0  # knots
        self.lookback_days = 3  # days to look for wind pattern changes

    @property
    def name(self) -> str:
        return "wind_direction_shift"

    @property
    def min_lookback(self) -> int:
        return self.lookback_days


    def _calculate_circular_difference(self, angle1: float, angle2: float) -> float:
        """
        Calculate the smallest angle difference between two angles (0-360°).
        
        Args:
            angle1: First angle in degrees
            angle2: Second angle in degrees
            
        Returns:
            Circular difference between angles (0-180°)
        """
        diff = abs(angle1 - angle2)
        return min(diff, 360 - diff)


    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string to datetime object."""
        return datetime.strptime(date_str, '%Y-%m-%d')


    def get_historical_wind_data(self, station: str, date_str: str, days_lookback: int = 3) -> List[Tuple[str, float, float]]:
        """
        Retrieve historical wind direction and speed for the given station.
        Returns list of (date, wind_direction_degrees, wind_speed_kt) tuples.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        start_date = self._subtract_days(date_str, days_lookback)
        
        cursor.execute("""
            SELECT date(date_utc) as date_only, 
                   AVG(wind_direction_deg) as avg_direction, 
                   AVG(wind_speed_kt) as avg_speed
            FROM metar_observations
            WHERE station = ? 
            AND date_utc BETWEEN ? AND ?
            AND wind_direction_deg IS NOT NULL 
            AND wind_speed_kt IS NOT NULL
            GROUP BY date_only
            ORDER BY date_only DESC
        """, (station, start_date, date_str))
        
        results = []
        for row in cursor.fetchall():
            date, direction, speed = row
            if direction is not None and speed is not None:
                results.append((date, float(direction), float(speed)))
        
        conn.close()
        return results


    def detect_wind_shift(self, wind_history: List[Tuple[str, float, float]]) -> Optional[Tuple[float, float, float, float]]:
        """
        Analyzes wind history for significant direction shifts with adequate wind speeds.
        
        Args:
            wind_history: List of (date, direction_deg, speed_kt) in reverse chronological order
            
        Returns:
            (shift_angle, avg_recent_wind_speed, old_direction, new_direction) or None if no significant shift
        """
        if len(wind_history) < 2:
            return None
        
        # Analyze the most recent vs the day before
        # Note: Data is in reverse chronological order (most recent first)
        if len(wind_history) >= 2:
            old_direction = wind_history[1][1]
            new_direction = wind_history[0][1] 
            new_speed = wind_history[0][2]
            old_speed = wind_history[1][2]
            
            # Calculate circular difference 
            angle_diff = self._calculate_circular_difference(old_direction, new_direction)
            
            # Check if both new and old speeds exceed threshold AND angle is significant
            avg_speed = (new_speed + old_speed) / 2.0 if new_speed and old_speed else max(new_speed or 0, old_speed or 0)
            
            if angle_diff > self.angle_threshold and avg_speed > self.wind_speed_threshold:
                return (angle_diff, avg_speed, old_direction, new_direction)
        
        # Check for longer term shifts as well
        if len(wind_history) >= 3:
            # Compare first (oldest) with last (newest) - this might detect slower frontal passages
            old_direction = wind_history[-1][1]
            new_direction = wind_history[0][1]
            new_speed = wind_history[0][2]
            
            angle_diff = self._calculate_circular_difference(old_direction, new_direction)
            
            if angle_diff > self.angle_threshold and new_speed > self.wind_speed_threshold:
                return (angle_diff, new_speed, old_direction, new_direction)
        
        return None


    def infer_temperature_implication(self, old_direction: float, new_direction: float, angle_shift: float) -> Tuple[str, float]:
        """
        Based on wind direction changes, infer temperature trend.
        
        Nomenclature:
        - North winds (270°-360°, 0°-45°) typically bring cooler/colder air in Northern Hemisphere
        - South winds (135°-225°) typically bring warmer air in Northern Hemisphere
        """
        # Calculate change in compass bearing to assess temperature implications
        # Simplified: N winds = colder air masses, S winds = warmer
        # East/West winds depend on regional specifics, but we'll use general direction
        
        # Get wind rose sectors
        old_sector = self._direction_to_sector(old_direction)
        new_sector = self._direction_to_sector(new_direction)
        
        # Temperature trend inference
        # If shift towards north (prev = S, now = N/W), likely cooling
        # If shift towards south (prev = N, now = S/E), likely warming
        
        # Calculate compass angle change and its temperature implication
        # Simplify assumption: shift towards North = cooling, S = warming
        angle_diff_norm = new_direction - old_direction if new_direction >= old_direction else new_direction + 360 - old_direction
        
        # Normalize to see if it's more northward or southward dominant change
        # From old direction perspective:
        # If angle change is moving towards north pole direction, cooling
        # If moving away from N (southward), warming
        
        # More direct approach: determine what the new wind source is
        avg_direction = (old_direction + new_direction) / 2.0 % 360.0
        
        # Simple temperature direction based on compass direction
        # 180-360 = southerly winds bringing warmth (positive) 0-90/270-359 - northerly winds bringing cold
        # So SSW to NE shift = transition through southerly direction = warming
        # NNE to SW shift = transition through northerly direction = cooling
        
        # For general rule based on wind direction change:
        if self._is_predominantly_northerly(new_direction):
            # Now has more northerly component = likely cooling temp trend
            direction = 'down' 
            base_conf = 0.45 + min(0.25, angle_shift / 90.0)  # Higher confidence with larger shifts
        elif self._is_predominantly_southerly(new_direction):
            # Now has more southerly component = likely warming temp trend
            direction = 'up'
            base_conf = 0.45 + min(0.25, angle_shift / 90.0)
        else:
            # E/W direction more variable by region
            # If we're unsure, make less confident
            direction = 'up' if new_direction < old_direction else 'down'  # Simple guess
            base_conf = 0.35
        
        # Confidence increases with greater shift and higher wind speeds
        final_conf = min(0.80, base_conf)
        return direction, final_conf


    def _is_predominantly_northerly(self, wind_direction: float) -> bool:
        """Check if wind is primarily coming from N-S sector (0-45, 315-360)."""
        return (0 <= wind_direction < 45) or (315 <= wind_direction <= 360) # N-NE and NNW-N sectors


    def _is_predominantly_southerly(self, wind_direction: float) -> bool:
        """Check if wind is primarily coming from S-N sector (135-225)."""
        return 135 <= wind_direction <= 225  # S-SE(SE) and SW(SSW)-S sectors


    def _direction_to_sector(self, direction: float) -> str:
        """Convert direction angle to compass sector."""
        sectors = {
            (0, 22.5): 'N', (22.5, 67.5): 'NE', (67.5, 112.5): 'E', (112.5, 157.5): 'SE',
            (157.5, 202.5): 'S', (202.5, 247.5): 'SW', (247.5, 292.5): 'W', (292.5, 337.5): 'NW', (337.5, 360): 'N'
        }
        for (start, end), sector in sectors.items():
            if start <= direction < end:
                return sector
        return 'N'  # Default for boundary case


    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate signal using in-memory days list (standard interface).

        Args:
            idx: Current day index
            days: List of daily weather dicts with wind_dir, wind_speed keys

        Returns:
            (direction, confidence) or (None, 0.0) if no signal
        """
        if idx < self.lookback_days + 1:
            return None, 0.0

        # Extract wind data from in-memory days list (reverse chronological)
        wind_history = []
        for i in range(idx, idx - self.lookback_days - 1, -1):
            if i < 0:
                break
            d = days[i]
            if d.get('wind_dir') is not None and d.get('wind_speed') is not None:
                wind_history.append((d['date'], float(d['wind_dir']), float(d['wind_speed'])))

        if len(wind_history) < 2:
            return None, 0.0

        shift_result = self.detect_wind_shift(wind_history)
        if not shift_result:
            return None, 0.0

        angle_diff, avg_wind_speed, old_direction, new_direction = shift_result
        direction, confidence = self.infer_temperature_implication(old_direction, new_direction, angle_diff)

        speed_boost = min(0.2, (avg_wind_speed - self.wind_speed_threshold) / 20.0)
        angle_boost = min(0.1, (angle_diff - self.angle_threshold) / 90.0)
        total_confidence = min(0.85, confidence + speed_boost + angle_boost)
        total_confidence = max(0.50, total_confidence)

        return direction, total_confidence


    def generate_signal(self, station: str, date_str: str) -> Optional[Tuple[str, float]]:
        """
        Generate the Wind Direction Shift signal.

        Args:
            station: Station code (e.g., 'KATL')
            date_str: Date in 'YYYY-MM-DD' format

        Returns:
            (direction, confidence) or None if no signal
                direction: 'up' if wind shift suggests warming, 'down' if cooling
                confidence: Based on magnitude of wind shift and average wind speed
        """
        # Get historical wind data
        wind_history = self.get_historical_wind_data(station, date_str, days_lookback=self.lookback_days)
        
        if len(wind_history) < 2:  # Need at least 2 days to compare
            return None
        
        # Detect significant wind shifts
        shift_result = self.detect_wind_shift(wind_history)
        
        if not shift_result:
            return None
            
        angle_diff, avg_wind_speed, old_direction, new_direction = shift_result
        
        # Infer temperature implications of the wind shift
        direction, confidence = self.infer_temperature_implication(old_direction, new_direction, angle_diff)
        
        # Boost confidence based on wind speed and angle shift size
        # Higher wind speeds and bigger changes = more reliable signal
        speed_boost = min(0.2, (avg_wind_speed - self.wind_speed_threshold) / 20.0)  # Up to 0.2 boost for strong winds
        angle_boost = min(0.1, (angle_diff - self.angle_threshold) / 90.0)  # Up to 0.1 boost for large angle changes
        total_confidence = min(0.85, confidence + speed_boost + angle_boost)
        
        # Clamp confidence to [0.50, 0.85] range for this signal
        total_confidence = max(0.50, total_confidence)
        
        return direction, total_confidence


    def _subtract_days(self, date_str: str, days: int) -> str:
        """Helper method to subtract days from a date string."""
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        new_dt = dt - timedelta(days=days)
        return new_dt.strftime('%Y-%m-%d')


def test_wind_signal():
    """Test function to validate the Wind Direction Shift signal."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    # Use a test date and station
    station = 'KATL'
    date_str = '2025-06-15'  # Use a date within the data range
    
    wind_signal = WindDirectionShiftSignal(db_path)
    result = wind_signal.generate_signal(station, date_str)
    
    if result:
        direction, confidence = result
        print(f"WindDirectionShiftSignal - Date: {date_str}, Station: {station}")
        print(f"  Direction: {direction}, Confidence: {confidence:.3f}")
    else:
        print(f"WindDirectionShiftSignal - No signal generated for {date_str}, {station}")


if __name__ == "__main__":
    test_wind_signal()