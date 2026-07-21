"""
Intraday METAR Confirmation Signal

This signal uses same-day METAR observations (10 AM, 1 PM local time) 
to confirm or weaken the ensemble's prediction. It's a confirmation 
signal, not a standalone prediction — it adjusts confidence based on 
whether the actual temperature is tracking toward the predicted direction.
"""
from datetime import datetime, timedelta
import sqlite3
from typing import Optional, Tuple, Dict, Any


class IntradayMETARConfirmation:
    """Intraday confirmation signal based on temperature trends."""
    
    def __init__(self, config: Dict[str, Any]):
        self.enabled = config.get('INTRADAY_CONFIRMATION_ENABLED', True)
        self.confirm_boost = config.get('CONFIRM_BOOST', 0.15)  # 15% confidence boost on confirmation
        self.weaken_penalty = config.get('WEAKEN_PENALTY', 0.25)  # 25% confidence reduction on weakening
        
    def _fetch_current_day_temperature_data(self, station: str, target_date: str, db_path: str) -> list:
        """
        Fetch all temperature observations for the current day 
        (14:00-20:00 UTC = 10AM-4PM Eastern)
        """
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Convert target_date to UTC date range
        obs_start_time = f"{target_date} 14:00:00"
        obs_end_time = f"{target_date} 20:00:00"
        
        query = """
            SELECT observation_time, temp_f 
            FROM metar_observations 
            WHERE station = ? 
            AND observation_time >= ? 
            AND observation_time <= ?
            AND temp_f IS NOT NULL
            ORDER BY observation_time ASC
        """
        cursor.execute(query, (station, obs_start_time, obs_end_time))
        results = cursor.fetchall()
        
        conn.close()
        return results

    def _compute_temperature_trend(self, temp_data: list) -> Tuple[float, float, str]:
        """
        Compute temperature trend from first to latest observation
        Return: (trend_rate_f_per_hour, elapsed_hours, reason_summary)
        """
        if len(temp_data) < 2:
            return 0.0, 0.0, "Insufficient observations for trend analysis"
            
        # Sort by time to ensure correct first/last
        sorted_temps = sorted(temp_data, key=lambda x: x[0])
        first_time_str, first_temp = sorted_temps[0]
        last_time_str, latest_temp = sorted_temps[-1]
        
        # Calculate time difference
        first_time = datetime.fromisoformat(first_time_str.replace("Z", "+00:00")) if "Z" in first_time_str else datetime.fromisoformat(first_time_str)
        last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00")) if "Z" in last_time_str else datetime.fromisoformat(last_time_str)
        
        hours_elapsed = (last_time - first_time).total_seconds() / 3600.0
        
        if hours_elapsed <= 0:
            return 0.0, 0.0, "No time progression in available data"
            
        temp_change = latest_temp - first_temp
        rate_per_hour = temp_change / hours_elapsed
        
        reason = f"Rate: {rate_per_hour:.2f}°F/hour ({hours_elapsed:.1f}h)"
        return rate_per_hour, hours_elapsed, reason

    def _fetch_yesterdays_high(self, station: str, target_date: str, db_path: str) -> Optional[float]:
        """
        Fetch yesterday's high temperature for comparison with current temperatures
        """
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Calculate yesterday's date
        target_dt = datetime.fromisoformat(target_date)
        yesterday = (target_dt - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # Get max temp for yesterday
        query = """
            SELECT MAX(temp_f)
            FROM metar_observations 
            WHERE station = ? 
            AND DATE(observation_time) = ?
            AND temp_f IS NOT NULL
        """
        cursor.execute(query, (station, yesterday))
        result = cursor.fetchone()
        
        conn.close()
        return result[0] if result[0] is not None else None

    def get_intraday_confirmation(
        self, 
        station: str, 
        date: str,
        metar_db_path: str, 
        predicted_direction: str,  # 'UP' or 'DOWN'
        base_confidence: float
    ) -> Optional[Tuple[float, str, dict]]:
        """
        Compute intraday METAR confirmation
        
        Args:
            station: Station code ('KJFK', etc.)
            date: Target prediction date in 'YYYY-MM-DD' format
            metar_db_path: Path to metar_backfill.db
            predicted_direction: 'UP' if predicting higher temperature, 'DOWN' if lower
            base_confidence: Starting confidence level
            
        Returns:
            (adjusted_confidence, reason, details) if within timeframe for processing, 
            None otherwise
        """
        if not self.enabled:
            return None
            
        # Only process if the date is today or yesterday (same-day METAR data availability)
        target_dt = datetime.fromisoformat(date)
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        
        if target_dt.date() != today and target_dt.date() != yesterday:
            return None  # Only for recent dates where same-day data is available
            
        # Get current METAR observations for the target period
        temp_data = self._fetch_current_day_temperature_data(station, date, metar_db_path)
        
        # Initialize default result
        adjusted_confidence = base_confidence
        reason = "No significant confirmation pattern detected"
        details = {
            'trend_computed': False,
            'temp_data_points': len(temp_data),
            'base_confidence': base_confidence
        }
        
        # Analyze temperature trend if we have enough data
        if len(temp_data) >= 2:
            rate_per_hour, elapsed_hours, trend_reason = self._compute_temperature_trend(temp_data)
            
            details['trend_rate'] = rate_per_hour
            details['elapsed_hours'] = elapsed_hours
            details['trend_reason'] = trend_reason
            details['trend_computed'] = True
            details['first_obs'] = temp_data[0]
            details['last_obs'] = temp_data[-1]
            
            # Compare with ensemble prediction direction
            expected_positive_trend = predicted_direction == 'UP'
            actual_positive_trend = rate_per_hour > 0
            
            if expected_positive_trend and actual_positive_trend:
                # Ensemble said UP, and we're trending up
                if rate_per_hour > 0.5:
                    # Strong confirmation trend
                    adjusted_confidence = base_confidence * (1 + self.confirm_boost)
                    reason = "Strong confirmation of upward trend detected"
                    details['confirmation_type'] = 'confirm_strong'
                elif abs(rate_per_hour) < 0.2:
                    # Flat trend - neutral
                    reason = "Flat temperature trend - no strong confirmation"
                    details['confirmation_type'] = 'neutral'
                else:
                    # Weak confirmation - positive but slow
                    adjusted_confidence = base_confidence * (1 + self.confirm_boost * 0.5)  # Lesser boost
                    reason = "Weak upward trend detected"
                    details['confirmation_type'] = 'confirm_weak'
            elif expected_positive_trend and not actual_positive_trend:
                # Ensemble said UP, but we're trending down - weaken
                adjusted_confidence = max(0.01, base_confidence * (1 - self.weaken_penalty))
                reason = "Contrary trend identified - ensemble prediction contradicted by observations"
                details['confirmation_type'] = 'weaken'
            elif not expected_positive_trend and actual_positive_trend:
                # Ensemble said DOWN, but temperature going up - counter trend - weaken
                adjusted_confidence = max(0.01, base_confidence * (1 - self.weaken_penalty))
                reason = "Counter trend identified - ensemble prediction contradicted by observations"
                details['confirmation_type'] = 'weaken_negative'
            else:
                # Ensemble said DOWN, temperature going down or is neutral
                if rate_per_hour < -0.5:
                    # Strong downward confirmation of DOWN prediction
                    adjusted_confidence = base_confidence * (1 + self.confirm_boost)
                    reason = "Strong confirmation of downward trend detected"
                    details['confirmation_type'] = 'confirm_downward'
                elif abs(rate_per_hour) < 0.2:
                    # Neutral trend for down prediction
                    reason = "Flat temperature trend - no strong disconfirmation for DOWN prediction"
                    details['confirmation_type'] = 'neutral'
                else:
                    # Upward drift for DOWN prediction - weaken
                    adjusted_confidence = max(0.01, base_confidence * (1 - self.weaken_penalty))
                    reason = "Temperature trending upward despite DOWN prediction"
                    details['confirmation_type'] = 'weaken_due_to_upward_drift'
        
        # Additional check: compare current temp to yesterday's high
        if temp_data:
            current_time = datetime.now()
            current_utc_hour = current_time.hour + current_time.minute / 60.0  # Include minutes
            
            current_temp = temp_data[-1][1]  # Latest temperature
            yesterdays_high = self._fetch_yesterdays_high(station, date, metar_db_path)
            
            details['current_temp'] = current_temp
            details['yesterday_high'] = yesterdays_high
            
            if yesterdays_high is not None:
                if current_temp > yesterdays_high and current_utc_hour < 18:  # Before 2PM EST
                    adjusted_confidence = min(1.0, adjusted_confidence * (1 + 0.10))  # Boost by 10%
                    if reason == "No significant confirmation pattern detected":
                        reason = "Current temp already exceeded yesterday's high - early sign of good tracking"
                    else:
                        reason += "; Current temp exceeds yesterday's high early in day"
                    details['early_exceed_check'] = 'positive_tracking'
                    
                elif current_temp < yesterdays_high and current_utc_hour > 20:  # After 4PM EST
                    adjusted_confidence = max(0.01, adjusted_confidence * 0.85)  # Penatly of 15%
                    if reason == "No significant confirmation pattern detected":
                        reason = "Late in day with temp still below yesterday's high - poor tracking"
                    else:
                        reason += "; Late in day with temp below yesterday's high - poor tracking"
                    details['late_track_check'] = 'poor_tracking'
        
        details['final_confidence'] = adjusted_confidence
        return adjusted_confidence, reason, details


# Convenience function for direct import/usage
def get_intraday_confirmation(station, date, metar_db_path, predicted_direction, base_confidence):
    """
    Convenience function for the intraday METAR confirmation signal
    
    Returns: (adjusted_confidence, reason, details) or None if not applicable
    """
    # Load config - minimal config for this signal only
    config = {
        'INTRADAY_CONFIRMATION_ENABLED': True,
        'CONFIRM_BOOST': 0.15,
        'WEAKEN_PENALTY': 0.25
    }
    
    signal = IntradayMETARConfirmation(config)
    return signal.get_intraday_confirmation(
        station, date, metar_db_path, predicted_direction, base_confidence
    )