"""
Core module for market phase classification.
Labels each city-day with a market phase affecting position sizing.

Part of Phase 7 - Kalshi API Integration.
"""

import os
from datetime import datetime, timedelta
from typing import Tuple, Dict, Any
import pytz
from zoneinfo import ZoneInfo


def classify_market_phase(
    station: str, 
    date: str, 
    hour_utc: int,
    news_event_today: bool = False,
    low_liquidity_day: bool = False
) -> str:
    """
    Classify the current market phase based on time, events, and conditions.
    
    Args:
        station: Station identifier (e.g., 'KJFK')
        date: Trading date (YYYY-MM-DD)
        hour_utc: Hour in UTC (0-23)
        news_event_today: Whether there's a significant weather news event
        low_liquidity_day: Whether this is likely a low liquidity day (holiday/weekend)
        
    Returns:
        Phase label as string
    """
    # Parse input date
    target_date = datetime.fromisoformat(date)
    utc_time = datetime.combine(target_date.date(), datetime.min.time()).replace(tzinfo=ZoneInfo('UTC'))
    current_hour_utc = hour_utc
    
    # Get settlement time in UTC (typically noon UTC for daily markets)
    settlement_hour_utc = 12  # Daily settlements at 12:00 UTC
    
    # Check for settlement convergence phase (within 2h of settlement)
    hour_diff_to_settlement = abs(current_hour_utc - settlement_hour_utc)
    if hour_diff_to_settlement <= 2:
        return 'SETTLEMENT_CONVERGENCE'
    
    # Check for overnight thin conditions (20:00-08:00 UTC)
    if 20 <= current_hour_utc or current_hour_utc < 8:
        return 'OVERNIGHT_THIN'
    
    # Check if day is holiday or weekend (low liquidity)
    # Weekends: Saturday (5) and Sunday (6) in python weekday() 
    is_weekend = target_date.weekday() > 4
    is_monday_tuesday_after_weekend = (
        target_date.weekday() in [0, 1] and  # Monday or Tuesday
        (target_date - timedelta(days=1 if target_date.weekday() == 0 else 2)).weekday() > 4  # after weekend
    )
    
    if (is_weekend or low_liquidity_day or is_monday_tuesday_after_weekend):
        return 'HOLIDAY_WEEKEND'
    
    # Check for information events (news, forecasts, etc.)
    if news_event_today:
        return 'INFORMATION_EVENT'
    
    # Everything else is normal
    return 'NORMAL'


class MarketPhaseClassifier:
    """
    Class to maintain state and compute market phases efficiently.
    """
    
    def __init__(self):
        self.stations_timezone_map = {
            # US East Coast
            'KNYC': 'America/New_York',
            'KJFK': 'America/New_York',
            'KLGA': 'America/New_York',
            'KEWR': 'America/New_York',
            'KPHL': 'America/New_York', 
            'KPIT': 'America/New_York',
            'KBOS': 'America/New_York',
            'KORD': 'America/Chicago',
            'KMDW': 'America/Chicago',
            'KDFW': 'America/Chicago', 
            'KIAD': 'America/New_York',
            'KDCA': 'America/New_York',
            'KBWI': 'America/New_York',
            'KCLE': 'America/New_York',
            'KCIN': 'America/New_York',
            # US West Coast
            'KLAX': 'America/Los_Angeles',
            'KSFO': 'America/Los_Angeles', 
            'KOAK': 'America/Los_Angeles',
            'KSAN': 'America/Los_Angeles',
            'KSEA': 'America/Los_Angeles',
            'KPHX': 'America/Phoenix',
            'KLAS': 'America/Los_Angeles',
            'KSNA': 'America/Los_Angeles',
            # US Central
            'KDEN': 'America/Denver',
            'KIAH': 'America/Chicago', 
            'KHOU': 'America/Chicago',
            'KMCI': 'America/Chicago',
            'KSLC': 'America/Denver',
            'KOKC': 'America/Chicago',
            'KTUL': 'America/Chicago',
            # US South
            'KMIA': 'America/New_York', 
            'KFLL': 'America/New_York',
            'KMCO': 'America/New_York',
            'KTPA': 'America/New_York',
            # US Mountain
            'KATL': 'America/New_York',  # EST but often has eastern influence
            'KAUS': 'America/Chicago',
            'KSAT': 'America/Chicago',
            'KELP': 'America/Chicago',
            'KSMF': 'America/Los_Angeles',
        }

    def _is_holiday_or_special_day(self, date_str: str) -> bool:
        """
        Check if the given date falls on a holiday or special low-liquidity day.
        This is a simplified version - could be extended with actual holiday lists.
        """
        try:
            dt = datetime.fromisoformat(date_str)
            # Basic US federal holidays (simplified)
            us_holidays = {
                (1, 1),  # New Year
                (7, 4),  # Independence Day 
                (12, 25),  # Christmas
                (12, 24),  # Christmas Eve
                (12, 31),  # New Year's Eve
            }
            
            if (dt.month, dt.day) in us_holidays:
                return True
                
            # Major weekends around holidays
            if dt.day == 4 and dt.month == 7:  # July 4th holiday weekend
                return True
            if dt.day == 24 and dt.month == 12:  # Christmas Eve weekend
                return True
                
            # Some major weather event days could also be tagged separately
            # Placeholder - would typically come from news/event API data
            
            return dt.weekday() > 4  # Weekend

        except ValueError:
            return False  # Error parsing date, assume normal day

    def get_station_local_hour(self, station: str, utc_datetime: datetime) -> int:
        """
        Get the local hour at a station for a given UTC datetime.
        """
        tz_name = self.stations_timezone_map.get(station, 'America/New_York')
        station_tz = pytz.timezone(tz_name)
        local_time = utc_datetime.astimezone(station_tz)
        return local_time.hour

    def classify_station_phase(
        self, 
        station: str, 
        date: str,
        hour_utc: int = None,
        include_news_check: bool = True
    ) -> Dict[str, Any]:
        """
        Comprehensive phase classification with additional context.
        
        Args:
            station: Station identifier
            date: Trading date
            hour_utc: Hour in UTC (calculate if not provided)
            include_news_check: Whether to simulate checking for news/events (could call external API)
            
        Returns:
            Dict with phase and additional decision parameters
        """
        # If hour not provided, we can't do detailed timing - assume mid-day for testing
        current_hour = hour_utc if hour_utc is not None else 12
    
        # Determine factors that affect phase classification
        is_holiday_like = self._is_holiday_or_special_day(date)
        
        # Simulate whether there's a news event (in real system would use news API)
        has_news_event = False  # Would normally come from news/event monitoring
        if include_news_check:
            # Placeholder for news integration
            pass
        
        # Classify phase
        phase = classify_market_phase(
            station=station,
            date=date,
            hour_utc=current_hour,
            news_event_today=has_news_event,
            low_liquidity_day=is_holiday_like
        )
        
        # Determine phase-specific position sizing multiplier
        position_multiplier = self.get_position_size_multiplier(phase)
        
        # Additional metrics specific to this phase
        phase_details = self._get_phase_details(phase, station, date, current_hour)
        
        return {
            'phase': phase,
            'position_multiplier': position_multiplier,
            'phase_details': phase_details,
            'station': station,
            'date': date,
            'utc_hour': current_hour,
            'is_low_liquidity_day': is_holiday_like,
            'has_information_event': has_news_event,
            'classification_timestamp': datetime.now().isoformat()
        }
    
    def get_position_size_multiplier(self, phase: str) -> float:
        """
        Get position sizing adjustment factor based on market phase.
        Returns multipliers between 0.0 (no position) and 1.0 (normal position size).
        """
        multipliers = {
            'NORMAL': 1.000,          # Normal position size
            'INFORMATION_EVENT': 0.800,  # Reduced on info events due to uncertainty
            'SETTLEMENT_CONVERGENCE': 0.600,  # Reduced near settlement, high volatility
            'OVERNIGHT_THIN': 0.400,     # Significantly reduced during thin overnight volumes
            'HOLIDAY_WEEKEND': 0.500     # Reduced on low-liquidity days
        }
        return multipliers.get(phase, 0.700)  # Default to slightly reduced if unknown phase
    
    def _get_phase_details(self, phase: str, station: str, date: str, hour_utc: int) -> Dict[str, Any]:
        """
        Get additional details about the market phase to assist decision-making.
        """
        details = {
            'risk_level': 'medium',
            'volatility_profile': 'normal',
            'expected_liquidity': 'normal',
            'activity_timing': 'normal',
            'comment': ''
        }
        
        if phase == 'NORMAL':
            details.update({
                'risk_level': 'low',
                'volatility_profile': 'low_to_medium',
                'expected_liquidity': 'high',
                'activity_timing': 'typical_business_hours',
                'comment': 'Standard market conditions with good liquidity and low volatility'
            })
        elif phase == 'INFORMATION_EVENT':
            details.update({
                'risk_level': 'high',
                'volatility_profile': 'high',
                'expected_liquidity': 'high',
                'activity_timing': 'potentially_unpredictable',
                'comment': 'High interest due to news event - expect increased volatility'
            })
        elif phase == 'SETTLEMENT_CONVERGENCE':
            details.update({
                'risk_level': 'high',
                'volatility_profile': 'very_high',
                'expected_liquidity': 'variable',
                'activity_timing': 'concentrated_near_deadline',
                'comment': 'Near settlement - positions may be unwinding quickly'
            })
        elif phase == 'OVERNIGHT_THIN':
            details.update({
                'risk_level': 'medium',
                'volatility_profile': 'medium',  # Can spike in thin volume
                'expected_liquidity': 'low',
                'activity_timing': 'very_low_activity',
                'comment': 'Low volume period - potential for wider spreads and jumps'
            })
        elif phase == 'HOLIDAY_WEEKEND':
            details.update({
                'risk_level': 'low_to_medium',
                'volatility_profile': 'low',
                'expected_liquidity': 'low',
                'activity_timing': 'inactive_off_hours',
                'comment': 'Lower overall activity, reduced liquidity periods'
            })
        
        return details

    def get_all_phase_descriptions(self) -> Dict[str, str]:
        """
        Return human-readable descriptions for all phases.
        """
        return {
            'NORMAL': 'Typical trading conditions with adequate liquidity and low volatility.',
            'INFORMATION_EVENT': 'Market-moving weather event or forecast has been issued.',
            'SETTLEMENT_CONVERGENCE': 'Within 2 hours of market settlement (typically 12:00 UTC).',
            'OVERNIGHT_THIN': 'Between 20:00-08:00 UTC with typically low market liquidity.',
            'HOLIDAY_WEEKEND': 'Weekends, US holidays, or other days with low market participation.'
        }


def get_phase_adjusted_params(
    station: str, 
    date: str, 
    base_position_size: float = 10
) -> Dict[str, Any]:
    """
    Get all necessary parameters for position taking considering market phase.
    
    Args:
        station: Station identifier
        date: Trading date
        base_position_size: Base position size before phase adjustment
        
    Returns:
        Dict with phase-adjusted parameters
    """
    classifier = MarketPhaseClassifier()
    
    # For this function call, we'll use a default UTC hour of 10
    classification = classifier.classify_station_phase(
        station=station,
        date=date,
        hour_utc=10,  # Mid-morning in UTC, typically business hours in US
        include_news_check=False  # Avoid external API for now
    )
    
    # Adjust base position by multiplier
    adjusted_position = base_position_size * classification['position_multiplier']
    
    return {
        **classification,
        'base_position_size': base_position_size,
        'adjusted_position_size': adjusted_position,
        'position_adjustment_magnitude': base_position_size - adjusted_position
    }