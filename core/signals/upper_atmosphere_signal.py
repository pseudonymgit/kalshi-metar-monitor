"""
Sprint 3 — NWP Upper-Atmosphere Signals

Two signals derived from NWP forecast upper-air fields in nwp_forecasts.db:

1. 850mb_temp_advection (EightHundredFiftyMbAdvectionSignal)
   - Uses the pre-computed `advection_850hPa` variable (or, where absent, a
     wind-vector advection proxy) at 850 hPa.
   - Warm advection (positive, > 0) -> predict UP; cold advection -> DOWN.

2. 500mb_height_pattern (FiveHundredMbHeightPatternSignal)
   - Evaluates 500 hPa geopotential height anomaly relative to recent history
     (rolling z-score).
   - Higher heights (positive z) -> ridging/warm -> UP;
     lower heights (negative z) -> troughing/cold -> DOWN.

Reference: Gray Room E3 projection (62-65%).

Deterministic math only. No AI/ML in loop.

Re-authored 2026-09-19 (class bodies were lost in the wipe; recovered .pyc held
only the module docstring + constants).
"""

import math
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.signals.base_signal import BaseSignal, validate_signal
from core.nwp_lookahead_gate import contemporaneous_where

logger = logging.getLogger(__name__)

NWP_DB_DEFAULT = 'data/nwp_forecasts.db'

# 850mb advection signal tuning.
MAX_CONFIDENCE_850 = 0.85
Z_THRESHOLD_850 = 0.5
ROLLING_WINDOW_850 = 20
DEFAULT_STD_ADVECTION = 5e-05

# 500mb height signal tuning.
MAX_CONFIDENCE_500 = 0.80
Z_THRESHOLD_500 = 0.6
ROLLING_WINDOW_500 = 30
DEFAULT_STD_HEIGHT = 60.0


def _resolve_db(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    # Resolve relative to the repo root (signals may be imported from anywhere).
    from pathlib import Path as _P
    here = _P(__file__).resolve()
    return str(here.parent.parent.parent / NWP_DB_DEFAULT)


def _fetch_series(conn: sqlite3.Connection, station: str, variable: str,
                 end_date: str, limit: int) -> List[Tuple[str, float]]:
    """Return (date, value) pairs for a variable, ascending, up to end_date.

    Contemporaneous-only gate (P0-F): only rows with fetch_date <= target_date
    are admissible.  The upper-air fields (geopotential_height_500hPa_daily_mean,
    temperature_850hPa_daily_mean) were 37,260 contaminated (station,target_date)
    keys vs 2,800 contemporaneous — 34,460 keys existed ONLY via lookahead
    (reanalysis/backfill with fetch_date > target_date).
    """
    cur = conn.execute(
        """
        SELECT target_date, value
        FROM nwp_forecasts
        WHERE station = ? AND variable = ? AND target_date <= ?
        """ + contemporaneous_where("") + " ORDER BY target_date DESC LIMIT ?",
        (station, variable, end_date, limit),
    )
    rows = cur.fetchall()
    rows.reverse()
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _z_score(value: float, history: List[float], default_std: float) -> Optional[float]:
    """Rolling z-score of `value` against `history` (excluding the value itself)."""
    if not history:
        return None
    n = len(history)
    mean = sum(history) / n
    var = sum((x - mean) ** 2 for x in history) / n
    std = math.sqrt(var)
    if std < 1e-12:
        std = default_std
    return (value - mean) / std


class EightHundredFiftyMbAdvectionSignal(BaseSignal):
    """850mb temperature advection -> directional temperature change."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path or _resolve_db(None))

    @property
    def name(self) -> str:
        return "850mb_temp_advection"

    @property
    def min_lookback(self) -> int:
        return ROLLING_WINDOW_850

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        # This signal reads NWP fields, not METAR `days`. The generic evaluate()
        # is not meaningful here; delegate is via evaluate_for_station.
        return (None, 0.0)

    def evaluate_for_station(self, station: str, date: str,
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path)
        try:
            series = _fetch_series(conn, station, 'advection_850hPa', date,
                                   ROLLING_WINDOW_850 + 1)
            if len(series) < 2:
                return (None, 0.0)

            # The last element is the target-day advection value.
            target_date, adv = series[-1]
            history = [v for (_, v) in series[:-1]]
            if target_date != date:
                return (None, 0.0)

            z = _z_score(adv, history, DEFAULT_STD_ADVECTION)
            if z is None:
                return (None, 0.0)

            if z > Z_THRESHOLD_850:
                confidence = min(MAX_CONFIDENCE_850, 0.5 + abs(z) * 0.25)
                return ('up', confidence)
            if z < -Z_THRESHOLD_850:
                confidence = min(MAX_CONFIDENCE_850, 0.5 + abs(z) * 0.25)
                return ('down', confidence)
            return (None, 0.0)
        except sqlite3.Error as e:
            logger.warning(f"850mb advection read failed for {station}: {e}")
            return (None, 0.0)
        finally:
            if own_conn and conn:
                conn.close()


class FiveHundredMbHeightPatternSignal(BaseSignal):
    """500mb geopotential height anomaly -> ridging (UP) vs troughing (DOWN)."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path or _resolve_db(None))

    @property
    def name(self) -> str:
        return "500mb_height_pattern"

    @property
    def min_lookback(self) -> int:
        return ROLLING_WINDOW_500

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        # Reads NWP fields, not METAR `days`; delegate via evaluate_for_station.
        return (None, 0.0)

    def evaluate_for_station(self, station: str, date: str,
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path)
        try:
            series = _fetch_series(conn, station, 'geopotential_height_500hPa_daily_mean',
                                   date, ROLLING_WINDOW_500 + 1)
            if len(series) < 2:
                return (None, 0.0)

            target_date, height = series[-1]
            history = [v for (_, v) in series[:-1]]
            if target_date != date:
                return (None, 0.0)

            z = _z_score(height, history, DEFAULT_STD_HEIGHT)
            if z is None:
                return (None, 0.0)

            if z > Z_THRESHOLD_500:
                confidence = min(MAX_CONFIDENCE_500, 0.5 + abs(z) * 0.2)
                return ('up', confidence)
            if z < -Z_THRESHOLD_500:
                confidence = min(MAX_CONFIDENCE_500, 0.5 + abs(z) * 0.2)
                return ('down', confidence)
            return (None, 0.0)
        except sqlite3.Error as e:
            logger.warning(f"500mb height read failed for {station}: {e}")
            return (None, 0.0)
        finally:
            if own_conn and conn:
                conn.close()
