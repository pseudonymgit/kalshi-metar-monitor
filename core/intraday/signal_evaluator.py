"""
HourlySignalEvaluator — Intraday Hourly Signal Evaluation (v1.0 — 2026-09-07)

Evaluates all registered intraday signals against hourly-aggregated METAR data
for a given station and UTC hour.

Two output modes:
  - evaluate_hour(): raw signal names → {name: (direction, confidence)}
  - evaluate_hour_with_pool_names(): pool-compatible names → {pool_name: (direction, confidence)}

Deterministic math only — no AI/ML.
"""

import logging
import sqlite3
from typing import Dict, List, Optional, Tuple

from core.sqlite_utils import get_sqlite_connection

logger = logging.getLogger(__name__)

# ─── Registered intraday signals ──────────────────────────────
# Each is a (name, pool_name, detector_fn) tuple where detector_fn is a
# callable that takes (station, date_str, hour_utc, metar_data) and
# returns (direction, confidence) where direction is 'up'|'down'|None.

# Pool-compatible names must match the pool definitions in the fusion engine.
# Pool names group signals by method type:
#   'gefs' — derived from ensemble model output
#   'heuristic' — derived from METAR observations or rule-based reasoning
#   'intraday' — intraday-specific (time-of-day dependent)
#   'metar_trend' — METAR trend analysis
#   'frontal' — frontal passage detection

_INTRADAY_SIGNALS: Dict[str, Tuple[str, str, callable]] = {}


def _register_signal(name: str, pool_name: str, detector_fn: callable) -> None:
    """Register an intraday signal function."""
    _INTRADAY_SIGNALS[name] = (name, pool_name, detector_fn)


def get_registered_signals() -> Dict[str, Tuple[str, str, callable]]:
    """Return copy of the signal registry."""
    return dict(_INTRADAY_SIGNALS)


# ─── Built-in Detectors ──────────────────────────────────────


def _detect_pressure_tendency(station: str, date_str: str, hour_utc: int, metar_data: Dict) -> Tuple[Optional[str], float]:
    """
    Detect signal from rapid pressure changes.

    If pressure has been rising or falling > 2mb in 3 hours, predict:
      - Rising pressure → DOWN (cold air, clearing skies)
      - Falling pressure → UP (warm air, approaching front)

    Returns ('up', confidence) or ('down', confidence) or (None, 0.0).
    """
    vectors = metar_data.get("vectors", [])
    target = None
    for v in vectors:
        if v.hour_utc == hour_utc:
            target = v
            break

    if target is None or target.pressure_tendency_3h is None:
        return None, 0.0

    tendency = target.pressure_tendency_3h
    if abs(tendency) < 1.5:
        return None, 0.0

    confidence = min(0.6, 0.3 + abs(tendency) * 0.05)
    if tendency > 0:
        # Rising pressure → cold air settling → DOWN
        return "down", confidence
    else:
        # Falling pressure → warm air approaching → UP
        return "up", confidence


def _detect_temp_trend(station: str, date_str: str, hour_utc: int, metar_data: Dict) -> Tuple[Optional[str], float]:
    """
    Detect signal from rapid temperature changes.

    If temperature changes > 3°F in 3 hours, predict:
      - Rising temp → UP
      - Falling temp → DOWN
    """
    vectors = metar_data.get("vectors", [])
    target = None
    for v in vectors:
        if v.hour_utc == hour_utc:
            target = v
            break

    if target is None or target.temp_trend_3h is None:
        return None, 0.0

    trend = target.temp_trend_3h
    if abs(trend) < 2.0:
        return None, 0.0

    confidence = min(0.55, 0.25 + abs(trend) * 0.05)
    if trend > 0:
        return "up", confidence
    else:
        return "down", confidence


def _detect_wind_shift(station: str, date_str: str, hour_utc: int, metar_data: Dict) -> Tuple[Optional[str], float]:
    """
    Detect signal from significant wind direction shifts.

    Uses the dispersion of wind direction observations within the hour.
    """
    vectors = metar_data.get("vectors", [])
    target = None
    for v in vectors:
        if v.hour_utc == hour_utc:
            target = v
            break

    if target is None or target.wind_direction_deg is None:
        return None, 0.0

    # Check raw obs for wind variability
    raw_obs = metar_data.get("raw_obs_hour", {}).get(hour_utc, [])
    wind_dirs = [o.get("wind_direction_deg") for o in raw_obs if o.get("wind_direction_deg") is not None]
    if len(wind_dirs) < 3:
        return None, 0.0

    # Compute max-min circular dispersion
    min_dir = min(wind_dirs)
    max_dir = max(wind_dirs)
    dispersion = abs(max_dir - min_dir)
    if dispersion > 180:
        dispersion = 360 - dispersion

    if dispersion < 45:
        return None, 0.0

    # Assess direction: northerly shift → cold front → DOWN
    #                    southerly shift → warm front → UP
    avg_dir = sum(wind_dirs) / len(wind_dirs)
    if avg_dir < 180:
        return "down", min(0.55, 0.3 + dispersion * 0.002)
    else:
        return "up", min(0.55, 0.3 + dispersion * 0.002)


def _detect_cloud_cover(station: str, date_str: str, hour_utc: int, metar_data: Dict) -> Tuple[Optional[str], float]:
    """
    Detect signal from cloud cover patterns.

    High cloud cover + falling pressure → convective → uncertainty
    Clear skies + rising pressure → stable → DOWN
    """
    vectors = metar_data.get("vectors", [])
    target = None
    for v in vectors:
        if v.hour_utc == hour_utc:
            target = v
            break

    if target is None or target.cloud_cover_pct is None or target.pressure_mb is None:
        return None, 0.0

    cloud = target.cloud_cover_pct
    if cloud > 0.7:
        return None, 0.0  # Too cloudy — no clear signal

    if cloud < 0.2:
        # Clear skies — check pressure for direction
        if target.pressure_tendency_3h is not None and target.pressure_tendency_3h > 1.0:
            return "down", 0.45
        return "up", 0.35

    return None, 0.0


# ─── Register built-in signals ────────────────────────────────

_register_signal("intraday_pressure_tendency", "heuristic", _detect_pressure_tendency)
_register_signal("intraday_temp_trend", "heuristic", _detect_temp_trend)
_register_signal("intraday_wind_shift", "heuristic", _detect_wind_shift)
_register_signal("intraday_cloud_cover", "heuristic", _detect_cloud_cover)


# ─── Module-level helper ──────────────────────────────────────

def load_hourly_signal_hours(station: str, date_str: str, metar_db: str) -> Tuple[List[int], List[Dict]]:
    """
    Load hourly-aggregated METAR data for a station+date.

    Args:
        station: ICAO station code
        date_str: ISO date string "YYYY-MM-DD"
        metar_db: Path to METAR SQLite database

    Returns:
        (hours_with_data, metadata_list) where metadata_list is the
        list of HourlyVector dicts averaged over each hour bucket.
    """
    from .pipeline import HourlyPipeline

    pipeline = HourlyPipeline(
        metar_db=metar_db,
        asos_db="",
        nwp_db="",
    )
    vectors = pipeline.build_for_station_date(station, date_str)
    hours = [v.hour_utc for v in vectors]
    # Serialize vectors to dicts for module-level API
    metadata = [
        {
            "hour_utc": v.hour_utc,
            "hour_before_settlement": v.hour_before_settlement,
            "temperature_f": v.temperature_f,
            "dewpoint_f": v.dewpoint_f,
            "wind_speed_kt": v.wind_speed_kt,
            "wind_direction_deg": v.wind_direction_deg,
            "pressure_mb": v.pressure_mb,
            "cloud_cover_pct": v.cloud_cover_pct,
            "pressure_tendency_3h": v.pressure_tendency_3h,
            "temp_trend_3h": v.temp_trend_3h,
            "n_observations": v.n_observations,
        }
        for v in vectors
    ]
    return hours, metadata


# ─── Pool name map ──────────────────────────────────────────

_SIGNAL_TO_POOL: Dict[str, str] = {
    name: pool_name for name, (_, pool_name, _) in _INTRADAY_SIGNALS.items()
}


class HourlySignalEvaluator:
    """
    Evaluate intraday hourly signals for a given station, date, and UTC hour.

    Uses hourly-aggregated METAR data loaded from the METAR database.

    Usage:
        evaluator = HourlySignalEvaluator(metar_db="...")
        results = evaluator.evaluate_hour("KATL", "2026-09-07", 14)
        # results: {"intraday_pressure_tendency": ("up", 0.55), ...}
    """

    def __init__(self, metar_db: str = ""):
        self.metar_db = metar_db
        self._signal_registry = _INTRADAY_SIGNALS
        logger.info(f"HourlySignalEvaluator initialized: metar_db={metar_db}")

    # ─── Data Loading ─────────────────────────────────────────

    def load_hourly_signal_hours(
        self, station: str, date_str: str
    ) -> Tuple[List[int], Dict]:
        """
        Load hourly-aggregated METAR data for signal evaluation.

        Returns (hours_with_data, metadata_dict) where metadata_dict contains:
          - "vectors": List[HourlyVector] (already aggregated)
          - "raw_obs_hour": Dict[int, List[Dict]] (raw obs per hour)

        This is the canonical data loading method for intraday signals.
        Uses HourlyPipeline internally for consistency.
        """
        from .pipeline import HourlyPipeline

        pipeline = HourlyPipeline(
            metar_db=self.metar_db,
            asos_db="",
            nwp_db="",
        )

        vectors = pipeline.build_for_station_date(station, date_str)
        hours = [v.hour_utc for v in vectors]

        # Build raw obs per hour from the pipeline's hourly buckets
        raw_obs = {}
        try:
            conn = sqlite3.connect(self.metar_db, timeout=30)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp_utc, temp_f, wind_direction_deg, pressure_mb,
                       clouds_cover_pct, dewpoint_f, wind_speed_kt
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, date_str))
            for row in cur.fetchall():
                ts = row["timestamp_utc"]
                try:
                    if "T" in ts:
                        h = int(ts.split("T")[1].split(":")[0])
                    elif " " in ts:
                        h = int(ts.split(" ")[1].split(":")[0])
                    else:
                        continue
                except (ValueError, IndexError):
                    continue
                raw_obs.setdefault(h, []).append(dict(row))
            conn.close()
        except Exception as e:
            logger.debug(f"Raw obs load failed for {station} {date_str}: {e}")

        metadata = {
            "vectors": vectors,
            "raw_obs_hour": raw_obs,
        }

        return hours, metadata

    # ─── Signal Evaluation ─────────────────────────────────────

    def evaluate_hour(
        self, station: str, date_str: str, hour: int
    ) -> Dict[str, Tuple[Optional[str], float]]:
        """
        Evaluate all registered signals for a given station, date, and UTC hour.

        Args:
            station: ICAO station code (e.g., "KATL")
            date_str: ISO date string "YYYY-MM-DD"
            hour: UTC hour (0-23)

        Returns:
            Dict[str, Tuple[Optional[str], float]] — signal_name → (direction_or_None, confidence)
        """
        # Load data
        _, metadata = self.load_hourly_signal_hours(station, date_str)
        metar_data = {
            "vectors": metadata.get("vectors", []),
            "raw_obs_hour": metadata.get("raw_obs_hour", {}),
        }

        results: Dict[str, Tuple[Optional[str], float]] = {}
        for name, (_, _, detector_fn) in self._signal_registry.items():
            try:
                direction, confidence = detector_fn(station, date_str, hour, metar_data)
                # Guard against (None, confidence>0) — invalid contract
                if direction is None:
                    results[name] = (None, 0.0)
                else:
                    results[name] = (direction, min(1.0, max(0.0, confidence)))
            except Exception as e:
                logger.warning(f"Signal '{name}' failed for {station} h{hour}: {e}")
                results[name] = (None, 0.0)

        # Count signals that fired
        n_fired = sum(1 for v in results.values() if v[0] is not None and v[1] > 0)
        logger.debug(
            f"Signals for {station} {date_str} h{hour}: "
            f"{n_fired}/{len(results)} fired"
        )

        return results

    def evaluate_hour_with_pool_names(
        self, station: str, date_str: str, hour: int
    ) -> Dict[str, Tuple[Optional[str], float]]:
        """
        Same as evaluate_hour() but maps signal names to pool-compatible names
        for fusion engine consumption.

        Pool names are defined by the fusion engine and group signals
        by methodology type. This mapping is needed because the fusion engine
        routes signals to the correct pool definition.

        Args:
            station: ICAO station code
            date_str: ISO date string "YYYY-MM-DD"
            hour: UTC hour (0-23)

        Returns:
            Dict[str, Tuple[Optional[str], float]] — pool_name → (direction, confidence)
        """
        raw = self.evaluate_hour(station, date_str, hour)

        pool_results: Dict[str, Tuple[Optional[str], float]] = {}
        for signal_name, (direction, confidence) in raw.items():
            pool_name = _SIGNAL_TO_POOL.get(signal_name, "heuristic")
            # If two signals map to the same pool, keep the one with higher confidence
            existing = pool_results.get(pool_name)
            if existing is not None and existing[0] is not None:
                if direction is not None and confidence > existing[1]:
                    pool_results[pool_name] = (direction, confidence)
            else:
                pool_results[pool_name] = (direction, confidence)

        return pool_results