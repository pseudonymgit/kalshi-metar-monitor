#!/usr/bin/env python3
"""
WeatherAPI Base Signal — shared infrastructure for P0/P1 WeatherAPI signals.

Both P0 signals (Cloud Cover Index, Feels-Like Delta) read from the
`hourly` table of `data/weatherapi_archive.db`, aggregate hourly observations
into a per-day metric, and compare that metric against the *station-month
climatological baseline* (mean/std of the same metric for that calendar month
across all years). This is a pure, deterministic, B-Mode signal — no AI/ML in
the prediction loop.

Design notes
------------
* `evaluate(idx, days)` follows the BaseSignal contract. `days` is a daily
  series where each dict carries `date` and the signal's metric key
  (`cci` or `feels_delta`). The climatological baseline is computed from the
  in-memory series grouped by calendar month (excluding the target day).
* `evaluate_for_station(station, date, conn)` is the DB-backed path: it loads
  hourly rows from `weatherapi_archive.db`, builds the daily metric series,
  and delegates to `evaluate()`.
* Hourly coverage is uneven across stations (e.g. KSAT/KSEA/KSFO have only
  ~6k hourly rows vs ~145k for KATL). The daily metric therefore tolerates
  sparse hours and falls back from the 6-hour rolling mean to a plain daily
  mean when a day has too few consecutive hours.
"""

import math
import os
import sqlite3
from typing import Optional, Tuple, List, Dict

from .base_signal import BaseSignal, _safe_get, validate_signal


class WeatherAPIBaseSignal(BaseSignal):
    """Abstract base for signals sourced from the WeatherAPI archive."""

    #: Filename of the WeatherAPI archive DB (under <repo>/data/).
    DB_FILENAME = "weatherapi_archive.db"

    #: Column on the daily metric dict that `evaluate()` reads.
    METRIC_KEY = "metric"

    #: Minimum number of prior same-month samples required for a stable
    #: climatological baseline before the signal is allowed to fire.
    MIN_CLIMATOLOGY_SAMPLES = 20

    #: z-score threshold before the signal fires.
    Z_THRESHOLD = 1.0

    #: z-score scale used to map magnitude to confidence (capped at 0.6).
    CONF_SCALE = 3.0

    #: Direction to emit when the daily metric is *above* the baseline.
    DIR_HIGH = "up"
    #: Direction to emit when the daily metric is *below* the baseline.
    DIR_LOW = "down"

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    # ── abstract ──────────────────────────────────────────────────────────
    @property
    def min_lookback(self) -> int:
        # Enough prior days to establish a climatological baseline.
        return self.MIN_CLIMATOLOGY_SAMPLES + 1

    # ── helpers ───────────────────────────────────────────────────────────
    def _repr_(self) -> str:
        return f"<{type(self).__name__} name={self.name} db_path={self.db_path!r}>"

    def _resolve_db_path(self) -> Optional[str]:
        """Resolve the archivedb path, preferring an explicit db_path."""
        if self.db_path:
            return self.db_path
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        return os.path.join(repo_root, "data", self.DB_FILENAME)

    def _load_hourly(
        self, station: str, conn: sqlite3.Connection
    ) -> List[Tuple]:
        """Return hourly rows for a station ordered by date, hour."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date, hour, cloud, temp_c, feelslike_c
            FROM hourly
            WHERE station = ?
            ORDER BY date ASC, hour ASC
            """,
            (station,),
        )
        return cur.fetchall()

    def _build_daily_series(self, rows: List[Tuple]) -> List[Dict]:
        """Aggregate hourly rows into a daily metric series.

        Subclasses override `_metric_from_hour_map` to define how the per-day
        metric is derived from the {hour: (cloud, temp_c, feelslike_c)} map.

        Returns a list of dicts `{"date": str, metric_key: float}` ascending.
        """
        by_date: Dict[str, Dict[int, Tuple]] = {}
        for date, hour, cloud, temp_c, feelslike_c in rows:
            by_date.setdefault(date, {})[hour] = (cloud, temp_c, feelslike_c)

        series: List[Dict] = []
        for date in sorted(by_date):
            metric = self._metric_from_hour_map(by_date[date])
            if metric is not None:
                series.append({"date": date, self.METRIC_KEY: round(metric, 6)})
        return series

    def _metric_from_hour_map(
        self, hour_map: Dict[int, Tuple]
    ) -> Optional[float]:
        """Compute the daily metric from a {hour: (cloud,temp,feelslike)} map.

        Must be implemented by subclasses. Return None if the day has no
        usable observations.
        """
        raise NotImplementedError

    # ── evaluate ──────────────────────────────────────────────────────────
    @validate_signal
    def evaluate(
        self, idx: int, days: List[Dict]
    ) -> Tuple[Optional[str], float]:
        """Evaluate the signal at day index `idx` given the daily metric series.

        `days` is a list of dicts carrying `date` and `self.METRIC_KEY`.
        The target day is `days[idx-1]` (the last completed day), matching the
        convention used by the other signals in the ensemble. The climatological
        baseline is the mean/std of all *other* same-calendar-month samples in
        the series.
        """
        if idx < 1 or idx > len(days):
            return None, 0.0

        current = _safe_get(days, idx - 1, self.METRIC_KEY)
        if current is None:
            return None, 0.0

        cur_date = days[idx - 1].get("date", "")
        cur_month = cur_date[5:7] if len(cur_date) >= 7 else ""

        samples = []
        for k, d in enumerate(days):
            if k == idx - 1:
                continue
            val = d.get(self.METRIC_KEY)
            if val is None:
                continue
            d_month = d.get("date", "")[5:7] if len(d.get("date", "")) >= 7 else ""
            if d_month == cur_month:
                samples.append(val)

        if len(samples) < self.MIN_CLIMATOLOGY_SAMPLES:
            return None, 0.0

        mean = sum(samples) / len(samples)
        var = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
        std = math.sqrt(var) if var > 0 else 0.01

        z = (current - mean) / std

        if z >= self.Z_THRESHOLD:
            conf = min(z / self.CONF_SCALE, 0.6)
            return self.DIR_HIGH, conf
        if z <= -self.Z_THRESHOLD:
            conf = min(-z / self.CONF_SCALE, 0.6)
            return self.DIR_LOW, conf
        return None, 0.0

    def evaluate_for_station(
        self, station: str, date: str, conn: sqlite3.Connection = None
    ) -> Tuple[Optional[str], float]:
        """DB-backed evaluation for a station/date using `weatherapi_archive.db`."""
        db_path = self._resolve_db_path()
        if not db_path or not os.path.exists(db_path):
            return None, 0.0

        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(db_path)
        try:
            rows = self._load_hourly(station, conn)
            if not rows:
                return None, 0.0
            series = self._build_daily_series(rows)
            target_idx = None
            for i, d in enumerate(series):
                if d["date"] == date:
                    target_idx = i
                    break
            if target_idx is None:
                return None, 0.0
            # `evaluate()` treats pandas-free `days[idx-1]` as the last
            # completed (current) day. To evaluate the *target* day itself,
            # pass target_idx + 1 so that days[(target_idx+1)-1] == target.
            return self.evaluate(target_idx + 1, series)
        finally:
            if own_conn and conn:
                conn.close()