#!/usr/bin/env python3
"""
honest_trend_signal.py — WS9 honest METAR trend signal.

Implements spec B.2 trend definition:
    trend_feature(today) = sign(METAR_yesterday_max - METAR_3ago_max)
    
Only uses METAR data with dates ≤ yesterday. No label access (no today_temp).
Confidence: fixed at 0.55 (slightly above floor — trend is a weak signal).
Label-safe: verified by WS2 test.

B-Mode compliant. No AI/ML.
"""
import logging
from typing import Dict, List, Optional, Tuple
from core.signals.base_signal import BaseSignal

logger = logging.getLogger(__name__)

HONEST_TREND_CONFIDENCE = 0.55


class HonestTrendSignal(BaseSignal):
    """
    Honest METAR-based 3-day trailing trend signal (spec B.2, Version B).

    trend_feature(today) = sign(METAR_yesterday_max - METAR_3ago_max)
    
    This is the clean, honest trend baseline that the system must beat.
    It uses ONLY METAR data with dates ≤ yesterday — no today_temp access,
    no settlement data peek. Label-safe by construction.
    
    Fires on any (station, date) where idx >= 3 and both yesterday and
    3ago have METAR 'high' values. Otherwise abstains (returns None).
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "honest_trend_B"

    @property
    def min_lookback(self) -> int:
        return 3

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate honest METAR trend B at day index `idx`.

        Args:
            idx: Index into days list.
            days: List of day dicts with 'high' key (METAR max temp).

        Returns:
            ('up'|'down'|None, confidence)
        """
        if idx < 3 or idx >= len(days):
            return None, 0.0

        yesterday_high = days[idx - 1].get('high')
        three_ago_high = days[idx - 3].get('high')

        if yesterday_high is None or three_ago_high is None:
            return None, 0.0

        diff = yesterday_high - three_ago_high

        if diff > 0:
            direction = 'up'
        elif diff < 0:
            direction = 'down'
        else:
            return None, 0.0  # zero trend — no signal

        return direction, HONEST_TREND_CONFIDENCE