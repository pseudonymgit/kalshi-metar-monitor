#!/usr/bin/env python3
"""
FOGR Reversion Signal — Intraday METAR-Based Signal

Reads raw METAR observations from metar_backfill.db to detect overnight
dewpoint depression patterns that predict rapid morning warming.

Logic:
- Query metar_observations for early-morning (05:00-07:00 local time) observations
- Compute dewpoint depression = temp_f - dewpoint_f
- If dewpoint_depression < 2°F at ~06:00 local → predict +1.5-3°F warming by 10:00 local
- If dewpoint_depression > 15°F (dry air) → predict +0.5-1°F warming (suppressed)
- Signal: UP (warming) with confidence based on depression magnitude

This is an intraday-only signal; it does not use the daily evaluate(idx, days) pattern.
Instead, it queries raw METAR observations directly.

Reference: FOGR = Fog/Overnight Ground Reversion
"""

from typing import Optional, Tuple
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

from .base_signal import BaseSignal, validate_signal

logger = logging.getLogger(__name__)

# Station timezone offsets (hours from UTC) for local time computation
_STATION_TZ_OFFSETS = {
    'KATL': -5, 'KAUS': -6, 'KBOS': -5, 'KDCA': -5,
    'KDEN': -7, 'KDFW': -6, 'KHOU': -6, 'KLAS': -8,
    'KLAX': -8, 'KMDW': -6, 'KMIA': -5, 'KMSP': -6,
    'KMSY': -6, 'KNYC': -5, 'KOKC': -6, 'KPHL': -5,
    'KPHX': -7, 'KSAT': -6, 'KSEA': -8, 'KSFO': -8,
    'KJFK': -5, 'KORD': -6, 'KBNA': -6, 'KPDX': -8,
    'KSLC': -7, 'PHNL': -10, 'KTPA': -5, 'KDTW': -5,
    'KCLT': -5,
}


class FogrReversionSignal(BaseSignal):
    """
    FOGR (Fog Overnight Ground Reversion) Signal.

    Detects morning fog/stratus conditions from dewpoint depression near zero
    and predicts rapid warming as the sun burns off the fog layer.

    This is a UP-only signal (it only predicts warming).
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "fogr_reversion"

    @property
    def min_lookback(self) -> int:
        return 0  # Only needs current day's morning observations

    def _get_tz_offset(self, station: str) -> int:
        """Get timezone offset from UTC for a station."""
        return _STATION_TZ_OFFSETS.get(station, -5)  # Default Eastern

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
        Evaluate FOGR reversion signal for a specific station and date.

        Queries metar_observations for early-morning (~06:00 local) readings
        to compute dewpoint depression and predict morning warming.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar_backfill.db
            market_type: 'HIGH' (warming) or 'LOW' (cooling) — FOGR is UP-only

        Returns:
            ('up', confidence) or (None, 0.0)
        """
        # FOGR is only relevant for HIGH markets (predicts warming to daily max)
        if market_type == 'LOW':
            return None, 0.0

        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            tz_offset = self._get_tz_offset(station)

            # Compute UTC time range for 05:00-07:00 local time
            local_early_morning_start = 5 - tz_offset  # 05:00 local in UTC
            local_early_morning_end = 7 - tz_offset    # 07:00 local in UTC

            # Handle negative UTC hours (wrap to previous day)
            if local_early_morning_start < 0:
                query_date = datetime.strptime(date, '%Y-%m-%d') - timedelta(days=1)
                query_date_str = query_date.strftime('%Y-%m-%d')
                utc_start = local_early_morning_start + 24
                utc_end = local_early_morning_end + 24
                if utc_end >= 24:
                    utc_end = 23
            else:
                query_date_str = date
                utc_start = local_early_morning_start
                utc_end = local_early_morning_end

            cur = conn.cursor()
            cur.execute("""
                SELECT temp_f, dewpoint_f, timestamp_utc
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND temp_f IS NOT NULL AND dewpoint_f IS NOT NULL
                  AND cast(substr(timestamp_utc, 12, 2) AS INTEGER) BETWEEN ? AND ?
                ORDER BY timestamp_utc ASC
            """, (station, query_date_str, utc_start, utc_end))

            rows = cur.fetchall()
            if not rows:
                return None, 0.0

            # Compute average dewpoint depression from early-morning obs
            depressions = []
            for temp_f, dewpoint_f, ts in rows:
                if temp_f is not None and dewpoint_f is not None:
                    depressions.append(temp_f - dewpoint_f)

            if not depressions:
                return None, 0.0

            mean_depression = sum(depressions) / len(depressions)
            min_depression = min(depressions)

            # FOGR logic: low dewpoint depression → fog → rapid warming
            if mean_depression < 2.0:
                # Strong fog signal — predict +1.5-3°F warming
                # Confidence inversely proportional to depression
                confidence = max(0.3, min(0.9, 1.0 - (mean_depression / 2.0)))
                return 'up', confidence

            elif mean_depression < 5.0:
                # Moderate fog potential — predict moderate warming
                confidence = max(0.2, min(0.6, 0.5 - (mean_depression / 10.0)))
                return 'up', confidence

            elif mean_depression > 15.0:
                # Very dry air — warming suppressed, low confidence
                confidence = max(0.1, min(0.4, 0.3))
                return 'up', confidence

            # No clear signal
            return None, 0.0

        finally:
            if own_conn and conn:
                conn.close()


def test_signal():
    """Quick test."""
    signal = FogrReversionSignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")
    print("Test complete.")


if __name__ == "__main__":
    test_signal()