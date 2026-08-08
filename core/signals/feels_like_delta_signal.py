#!/usr/bin/env python3
"""
P0 Signal: Feels-Like / Actual Temp Delta (ΔFL)

Source:        `hourly.temp_c`, `hourly.feelslike_c` from `weatherapi_archive.db`
Formula:       ΔFL_h = feelslike_c - temp_c  (per hour)
               - Positive → feels warmer than actual (heat index)
               - Negative → feels colder than actual (wind chill)
Daily metric:  Mean of hourly ΔFL values over the day.
Climatology:   Station-month baseline (mean, std) of the daily ΔFL metric.
Direction:     Anomalously positive ΔFL (stronger heat index than normal) → 'up'
               Anomalously negative ΔFL (stronger wind chill than normal) → 'down'
Edge:          Captures wind chill (extreme cold) and heat index (humid heat)
               — effects that drive human behavior (travel disruption, energy
               demand) but are invisible in raw METAR temperature.
Observed      Range: −47.1°C to +58.2°C. Moderately correlated with raw temp
               (~0.05-0.15), but the *delta from climatology* is the novel
               component the ensemble cannot derive from METAR.

Note on sign convention:
    The spec defines ΔFL_t = temp_c - feelslike_c, meaning "positive = actual
    feels warmer".  The task description instead says "Δ = feels_like -
    actual_temp. Large positive = heat index, large negative = wind chill."
    We follow the *task* convention (feelslike - temp) because it is
    physically  intuitive: positive Δ = heat index, negative Δ = wind chill.
    See the report for  the discrepancy.
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
from .weatherapi_base_signal import WeatherAPIBaseSignal, validate_signal


class FeelsLikeDeltaSignal(WeatherAPIBaseSignal):
    """Signal comparing the feels-like delta against its station-month climatology.

    Fires when the daily ΔFL z-score exceeds ±1.0 relative to the same
    station-month baseline.
    """

    METRIC_KEY = "feels_delta"
    Z_THRESHOLD = 1.0
    CONF_SCALE = 3.0
    DIR_HIGH = "up"    # stronger heat index than normal → predict UP (warmer)
    DIR_LOW = "down"   # stronger wind chill than normal → predict DOWN (cooler)

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "feels_like_delta"

    def _metric_from_hour_map(
        self, hour_map: Dict[int, Tuple]
    ) -> Optional[float]:
        """Compute the daily ΔFL metric.

        Hourly ΔFL = feelslike_c - temp_c.  The daily metric is the simple
        mean over all available hours.
        """
        deltas = []
        for hour, (cloud, temp_c, feelslike_c) in hour_map.items():
            if temp_c is not None and feelslike_c is not None:
                deltas.append(feelslike_c - temp_c)

        if not deltas:
            return None
        return sum(deltas) / len(deltas)

    @validate_signal
    def evaluate(
        self, idx: int, days: List[Dict]
    ) -> Tuple[Optional[str], float]:
        """Evaluate ΔFL signal — delegates to base climatology comparison."""
        return super().evaluate(idx, days)

    def evaluate_for_station(
        self, station: str, date: str, conn: sqlite3.Connection = None
    ) -> Tuple[Optional[str], float]:
        """DB-backed evaluation — delegates to the base implementation."""
        return super().evaluate_for_station(station, date, conn)