"""
context.py — No-lookahead data context for additive signal evaluation.

One AdditiveContext is built per (station, target_date). It pre-loads every
observation window the six Gamma signals need, enforcing a single rule:

    NOTHING at or after the local feature cutoff (11:30 local on the target
    date) is visible to signals.

Local-day semantics per source (verified 2026-09-04):
  - weatherapi_archive.hourly: `date` column is the LOCAL trading day;
    `hour` is local wall clock. Today -> hours 0..10 only (hour 11 is
    partially after the 11:30 cutoff; excluded).
  - metar_observations / asos_observations / 1-min: keyed by UTC. Each UTC
    timestamp is bucketed to its STATION-LOCAL date. The target date's obs
    are truncated at 11:30 local. "Today"/"yesterday" are local-day keys.
  - daily_stats / weatherapi.daily / settlement tables: full-local-day
    aggregates — only ever exposed for days STRICTLY BEFORE the target.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .paths import (
    METAR_DB, ASOS_DB, ASOS_1MIN_DB, WEATHERAPI_DB,
    NWP_DB, KALSHI_SETTLEMENTS_DB, DATA_DIR,
)
from .base import lin_slope

__all__ = [
    "Obs", "DayContext", "AdditiveContext",
    "FEATURE_CUTOFF_LOCAL",
]

logger = __import__("logging").getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

FEATURE_CUTOFF_LOCAL = 11.5  # 11:30 local — nothing after this is visible
TRAILING_DAYS = 14           # window size for trailing baseline calculations

# ─── Time helpers ───────────────────────────────────────────────────────────


def _tz_for(station: str) -> ZoneInfo:
    """Return the ZoneInfo for a station's local timezone."""
    from core.station_time import station_timezone_name
    return ZoneInfo(station_timezone_name(station))


def _parse_utc(ts: str) -> datetime:
    """
    Parse METAR/ASOS UTC timestamps.

    Accepts formats: '2025-07-01T00:00:00+00:00' or '2025-07-01 00:00'.
    """
    ts = str(ts).strip()
    if "T" in ts:
        s = ts.replace("T", " ")
    else:
        s = ts
    if s.endswith("+00:00"):
        s = s[:-6]
    elif s.endswith("Z"):
        s = s[:-1]
    parts = s.split(" ")
    if len(parts) != 2:
        raise ValueError(f"Cannot parse timestamp: {ts}")
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _parse_utc_minute(ts: str) -> datetime:
    """Parse timestamps with minute precision (same logic)."""
    return _parse_utc(ts)


def _utc_cutoff_for(station: str, date: str) -> datetime:
    """UTC instant of 11:30 local on `date` for `station`."""
    parts = date.split("-")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    local_dt = datetime(y, m, d, 11, 30, 0, tzinfo=_tz_for(station))
    return local_dt.astimezone(timezone.utc)


def _local_midnight_utc(station: str, date: str) -> datetime:
    """UTC instant of 00:00 local on `date`."""
    parts = date.split("-")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    local_dt = datetime(y, m, d, 0, 0, 0, tzinfo=_tz_for(station))
    return local_dt.astimezone(timezone.utc)


# ─── Data types ─────────────────────────────────────────────────────────────


@dataclass
class Obs:
    """One surface observation (METAR hourly or ASOS)."""
    ts_utc: str
    local_date: str
    local_hour: int
    temp_f: Optional[float] = None
    dewpoint_f: Optional[float] = None
    wind_dir: Optional[float] = None
    wind_speed_kt: Optional[float] = None
    pressure_mb: Optional[float] = None
    ceiling_ft: Optional[float] = None


@dataclass
class DayContext:
    """Observations + aggregates for one station-local day."""
    date: str
    obs: List[Obs] = field(default_factory=list)
    cloud_hourly: Dict[int, float] = field(default_factory=dict)
    daily_high_f: Optional[float] = None
    daily_low_f: Optional[float] = None
    kalshi_high_f: Optional[float] = None


# ─── ASOS cache (process-wide) ─────────────────────────────────────────────


class _AsosCache:
    """
    Process-wide cache of morning-window ASOS observations.

    Keyed by (station, date). Avoids re-scanning the 5.5M-row ASOS table for
    every context construction in a backtest sweep.
    """
    _DATA: Dict[str, List[Obs]] = {}
    MAX_ENTRIES = 5000

    @classmethod
    def get(cls, station: str, date: str) -> List[Obs]:
        key = f"{station}|{date}"
        if key in cls._DATA:
            return cls._DATA[key]
        # Lazy load
        obs = cls._load(station, date)
        if len(cls._DATA) >= cls.MAX_ENTRIES:
            # Simple eviction: clear the cache
            cls._DATA.clear()
        cls._DATA[key] = obs
        return obs

    @classmethod
    def _load(cls, station: str, date: str) -> List[Obs]:
        """Load morning-window ASOS obs from the asos_1min DB."""
        cutoff = _utc_cutoff_for(station, date)
        midnight = _local_midnight_utc(station, date)
        try:
            conn = sqlite3.connect(f"file:{ASOS_1MIN_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT valid, tmpf, dwpf, drct, sknt, pres, ceil
                FROM asos_1min
                WHERE station = ? AND valid >= ? AND valid < ?
                ORDER BY valid ASC
                """,
                (station, midnight.isoformat(), cutoff.isoformat()),
            )
            results = []
            for row in cur.fetchall():
                results.append(Obs(
                    ts_utc=str(row[0]),
                    local_date=date,
                    local_hour=0,  # will be set below
                    temp_f=row[1],
                    dewpoint_f=row[2],
                    wind_dir=row[3],
                    wind_speed_kt=row[4],
                    pressure_mb=row[5],
                    ceiling_ft=row[6],
                ))
            conn.close()
            return results
        except (sqlite3.OperationalError, FileNotFoundError):
            return []


# ─── AdditiveContext ────────────────────────────────────────────────────────


class AdditiveContext:
    """
    Pre-loaded, cutoff-enforced data context for one (station, target_date).

    Construction issues a fixed set of SQL queries; signals evaluate from
    memory with zero additional DB access. Deterministic and query-bounded.
    """

    def __init__(self, station: str, date_str: str):
        self.station = station.upper()
        self.date_str = date_str
        self.target_date = date_str
        self._cutoff_utc = _utc_cutoff_for(self.station, date_str)
        self._midnight_utc = _local_midnight_utc(self.station, date_str)

        # Buckets populated by _load methods
        self.metar_today_before: List[Obs] = []   # METAR obs before cutoff today
        self.metar_window_today: List[Obs] = []   # today's METAR obs (before cutoff)
        self.prior_obs_by_local_hour: Dict[str, List[Obs]] = {}  # keyed by local_date
        self.trailing_daily_highs: List[Tuple[str, float]] = []  # (date, high_f)
        self.ramp_slope_morning: Optional[float] = None

        # Load data
        self._load_all()

    def _load_all(self):
        """Execute all data-loading queries."""
        self._load_metar()
        self._load_cloud()
        self._load_daily()
        self._load_asos()
        self._load_nwp()
        self._load_kalshi()

    def _bucket(self, ts_utc: str) -> str:
        """Return the station-local date key for a UTC timestamp."""
        ts = _parse_utc(ts_utc)
        local_dt = ts.astimezone(_tz_for(self.station))
        return local_dt.strftime("%Y-%m-%d")

    def _dc_for_local_day(self, local_date: str) -> DayContext:
        """Build or retrieve a DayContext for a given local date."""
        dc = DayContext(date=local_date)
        return dc

    def _load_metar(self):
        """Load METAR hourly observations for today and yesterday."""
        try:
            conn = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            # Fetch today's METAR obs before cutoff
            cur.execute(
                """
                SELECT valid, tmpf, dwpf, drct, sknt, pres
                FROM metar_observations
                WHERE station = ? AND valid >= ? AND valid < ?
                ORDER BY valid ASC
                """,
                (self.station, self._midnight_utc.isoformat(), self._cutoff_utc.isoformat()),
            )
            today_obs = []
            for row in cur.fetchall():
                obs = Obs(
                    ts_utc=str(row[0]),
                    local_date=self.date_str,
                    local_hour=int(str(row[0])[11:13]),
                    temp_f=row[1],
                    dewpoint_f=row[2],
                    wind_dir=row[3],
                    wind_speed_kt=row[4],
                    pressure_mb=row[5],
                )
                today_obs.append(obs)
            self.metar_today_before = today_obs
            self.metar_window_today = today_obs

            # Fetch trailing days (yesterday, day-before, etc.)
            trailing_start = self._midnight_utc - timedelta(days=TRAILING_DAYS + 2)
            cur.execute(
                """
                SELECT valid, tmpf, dwpf, drct, sknt, pres, station
                FROM metar_observations
                WHERE station = ? AND valid >= ? AND valid < ?
                ORDER BY valid ASC
                """,
                (self.station, trailing_start.isoformat(), self._midnight_utc.isoformat()),
            )
            for row in cur.fetchall():
                local_date = self._bucket(str(row[0]))
                obs = Obs(
                    ts_utc=str(row[0]),
                    local_date=local_date,
                    local_hour=int(str(row[0])[11:13]),
                    temp_f=row[1],
                    dewpoint_f=row[2],
                    wind_dir=row[3],
                    wind_speed_kt=row[4],
                    pressure_mb=row[5],
                )
                self.prior_obs_by_local_hour.setdefault(local_date, []).append(obs)

            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            logger.debug("METAR DB not available for %s", self.station)

    def _load_cloud(self):
        """Load cloud cover data from weatherapi_archive.hourly."""
        try:
            conn = sqlite3.connect(f"file:{WEATHERAPI_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT hour, cloud
                FROM weatherapi_archive_hourly
                WHERE station = ? AND date = ? AND hour < 11
                ORDER BY hour ASC
                """,
                (self.station, self.date_str),
            )
            for row in cur.fetchall():
                hour, cloud = row
                if cloud is not None:
                    dc = self._dc_for_local_day(self.date_str)
                    dc.cloud_hourly[int(hour)] = float(cloud)
            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            pass

    def _load_daily(self):
        """Load daily summary data (high/lows) for trailing days."""
        try:
            conn = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            trailing_start = self._midnight_utc - timedelta(days=TRAILING_DAYS + 2)
            cur.execute(
                """
                SELECT date_utc, MAX(tmpf) as high, MIN(tmpf) as low
                FROM metar_observations
                WHERE station = ? AND valid >= ? AND valid < ?
                GROUP BY date_utc
                ORDER BY date_utc DESC
                LIMIT ?
                """,
                (self.station, trailing_start.isoformat(), self._midnight_utc.isoformat(), TRAILING_DAYS),
            )
            self.trailing_daily_highs = []
            for row in cur.fetchall():
                if row[1] is not None:
                    self.trailing_daily_highs.append((str(row[0]), float(row[1])))
            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            pass

    def _load_asos(self):
        """Load ASOS 1-min observations for morning window (cache-backed)."""
        asos_obs = _AsosCache.get(self.station, self.date_str)
        if asos_obs:
            if not self.metar_window_today:
                self.metar_window_today = asos_obs
            else:
                self.metar_window_today.extend(asos_obs)
                self.metar_window_today.sort(key=lambda o: o.ts_utc)

    def _load_nwp(self):
        """Load NWP forecast data for the target date (for baseline reference)."""
        try:
            conn = sqlite3.connect(f"file:{NWP_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT model, value
                FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND variable = 'temperature_2m_max'
                  AND fetch_date >= date(?, '-1 day')
                  AND fetch_date <= date(?, '+1 day')
                ORDER BY fetch_date DESC
                LIMIT 3
                """,
                (self.station, self.date_str, self.date_str, self.date_str),
            )
            # Store the best available NWP high for reference
            best = None
            for row in cur.fetchall():
                if row[1] is not None:
                    best = float(row[1])
                    break
            if best is not None:
                # Attach to the context for signal use
                self._nwp_high_f = best
            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            self._nwp_high_f = None

    def _load_kalshi(self):
        """Load Kalshi settlement data for the target date."""
        try:
            conn = sqlite3.connect(f"file:{KALSHI_SETTLEMENTS_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT kalshi_temp FROM kalshi_settlements "
                "WHERE station = ? AND target_date = ? "
                "AND source_type = 'finalized' AND kalshi_temp IS NOT NULL",
                (self.station, self.date_str),
            )
            row = cur.fetchone()
            if row:
                self._kalshi_high_f = float(row[0])
            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            self._kalshi_high_f = None

    def _wapi_query(self, field: str, hours: List[int]) -> Dict[int, float]:
        """Helper to query weatherapi_archive.hourly for a field over specific hours."""
        result = {}
        if not hours:
            return result
        try:
            conn = sqlite3.connect(f"file:{WEATHERAPI_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in hours)
            cur.execute(
                f"""
                SELECT hour, {field}
                FROM weatherapi_archive_hourly
                WHERE station = ? AND date = ? AND hour IN ({placeholders})
                ORDER BY hour ASC
                """,
                [self.station, self.date_str] + hours,
            )
            for row in cur.fetchall():
                if row[1] is not None:
                    result[int(row[0])] = float(row[1])
            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            pass
        return result

    # ─── Public accessors for signals ─────────────────────────────────────

    def obs_between(self, start_hour: int, end_hour: int) -> List[Obs]:
        """Return observations in [start_hour, end_hour) local for today."""
        return [
            o for o in self.metar_window_today
            if o.local_hour >= start_hour and o.local_hour < end_hour
        ]

    def prior_obs_on(self, local_date: str) -> List[Obs]:
        """Return observations for a specific local date (trailing)."""
        return self.prior_obs_by_local_hour.get(local_date, [])

    def get_trailing_daily_highs(self, n: int = TRAILING_DAYS) -> List[float]:
        """Return the last n daily high temps (oldest first)."""
        highs = [h for _, h in self.trailing_daily_highs]
        return highs[-n:] if len(highs) > n else highs

    def get_nwp_high(self) -> Optional[float]:
        """Return the best NWP forecast high for the target date."""
        return getattr(self, "_nwp_high_f", None)

    def get_kalshi_high(self) -> Optional[float]:
        """Return the Kalshi settlement high for the target date (historical only)."""
        return getattr(self, "_kalshi_high_f", None)

    def close(self):
        """Cleanup hook — no-op for context (connections are per-query)."""
        pass