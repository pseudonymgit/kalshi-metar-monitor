#!/usr/bin/env python3
"""
Trajectory Confirmation Gate — Epoch-Based Analog Matching for Signal Verification

Implements a confirmation gate that uses epoch-based analog matching to
confirm or reject a signal's direction prediction before it reaches trade
generation.

Dispositions:
  - CONFIRM:  trajectory agrees with signal -> boost conviction 1.0-1.3x
  - NEUTRAL:  insufficient or ambiguous trajectory -> no change (1.0x)
  - OVERRIDE: trajectory contradicts signal with high confidence -> 0.0x or block

Per spec: docs/plans/TRAJECTORY-CONFIRMATION-GATE-SPEC.md

Data sources (local, no external API):
  - data/metar_backfill.db   -> METAR observations (daily_stats, metar_observations)
  - data/kalshi_settlements.db -> settlement temperatures
"""

import math
import os
import sqlite3
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from core.db_utils import query_db, with_db

# -- Paths --------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
DEFAULT_EPOCH_DB = str(REPO_ROOT / "data" / "trajectory_gate.db")


# -- Climate Zones (from TRAJECTORY-LANE-DESIGN.md) ---------------------------
CLIMATE_ZONES: Dict[str, List[str]] = {
    "Northeast":   ["KNYC", "KBOS", "KPHL", "KDCA", "KJFK", "KBWI"],
    "Midwest":     ["KMDW", "KMSP", "KORD", "KDTW", "KMKE"],
    "South":       ["KATL", "KMIA", "KMSY", "KHOU", "KBNA", "KCLT"],
    "SouthCentral":["KDFW", "KAUS", "KSAT", "KOKC"],
    "West":        ["KLAX", "KSFO", "KSEA", "KPHX", "KLAS", "KPDX", "KSLC"],
    "RockyWest":   ["KDEN"],
}

# Reverse lookup: station -> zone
STATION_TO_ZONE: Dict[str, str] = {}
for zone, stns in CLIMATE_ZONES.items():
    for s in stns:
        STATION_TO_ZONE[s] = zone
# Zone adjacency for geographic pre-filtering (from TRAJECTORY-DB-FIX-SPEC.md)
ZONE_ADJACENCY: Dict[str, List[str]] = {
    "Northeast":    ["Midwest"],
    "Midwest":      ["Northeast", "SouthCentral", "RockyWest"],
    "South":        ["SouthCentral", "Northeast"],
    "SouthCentral": ["South", "Midwest", "RockyWest", "West"],
    "West":         ["RockyWest", "SouthCentral"],
    "RockyWest":    ["West", "Midwest"],
}



# Cross-zone distance penalty multiplier
CROSS_ZONE_MULTIPLIER = 1.5
# Same-station bonus multiplier
SAME_STATION_MULTIPLIER = 2.0


# -- Feature Weights (from spec sec2.4) ---------------------------------------
FEATURE_WEIGHTS: Dict[str, float] = {
    "temp_max_f":         0.20,
    "temp_min_f":         0.15,
    "pressure_mean_mb":   0.15,
    "pressure_trend":     0.10,
    "wind_dir_mode":      0.10,
    "wind_speed_mean_kt": 0.08,
    "dewpoint_mean_f":    0.08,
    "cloud_cover_mean":   0.07,
    "temp_range_f":       0.05,
    "rh_mean_pct":        0.02,
}

FEATURE_NAMES = list(FEATURE_WEIGHTS.keys())
FEATURE_WEIGHT_ARRAY = np.array([FEATURE_WEIGHTS[f] for f in FEATURE_NAMES], dtype=np.float64)

# -- Wind Regimes (from spec sec4.4) ------------------------------------------
WIND_REGIMES: Dict[str, Tuple[int, int]] = {
    "northerly": (315, 45),   # cold advection
    "easterly":  (45, 135),   # maritime/cool
    "southerly": (135, 225),  # warm advection
    "westerly":  (225, 315),  # dry/continental
}

WIND_REGIME_ADJACENT: Dict[str, List[str]] = {
    "northerly": ["northerly", "westerly"],
    "easterly":  ["easterly", "southerly"],
    "southerly": ["southerly", "easterly"],
    "westerly":  ["westerly", "northerly"],
}

MOISTURE_ORDER = ["dry", "moderate", "moist", "saturated"]

# -- Analog Count Tiers (from spec sec5) --------------------------------------
ANALOG_COUNT_TIERS_CONFIG = {
    "insufficient": (0, 9),
    "minimal":      (10, 29),
    "adequate":     (30, 99),
    "strong":       (100, None),
}

# -- Kalshi HIGH bucket thresholds -------------------------------------------
BUCKET_THRESHOLDS_HIGH: Dict[str, Tuple[float, float]] = {
    "below_30":    (-float("inf"), 30.0),
    "30-35":       (30.0, 35.0),
    "35-40":       (35.0, 40.0),
    "above_40":    (40.0, float("inf")),
}


class EpochBuildError(Exception):
    """Raised when an epoch cannot be built due to insufficient data."""
    pass


# =============================================================================
# 1. EPOCH DATA STRUCTURE
# =============================================================================
class Epoch:
    """
    A 24-hour meteorological state vector for a single station on a single date.
    Fields match the spec sec2.2 epoch vector.
    """
    __slots__ = (
        "station", "date",
        "temp_max_f", "temp_min_f", "temp_mean_f",
        "dewpoint_mean_f", "rh_mean_pct",
        "wind_speed_mean_kt", "wind_dir_mode",
        "pressure_mean_mb", "pressure_trend",
        "cloud_cover_mean",
        "temp_range_f", "dp_depression_mean", "wind_speed_max_kt",
        "settlement_temp", "settlement_bucket", "target_date",
        "is_query", "zone",
    )

    def __init__(self, station: str, date: str,
                 temp_max_f: Optional[float] = None,
                 temp_min_f: Optional[float] = None,
                 temp_mean_f: Optional[float] = None,
                 dewpoint_mean_f: Optional[float] = None,
                 rh_mean_pct: Optional[float] = None,
                 wind_speed_mean_kt: Optional[float] = None,
                 wind_dir_mode: Optional[int] = None,
                 pressure_mean_mb: Optional[float] = None,
                 pressure_trend: Optional[float] = None,
                 cloud_cover_mean: Optional[float] = None,
                 temp_range_f: Optional[float] = None,
                 dp_depression_mean: Optional[float] = None,
                 wind_speed_max_kt: Optional[float] = None,
                 settlement_temp: Optional[float] = None,
                 settlement_bucket: Optional[str] = None,
                 target_date: Optional[str] = None,
                 is_query: bool = False,
                 zone: Optional[str] = None):
        self.station = station
        self.date = date
        self.temp_max_f = temp_max_f
        self.temp_min_f = temp_min_f
        self.temp_mean_f = temp_mean_f
        self.dewpoint_mean_f = dewpoint_mean_f
        self.rh_mean_pct = rh_mean_pct
        self.wind_speed_mean_kt = wind_speed_mean_kt
        self.wind_dir_mode = wind_dir_mode
        self.pressure_mean_mb = pressure_mean_mb
        self.pressure_trend = pressure_trend
        self.cloud_cover_mean = cloud_cover_mean
        self.temp_range_f = temp_range_f
        self.dp_depression_mean = dp_depression_mean
        self.wind_speed_max_kt = wind_speed_max_kt
        self.settlement_temp = settlement_temp
        self.settlement_bucket = settlement_bucket
        self.target_date = target_date
        self.is_query = is_query
        self.zone = zone or STATION_TO_ZONE.get(station, "Unknown")

    def feature_vector(self) -> np.ndarray:
        values = []
        for f in FEATURE_NAMES:
            v = getattr(self, f, None)
            values.append(v if v is not None else float("nan"))
        return np.array(values, dtype=np.float64)

    def __repr__(self) -> str:
        return (f"Epoch(station={self.station}, date={self.date}, "
                f"zone={self.zone}, target={self.target_date})")


# =============================================================================
# 2. EPOCH BUILDER
# =============================================================================
class EpochBuilder:
    """Constructs epoch vectors from METAR observations and settlement data."""

    def __init__(self, metar_db: str = METAR_DB,
                 settlements_db: str = SETTLEMENTS_DB):
        self.metar_db = metar_db
        self.settlements_db = settlements_db

    def build_epoch(self, station: str, date: str) -> Epoch:
        """
        Build a single epoch for a station on a given date (UTC).

        Args:
            station: ICAO station code (e.g. 'KNYC')
            date: UTC date string 'YYYY-MM-DD'

        Returns:
            Epoch object with observed fields populated.

        Raises:
            EpochBuildError if insufficient data (< 6 observations)
        """
        with with_db(self.metar_db, row_factory=True, commit=False) as conn:
            cur = conn.cursor()

            cur.execute("SELECT * FROM daily_stats WHERE station=? AND date_utc=?",
                         (station, date))
            day_row = cur.fetchone()
            if day_row is None:
                raise EpochBuildError(f"No daily stats for {station} on {date}")

            obs_count = day_row["observation_count"] or 0
            if obs_count < 6:
                raise EpochBuildError(
                    f"Insufficient observations ({obs_count}) for {station} on {date}")

            cur.execute("""
                SELECT mo.temp_f, mo.dewpoint_f, mo.wind_speed_kt,
                       mo.wind_direction_deg, mo.pressure_mb,
                       CASE
                           WHEN mo.ceiling_ft IS NULL THEN NULL
                           WHEN mo.ceiling_ft = 0 THEN 1.0
                           WHEN mo.ceiling_ft >= 25000 THEN 0.0
                           ELSE 1.0 - mo.ceiling_ft / 25000.0
                       END as cloud_cover
                FROM metar_observations mo
                WHERE mo.station=? AND mo.date_utc=?
                ORDER BY mo.timestamp_utc
            """, (station, date))
            rows = cur.fetchall()

        if len(rows) < 6:
            raise EpochBuildError(
                f"Insufficient hourly obs ({len(rows)}) for {station} on {date}")

        temp_max = day_row["max_temp_f"]
        temp_min = day_row["min_temp_f"]
        temp_mean = day_row["avg_temp_f"]

        dewpoints = [r["dewpoint_f"] for r in rows if r["dewpoint_f"] is not None]
        temps = [r["temp_f"] for r in rows if r["temp_f"] is not None]
        wind_speeds = [r["wind_speed_kt"] for r in rows if r["wind_speed_kt"] is not None]
        wind_dirs = [r["wind_direction_deg"] for r in rows if r["wind_direction_deg"] is not None]
        pressures = [r["pressure_mb"] for r in rows if r["pressure_mb"] is not None]

        if not dewpoints or not temps or not wind_speeds or not pressures:
            raise EpochBuildError(f"Missing critical fields for {station} on {date}")

        dewpoint_mean = float(np.mean(dewpoints))
        wind_speed_mean = float(np.mean(wind_speeds))
        wind_speed_max = float(np.max(wind_speeds)) if wind_speeds else None
        pressure_mean = float(np.mean(pressures))

        # Wind direction mode (binned to 16 compass points)
        if wind_dirs:
            dir_counts = Counter()
            for d in wind_dirs:
                bin_deg = round(d / 22.5) * 22.5 % 360
                dir_counts[int(bin_deg)] += 1
            wind_dir_mode = dir_counts.most_common(1)[0][0]
        else:
            wind_dir_mode = None

        # Relative humidity (Magnus formula)
        rh_values = []
        for t, d in zip(temps, dewpoints):
            if t is not None and d is not None and t > -100:
                es_t = 6.112 * math.exp(17.67 * t / (t + 243.5))
                es_d = 6.112 * math.exp(17.67 * d / (d + 243.5))
                rh = (es_d / es_t) * 100.0 if es_t > 0 else 0.0
                rh = min(100.0, max(0.0, rh))
                rh_values.append(rh)
        rh_mean = float(np.mean(rh_values)) if rh_values else None

        temp_range = (temp_max - temp_min) if (temp_max is not None and temp_min is not None) else None
        dp_depression = (temp_mean - dewpoint_mean) if (temp_mean is not None and dewpoint_mean is not None) else None

        cc_values = [r["cloud_cover"] for r in rows if r["cloud_cover"] is not None]
        cloud_cover = float(np.nanmean(cc_values)) if cc_values else None

        pressure_trend = None
        if len(pressures) >= 2:
            pressure_trend = pressures[-1] - pressures[0]

        return Epoch(
            station=station, date=date,
            temp_max_f=temp_max, temp_min_f=temp_min, temp_mean_f=temp_mean,
            dewpoint_mean_f=dewpoint_mean, rh_mean_pct=rh_mean,
            wind_speed_mean_kt=wind_speed_mean, wind_dir_mode=wind_dir_mode,
            pressure_mean_mb=pressure_mean, pressure_trend=pressure_trend,
            cloud_cover_mean=cloud_cover, temp_range_f=temp_range,
            dp_depression_mean=dp_depression, wind_speed_max_kt=wind_speed_max)

    def get_settlements(self) -> Dict[str, Dict[str, float]]:
        """Load settlement temperatures: {station: {target_date: temp_f}}."""
        rows = query_db(self.settlements_db,
                        "SELECT station, target_date, kalshi_temp FROM kalshi_settlements")
        result: Dict[str, Dict[str, float]] = defaultdict(dict)
        for r in rows:
            if r['kalshi_temp'] is not None:
                result[r['station']][r['target_date']] = float(r['kalshi_temp'])
        return dict(result)

    def build_epoch_db(self, stations: Optional[List[str]] = None,
                       output_db: str = DEFAULT_EPOCH_DB) -> str:
        """
        Build the entire epoch corpus from METAR data and settlements.
        Creates a SQLite database with epochs table per spec schema sec8.1.
        """
        if stations is None:
            stations = list(STATION_TO_ZONE.keys())

        settlements = self.get_settlements()

        with with_db(output_db, row_factory=False) as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS epochs (
                    id INTEGER PRIMARY KEY,
                    station TEXT NOT NULL,
                    date TEXT NOT NULL,
                    temp_max_f REAL,
                    temp_min_f REAL,
                    temp_mean_f REAL,
                    dewpoint_mean_f REAL,
                    pressure_mean_mb REAL,
                    pressure_trend_24h REAL,
                    wind_speed_mean_kt REAL,
                    wind_dir_mode INT,
                    cloud_cover_mean REAL,
                    dp_depression_mean REAL,
                    rh_mean_pct REAL,
                    temp_range_f REAL,
                    settlement_high_f REAL,
                    settlement_low_f REAL,
                    target_date TEXT,
                    UNIQUE(station, date)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_epochs_sd ON epochs(station, date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_epochs_pres ON epochs(pressure_mean_mb)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_epochs_temp ON epochs(temp_mean_f)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_epochs_zone ON epochs(zone)")

            inserted = 0
            skipped = 0

            for station in stations:
                station_settlements = settlements.get(station, {})
                if not station_settlements:
                    continue

                with with_db(self.metar_db, row_factory=False, commit=False) as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute(
                        """SELECT date_utc FROM daily_stats
                           WHERE station=? AND observation_count >= 6
                           ORDER BY date_utc""", (station,))
                    daily_rows = cur2.fetchall()

                for (date_utc,) in daily_rows:
                    try:
                        dt = datetime.strptime(date_utc, "%Y-%m-%d")
                        target_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    except ValueError:
                        continue

                    target_temp = station_settlements.get(target_date)
                    if target_temp is None:
                        continue

                    try:
                        epoch = self.build_epoch(station, date_utc)
                    except EpochBuildError:
                        skipped += 1
                        continue

                    cur.execute(
                        """INSERT OR REPLACE INTO epochs
                           (station, date, temp_max_f, temp_min_f, temp_mean_f,
                            dewpoint_mean_f, pressure_mean_mb, pressure_trend_24h,
                            wind_speed_mean_kt, wind_dir_mode, cloud_cover_mean,
                            dp_depression_mean, rh_mean_pct, temp_range_f,
                            settlement_high_f, settlement_low_f, target_date)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (station, date_utc,
                         epoch.temp_max_f, epoch.temp_min_f, epoch.temp_mean_f,
                         epoch.dewpoint_mean_f, epoch.pressure_mean_mb,
                         epoch.pressure_trend, epoch.wind_speed_mean_kt,
                         epoch.wind_dir_mode, epoch.cloud_cover_mean,
                         epoch.dp_depression_mean, epoch.rh_mean_pct,
                         epoch.temp_range_f,
                         target_temp, target_temp, target_date))
                    inserted += 1

        print(f"  Epoch DB built: {inserted} inserted, {skipped} skipped")
        return output_db

    def estimate_climatology(
        self, stations: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Dict[str, float]]]:
        """Estimate per-station z-score normalization params (mean, std)."""
        if stations is None:
            stations = list(STATION_TO_ZONE.keys())

        stats: Dict[str, Dict[str, Dict[str, float]]] = {}

        with with_db(self.metar_db, row_factory=False, commit=False) as conn:
            cur = conn.cursor()

            for station in stations:
                cur.execute("""SELECT date_utc FROM daily_stats
                               WHERE station=? AND observation_count >= 6
                               ORDER BY date_utc""", (station,))
                rows = cur.fetchall()
                if len(rows) < 30:
                    continue

                feature_values: Dict[str, List[float]] = {f: [] for f in FEATURE_NAMES}
                for (date_utc,) in rows:
                    try:
                        epoch = self.build_epoch(station, date_utc)
                    except EpochBuildError:
                        continue
                    vec = epoch.feature_vector()
                    for i, f in enumerate(FEATURE_NAMES):
                        v = vec[i]
                        if not math.isnan(v):
                            feature_values[f].append(v)

                station_stats: Dict[str, Dict[str, float]] = {}
                for f in FEATURE_NAMES:
                    vals = feature_values[f]
                    if len(vals) >= 10:
                        station_stats[f] = {
                            "mean": float(np.mean(vals)),
                            "std": float(np.std(vals, ddof=1) if len(vals) > 1 else 1.0)}
                    else:
                        station_stats[f] = {"mean": 0.0, "std": 1.0}
                stats[station] = station_stats

        return stats


# =============================================================================
# 3. ANALOG MATCHER
# =============================================================================
class AnalogMatcher:
    """Finds historical analogs using weighted Euclidean distance."""

    def __init__(self, epoch_db: str = DEFAULT_EPOCH_DB,
                 metar_db: str = METAR_DB,
                 settlements_db: str = SETTLEMENTS_DB):
        self.epoch_db = epoch_db
        self.metar_db = metar_db
        self.settlements_db = settlements_db
        self._climatology: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None
        self._builder = EpochBuilder(metar_db, settlements_db)

    def _ensure_climatology(self, stations: Optional[List[str]] = None):
        if self._climatology is not None:
            return
        self._climatology = self._builder.estimate_climatology(stations)

    def _normalize_vector(self, vec: np.ndarray, station: str) -> np.ndarray:
        climo = self._climatology.get(station, {}) if self._climatology else {}
        normalized = np.full_like(vec, float("nan"))
        for i, f in enumerate(FEATURE_NAMES):
            v = vec[i]
            if math.isnan(v):
                continue
            fstats = climo.get(f, {"mean": 0.0, "std": 1.0})
            mean_val = fstats.get("mean", 0.0)
            std_val = fstats.get("std", 1.0)
            if std_val < 1e-8:
                std_val = 1.0
            normalized[i] = (v - mean_val) / std_val
        return normalized

    def _load_epochs_for_zone(self, zone: str, query_date: Optional[str] = None,
                                include_adjacent: bool = True) -> List[Dict]:
        """Load epochs for the given climate zone (and optionally adjacent zones).

        Replaces _load_epochs_from_db with indexed zone pre-filtering.
        Cuts memory usage by 70-80% vs loading all stations.
        Also pre-filters the 14-day temporal exclusion window in SQL.
        """
        if not os.path.exists(self.epoch_db):
            print(f"  [TrajectoryGate] Epoch DB not found. Building...")
            self._builder.build_epoch_db(output_db=self.epoch_db)

        zone_stations: List[str] = CLIMATE_ZONES.get(zone, [])
        if include_adjacent:
            for adj_zone in ZONE_ADJACENCY.get(zone, []):
                zone_stations.extend(CLIMATE_ZONES.get(adj_zone, []))
        
        if not zone_stations:
            return []
        
        placeholders = ','.join('?' for _ in zone_stations)
        params: List[Any] = list(zone_stations)
        
        # Temporal pre-filter in SQL (14-day exclusion window)
        MAX_EPOCHS = 50000
        if query_date:
            try:
                from datetime import timedelta
                qd = datetime.strptime(query_date, "%Y-%m-%d")
                exclude_low = (qd - timedelta(days=14)).strftime("%Y-%m-%d")
                exclude_high = (qd + timedelta(days=14)).strftime("%Y-%m-%d")
                # Exclude same station+date and nearby dates
                rows = query_db(self.epoch_db,
                    f"""SELECT * FROM epochs 
                       WHERE station IN ({placeholders})
                       AND (date < ? OR date > ?)
                       ORDER BY station, date LIMIT ?""",
                    params + [exclude_low, exclude_high, MAX_EPOCHS])
            except ValueError:
                rows = query_db(self.epoch_db,
                    f"SELECT * FROM epochs WHERE station IN ({placeholders}) ORDER BY station, date LIMIT ?",
                    params + [MAX_EPOCHS])
        else:
            rows = query_db(self.epoch_db,
                f"SELECT * FROM epochs WHERE station IN ({placeholders}) ORDER BY station, date LIMIT ?",
                params + [MAX_EPOCHS])
        
        return [dict(r) for r in rows]

    def _epoch_dict_to_object(self, row: Dict, is_query: bool = False) -> Epoch:
        st = row.get("settlement_high_f") or row.get("settlement_low_f")
        return Epoch(
            station=row["station"], date=row["date"],
            temp_max_f=row.get("temp_max_f"), temp_min_f=row.get("temp_min_f"),
            temp_mean_f=row.get("temp_mean_f"),
            dewpoint_mean_f=row.get("dewpoint_mean_f"),
            rh_mean_pct=row.get("rh_mean_pct"),
            wind_speed_mean_kt=row.get("wind_speed_mean_kt"),
            wind_dir_mode=row.get("wind_dir_mode"),
            pressure_mean_mb=row.get("pressure_mean_mb"),
            pressure_trend=row.get("pressure_trend_24h"),
            cloud_cover_mean=row.get("cloud_cover_mean"),
            temp_range_f=row.get("temp_range_f"),
            dp_depression_mean=row.get("dp_depression_mean"),
            settlement_temp=st,
            settlement_bucket=_classify_bucket(st),
            target_date=row.get("target_date"),
            is_query=is_query)

    def find_analogs(self, query_epoch: Epoch, top_k: int = 200,
                     exclude_window_days: int = 14) -> List[Dict]:
        """
        Find the top-K most similar historical epochs.

        Implements per-spec sec3: Z-score normalized weighted Euclidean distance,
        temporal exclusion window (14 days), climate zone pooling.
        """
        self._ensure_climatology()
        corpus = self._load_epochs_for_zone(query_epoch.zone, query_epoch.date)
        if not corpus:
            return []

        q_vec = query_epoch.feature_vector()
        q_norm = self._normalize_vector(q_vec, query_epoch.station)
        q_zone = query_epoch.zone

        try:
            q_dt = datetime.strptime(query_epoch.date, "%Y-%m-%d")
        except ValueError:
            q_dt = None

        results: List[Dict] = []

        for row in corpus:
            if row["station"] == query_epoch.station and row["date"] == query_epoch.date:
                continue

            if q_dt is not None:
                try:
                    c_dt = datetime.strptime(row["date"], "%Y-%m-%d")
                    if abs((c_dt - q_dt).days) <= exclude_window_days:
                        continue
                except ValueError:
                    pass

            candidate = self._epoch_dict_to_object(row)
            c_vec = candidate.feature_vector()
            c_norm = self._normalize_vector(c_vec, candidate.station)

            diff = q_norm - c_norm
            valid = ~(np.isnan(diff) | np.isnan(q_norm) | np.isnan(c_norm))
            if np.sum(valid) < 3:
                continue

            w = FEATURE_WEIGHT_ARRAY * valid.astype(float)
            w_sum = np.sum(w)
            if w_sum < 1e-6:
                continue
            w_adj = w / w_sum
            d = float(np.sqrt(np.sum(w_adj * diff * diff, where=valid)))

            c_zone = candidate.zone
            is_same_zone = (c_zone == q_zone)
            is_same_station = (candidate.station == query_epoch.station)

            if is_same_station:
                d /= SAME_STATION_MULTIPLIER
            elif not is_same_zone:
                d *= CROSS_ZONE_MULTIPLIER

            match_score = 1.0 / (1.0 + d)

            results.append({
                "distance": d,
                "epoch": candidate,
                "match_score": match_score,
                "is_same_station": is_same_station,
                "is_same_zone": is_same_zone,
            })

        results.sort(key=lambda x: x["distance"])
        return results[:top_k]

    def _load_epochs_from_db(self) -> List[Dict]:
        """Backward compat alias — delegates to _load_epochs_for_zone with all zones."""
        all_zones = list(CLIMATE_ZONES.keys())
        rows = []
        for zone in all_zones:
            placeholders = ','.join('?' for _ in CLIMATE_ZONES[zone])
            zone_rows = query_db(self.epoch_db,
                f"SELECT * FROM epochs WHERE station IN ({placeholders}) ORDER BY station, date LIMIT 10000",
                list(CLIMATE_ZONES[zone]))
            rows.extend(zone_rows)
        return [dict(r) for r in rows]

    def compute_trajectory_distribution(self, analogs: List[Dict]) -> Optional[Dict[str, float]]:
        """Compute forward outcome distribution from analog epochs."""
        if not analogs:
            return None
        outcomes: Dict[str, float] = defaultdict(float)
        total = len(analogs)
        for entry in analogs:
            ep = entry["epoch"]
            if ep.settlement_bucket:
                outcomes[ep.settlement_bucket] += 1.0
        if not outcomes:
            return None
        return {b: c / total for b, c in outcomes.items()}


# =============================================================================
# 4. CONFIRMATION GATE ENGINE
# =============================================================================

def _classify_bucket(temp: Optional[float]) -> Optional[str]:
    if temp is None:
        return None
    for bucket, (lo, hi) in BUCKET_THRESHOLDS_HIGH.items():
        if lo <= temp < hi:
            return bucket
    return "above_40"


def classify_wind_regime(wind_dir: Optional[int]) -> Optional[str]:
    if wind_dir is None:
        return None
    for regime, (lo, hi) in WIND_REGIMES.items():
        if lo <= hi:
            if lo <= wind_dir < hi:
                return regime
        else:
            if wind_dir >= lo or wind_dir < hi:
                return regime
    return None


def classify_pressure_pattern(pressure_trend: Optional[float],
                               pressure_mean: float = 1013.25) -> str:
    if pressure_trend is None:
        return "zonal"
    if pressure_trend > 3.0:
        return "ridge"
    elif pressure_trend < -3.0:
        return "trough"
    elif abs(pressure_trend) <= 1.0:
        return "zonal"
    elif pressure_mean > 1025:
        return "blocking"
    else:
        return "front"


def bucket_is_warm(bucket: str) -> bool:
    return bucket in {"above_40", "35-40"}


def bucket_is_cool(bucket: str) -> bool:
    return bucket in {"below_30", "30-35"}


class TrajectoryConfirmationGate:
    """
    The confirmation gate evaluates signals against trajectory analogs.

    Dispositions:
      CONFIRM  (1.0-1.3x):  trajectory agrees with signal
      NEUTRAL  (1.0x):       insufficient/ambiguous trajectory
      OVERRIDE (0.0-0.5x):   trajectory contradicts signal (block or reduce)
    """

    def __init__(self, epoch_db: str = DEFAULT_EPOCH_DB,
                 metar_db: str = METAR_DB,
                 settlements_db: str = SETTLEMENTS_DB):
        self.matcher = AnalogMatcher(epoch_db, metar_db, settlements_db)
        self._builder = EpochBuilder(metar_db, settlements_db)

    # ── Public API Methods ────────────────────────────────────────────────

    def build_epoch(self, station: str, date: str) -> Epoch:
        """Proxy to EpochBuilder.build_epoch()."""
        return self._builder.build_epoch(station, date)

    def find_analogs(self, epoch: Epoch, n: int = 30) -> List[Dict]:
        """Proxy to AnalogMatcher.find_analogs() with top_k=n."""
        return self.matcher.find_analogs(epoch, top_k=max(n, 200))

    def evaluate(self, signal_direction: str, signal_confidence: float,
                 epoch: Epoch) -> Tuple[str, float]:
        """
        Evaluate a signal against the trajectory gate.

        Args:
            signal_direction: 'up' or 'down'
            signal_confidence: raw confidence from signal (0-1)
            epoch: The query epoch for this station+date

        Returns:
            (verdict, modulated_confidence) where verdict is
            'CONFIRM', 'NEUTRAL', or 'OVERRIDE' and modulated_confidence
            is the adjusted confidence value.
        """
        if signal_confidence <= 0 or signal_confidence > 1.0:
            return ("NEUTRAL", signal_confidence)

        # 1. Find analogs
        analogs = self.matcher.find_analogs(epoch, top_k=200)
        analog_count = len(analogs)

        if analog_count < 10:
            # Insufficient data — gate does nothing
            return ("NEUTRAL", signal_confidence)

        # 2. Compute trajectory distribution
        traj_dist = self.matcher.compute_trajectory_distribution(analogs)

        if traj_dist is None:
            return ("NEUTRAL", signal_confidence)

        # 3. Compute trajectory direction
        traj_dir = self._compute_trajectory_direction(traj_dist)

        if traj_dir is None:
            return ("NEUTRAL", signal_confidence)

        # 4. Compute match quality score (mean of top-50 match scores)
        match_score = float(np.mean([a["match_score"] for a in analogs[:min(50, len(analogs))]]))
        match_score = min(1.0, match_score)

        # 5. Compare directions
        if signal_direction.lower() == traj_dir:
            return self._confirm_disposition(analog_count, match_score, signal_confidence)
        else:
            return self._override_disposition(analog_count, match_score, signal_confidence)

    # ── Internal Methods ──────────────────────────────────────────────────

    def _compute_trajectory_direction(self, traj_dist: Dict[str, float]) -> Optional[str]:
        """Determine trajectory's implied direction: 'up', 'down', or None."""
        p_warm = sum(p for b, p in traj_dist.items() if bucket_is_warm(b))
        p_cool = sum(p for b, p in traj_dist.items() if bucket_is_cool(b))
        if p_warm > 0.55:
            return "up"
        elif p_cool > 0.55:
            return "down"
        return None

    def _confirm_disposition(self, analog_count: int, match_score: float,
                              base_confidence: float) -> Tuple[str, float]:
        """Signal confirmed by trajectory — boost confidence."""
        boost = 1.0
        if analog_count >= 100:
            boost = 1.30
        elif analog_count >= 50:
            boost = 1.20
        elif analog_count >= 30:
            boost = 1.15
        elif analog_count >= 10:
            boost = 1.10

        # Additional quality boost
        boost += 0.10 * (match_score - 0.60)
        boost = min(1.30, max(1.0, boost))

        modulated = min(1.0, base_confidence * boost)
        return ("CONFIRM", modulated)

    def _override_disposition(self, analog_count: int, match_score: float,
                               base_confidence: float) -> Tuple[str, float]:
        """Signal contradicted by trajectory — reduce or block."""
        if analog_count >= 100 and match_score >= 0.80:
            # Strong override — block
            return ("OVERRIDE", 0.0)
        elif analog_count >= 50 and match_score >= 0.70:
            # Moderate override — severely reduce
            return ("OVERRIDE", base_confidence * 0.30)
        elif analog_count >= 30:
            # Weak override — reduce
            return ("OVERRIDE", base_confidence * 0.50)
        else:
            # Minimal analogs — soft modulation
            return ("NEUTRAL", base_confidence * 0.85)


# =============================================================================
# 5. CONVENIENCE / MAIN ENTRY POINT
# =============================================================================

def evaluate_gate_for_station_date(
    station: str, date: str, signal_direction: str, signal_confidence: float,
    gate: Optional[TrajectoryConfirmationGate] = None
) -> Tuple[str, float, int, str, float]:
    """
    One-shot evaluation: build epoch, find analogs, evaluate gate.

    Returns: (verdict, modulated_confidence, analog_count, reason, analog_pct_warm)
    """
    if gate is None:
        gate = TrajectoryConfirmationGate()

    try:
        epoch = gate.build_epoch(station, date)
    except EpochBuildError as e:
        return ("NEUTRAL", signal_confidence, 0, str(e), 0.0)

    analogs = gate.find_analogs(epoch, n=30)
    analog_count = len(analogs)

    if analog_count < 10:
        return ("NEUTRAL", signal_confidence, analog_count,
                f"insufficient_analogs({analog_count})", 0.0)

    traj_dist = gate.matcher.compute_trajectory_distribution(analogs)
    if traj_dist is None:
        return ("NEUTRAL", signal_confidence, analog_count, "no_trajectory_distribution", 0.0)

    traj_dir = gate._compute_trajectory_direction(traj_dist)
    if traj_dir is None:
        return ("NEUTRAL", signal_confidence, analog_count, "ambiguous_trajectory", 0.0)

    match_score = float(np.mean([a["match_score"] for a in analogs[:50]]))
    match_score = min(1.0, match_score)

    p_warm = sum(p for b, p in traj_dist.items() if bucket_is_warm(b))

    if signal_direction.lower() == traj_dir:
        verdict, mod_conf = gate._confirm_disposition(analog_count, match_score, signal_confidence)
    else:
        verdict, mod_conf = gate._override_disposition(analog_count, match_score, signal_confidence)

    return (verdict, mod_conf, analog_count, verdict.lower(), p_warm)


# =============================================================================
# 6. CLI ENTRY POINT (for testing / direct invocation)
# =============================================================================

def main():
    """CLI entry point for testing the trajectory confirmation gate."""
    import argparse

    parser = argparse.ArgumentParser(description="Trajectory Confirmation Gate Tester")
    parser.add_argument("--station", default="KNYC", help="ICAO station code")
    parser.add_argument("--date", default=None, help="Date in YYYY-MM-DD format")
    parser.add_argument("--direction", default="up", choices=["up", "down"],
                        help="Signal direction to test")
    parser.add_argument("--confidence", type=float, default=0.65,
                        help="Signal confidence (0-1)")
    parser.add_argument("--build-db", action="store_true",
                        help="Build the epoch database and exit")
    parser.add_argument("--stations", nargs="*", default=None,
                        help="Stations for --build-db")
    args = parser.parse_args()

    if args.build_db:
        builder = EpochBuilder()
        stns = args.stations or None
        path = builder.build_epoch_db(stations=stns)
        print(f"  Epoch DB: {path}")
        return

    if args.date is None:
        args.date = datetime.utcnow().strftime("%Y-%m-%d")

    gate = TrajectoryConfirmationGate()
    verdict, mod_conf, n_analogs, reason, p_warm = evaluate_gate_for_station_date(
        args.station, args.date, args.direction, args.confidence, gate)

    print(f"  Station: {args.station}")
    print(f"  Date: {args.date}")
    print(f"  Signal: {args.direction} @ {args.confidence:.4f}")
    print(f"  Analogs: {n_analogs}")
    print(f"  Trajectory warm prob: {p_warm:.4f}")
    print(f"  Verdict: {verdict}")
    print(f"  Reason: {reason}")
    print(f"  Modulated confidence: {mod_conf:.4f}")


if __name__ == "__main__":
    main()
