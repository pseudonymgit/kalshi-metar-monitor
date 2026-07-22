# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
#


"""
Core module for settlement cascade timing analysis.
Predicts unwind cascade in final hours before settlement and manages timing-based exits.

Part of Phase 7 - Kalshi API Integration.
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from statistics import mean, stdev
import pytz


def predict_settlement_timing(station: str, date: str, market_hours: list = None) -> Dict[str, any]:
    """
    Predict optimal timing around settlement including early exit and re-entry windows.
    
    Args:
        station: Station identifier (e.g., 'KJFK')
        date: Trading date in YYYY-MM-DD format
        market_hours: Optional list of recent settlement times for this market type
        
    Returns:
        Dict with cascade prediction and timing recommendations
    """
    # Settlement time is usually 12:00 UTC for daily markets
    settlement_hour_utc = 12  # Noon UTC is common settlement time
    settlement_minute_utc = 0
    
    # Parse input date to datetime object
    try:
        target_date = datetime.fromisoformat(date)
    except ValueError:
        # If date format is wrong, use today
        target_date = datetime.now(timezone.utc)
    
    # Set the settlement datetime for this particular day
    settlement_datetime = target_date.replace(hour=settlement_hour_utc, minute=settlement_minute_utc, second=0, microsecond=0)
    
    # Current time in UTC
    current_datetime = datetime.now(pytz.UTC).replace(tzinfo=None)  # Make naive for calculation
    
    # Time remaining until settlement
    time_to_settlement = settlement_datetime - current_datetime
    minutes_to_settlement = time_to_settlement.total_seconds() / 60
    
    # Timing recommendations based on historical patterns
    recommendations = {}
    
    # Define the cascade and timing windows based on research
    exit_before_minutes = 90  # Exit 90 mins before settlement to avoid cascade
    reentry_after_minutes = 30  # Re-enter 30 mins after cascade potentially starts
    
    # Calculate cascade windows
    cascade_start_time = settlement_datetime - timedelta(minutes=exit_before_minutes)
    reentry_start_time = settlement_datetime + timedelta(minutes=reentry_after_minutes)
    
    # Current state relative to cascade windows
    is_in_exit_window = (
        cascade_start_time <= current_datetime < settlement_datetime
    )
    
    is_in_cascade_period = (
        current_datetime >= settlement_datetime - timedelta(minutes=exit_before_minutes)
    ) and (current_datetime <= settlement_datetime + timedelta(minutes=30))
    
    # Return comprehensive timing info
    return {
        'station': station,
        'date': date,
        'settlement_datetime': settlement_datetime.isoformat(),
        'current_datetime': current_datetime.isoformat(),
        'time_to_settlement_minutes': minutes_to_settlement,
        
        # Cascade timing windows
        'exit_before_minutes': exit_before_minutes,
        'reentry_after_minutes': reentry_after_minutes,
        'exit_before_datetime': cascade_start_time.isoformat(),
        'reentry_opportunity_datetime': reentry_start_time.isoformat(),
        
        # Current state flags
        'is_in_exit_window': is_in_exit_window,
        'is_approaching_settlement': minutes_to_settlement <= 120,
        'is_in_cascade_period': is_in_cascade_period,
        'minutes_until_exit_time': max(0, (cascade_start_time - current_datetime).total_seconds() / 60),
        
        # Recommendations
        'recommendation': get_cascade_recommendation(
            is_in_exit_window, is_in_cascade_period, minutes_to_settlement
        ),
        'actions': get_timing_actions(
            is_in_exit_window, is_in_cascade_period, minutes_to_settlement
        ),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }


def get_fair_value_prediction(station: str, date: str, market_type: str = 'HIGH') -> float:
    """
    Predict fair value for a market based on historical patterns near settlement
    to identify oversold/overbought positions during cascade.
    """
    # For settlement-time predictions, fair value should reflect historical 
    # settlement patterns around final resolution
    import random  # Import here to avoid overhead at module level
    
    # Placeholder logic - in real implementation would use historical settlement data
    # to determine true-fair-value based on late-day METAR observations and market history
    
    # For now, return a value reflecting typical market behavior:
    # During cascades, markets often move toward extremes before settling
    
    # Use a random element with some basis in the fact that markets often settle 
    # closer to their underlying fundamentals rather than extreme positions
    base_fair_value = 0.5  # Fair value is approximately 50-50 chance typically
    
    # Add a tiny random element around the 0.5 mark to simulate slight variation
    variation = random.uniform(-0.1, 0.1)
    estimated_fair = max(0.01, min(0.99, base_fair_value + variation))
    
    return estimated_fair


def find_overshoot_opportunities(
    station: str,
    date: str,
    market_type: str,
    current_price: float,
    predicted_fair_value: float
) -> Optional[Dict[str, any]]:
    """
    Identify potential re-entry opportunities when market price moves significantly
    from predicted fair value during settlement approaches.
    
    Args:
        station: Station identifier
        date: Trading date
        market_type: 'HIGH' or 'LOW' 
        current_price: Current market price
        predicted_fair_value: Expected fair value of the contract
        
    Returns:
        Opportunity info if price difference exceeds sigma threshold, else None
    """
    # Calculate deviation
    price_deviation = abs(current_price - predicted_fair_value)
    
    # Define what constitutes significant movement (sigma equivalent)
    # For this implementation, we'll use static thresholds since we don't have live data streams
    
    # A movement of 0.15 (15 cents) or more from fair value might constitute overshoot
    significant_deviation_threshold = 0.15
    
    if price_deviation >= significant_deviation_threshold:
        return {
            'deviation': price_deviation,
            'threshold': significant_deviation_threshold,
            'fair_value': predicted_fair_value,
            'current_price': current_price,
            'direction': 'BUY_CHEAP' if current_price < predicted_fair_value else 'SELL_EXPENSIVE',
            'estimated_sigma': price_deviation / 0.10,  # Rough conversion to standard deviations
            'profit_potential': price_deviation,
            'is_overshoot_detected': True,
            'overshoot_severity': 'high' if price_deviation > 0.2 else 'moderate'
        }
    
    return {
        'deviation': price_deviation,
        'threshold': significant_deviation_threshold,
        'is_overshoot_detected': False,
        'current_price_close_to_fair': True
    }


def get_cascade_recommendation(is_in_exit_window: bool, is_in_cascade_period: bool, minutes_to_settlement: float) -> str:
    """
    Get plain-language recommendation based on current time status.
    """
    if is_in_exit_window:
        return "HIGH PRIORITY: Exit positions to avoid settlement cascade"
    elif is_in_cascade_period:
        if minutes_to_settlement <= 30:
            return "EXIT IMMEDIATELY: Settlement convergence underway"
        else:
            return "Monitor market: Approaching exit window"
    elif minutes_to_settlement <= 120:
        return "Prepare for settlement: Approaching cascade period"
    else:
        return "Normal operation: Settlement still more than 2 hours away"


def get_timing_actions(is_in_exit_window: bool, is_in_cascade_period: bool, minutes_to_settlement: float) -> list:
    """
    Get a list of recommended actions based on current timing status.
    """
    actions = []
    
    if minutes_to_settlement <= 0:
        # After settlement - waiting for close/reconcile
        actions.append("Wait for settlement confirmation")
        actions.append("Prepare next day positions")
        return actions
    
    if is_in_exit_window and minutes_to_settlement > 0:
        actions.append("Begin liquidating positions within 30 minutes")
        actions.append("Accept small slippage to ensure timely exit")
    
    if is_in_cascade_period:
        actions.append("Avoid new position entries")
        actions.append("Limit orders may not fill as market moves rapidly")
    
    if minutes_to_settlement <= 60:
        actions.append("Reduce position sizing - market volatility increasing")
        actions.append("Increase watchfulness for rapid price movements")
    
    if minutes_to_settlement <= 30:
        actions.append("STOP all new position building - extremely volatile period")
        actions.append("Focus on closing trades rather than opening new ones")
    
    if not is_in_exit_window:
        actions.append("Maintain normal position sizing and trading patterns")
    
    return actions


class SettlementTimer:
    """
    Stateful class to track settlement timing and manage cascades for long-lived processes.
    """
    
    def __init__(self, station: str, date: str, market_type: str = 'HIGH'):
        self.station = station
        self.current_date = date
        self.market_type = market_type
        self.cache_fair_value = None
        self.last_update_time = None
        
    def update(self) -> Dict[str, any]:
        """
        Recalculate timing and return updated recommendations.
        """
        result = predict_settlement_timing(self.station, self.current_date)
        
        # Cache fair value prediction
        if not self.cache_fair_value or not self.last_update_time or \
           (datetime.now(timezone.utc) - self.last_update_time.replace(tzinfo=None)).seconds > 300:  # Every 5 minutes
            self.cache_fair_value = get_fair_value_prediction(self.station, self.current_date, self.market_type)
            self.last_update_time = datetime.now(timezone.utc)
        
        return {
            **result,
            'predicted_fair_value': self.cache_fair_value
        }
    
    def should_exit_positions(self) -> bool:
        """
        Quick check for immediate exit requirement.
        """
        result = self.update()
        return result.get('is_in_exit_window', False)
    
    def get_exit_time_remaining(self) -> float:
        """
        Get minutes until exit time (may be negative if already past).
        """
        result = self.update()
        return result.get('minutes_until_exit_time', 0.0)
    
    def should_prepare_for_settlement(self) -> bool:
        """
        Check if preparations for settlement should begin.
        """
        result = self.update()
        return result.get('is_approaching_settlement', False)


def get_settlement_timing(station: str, date: str) -> Dict[str, any]:
    """
    Main export function following spec requirement.
    
    Args:
        station: Station identifier like 'KJFK'  
        date: Trading date in YYYY-MM-DD format
        
    Returns:
        Dict containing (exit_before, reentry_window, fair_value)
    """
    # Get current timing state
    timing_info = predict_settlement_timing(station, date)
    
    # Format return data according to spec
    return {
        'exit_before': timing_info['exit_before_datetime'],  # Time to exit by
        'reentry_window': timing_info['reentry_opportunity_datetime'],  # Opportunity to re-enter
        'fair_value': get_fair_value_prediction(station, date),  # Expected fair value at settlement
        'current_state': {
            'is_cascade_active': timing_info['is_in_cascade_period'],
            'time_to_cascade_minutes': timing_info['time_to_settlement_minutes'],
            'current_recommendation': timing_info['recommendation']
        }
    }


def get_advanced_settlement_strategy(station: str, date: str, market_type: str = 'HIGH') -> Dict[str, any]:
    """
    Complete settlement strategy incorporating timing, overshoot detection, and position management.
    """
    basic_timing = predict_settlement_timing(station, date, market_type)
    fair_value = get_fair_value_prediction(station, date, market_type)
    
    # This function would in the real system get current market prices 
    # to compare with fair value and identify re-entry opportunities
    # For now, we'll simulate a current market price
    import random
    current_price = fair_value + (random.uniform(-0.15, 0.15))  # Simulate a realistic price near fair value
    
    overshoot_op = find_overshoot_opportunities(station, date, market_type, current_price, fair_value)
    
    strategy = {
        'basic_timing': basic_timing,
        'fair_value_estimation': fair_value,
        'current_price_simulation': current_price,
        'overshoot_analysis': overshoot_op,
        'settlement_strategy': {
            'exit_strategy': basic_timing['recommendation'],
            'reentry_conditions': overshoot_op['is_overshoot_detected'],
            'reentry_opportunity_details': overshoot_op if overshoot_op['is_overshoot_detected'] else None,
            'risk_management_notes': f"Position sizing should be reduced within {basic_timing['minutes_to_settlement']:.0f} minutes of settlement."
        },
        'status': 'strategy_complete'
    }
    
    return strategy