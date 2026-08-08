#!/usr/bin/env python3
"""
P0 Signal: Cloud Cover Index (CCI)

Source:        `hourly.cloud` (integer 0-100) from `weatherapi_archive.db`
Formula:       CCI_h = cloud / 100.0  (per hour, scaled to [0, 1])
Daily metric:  Mean of 6-hour rolling means across the day's hours.
               Falls back to a plain daily mean when fewer than 3 consecutive
               hours are available (common for sparsely-sampled stations).
Climatology:   Station-month baseline (mean, std) of the daily CCI metric.
Direction:     Anomalously high cloud (cloudier than normal) → 'down'
               (reduced solar insolation → cooler temperatures).
               Anomalously low cloud (clearer than normal) → 'up'.
Edge:          Cloud cover drives solar irradiance. Two days at identical
               METAR temperatures (e.g. 20°C) can be clear vs overcast.
               CCI is an orthogonal correction — no METAR field captures
               this directly.
Independence:  Strong. Correlation with METAR temp is ~0.10-0.25.
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
from .weatherapi_base_signal import WeatherAPIBaseSignal, validate_signal


class CloudCoverIndexSignal(WeatherAPIBaseSignal):
    """Signal that compares cloud cover against its station-month climatology.

    Fires when the daily CCI z-score exceeds ±1.0 relative to the same
    station-month baseline.
    """

    METRIC_KEY = "cci"
    Z_THRESHOLD = 1.0
    CONF_SCALE = 3.0
    DIR_HIGH = "down"   # cloudier → cooler → predict DOWN
    DIR_LOW = "up"      # clearer → warmer → predict UP

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "cloud_cover_index"

    def _metric_from_hour_map(
        self, hour_map: Dict[int, Tuple]
    ) -> Optional[float]:
        """Compute the daily CCI metric using 6-hour rolling means.

        1. Build a sorted {hour: CCI} map from the hourly data.
        2. For each hour, compute a 6-hour rolling mean of CCI (require ≥3
           of the 6 slots to be present).
        3. The daily metric is the mean of all valid 6-hour rolling windows.
        4. If fewer than one valid window, fall back to the plain daily mean.
        """
        # Build hour → cci, skipping null cloud
        hour_cci: Dict[int, float] = {}
        for hour, tup in hour_map.items():
            cloud = tup[0]
            if cloud is not None:
                hour_cci[hour] = cloud / 100.0

        if not hour_cci:
            return None

        hlist = sorted(hour_cci)
        rolling_means = []

        for i in range(len(hlist)):
            window = hlist[i : i + 6]
            window_vals = [hour_cci[h] for h in window if h in hour_cci]
            if len(window_vals) >= 3:
                rolling_means.append(sum(window_vals) / len(window_vals))

        if rolling_means:
            return sum(rolling_means) / len(rolling_means)
        else:
            vals = [hour_cci[h] for h in hlist]
            return sum(vals) / len(vals) if vals else None

    @validate_signal
    def evaluate(
        self, idx: int, days: List[Dict]
    ) -> Tuple[Optional[str], float]:
        """Evaluate CCI signal — delegates to base climatology comparison."""
        return super().evaluate(idx, days)

    def evaluate_for_station(
        self, station: str, date: str, conn: sqlite3.Connection = None
    ) -> Tuple[Optional[str], float]:
        """DB-backed evaluation — delegates to the base implementation."""
        return super().evaluate_for_station(station, date, conn)