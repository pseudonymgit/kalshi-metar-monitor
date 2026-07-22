#!/usr/bin/env python3
"""
METAR dT/dt Signal — Intraday Temperature Trend Signal

Calculates the 3-hour temperature change rate from raw METAR observations
and uses the recent trend as a directional predictor.

Logic:
- Read METAR observations for the last 3 hours
- Calculate ΔT = T(now) - T(3h ago)
- If ΔT > +2°F/3h → predict UP (continuing warming) with confidence = ΔT/5
- If ΔT < -2°F/3h → predict DOWN (continuing cooling) with confidence = |ΔT|/5
- Otherwise → no signal

This is an intraday-only signal using raw METAR timestamps.
"""

from typing import Optional, Tuple
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

from .base_signal import BaseSignal, validate_signal
from ..sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

logger = logging.getLogger(__name__)


class MetarDtdtSignal(BaseSignal):
    """
    METAR dT/dt Signal — 3-hour temperature change rate.

    Uses the most recent 3 hours of METAR observations to detect
    sustained warming or cooling trends and predict continuation.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "metar_dtdt"

    @property
    def min_lookback(self) -> int:
        return 0  # Only needs recent observations

    @validate_signal
    def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
        """
        Standard evaluate interface — not applicable to intraday signals.
        Returns (None, 0.0). Use evaluate_for_station() instead.
        """
        return None, 0.0

    def evaluate_for_station(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection = None,
        market_type: str = 'HIGH'
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate METAR dT/dt signal for a specific station and date.

        Computes the 3-hour temperature change rate from the most recent
        METAR observations and predicts trend continuation.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar_backfill.db
            market_type: 'HIGH' or 'LOW' — market type being predicted

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = get_sqlite_connection(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            cur = conn.cursor()

            # For HIGH markets, focus on the most recent observations of the day
            # For LOW markets, we look at the overnight period
            # In both cases, we want the last 3 hours of data

            # Get the most recent observations for this station and date
            # Order by timestamp descending to get the latest first
            cur.execute("""
                SELECT temp_f, timestamp_utc
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc DESC
                LIMIT 20
            """, (station, date))

            rows = cur.fetchall()
            if len(rows) < 2:
                return None, 0.0

            # The most recent observation is the current temperature
            latest_temp = rows[0][0]
            latest_ts = rows[0][1]
            if latest_temp is None:
                return None, 0.0

            # Parse latest timestamp
            try:
                latest_dt = datetime.strptime(latest_ts, '%Y-%m-%dT%H:%M:%S')
            except ValueError:
                try:
                    latest_dt = datetime.strptime(latest_ts, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    return None, 0.0

            # Find an observation approximately 3 hours before the latest
            three_hours_ago = latest_dt - timedelta(hours=3)
            past_temp = None

            for temp_f, ts in rows:
                if ts is None:
                    continue
                try:
                    obs_dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    try:
                        obs_dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue

                # Find the closest observation to 3 hours ago
                if obs_dt <= three_hours_ago:
                    if temp_f is not None:
                        past_temp = temp_f
                    break

            # If we didn't find an observation exactly 3 hours ago, use the
            # earliest observation within the last 3 hours
            if past_temp is None and len(rows) >= 2:
                # Use the second most recent if we have at least 2 obs
                # that are at least 1 hour apart
                oldest_temp = rows[-1][0]
                oldest_ts = rows[-1][1]
                if oldest_temp is not None and oldest_ts is not None:
                    try:
                        oldest_dt = datetime.strptime(oldest_ts, '%Y-%m-%dT%H:%M:%S')
                    except ValueError:
                        try:
                            oldest_dt = datetime.strptime(oldest_ts, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            oldest_dt = None

                    if oldest_dt is not None:
                        hours_diff = (latest_dt - oldest_dt).total_seconds() / 3600.0
                        if hours_diff >= 1.0:
                            past_temp = oldest_temp

            if past_temp is None:
                return None, 0.0

            # Compute the 3-hour temperature change rate
            # Normalize to 3-hour rate if we have a different time span
            if past_temp is not None:
                # Use the actual time span of available data
                delta_t = latest_temp - past_temp

                # Normalize confidence based on magnitude of change
                abs_delta_t = abs(delta_t)
                threshold = 2.0  # 2°F / 3h

                if delta_t > threshold:
                    # Warming trend
                    confidence = min(abs_delta_t / 5.0, 0.85)
                    return 'up', confidence
                elif delta_t < -threshold:
                    # Cooling trend
                    confidence = min(abs_delta_t / 5.0, 0.85)
                    return 'down', confidence

            return None, 0.0

        finally:
            if own_conn and conn:
                conn.close()


def test_signal():
    """Quick test."""
    signal = MetarDtdtSignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")
    print("Test complete.")


if __name__ == "__main__":
    test_signal()