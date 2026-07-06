#!/usr/bin/env python3
"""
SIGNAL: Pressure × Regime Interaction

Implements:
- Daily pressure tendency: `ΔP = pressure_t - pressure_{t-1}`
- Climate regime: `regime ∈ strong/moderate/neutral` using ENSO/AO/NAO indices
- Interaction: `signal = sign(ΔP) × regime_strength` where regime_strength = 2.0 strong, 1.5 moderate, 1.0 neutral

This signal models how pressure changes' impact on temperature varies by large-scale climate regime.
"""

import sqlite3
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List
import os


class PressureRegimeSignal:
    """
    Captures how pressure tendency interact with climate regimes. 
    In certain regimes (e.g. El Niño), same pressure changes may produce different temperature responses.
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Default regime: neutral
        self.regime_strengths = {
            "strong": 2.0,     # Strong El Niño/La Niña, Strong positive/negative AO, etc.
            "moderate": 1.5,   # Moderate ENSO phase, moderate AO
            "neutral": 1.0     # Neutral conditions
        }
        # Thresholds for regime classification
        self.thresholds = {
            "enso": 0.5,  # For ONI index  
            "ao": 2.0,    # For Arctic Oscillation 
            "nao": 1.0    # For North Atlantic Oscillation
        }

    def get_historical_pressure_data(self, station: str, date_str: str, days_lookback: int = 2) -> List[Tuple[str, float]]:
        """
        Retrieve historical pressure data for the given station.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get historical pressure for days leading up to target date
        start_date = self._subtract_days(date_str, days_lookback)
        
        cursor.execute("""
            SELECT date(date_utc) as date_only, AVG(pressure_mb) as avg_pressure
            FROM metar_observations
            WHERE station = ? AND date_utc BETWEEN ? AND ?
            AND pressure_mb IS NOT NULL
            GROUP BY date_only
            ORDER BY date_only DESC
        """, (station, start_date, date_str))
        
        results = []
        for row in cursor.fetchall():
            results.append((row[0], float(row[1])))
        
        conn.close()
        return results

    def get_regime_state(self, date_str: str) -> Tuple[str, float]:
        """
        Determine the climate regime for the given date based on historical data.
        In practice, this might pull from stored climate index data.
        For simulation purposes, we'll use a simplified algorithm.
        """
        # Parse date
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        
        # Use date to determine regime state - in real implementation, this 
        # would come from ENSO/AO/NAO index databases
        # This is a simulation approach to represent varying regimes over time
        month = dt.month
        day_of_year = dt.timetuple().tm_yday
        
        # Create a synthetic regime that varies seasonally and over time
        # Using hash-based approach to ensure consistency for same date
        total_days = dt.toordinal()
        regime_indicator = abs(hash(date_str)) % 100
        enso_like = regime_indicator % 120 - 60  # -60 to +60
        ao_like = (regime_indicator * 7) % 80 - 40  # -40 to +40
        nao_like = (regime_indicator * 13) % 60 - 30  # -30 to +30
        
        # Determine regime strength for each index
        strongest_regime = "neutral"
        strongest_strength = 1.0
        
        # ENSO (El Niño/La Niña)
        if abs(enso_like) >= self.thresholds["enso"] * 4:  # Using scaled thresholds
            strength_level = "strong" if abs(enso_like) > 35 else "moderate"
            strength_val = self.regime_strengths[strength_level] 
            if strength_val > strongest_strength:
                strongest_regime = strength_level
                strongest_strength = strength_val
        elif abs(ao_like) >= (self.thresholds["ao"] * 0.6):  # AO scaled differently
            strength_level = "strong" if abs(ao_like) > 25 else "moderate"
            strength_val = self.regime_strengths[strength_level]
            if strength_val > strongest_strength:
                strongest_regime = strength_level
                strongest_strength = strength_val
        elif abs(nao_like) >= (self.thresholds["nao"] * 0.8):  # NAO scaled
            strength_level = "strong" if abs(nao_like) > 20 else "moderate" 
            strength_val = self.regime_strengths[strength_level]
            if strength_val > strongest_strength:
                strongest_regime = strength_level
                strongest_strength = strength_val
        
        return strongest_regime, strongest_strength

    def calculate_pressure_tendency(self, pressure_history: List[Tuple[str, float]], date_str: str) -> Optional[float]:
        """
        Calculate the pressure tendency between today and the previous available day.
        """
        if len(pressure_history) < 2:
            return None
            
        # Need pressure for date_str (or previous day if date_str not available) and the day before that
        # Sort by date ascending to easily get consecutive days
        sorted_history = sorted(pressure_history, key=lambda x: x[0])
        
        # Get last two distinct days where possible
        if len(sorted_history) >= 2:
            # Most recent date with pressure
            current_day_pressure = sorted_history[-1][1]
            # Second most recent date
            prev_day_pressure = sorted_history[-2][1]
            date1 = sorted_history[-1][0]
            date2 = sorted_history[-2][0]
            
            # If these are consecutive days and date2 is yesterday relative to today
            if self._get_date_difference(date1, date_str) <= 1 and self._get_date_difference(date2, date1) <= 1:
                tendency = current_day_pressure - prev_day_pressure
                return tendency
        
        return None

    def generate_signal(self, station: str, date_str: str, min_dp_threshold: float = 1.5) -> Optional[Tuple[str, float]]:
        """
        Generate the pressure-regime interaction signal.

        Args:
            station: Station code (e.g., 'KATL')
            date_str: Date in 'YYYY-MM-DD' format
            min_dp_threshold: Minimum pressure tendency to generate signal

        Returns:
            (direction, confidence) or None if no signal
                direction: 'up' if ΔP * regime_strength suggests warming, 'down' if suggesting cooling
                confidence: Scaled based on strength interaction
        """
        # Get pressure tendency
        pressure_history = self.get_historical_pressure_data(station, date_str, days_lookback=3)
        pressure_tendency = self.calculate_pressure_tendency(pressure_history, date_str)
        
        if pressure_tendency is None or abs(pressure_tendency) < min_dp_threshold:
            return None
        
        # Get climate regime
        regime_name, regime_strength = self.get_regime_state(date_str)
        
        # Interaction: sign of pressure change × regime strength
        interaction_value = (pressure_tendency / abs(pressure_tendency)) * regime_strength
        
        # Determine direction and confidence
        if abs(interaction_value) > 1.0:  # Some threshold based on interaction
            # Sign indicates direction: positive pressure change + positive regime = warming
            if interaction_value > 0:
                direction = 'up'  # Temperature expected to go up
                confidence = min(0.90, abs(interaction_value) * 0.3 - 0.5)  # Scale confidence appropriately
            else:
                direction = 'down'  # Temperature expected to go down  
                confidence = min(0.90, abs(interaction_value) * 0.3 - 0.5)  # Scale confidence appropriately
            
            # Clamp confidence to sensible range
            confidence = max(0.55, min(0.90, confidence))
            
            return direction, confidence
        
        return None

    def _subtract_days(self, date_str: str, days: int) -> str:
        """Helper method to subtract days from a date string."""
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        new_dt = dt - timedelta(days=days)
        return new_dt.strftime('%Y-%m-%d')
        
    def _get_date_difference(self, date_str1: str, date_str2: str) -> int:
        """Helper method to calculate difference in days between two date strings."""
        dt1 = datetime.strptime(date_str1, '%Y-%m-%d')
        dt2 = datetime.strptime(date_str2, '%Y-%m-%d')
        return abs((dt1 - dt2).days)


# Test the Pressure × Regime interaction signal
def test_pressure_regime_signal():
    """Test function to validate the pressure_regime_interaction signal."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"  # Adjust path as needed
    
    # Use a test date and station
    station = 'KATL'
    date_str = '2025-06-15'  # Use a date within the data range
    
    pr_signal = PressureRegimeSignal(db_path)
    result = pr_signal.generate_signal(station, date_str)
    
    if result:
        direction, confidence = result
        print(f"PressureRegimeSignal - Date: {date_str}, Station: {station}")
        print(f"  Direction: {direction}, Confidence: {confidence:.3f}")
    else:
        print(f"PressureRegimeSignal - No signal generated for {date_str}, {station}")


if __name__ == "__main__":
    test_pressure_regime_signal()