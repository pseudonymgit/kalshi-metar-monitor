#!/usr/bin/env python3
"""
SIGNAL: Forecast Disagreement

When yesterday's actual differs from the 7-day rolling mean, bet in the
direction of the mean (longer-term expectation). Proxy for GFS vs NWS
forecast disagreement.

Based on approach_forecast_disagreement() from ensemble_v9_with_edge5.
"""
import math
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _window, _safe_get, validate_signal


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


class ForecastDisagreementSignal(BaseSignal):
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.disagreement_threshold = 5.0  # deg F
        self.window_days = 7

    @property
    def name(self) -> str:
        return "forecast_disagreement"

    @property
    def min_lookback(self) -> int:
        return self.window_days + 1

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Forecast disagreement: compare yesterday's high vs 7-day mean."""
        if idx < self.window_days + 1:
            return None, 0.0

        yesterday_high = _safe_get(days, idx - 1, 'high')
        if yesterday_high is None:
            return None, 0.0

        week_days = _window(days, idx, self.window_days, offset=1)
        if week_days is None:
            return None, 0.0

        week_highs = [d.get('high') for d in week_days if d.get('high') is not None]
        if len(week_highs) < 3:
            return None, 0.0

        weekly_mean = sum(week_highs) / len(week_highs)

        disagreement = yesterday_high - weekly_mean
        abs_disagreement = abs(disagreement)

        if abs_disagreement < self.disagreement_threshold:
            return None, 0.0

        direction = 'down' if disagreement > 0 else 'up'
        confidence = sigmoid((abs_disagreement - self.disagreement_threshold) / 3.0)

        return direction, confidence

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        return None, 0.0