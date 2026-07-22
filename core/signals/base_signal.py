#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 2. [2026-07-06 fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL]
#

"""
Base Signal Interface for Weather Engine

All signals in core/signals/ should implement this interface so that
both the backtest scripts and the paper trading engine can use the
same signal code path.

Usage:
    from core.signals import BaseSignal, SignalRegistry
    registry = SignalRegistry(db_path)
    signal = registry.get_signal('reversion')
    direction, confidence = signal.evaluate(idx, days)
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, Dict
import sqlite3
import logging
from ..sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection


logger = logging.getLogger(__name__)


def _window(days, idx, n, offset=1):
    """Return n values before idx-offset, exclusive of the target day.

    Args:
        days: List of daily weather dicts
        idx: Current day index
        n: Number of values to return
        offset: How many days before idx to start (default 1 = yesterday)

    Returns:
        List of n dicts, or None if insufficient history
    """
    start = idx - n - offset
    end = idx - offset
    if start < 0:
        return None  # insufficient history
    return days[start:end]


def _safe_get(days, idx, field, default=None):
    """Safely retrieve a field from days[idx], returning default if unavailable.

    Args:
        days: List of daily weather dicts
        idx: Index to access
        field: Key to retrieve
        default: Default value if idx OOB or field is None

    Returns:
        Field value or default
    """
    if idx < 0 or idx >= len(days):
        return default
    val = days[idx].get(field)
    return val if val is not None else default


def validate_signal(func):
    """Decorator that validates evaluate() return values."""
    def wrapper(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        if result is None:
            return None, 0.0
        direction, confidence = result
        if direction is not None and direction not in ('up', 'down'):
            logger.warning(
                f"Signal '{self.name}' returned invalid direction={direction!r}. "
                f"Expected 'up', 'down', or None. Returning (None, 0.0)."
            )
            return None, 0.0
        confidence = max(0.0, min(1.0, confidence if confidence is not None else 0.0))
        return direction, confidence
    return wrapper


class BaseSignal(ABC):
    """
    Abstract base class for all weather trading signals.

    A signal takes historical daily weather data and produces a directional
    prediction (up/down) with a confidence score.

    Signals must be deterministic — no ML/AI in the prediction loop.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical name for this signal."""
        ...

    @property
    @abstractmethod
    def min_lookback(self) -> int:
        """Minimum number of prior days required for this signal to fire."""
        ...

    @abstractmethod
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate the signal at day index `idx` given historical `days`.

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
        """
        ...

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.

        Default implementation loads daily data from metar_observations
        and delegates to evaluate(). Subclasses may override for custom logic.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB

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
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                       AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                       AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                       AVG(pressure_mb) as pressure
                FROM metar_observations
                WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
                GROUP BY date_utc ORDER BY date_utc ASC
            """, (station,))
            days = []
            for r in cur.fetchall():
                if any(v is None for v in r[1:]):
                    continue
                days.append({
                    'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                    'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
                })

            # Find the index for the target date
            target_idx = None
            for i, d in enumerate(days):
                if d['date'] == date:
                    target_idx = i
                    break

            if target_idx is None:
                return None, 0.0

            return self.evaluate(target_idx, days)
        finally:
            if own_conn and conn:
                conn.close()
