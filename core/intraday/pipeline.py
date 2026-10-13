"""
HourlyPipeline — Intraday Feature Vector Builder (v1.0 — 2026-09-07)

Reads weather data from METAR DB, ASOS (IEM 1-min) DB, and NWP DB to
produce hourly feature vectors for a given station and date.

Each HourlyVector captures:
  - hour_utc: UTC hour (0-23)
  - temperature_f: current temperature (°F)
  - dewpoint_f: dewpoint (°F)
  - wind_speed_kt: wind speed (knots)
  - wind_direction_deg: wind direction (degrees)
  - pressure_mb: sea-level pressure (mb)
  - cloud_cover_pct: estimated cloud cover fraction (0-1)
  - pressure_tendency_3h: pressure change over 3 hours (mb)
  - temp_trend_3h: temperature change over 3 hours (°F)
  - hour_before_settlement: hours until settlement (0 = settlement hour)

Deterministic math only — no AI/ML.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# om_gfs (Open-Meteo bias-corrected GFS) substitution (Workstream P). Optional
# import: if unavailable the flag is simply inert and the raw local GFS path is
# used unchanged.
try:
    from core.intraday.om_gfs_nwp import (
        om_gfs_enabled,
        om_gfs_state_for_day,
    )
    OM_GFS_AVAILABLE = True
except Exception:  # noqa: BLE001 - optional dependency
    OM_GFS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─── Settlement UTC hour (Kalshi HIGH markets settle ~18 UTC / 12 ET) ────
SETTLEMENT_HOUR_UTC = 18


@dataclass
class HourlyVector:
    """Single hourly feature vector for intraday signal evaluation."""
    station: str
    date_str: str
    hour_utc: int
    hour_before_settlement: int

    # Weather fields (may be None if observation is unavailable)
    temperature_f: Optional[float] = None
    dewpoint_f: Optional[float] = None
    wind_speed_kt: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    pressure_mb: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    pressure_tendency_3h: Optional[float] = None
    temp_trend_3h: Optional[float] = None

    # Metadata
    n_observations: int = 0        # Number of raw METAR observations in this bucket
    metar_observations: List[Dict] = field(default_factory=list)  # Raw obs for signal evaluators

    @property
    def is_valid(self) -> bool:
        """Return True if at least one weather field is populated."""
        return any(
            v is not None for v in [
                self.temperature_f, self.dewpoint_f, self.wind_speed_kt,
                self.wind_direction_deg, self.pressure_mb,
            ]
        )


def _get_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-write SQLite connection with WAL mode."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def _hour_to_hour_before_settlement(hour_utc: int) -> int:
    """
    Compute hours before settlement (18 UTC).

    Returns max(0, 18 - hour_utc).
    """
    return max(0, SETTLEMENT_HOUR_UTC - hour_utc)


class HourlyPipeline:
    """
    Build hourly feature vectors for a station+date from multiple DB sources.

    Sources:
      - METAR DB: hourly-aggregated METAR observations
      - ASOS DB: 1-minute IEM ASOS (sub-hourly precision)
      - NWP DB: Numerical Weather Prediction forecasts

    Usage:
        pipeline = HourlyPipeline(metar_db="...", asos_db="...", nwp_db="...")
        vectors = pipeline.build_for_station_date("KATL", "2026-09-07")
    """

    def __init__(
        self,
        metar_db: str = "",
        asos_db: str = "",
        nwp_db: str = "",
    ):
        self.metar_db = metar_db
        self.asos_db = asos_db
        self.nwp_db = nwp_db
        logger.info(
            f"HourlyPipeline initialized: metar={metar_db}, asos={asos_db}, nwp={nwp_db}"
        )

    # ── METAR Hourly Buckets ──────────────────────────────────

    def _load_metar_hourly(self, station: str, date_str: str) -> Dict[int, List[Dict]]:
        """
        Load METAR observations from the METAR DB, bucketed by UTC hour.

        Returns dict[hour_utc] -> list of observation dicts.
        Returns empty dict if DB doesn't exist or query fails.
        """
        if not self.metar_db:
            return {}

        try:
            conn = _get_connection(self.metar_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Query obs for this station and date
            # NOTE (WS-N 2026-09-27): metar_observations has no clouds_cover_pct
            # or ceil_humidity columns — querying them raised "no such column"
            # and silently yielded 0 calibrated stations. Select only real columns.
            cur.execute("""
                SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt,
                       wind_direction_deg, pressure_mb,
                       visibility_mi
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, date_str))

            hourly_buckets: Dict[int, List[Dict]] = {}
            for row in cur.fetchall():
                ts_str = row["timestamp_utc"]
                try:
                    # timestamp_utc can be 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DD HH:MM:SS'
                    if "T" in ts_str:
                        hour_utc = int(ts_str.split("T")[1].split(":")[0])
                    elif " " in ts_str:
                        hour_utc = int(ts_str.split(" ")[1].split(":")[0])
                    else:
                        continue
                except (ValueError, IndexError):
                    continue

                obs = dict(row)
                obs.pop("timestamp_utc", None)  # Already captured via hour
                hourly_buckets.setdefault(hour_utc, []).append(obs)

            conn.close()
            return hourly_buckets

        except Exception as e:
            logger.warning(f"Failed to load METAR hourly for {station} {date_str}: {e}")
            return {}

    def _aggregate_hour_bucket(self, obs_list: List[Dict]) -> Dict:
        """
        Aggregate a list of observations within a single UTC hour into a feature dict.

        Uses mean for continuous fields, last-observed for cloud cover.
        """
        if not obs_list:
            return {}

        temps = [o.get("temp_f") for o in obs_list if o.get("temp_f") is not None]
        dews = [o.get("dewpoint_f") for o in obs_list if o.get("dewpoint_f") is not None]
        winds = [o.get("wind_speed_kt") for o in obs_list if o.get("wind_speed_kt") is not None]
        wind_dirs = [o.get("wind_direction_deg") for o in obs_list if o.get("wind_direction_deg") is not None]
        pressures = [o.get("pressure_mb") for o in obs_list if o.get("pressure_mb") is not None]
        clouds = [o.get("clouds_cover_pct") for o in obs_list if o.get("clouds_cover_pct") is not None]

        result = {
            "temperature_f": sum(temps) / len(temps) if temps else None,
            "dewpoint_f": sum(dews) / len(dews) if dews else None,
            "wind_speed_kt": sum(winds) / len(winds) if winds else None,
            "wind_direction_deg": sum(wind_dirs) / len(wind_dirs) if wind_dirs else None,
            "pressure_mb": sum(pressures) / len(pressures) if pressures else None,
            "cloud_cover_pct": clouds[-1] if clouds else None,
            "n_observations": len(obs_list),
        }
        return result

    def _compute_trends(
        self, vectors: List[HourlyVector]
    ) -> List[HourlyVector]:
        """
        Compute 3-hour pressure and temperature trends for each hourly vector.

        Pressure tendency: pressure_mb[hour] - pressure_mb[hour-3]
        Temp trend: temp_f[hour] - temp_f[hour-3]

        Mutates vectors in place.
        """
        # Build hour map
        hour_map: Dict[int, HourlyVector] = {}
        for v in vectors:
            hour_map[v.hour_utc] = v

        for v in vectors:
            h = v.hour_utc
            if h >= 3:
                prev = hour_map.get(h - 3)
                if prev is not None:
                    if v.pressure_mb is not None and prev.pressure_mb is not None:
                        v.pressure_tendency_3h = v.pressure_mb - prev.pressure_mb
                    if v.temperature_f is not None and prev.temperature_f is not None:
                        v.temp_trend_3h = v.temperature_f - prev.temperature_f
        return vectors

    # ── ASOS 1-min Data (optional enrichment) ──────────────────

    def _enrich_from_asos(
        self, vectors: List[HourlyVector], station: str, date_str: str
    ) -> List[HourlyVector]:
        """
        Enrich hourly vectors with ASOS 1-minute data (sub-hour precision).

        Real schema (data/iem_asos_1min.db): a single `observations` table
        (NO per-station `t{station}` tables) with columns:
            station, valid ('YYYY-MM-DD HH:MM'), tmpf, dwpf, sknt, drct,
            alti, mslp, ...
        Prefers mslp (mb); falls back to alti (inHg -> mb).
        Fills ONLY null fields; METAR values are never overwritten.
        """
        if not self.asos_db:
            return vectors

        try:
            conn = _get_connection(self.asos_db)
            cur = conn.cursor()
            enriched = 0

            for v in vectors:
                cur.execute("""
                    SELECT AVG(tmpf) AS tmpf, AVG(dwpf) AS dwpf,
                           AVG(sknt) AS sknt, AVG(drct) AS drct,
                           AVG(mslp) AS mslp, AVG(alti) AS alti, COUNT(*) AS n
                    FROM observations
                    WHERE station = ? AND valid LIKE ?
                """, (station, f"{date_str} {v.hour_utc:02d}:%"))
                row = cur.fetchone()
                if not row or not row["n"]:
                    continue
                row_used = False
                if v.temperature_f is None and row["tmpf"] is not None:
                    v.temperature_f = float(row["tmpf"]); row_used = True
                if v.dewpoint_f is None and row["dwpf"] is not None:
                    v.dewpoint_f = float(row["dwpf"]); row_used = True
                if v.wind_speed_kt is None and row["sknt"] is not None:
                    v.wind_speed_kt = float(row["sknt"]); row_used = True
                if v.wind_direction_deg is None and row["drct"] is not None:
                    v.wind_direction_deg = float(row["drct"]); row_used = True
                if v.pressure_mb is None:
                    if row["mslp"] is not None:
                        v.pressure_mb = float(row["mslp"]); row_used = True
                    elif row["alti"] is not None:
                        v.pressure_mb = float(row["alti"]) * 33.8639; row_used = True
                if row_used:
                    enriched += 1

            conn.close()
            if enriched:
                logger.info(
                    f"ASOS enrichment: {enriched}/{len(vectors)} vectors "
                    f"filled for {station} {date_str}"
                )
        except Exception as e:
            logger.warning(f"ASOS enrichment failed for {station} {date_str}: {e}")

        return vectors

    # ── NWP Forecast Enrichment ────────────────────────────────

    def _enrich_from_nwp(
        self, vectors: List[HourlyVector], station: str, date_str: str
    ) -> List[HourlyVector]:
        """
        Enrich hourly vectors with NWP forecast data.

        Real schema (data/nwp_forecasts.db): `nwp_forecasts` is LONG format,
            fetch_date, target_date, station, model, variable, value
        There is NO `nwp_hourly_forecasts` table. Variables are daily
        (`temperature_2m_max` °C, `dew_point_2m_daily_mean` °C,
        `cloud_cover_daily_mean` % 0-100, `wind_*_daily_mean` km/h) or member
        maxima (`temperature_2m_max_member*`). We derive one value per feature
        for the whole day (daily data -> broadcast to every hourly vector) and
        fill ONLY null fields. Uses the most recent fetch_date <= date_str and
        the first model (priority gfs, ecmwf, icon, gem) that has a temperature.
        """
        if not self.nwp_db:
            return vectors

        try:
            conn = _get_connection(self.nwp_db)
            cur = conn.cursor()

            # Latest fetch_date at or before the target date across models.
            cur.execute("""
                SELECT MAX(fetch_date) FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND fetch_date <= ?
            """, (station, date_str, date_str))
            fd_row = cur.fetchone()
            if not fd_row or fd_row[0] is None:
                conn.close()
                return vectors
            fetch_date = fd_row[0]

            cur.execute("""
                SELECT model, variable, value FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND fetch_date = ?
            """, (station, date_str, fetch_date))
            by_model: Dict[str, Dict[str, float]] = {}
            for m, var, val in cur.fetchall():
                by_model.setdefault(m, {})[var] = float(val)
            conn.close()

            if not by_model:
                return vectors

            # Choose the best model: prefer one carrying a 2m temperature.
            model_priority = ["gfs", "ecmwf", "icon", "gem"]
            chosen = None
            for m in model_priority:
                if m in by_model and "temperature_2m_max" in by_model[m]:
                    chosen = m
                    break
            if chosen is None:
                chosen = next(
                    (m for m in by_model if "temperature_2m_max" in by_model[m]),
                    None,
                )
            if chosen is None:
                return vectors

            nwp = by_model[chosen]

            def _c_to_f(c):
                return c * 9.0 / 5.0 + 32.0

            temp_f = _c_to_f(nwp["temperature_2m_max"]) if "temperature_2m_max" in nwp else None
            dew_f = (
                _c_to_f(nwp["dew_point_2m_daily_mean"])
                if "dew_point_2m_daily_mean" in nwp else None
            )
            wind_kt = (
                nwp["wind_speed_10m_daily_mean"] / 1.852
                if "wind_speed_10m_daily_mean" in nwp else None
            )
            wind_dir = nwp.get("wind_direction_10m_daily_mean")
            cloud = (
                min(1.0, max(0.0, nwp["cloud_cover_daily_mean"] / 100.0))
                if "cloud_cover_daily_mean" in nwp else None
            )

            # ── om_gfs substitution (Workstream P, 2nd wiring point) ────────
            # USE_OM_GFS_NWP=1 -> the temperature level is taken from om_gfs
            # (Open-Meteo bias-corrected GFS Tmax, via core/intraday/om_gfs_nwp).
            # om_gfs is a LEVER/level-axis substitution only (collinear with
            # rate_1h), so only temperature is overridden; dew/wind/cloud stay
            # from the raw local GFS block. Flag OFF -> byte-identical to the
            # pre-change behaviour. Flag ON but om_gfs unavailable/failed -> raw
            # local GFS fallback (never raises).
            if OM_GFS_AVAILABLE and om_gfs_enabled():
                try:
                    om_state = om_gfs_state_for_day(station, date_str, live=True)
                except Exception as e:  # noqa: BLE001 - never break enrichment
                    logger.warning(
                        f"om_gfs enrichment failed for {station} {date_str}: {e}"
                    )
                    om_state = None
                if om_state is not None:
                    temp_f = om_state.mu
                    logger.info(
                        f"NWP enrichment: om_gfs bias-corrected Tmax "
                        f"{om_state.mu:.1f}F substituted for {station} {date_str}"
                    )

            enriched = 0
            for v in vectors:
                row_used = False
                if v.temperature_f is None and temp_f is not None:
                    v.temperature_f = temp_f; row_used = True
                if v.dewpoint_f is None and dew_f is not None:
                    v.dewpoint_f = dew_f; row_used = True
                if v.wind_speed_kt is None and wind_kt is not None:
                    v.wind_speed_kt = wind_kt; row_used = True
                if v.wind_direction_deg is None and wind_dir is not None:
                    v.wind_direction_deg = wind_dir; row_used = True
                if v.cloud_cover_pct is None and cloud is not None:
                    v.cloud_cover_pct = cloud; row_used = True
                if row_used:
                    enriched += 1

            if enriched:
                logger.info(
                    f"NWP enrichment: {enriched}/{len(vectors)} vectors filled "
                    f"for {station} {date_str} (model={chosen}, fetch={fetch_date})"
                )
        except Exception as e:
            logger.warning(f"NWP enrichment failed for {station} {date_str}: {e}")

        return vectors

    # ── Main Builder ──────────────────────────────────────────

    def build_for_station_date(
        self, station: str, date_str: str
    ) -> List[HourlyVector]:
        """
        Build hourly feature vectors for a single station on a given date.

        Pipeline:
          1. Load METAR hourly buckets from METAR DB
          2. Aggregate each bucket into features
          3. Enrich with ASOS 1-minute data if available
          4. Enrich with NWP forecast data if available
          5. Compute 3-hour pressure and temperature trends

        Args:
            station: ICAO station code (e.g., "KATL", "KNYC")
            date_str: ISO date string "YYYY-MM-DD"

        Returns:
            List of HourlyVector for hours 0-23 (only hours with data).
            Empty list if no data is available.
        """
        # Step 1: Load METAR hourly buckets
        hourly_buckets = self._load_metar_hourly(station, date_str)

        if not hourly_buckets:
            logger.debug(f"No METAR data for {station} on {date_str}")
            return []

        # Step 2: Build HourlyVectors from aggregated buckets
        vectors: List[HourlyVector] = []
        for hour_utc in sorted(hourly_buckets.keys()):
            obs_list = hourly_buckets[hour_utc]
            agg = self._aggregate_hour_bucket(obs_list)

            if not agg.get("n_observations", 0):
                continue

            hb = _hour_to_hour_before_settlement(hour_utc)

            vector = HourlyVector(
                station=station,
                date_str=date_str,
                hour_utc=hour_utc,
                hour_before_settlement=hb,
                temperature_f=agg.get("temperature_f"),
                dewpoint_f=agg.get("dewpoint_f"),
                wind_speed_kt=agg.get("wind_speed_kt"),
                wind_direction_deg=agg.get("wind_direction_deg"),
                pressure_mb=agg.get("pressure_mb"),
                cloud_cover_pct=agg.get("cloud_cover_pct"),
                n_observations=agg.get("n_observations", 0),
                metar_observations=obs_list,
            )
            vectors.append(vector)

        # Step 3: Enrich from ASOS
        vectors = self._enrich_from_asos(vectors, station, date_str)

        # Step 4: Enrich from NWP
        vectors = self._enrich_from_nwp(vectors, station, date_str)

        # Step 5: Compute trends
        vectors = self._compute_trends(vectors)

        logger.info(
            f"Pipeline: {station} {date_str} → {len(vectors)} hourly vectors"
        )
        return vectors