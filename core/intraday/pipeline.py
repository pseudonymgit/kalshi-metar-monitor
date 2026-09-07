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
            cur.execute("""
                SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt,
                       wind_direction_deg, pressure_mb, clouds_cover_pct,
                       ceil_humidity, visibility_mi
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
            "dewpoint_f": sum(dews) / len(devs) if dews else None,
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

        Falls back gracefully if ASOS DB is unavailable.
        """
        if not self.asos_db:
            return vectors

        try:
            conn = _get_connection(self.asos_db)
            cur = conn.cursor()

            for v in vectors:
                hour_label = f"{date_str} {v.hour_utc:02d}:%"
                cur.execute("""
                    SELECT AVG(tmpf) as avg_temp, AVG(skid) as avg_ws,
                           AVG(drct) as avg_dir, AVG(pressure) as avg_pres
                    FROM t{station}
                    WHERE valid LIKE ?
                """.replace("{station}", station.lower()), (hour_label,))
                row = cur.fetchone()
                if row and any(row):
                    if v.temperature_f is None and row["avg_temp"] is not None:
                        v.temperature_f = float(row["avg_temp"])
                    if v.wind_speed_kt is None and row["avg_ws"] is not None:
                        v.wind_speed_kt = float(row["avg_ws"])
                    if v.wind_direction_deg is None and row["avg_dir"] is not None:
                        v.wind_direction_deg = float(row["avg_dir"])
                    if v.pressure_mb is None and row["avg_pres"] is not None:
                        v.pressure_mb = float(row["avg_pres"])

            conn.close()
        except Exception as e:
            logger.debug(f"ASOS enrichment failed for {station} {date_str}: {e}")

        return vectors

    # ── NWP Forecast Enrichment ────────────────────────────────

    def _enrich_from_nwp(
        self, vectors: List[HourlyVector], station: str, date_str: str
    ) -> List[HourlyVector]:
        """
        Enrich hourly vectors with NWP forecast data.

        Falls back gracefully if NWP DB is unavailable.
        """
        if not self.nwp_db:
            return vectors

        try:
            conn = _get_connection(self.nwp_db)
            cur = conn.cursor()

            for v in vectors:
                # Look up NWP forecast for this station + date + hour
                hour_str = f"{v.hour_utc:02d}"
                cur.execute("""
                    SELECT temp_f, dewpoint_f, wind_speed_kt,
                           wind_direction_deg, pressure_mb, cloud_cover_pct
                    FROM nwp_hourly_forecasts
                    WHERE station = ? AND date_utc = ? AND hour_utc = ?
                """, (station, date_str, hour_str))
                row = cur.fetchone()
                if row and any(row):
                    if v.temperature_f is None:
                        v.temperature_f = row["temp_f"]
                    if v.dewpoint_f is None:
                        v.dewpoint_f = row["dewpoint_f"]
                    if v.wind_speed_kt is None:
                        v.wind_speed_kt = row["wind_speed_kt"]
                    if v.wind_direction_deg is None:
                        v.wind_direction_deg = row["wind_direction_deg"]
                    if v.pressure_mb is None:
                        v.pressure_mb = row["pressure_mb"]
                    if v.cloud_cover_pct is None:
                        v.cloud_cover_pct = row["cloud_cover_pct"]

            conn.close()
        except Exception as e:
            logger.debug(f"NWP enrichment failed for {station} {date_str}: {e}")

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