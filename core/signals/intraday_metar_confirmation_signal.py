"""
Intraday METAR Confirmation Signal - BaseSignal Extension

This signal uses same-day METAR observations to confirm or weaken ensemble predictions.
It is a confirmation signal, not a standalone predictor, adjusting confidence based on 
observed temperature movement toward the predicted direction. This is primarily 
a runtime signal that affects trading confidence in real-time, but during backtests,
it serves mainly to validate whether such intraday confirmation factors were available.
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
import logging
import sqlite3

from .base_signal import BaseSignal, _safe_get, validate_signal

logger = logging.getLogger(__name__)


class IntradayMetarConfirmationSignal(BaseSignal):
    """Intraday confirmation signal based on real-time temperature trends."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        # Configuration for confirmation adjustment
        self.confirm_boost_fraction = 0.15  # 15% confidence boost factor
        self.weaken_penalty_fraction = 0.25  # 25% confidence reduction factor
        
    @property
    def name(self) -> str:
        return "intraday_metar_confirmation"

    @property  
    def min_lookback(self) -> int:
        return 1  # Only needs current day's baseline

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate the intraday confirmation signal at day index `idx`.

        For backtesting purposes, this signal typically returns (None, 0.0),
        since true "intraday" confirmation requires real-time data from the current day.
        However, we can simulate the concept by looking for unusual intraday trends
        that correlate with daily temperature movements.

        Args:
            idx: Current day index in the `days` list  
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence): Returns (None, 0.0) for backtesting purposes,
            since true intraday confirmation happens during the day of prediction
        """
        # In historical backtesting, we cannot meaningfully run same-day confirmation
        # since intraday confirmation is about same-day temperature movement
        if idx < self.min_lookback:
            return None, 0.0

        # For backtesting framework compatibility, return None to indicate
        # this should be processed as a confirmation factor rather than 
        # a standalone prediction source
        return None, 0.0

    def evaluate_as_confirmation(
        self, 
        idx: int, 
        days: List[dict], 
        base_direction: str, 
        base_confidence: float,
        time_of_day_ratio: float = 1.0  # 0.0=morning, 1.0=end of day
    ) -> Tuple[str, float]:
        """
        Special evaluation method for use as confirmation, not for backtesting.
        
        This is called during actual paper/live trading when the base prediction
        already exists and we want to confirm or weaken it based on intraday data.
        
        Args:
            idx: Current day index
            days: Daily weather data
            base_direction: Original prediction direction ('up'/'down')
            base_confidence: Original confidence
            time_of_day_ratio: How far through the day we are (0.0-1.0)
            
        Returns:
            Adjusted (direction, confidence) with intraday confirmation adjustments
        """
        if idx < self.min_lookback:
            return base_direction, base_confidence

        # In a real system, we'd fetch intraday data here
        # For simulation with day-level data, we'll look at the consistency
        # between early indicators and final outcomes
        
        current_temp = _safe_get(days, idx - 1, 'temp')
        morning_indicator = _safe_get(days, idx - 1, 'temp')  # In real system would be early temp
        afternoon_indicator = current_temp

        if current_temp is not None and morning_indicator is not None and afternoon_indicator is not None:
            # Simulate whether temp is tracking in the predicted direction
            if base_direction == 'up' and afternoon_indicator > morning_indicator:
                # Tracking in predicted direction - confirm  
                adjusted_confidence = min(1.0, base_confidence * (1 + self.confirm_boost_fraction))
                return base_direction, adjusted_confidence
            elif base_direction == 'down' and afternoon_indicator < morning_indicator:
                # Tracking in predicted direction - confirm
                adjusted_confidence = min(1.0, base_confidence * (1 + self.confirm_boost_fraction))
                return base_direction, adjusted_confidence
            elif base_direction == 'up' and afternoon_indicator < morning_indicator:
                # Contrary trend detected - weaken
                adjusted_confidence = max(0.01, base_confidence * (1 - self.weaken_penalty_fraction))
                return base_direction, adjusted_confidence
            elif base_direction == 'down' and afternoon_indicator > morning_indicator:
                # Contrary trend detected - weaken  
                adjusted_confidence = max(0.01, base_confidence * (1 - self.weaken_penalty_fraction))
                return base_direction, adjusted_confidence

        # If no clear pattern, return original values unchanged
        return base_direction, base_confidence


def test_signal():
    """Test function for verification."""
    signal = IntradayMetarConfirmationSignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")


if __name__ == "__main__":
    test_signal()