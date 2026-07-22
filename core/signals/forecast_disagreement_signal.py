#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 2. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

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
        """DB-based evaluation for forecast disagreement signal."""
        import math
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
        
        own_conn = conn is None
        if own_conn:
            conn = get_sqlite_connection(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            # Query max daily temperature for the forecast disagreement analysis
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high
                FROM metar_observations
                WHERE station=? AND temp_f IS NOT NULL AND date_utc < ?
                GROUP BY date_utc
                ORDER BY date_utc ASC
            """, (station, date))

            days_data = []
            for r in cur.fetchall():
                if r[1] is not None:  # high temp
                    days_data.append({
                        'date': r[0],
                        'high': r[1]
                    })

            # We need to find yesterday's high (the day before the forecast day)
            if len(days_data) < 2:
                return None, 0.0
            
            # Get the most recent day that's before the current date (which is our forecast reference)
            # This uses the last 2+ days of data to perform the analysis on the "date"
            if len(days_data) < self.window_days + 1:
                return None, 0.0
            
            # Take the most recent days to perform the analysis (based on when we have data)
            recent_data = days_data[-(self.window_days + 1):]  # +1 to get yesterday plus window
            if len(recent_data) < 2:
                return None, 0.0
            
            # Get yesterday's high (the last available historical data)
            yesterday_data = recent_data[-1]  # Latest is "yesterday" relative to forecast target
            yesterday_high = yesterday_data.get('high')
            if yesterday_high is None:
                return None, 0.0
            
            # Get the week before that for rolling mean
            week_days = recent_data[-(self.window_days + 1):-1]  # Exclude the "yesterday" itself
            if len(week_days) < 3:
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

        finally:
            if own_conn and conn:
                conn.close()