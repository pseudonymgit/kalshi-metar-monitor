#!/usr/bin/env python3
"""
metar_trend_signal.py — Simple METAR temperature trend-following signal.

Compares today's high with yesterday's high to determine direction.
Confidence based on trend consistency over the last 3 days.

This is what the old "ECMWF bias-corrected" signal's evaluate() method
actually did — it was a METAR trend signal, not an ECMWF-based signal.

B-Mode compliant. No AI/ML.
"""

import logging
from typing import Dict, List, Optional, Tuple

from .base_signal import BaseSignal

logger = logging.getLogger(__name__)


class MetarTrendSignal(BaseSignal):
    """
    Simple METAR temperature trend-following signal.

    Compares today's high with yesterday's high to determine direction,
    with confidence based on trend consistency over the last 3 days.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "metar_trend"

    @property
    def min_lookback(self) -> int:
        return 2

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate signal using METAR temperature trend from historical data.

        Compares today's high with yesterday's high to determine direction,
        with confidence based on trend consistency over the last 3 days.

        Args:
            idx: Index into days list
            days: List of daily dicts with 'high' key

        Returns:
            (direction, confidence) or (None, 0.0) if insufficient data
        """
        if idx < 2 or idx >= len(days):
            return None, 0.0

        # Use yesterday[idx-1] vs day-before[idx-2] to predict today[idx]
        # This avoids look-ahead bias (using today's settlement data)
        yesterday = days[idx - 1].get('high')
        day_before = days[idx - 2].get('high')

        if yesterday is None or day_before is None:
            return None, 0.0

        # Direction based on yesterday's vs day-before temperature change
        direction = 'up' if yesterday > day_before else 'down'

        # Confidence based on trend consistency over last 3 complete days
        consistent = 0
        for i in range(max(2, idx - 2), idx):
            prev = days[i - 1].get('high')
            curr = days[i].get('high')
            if prev is not None and curr is not None:
                if (curr > prev and direction == 'up') or (curr < prev and direction == 'down'):
                    consistent += 1

        confidence = 0.35 + (consistent * 0.1)
        confidence = min(0.75, confidence)

        return direction, round(confidence, 3)