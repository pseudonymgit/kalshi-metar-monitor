#!/usr/bin/env python3
"""
Pressure Tendency Signal — Intraday 3-Hour Pressure Change Signal

Calculates the 3-hour pressure change rate from raw METAR observations
and uses barometric tendency as a short-term directional predictor.

This is DIFFERENT from PressureDeltaSignal (which is a daily signal comparing
today vs yesterday). This is a 3-hour intraday signal.

Logic:
- Read METAR observations for the last 3 hours
- Calculate ΔP = P(now) - P(3h ago)
- Rapid pressure drop (>3hPa/3h) → expect warming in next 1-2h (incoming warm front)
- Rapid pressure rise (>3hPa/3h) → expect cooling (incoming cold front/ridge)
- Confidence: |ΔP| / 6.0, capped at 0.8
"""

from typing import Optional, Tuple
import sqlite3
import logging
from datetime import datetime, timedelta

from .base_signal import BaseSignal, validate_signal

logger = logging.getLogger(__name__)


class PressureTendencySignal(BaseSignal):
    """
    Pressure Tendency Signal — 3-hour intraday pressure change.

    Uses barometric pressure tendency from raw METAR observations to predict
    short-term temperature direction:
    - Rapid pressure drop → warming (incoming warm front)
    - Rapid pressure rise → cooling (incoming cold front/ridge)
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "pressure_tendency"

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
        Evaluate pressure tendency signal for a specific station and date.

        Computes the 3-hour pressure change rate from the most recent
        METAR observations and predicts temperature direction.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar_backfill.db
            market_type: 'HIGH' or 'LOW' — determines direction mapping

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            cur = conn.cursor()

            # Get the most recent observations with pressure data
            cur.execute("""
                SELECT pressure_mb, temp_f, timestamp_utc
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND pressure_mb IS NOT NULL
                ORDER BY timestamp_utc DESC
                LIMIT 20
            """, (station, date))

            rows = cur.fetchall()
            if len(rows) < 2:
                return None, 0.0

            # Latest pressure reading
            latest_pressure = rows[0][0]
            latest_ts = rows[0][2]
            if latest_pressure is None:
                return None, 0.0

            # Parse latest timestamp
            try:
                latest_dt = datetime.strptime(latest_ts.split('+')[0].split('Z')[0], '%Y-%m-%dT%H:%M:%S')
            except ValueError:
                try:
                    latest_dt = datetime.strptime(latest_ts.split('+')[0].split('Z')[0], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    return None, 0.0

            # Find observation approximately 3 hours before latest
            three_hours_ago = latest_dt - timedelta(hours=3)
            past_pressure = None

            for pressure_mb, temp_f, ts in rows:
                if ts is None:
                    continue
                try:
                    obs_dt = datetime.strptime(ts.split('+')[0].split('Z')[0], '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    try:
                        obs_dt = datetime.strptime(ts.split('+')[0].split('Z')[0], '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue

                if obs_dt <= three_hours_ago and pressure_mb is not None:
                    past_pressure = pressure_mb
                    break

            # Fallback: use earliest observation if we have at least 1 hour gap
            if past_pressure is None and len(rows) >= 2:
                oldest_pressure = rows[-1][0]
                oldest_ts = rows[-1][2]
                if oldest_pressure is not None and oldest_ts is not None:
                    try:
                        oldest_dt = datetime.strptime(oldest_ts.split('+')[0].split('Z')[0], '%Y-%m-%dT%H:%M:%S')
                    except ValueError:
                        try:
                            oldest_dt = datetime.strptime(oldest_ts.split('+')[0].split('Z')[0], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            oldest_dt = None

                    if oldest_dt is not None:
                        hours_diff = (latest_dt - oldest_dt).total_seconds() / 3600.0
                        if hours_diff >= 1.0:
                            past_pressure = oldest_pressure

            if past_pressure is None:
                return None, 0.0

            # Compute 3-hour pressure change
            delta_p = latest_pressure - past_pressure
            threshold = 3.0  # 3 hPa / 3h

            if delta_p < -threshold:
                # Pressure dropping → warm front approaching → warming
                confidence = min(abs(delta_p) / 6.0, 0.8)
                return 'up', confidence

            elif delta_p > threshold:
                # Pressure rising → cold front/ridge → cooling
                confidence = min(abs(delta_p) / 6.0, 0.8)
                return 'down', confidence

            return None, 0.0

        finally:
            if own_conn and conn:
                conn.close()


def test_signal():
    """Quick test."""
    signal = PressureTendencySignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")
    print("Test complete.")


if __name__ == "__main__":
    test_signal()