#!/usr/bin/env python3
"""
GHCN Calibration Signal — Calibrates METAR/ERA5 temperatures against
GHCN (Global Historical Climatology Network) station records for
long-term bias correction.

Uses data/ghcn_settlements.db (ghcn_settlements table) as the reference
ground-truth. When METAR readings deviate systematically from GHCN
normals, the signal predicts a directional correction.

B-Mode compliant. No AI/ML.
"""
import math
import signal
from typing import Optional, Tuple, List, Dict
from datetime import datetime
import sqlite3
import os

from .base_signal import BaseSignal, validate_signal

# Default paths
DEFAULT_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
GHCN_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/ghcn_settlements.db"

# GHCN climatology reference: number of days in seasonal window for normals
GHCN_SEASONAL_WINDOW = 15


def _compute_deviation_metrics(
    recent_temps: List[float],
    ghcn_means: List[float],
) -> Tuple[float, int]:
    """Compute mean deviation and consistent sign count.

    Args:
        recent_temps: List of recent METAR temperature readings.
        ghcn_means: List of corresponding GHCN reference values.

    Returns:
        (mean_deviation, consistent_sign_count)
    """
    if not recent_temps or not ghcn_means or len(recent_temps) != len(ghcn_means):
        return 0.0, 0

    deviations = [t - g for t, g in zip(recent_temps, ghcn_means) if t is not None and g is not None]
    if not deviations:
        return 0.0, 0

    mean_dev = sum(deviations) / len(deviations)

    # Count how many deviations have the same sign as the mean
    consistent = sum(1 for d in deviations if (d > 0 and mean_dev > 0) or (d < 0 and mean_dev < 0))

    return mean_dev, consistent


def _parse_via_date(date_str: str) -> datetime:
    """Parse YYYY-MM-DD to datetime, returning None on failure."""
    try:
        from datetime import datetime as dt
        return dt.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class GHCNCalibrationSignal(BaseSignal):
    """Calibrates METAR/ERA5 temperatures against GHCN station records.

    evaluate(): compares recent day temps against GHCD-derived normals
    computed from the days list itself (rolling lookback window).
    evaluate_for_station(): directly queries the GHCN settlements DB and
    METAR DB for station-specific bias correction.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path or DEFAULT_DB_PATH)
        self.calibration_window = 30

    @property
    def name(self) -> str:
        return "ghcn_calibration"

    @property
    def min_lookback(self) -> int:
        return self.calibration_window

    def _get_ghcn_climatology(self, station: str, target_date: str, window: int = 15) -> dict:
        """Get GHCN reference temperatures for a station around a target date.

        Queries ghcn_settlements.db for all records matching the station
        within ±window days of the target date's day-of-year across all years.

        Returns dict with:
            mean_high, mean_low, mean_temp, count, std_temp
        """
        if not station or not target_date:
            return {"mean_high": None, "mean_low": None, "mean_temp": None, "count": 0, "std_temp": None}

        ghcn_path = GHCN_DB_PATH
        if not os.path.exists(ghcn_path):
            return {"mean_high": None, "mean_low": None, "mean_temp": None, "count": 0, "std_temp": None}

        try:
            dt_val = _parse_via_date(target_date)
            if dt_val is None:
                return {"mean_high": None, "mean_low": None, "mean_temp": None, "count": 0, "std_temp": None}

            target_doy = dt_val.timetuple().tm_yday

            conn = sqlite3.connect(ghcn_path, timeout=10)
            cur = conn.cursor()

            # Query GHCN records near this day-of-year, NOT year-specific
            # GHCN is a long-term climatology reference
            cur.execute("""
                SELECT high_f, low_f
                FROM ghcn_settlements
                WHERE station = ? AND CAST(strftime('%j', date) AS INTEGER) BETWEEN ? AND ?
                AND high_f IS NOT NULL AND low_f IS NOT NULL
            """, (
                station.upper(),
                max(1, target_doy - window),
                min(366, target_doy + window),
            ))

            rows = cur.fetchall()
            conn.close()

            if not rows:
                # Try broader window
                conn = sqlite3.connect(ghcn_path, timeout=10)
                cur = conn.cursor()
                cur.execute("""
                    SELECT high_f, low_f
                    FROM ghcn_settlements
                    WHERE station = ? AND CAST(strftime('%j', date) AS INTEGER) BETWEEN ? AND ?
                    AND high_f IS NOT NULL AND low_f IS NOT NULL
                """, (
                    station.upper(),
                    max(1, target_doy - window * 2),
                    min(366, target_doy + window * 2),
                ))
                rows = cur.fetchall()
                conn.close()

            if not rows:
                return {"mean_high": None, "mean_low": None, "mean_temp": None,
                        "count": 0, "std_temp": None}

            highs = [r[0] for r in rows if r[0] is not None]
            lows = [r[1] for r in rows if r[1] is not None]
            temps = [(h + l) / 2.0 for h, l in zip(highs, lows)] if highs and lows else []

            count = len(temps)
            if count == 0:
                return {"mean_high": None, "mean_low": None, "mean_temp": None,
                        "count": 0, "std_temp": None}

            mean_temp = sum(temps) / count
            mean_high = sum(highs) / len(highs)
            mean_low = sum(lows) / len(lows)
            var = sum((t - mean_temp) ** 2 for t in temps) / count
            std_temp = math.sqrt(var) if var > 0 else 1.0

            return {
                "mean_high": mean_high,
                "mean_low": mean_low,
                "mean_temp": mean_temp,
                "count": count,
                "std_temp": std_temp,
            }
        except Exception:
            return {"mean_high": None, "mean_low": None, "mean_temp": None,
                    "count": 0, "std_temp": None}

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate GHCN calibration signal.

        Compares recent day temperatures against a rolling baseline computed
        from the days list. If the last N days show a systematic bias (average
        deviation > 1 std of the baseline), predict a correction in the
        opposite direction.

        This models the concept: GHCN tells us the "true" climatology; when
        METAR drifts, we expect a reversion toward the GHCN reference.
        """
        if idx < self.min_lookback:
            return None, 0.0

        # Extract recent temperatures, ensuring enough valid data
        recent_temps = []
        recent_window = min(self.calibration_window, idx)
        for i in range(idx - recent_window, idx + 1):
            d = days[i]
            if isinstance(d, dict) and d.get("temp") is not None:
                recent_temps.append(float(d["temp"]))

        if len(recent_temps) < 7:
            return None, 0.0

        # Compute baseline (rolling mean of the whole lookback period)
        all_temps = []
        for i in range(idx - recent_window, idx + 1):
            d = days[i]
            if isinstance(d, dict) and d.get("temp") is not None:
                all_temps.append(float(d["temp"]))

        if len(all_temps) < 5:
            return None, 0.0

        baseline_mean = sum(all_temps) / len(all_temps)
        variance = sum((t - baseline_mean) ** 2 for t in all_temps) / len(all_temps)
        baseline_std = math.sqrt(variance) if variance > 0 else 1.0

        # Compute deviation of most recent readings from baseline
        # Use the last third of the window as "recent" readings
        recent_count = max(3, len(recent_temps) // 3)
        recent_slice = recent_temps[-recent_count:]
        recent_mean = sum(recent_slice) / len(recent_slice)

        deviation = recent_mean - baseline_mean

        # Normalize deviation to standard deviations
        z_dev = deviation / baseline_std if baseline_std > 0 else 0.0

        # If |z_dev| > 0.5, there's a detectable bias -> predict correction
        abs_z = abs(z_dev)
        if abs_z < 0.5:
            return None, 0.0

        # Direction: if recent temps are above baseline (warm bias),
        # expect downward correction (predict "down").
        # If recent temps are below baseline (cold bias),
        # expect upward correction (predict "up").
        direction = "down" if z_dev > 0 else "up"

        # Confidence based on magnitude and consistency
        confidence = min(1.0, abs_z / 3.0)

        # Modulate by fraction of readings that agree
        same_sign_count = sum(
            1 for t in recent_slice
            if (t - baseline_mean > 0 and z_dev > 0) or (t - baseline_mean < 0 and z_dev < 0)
        )
        consistency = same_sign_count / len(recent_slice)
        confidence *= (0.5 + 0.5 * consistency)

        # Clamp
        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence

    def evaluate_for_station(
        self, station: str, date: str, conn=None
    ) -> Tuple[Optional[str], float]:
        """Station-level evaluation using GHCN DB bias correction.

        Queries GHCN settlements DB for the station's long-term climatology,
        queries METAR DB for recent readings, and predicts a directional
        correction based on systematic deviation.
        """
        if not station or not date:
            return None, 0.0

        # Get GHCN climatology for this station around this date
        ghcn = self._get_ghcn_climatology(station, date)
        if ghcn["mean_temp"] is None or ghcn["count"] < 20:
            # Not enough GHCN data to form a reference
            return None, 0.0

        # Get recent METAR readings for this station
        # Timeout handler
        def _timeout_handler(signum, frame):
            raise TimeoutError("metar query timed out")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(10)
        try:
            own_conn = conn is None
            if own_conn:
                conn = sqlite3.connect(self.db_path, timeout=10)
                conn.execute("PRAGMA busy_timeout=3000;")

            cur = conn.cursor()

            # Get up to calibration_window days of historical data before target date
            cur.execute("""
                SELECT date_utc, AVG(temp_f) as temp
                FROM metar_observations
                WHERE station=? AND date_utc < ? AND temp_f IS NOT NULL
                GROUP BY date_utc
                ORDER BY date_utc DESC
                LIMIT ?
            """, (station.upper(), date, self.calibration_window))

            recent_rows = cur.fetchall()

            if not recent_rows:
                if own_conn:
                    conn.close()
                return None, 0.0

            recent_temps = [float(r[1]) for r in recent_rows if r[1] is not None]
            if len(recent_temps) < 5:
                if own_conn:
                    conn.close()
                return None, 0.0

            # Compute recent METAR mean
            metar_mean = sum(recent_temps) / len(recent_temps)

            # The bias = METAR read - GHCN reference
            bias = metar_mean - ghcn["mean_temp"]

            if own_conn and conn:
                conn.close()
        except TimeoutError:
            return None, 0.0
        except Exception:
            return None, 0.0
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # Standardize the bias
        z_bias = bias / ghcn["std_temp"] if ghcn["std_temp"] > 0 else 0.0

        abs_z = abs(z_bias)
        if abs_z < 0.3:
            return None, 0.0

        # Direction: METAR warm bias → expect downward correction
        direction = "down" if bias > 0 else "up"

        confidence = min(1.0, abs_z / 3.0)
        # GHCN sample size adjustment: more GHCN reference points = higher confidence
        sample_factor = min(1.0, ghcn["count"] / 100.0)
        confidence *= (0.5 + 0.5 * sample_factor)

        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence