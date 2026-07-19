"""
Market Phase Classifier
======================

Purpose: Classifies each market day as one of several states: Normal,
Information Event, Settlement Convergence, Overnight Thin, Holiday.
Modulates trading behavior based on phase classification.

Inputs:
- Settlement calendar events (from kalshi_calendar)
- METAR data timestamps and activity levels
- Historical market behavior patterns

Outputs:
- Phase label (Normal, Information Event, Settlement Convergence, Overnight Thin, Holiday)
- Confidence score for the classification
- Parameters for modulating trade behavior

Usage: python market_phase_classification.py --station STATION_CODE \
       --date YYYY-MM-DD [--calendar_path CALENDAR_FILE]
"""

import pandas as pd
import numpy as np
from enum import Enum
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.joinpath("core")))
import station_registry
STATIONS = station_registry.get_all_stations()
from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
# Import individual functions from kalshi_calendar instead of a class
from kalshi_calendar import is_trading_day, get_settlement_date, get_next_trading_day, is_valid_entry_date
from datetime import datetime, date, timedelta


class KalshiCalendar:
    """
    Thin wrapper for kalshi_calendar functions to maintain backward compatibility
    where a class object was expected
    """
    def __init__(self):
        pass

    def get_next_settlement_time(self, date):
        # Return a fixed time for now to allow basic functionality
        # Settlement typically happens at the start of the next trading day
        next_trading_day = get_next_trading_day(date)
        # Return datetime object at midnight of the next trading day
        return datetime.combine(next_trading_day, datetime.min.time())


# Placeholder for SettlementCalendar if used anywhere
SettlementCalendar = None


# Try importing KalshiMonitor but if it fails, use a placeholder
try:
    from kalshi_monitor import KalshiMonitor
except ImportError:
    # Fake a basic interface if module is not available
    class KalshiMonitor:
        def __init__(self):
            pass

        def get_next_settlement_time(self, date):
            # Same as above
            next_trading_day = get_next_trading_day(date)
            return datetime.datetime.combine(next_trading_day, datetime.time(0, 0))


class MarketPhase(Enum):
    """Market phase enumeration"""
    NORMAL = "Normal"
    INFORMATION_EVENT = "Information Event"
    SETTLEMENT_CONVERGENCE = "Settlement Convergence"
    OVERNIGHT_THIN = "Overnight Thin"
    HOLIDAY = "Holiday"


class MarketPhaseClassifier:
    """
    Classifies market conditions into different phases that require different
    trading strategies and risk management approaches.
    """

    def __init__(self, calendar_path: str = None):
        """
        Initialize with calendar information

        Args:
            calendar_path: Path to calendar file with settlement/holiday info
        """
        self.calendar = KalshiCalendar()
        # Skip SettlementCalendar since it's not available
        self.settlement_calendar = None

        self.phases = MarketPhase

    def classify_phase(self, station_code: str, date_str: str, metar_data: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """
        Classify the market phase for a given station and date

        Args:
            station_code: Kalshi code for the station
            date_str: Date to classify in YYYY-MM-DD format
            metar_data: Optional DataFrame with METAR data for analysis

        Returns:
            Dictionary with classification result and confidence
        """
        date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Check for holidays first
        if self._is_holiday(date):
            return {
                'phase': MarketPhase.HOLIDAY,
                'confidence': 0.95,
                'description': "Non-trading day due to holiday",
                'parameters': {
                    'trade_volume_threshold': 0.1,
                    'min_spread_multiplier': 1.5,
                    'reduce_position_size': True
                }
            }

        # Analyze metar data patterns, spreads, and market activity
        # to determine current phase

        # Initialize analysis variables
        settlement_proximity_minutes = 0
        metar_volatility = 0.0
        market_activity_ratio = 0.0
        has_information_event = False

        # Get proximity to settlement
        settlement_datetime = self.calendar.get_next_settlement_time(date)
        if settlement_datetime:
            settlement_datetime_date_only = datetime.combine(date, datetime.min.time())
            settlement_proximity_minutes = (settlement_datetime - settlement_datetime_date_only).total_seconds() / 60
            print(f'DEBUG: settlement_datetime={settlement_datetime}, date_combined={settlement_datetime_date_only}')

        # Estimate information event flags
        if metar_data is not None:
            has_information_event = self._detect_information_event(metar_data)
            metar_volatility = self._calculate_metar_volatility(metar_data)

        # Determine overnight thin based on the fact it's a specific date
        # For market phase analysis, midnight represents overnight markets
        # Since we don't have a specific time, we'll use a default value
        overnight_hours_low_activity = False  # Default to day-time analysis since no specific time provided

        # Settlement convergence check
        is_near_settlement_convergence = (0 <= settlement_proximity_minutes <= 180)  # Within 3 hours of settlement

        # Information event detection
        info_event_confidence = 0.8 if has_information_event else 0.2
        info_event_params = {
            'position_sizing': 'reduced' if has_information_event else 'normal',
            'volatility_buffer': 1.2 if has_information_event else 1.0,
            'order_aggression': 'conservative' if has_information_event else 'normal'
        }

        # Classification logic
        if is_near_settlement_convergence:
            # As we approach settlement, markets converge differently
            return {
                'phase': MarketPhase.SETTLEMENT_CONVERGENCE,
                'confidence': 0.85,
                'description': f"Within {int(settlement_proximity_minutes)} minutes of settlement",
                'parameters': {
                    'position_reduction': 0.3,
                    'time_based_exit': True,
                    'volatility_scaler': 0.7  # Reduce volatility expectations as price converges
                }
            }
        elif info_event_confidence > 0.7:
            # High likelihood of information event
            return {
                'phase': MarketPhase.INFORMATION_EVENT,
                'confidence': info_event_confidence,
                'description': "High-impact weather information detected",
                'parameters': info_event_params
            }
        elif overnight_hours_low_activity:
            # Low liquidity overnight periods
            return {
                'phase': MarketPhase.OVERNIGHT_THIN,
                'confidence': 0.75,
                'description': "Low activity overnight period",
                'parameters': {
                    'reduce_trade_frequency': True,
                    'widen_stop_losses': True,
                    'lower_position_sizes': True
                }
            }
        else:
            # Normal market conditions
            return {
                'phase': MarketPhase.NORMAL,
                'confidence': 0.90,
                'description': "Regular market conditions",
                'parameters': {
                    'standard_risk_management': True,
                    'normal_position_sizes': True,
                    'regular_trade_frequency': True
                }
            }

    def _is_holiday(self, date: datetime.date) -> bool:
        """
        Check if a date is a holiday
        """
        # US Federal Holidays - simplified version
        year = date.year
        month = date.month
        day = date.day

        # New Year's Day (Jan 1 or preceding Friday/Following Monday)
        if month == 1 and (day == 1 or (day == 2 and date(year, 1, 2).weekday() == 0)):
            return True

        # Independence Day (July 4th)
        if month == 7 and (day == 4 or (
                (day == 3 and date(year, 7, 3).weekday() == 4) or  # Fri before
                (day == 5 and date(year, 7, 5).weekday() == 0)     # Mon after
        )):
            return True

        # Christmas
        if month == 12 and (day == 25 or (
                (day == 24 and date(year, 12, 24).weekday() == 4) or  # Fri before
                (day == 26 and date(year, 12, 26).weekday() == 0)     # Mon after
        )):
            return True

        # Thanksgiving
        thanksgiving = self._get_thanksgiving_date(year)
        if date == thanksgiving:
            return True

        # Black Friday (next day) is often also treated as an effective market holiday
        if date == (thanksgiving + timedelta(days=1)):
            return True

        return False


    def _get_thanksgiving_date(self, year: int) -> date:
        """Calculate Thanksgiving (4th Thursday of November)"""
        nov_first = date(year, 11, 1).weekday()
        # Find first Thursday in November (Thursday is weekday 3)
        days_to_first_thurs = (3 - nov_first) % 7
        first_thursday = 1 + days_to_first_thurs
        # Thankgiving is fourth Thursday
        thanksgiving_day = first_thursday + 3 * 7
        if thanksgiving_day > 30:
            thanksgiving_day -= 7  # Adjust if it would extend beyond November 30
        return date(year, 11, thanksgiving_day)


    def _detect_information_event(self, metar_df: pd.DataFrame) -> bool:
        """
        Detect if a significant market-moving weather event occurred
        """
        # Detect rapid temperature/wind changes, precipitation events, etc.
        wind_speed_changes = abs(metar_df['wind_speed'].diff()).max() >= 15
        temp_changes = abs(metar_df['temperature'].diff()).max() >= 8
        weather_code_events = ('TS' in ''.join(str(x) for x in metar_df['wx_codes'])) or \
                             ('SN' in ''.join(str(x) for x in metar_df['wx_codes']))

        return wind_speed_changes or temp_changes or weather_code_events


    def _calculate_metar_volatility(self, metar_df: pd.DataFrame) -> float:
        """
        Calculate volatility in weather conditions for the day
        """
        temperature_std = metar_df['temperature'].std()
        wind_speed_std = metar_df['wind_speed'].std()
        return max(temperature_std or 0.0, wind_speed_std or 0.0)

    def get_recommended_strategy(self, phase: MarketPhase, parameters: dict) -> str:
        """
        Provide recommended strategy modifications based on phase

        Args:
            phase: Identified market phase
            parameters: Additional parameters for the phase

        Returns:
            Textual recommendation
        """
        strategy_map = {
            MarketPhase.HOLIDAY: "DO NOT TRADE",
            MarketPhase.SETTLEMENT_CONVERGENCE: "EXIT POSITIONS GRADUALLY",
            MarketPhase.INFORMATION_EVENT: "REDUCE POSITION SIZE AND INCREASE VOLATILITY BUFFER",
            MarketPhase.OVERNIGHT_THIN: "REDUCE TRADE FREQUENCY AND WIDEN SLIPPAGE TOLERANCE",
            MarketPhase.NORMAL: "PROCEED WITH STANDARD RISK MANAGEMENT PROTOCOLS"
        }
        return strategy_map.get(phase, "MAINTAIN NEUTRAL POSTURE")


def main():
    parser = argparse.ArgumentParser(description='Classify market phases')
    parser.add_argument('--station', type=str, required=True, help='Station code (e.g., KJFK)')
    parser.add_argument('--date', type=str, required=True, help='Date in YYYY-MM-DD format')

    args = parser.parse_args()

    classifier = MarketPhaseClassifier()
    result = classifier.classify_phase(args.station, args.date)

    print(f"Market Phase for {args.station} on {args.date}:")
    print(f"  Phase: {result['phase'].value}")
    print(f"  Confidence: {result['confidence']:.2f}")
    print(f"  Description: {result['description']}")
    print(f"  Strategy: {classifier.get_recommended_strategy(result['phase'], result['parameters'])}")
    print("\nParameters:")
    for param, value in result['parameters'].items():
        print(f"  {param}: {value}")


if __name__ == "__main__":
    main()