"""
historical_adapter.py — Gamma historical validation adapter (2026-09-14)

RESPONSIBILITY
  Provide held-out validation data (truth + base forecasts) for the six Gamma
  additive signals WITHOUT modifying the signal modules, signal_families.py,
  the daemon, or cron config.

CONTRACT
  - Every truth record carries a provenance tag indicating which surviving
    source provided it: 'kalshi' | 'settlement_epochs' | 'ghcn'.
  - No synthetic data. If a required source is genuinely absent, the loader
    returns empty and the caller must proceed with available data or stop.
  - Base forecasts use the same model-precedence chain as the existing
    backtest.py (era5 → ecmwf → gfs → gem → icon), with lead ≤ 1 day.

TRUTH SOURCES (ordered by authority)
  1. kalshi_settlements.db — Kalshi finalized settlements (13695 rows,
     20 stations, range 2026-05-28..2026-09-12). Provenance: 'kalshi'.
  2. metar_backfill.db → settlement_epochs — Epoch-based bucket values
     (117397 rows, 31 stations, range 2021-01-01..2026-09-14).
     Filtered to market_type='HIGH' closed epochs.
     Provenance: 'settlement_epochs'.
  3. ghcn_settlements.db — GHCN daily station data (87560 rows,
     range 1869..present). Provenance: 'ghcn'.

NOTE on the column mismatch in backtest.py::load_truth()
  The existing backtest.py's load_truth() queries settlement_epochs for
  column names (bucket_date, bucket_high_f) that DO NOT EXIST in the actual
  database. The correct column names are (local_trading_date, settlement_bucket).
  This is a latent bug. The adapter below uses the correct column names.

USAGE
    from core.additive.historical_adapter import HistoricalAdapter
    adapter = HistoricalAdapter()
    truth = adapter.load_truth()
    base_all = adapter.load_base_forecasts()
    print(truth.get(("KATL", "2026-06-15")))
    # Returns: {"temp_f": 92.0, "provenance": "kalshi", ...}

See also:
  - core/additive/backtest.py    — the original harness (buggy settlement_epochs query)
  - core/additive/base.py        — AdditiveSignal ABC
  - core/additive/context.py     — AdditiveContext builder
"""

import logging
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import METAR_DB, NWP_DB, KALSHI_SETTLEMENTS_DB

logger = logging.getLogger(__name__)

__all__ = [
    "HistoricalAdapter",
    "SETTLE_STATIONS",
    "HELD_OUT_START",
    "HELD_OUT_END",
]

# ─── Repo root resolution — 2 levels up from core/additive/ ────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"

# ─── Constants ─────────────────────────────────────────────────────────────

SETTLE_STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

HELD_OUT_START = "2026-03-01"
HELD_OUT_END = "2026-08-31"

# Base forecast model precedence (best → fallback)
_MODEL_PRECEDENCE = ["era5", "ecmwf", "gfs", "gem", "icon"]

# Truth provenance tags
PROV_KALSHI = "kalshi"
PROV_EPOCH = "settlement_epochs"
PROV_GHCN = "ghcn"

# Sanity band (guards against DB corruption seen 2026-07-07..12)
SANITY_MIN_F = -60.0
SANITY_MAX_F = 135.0


# ─── Truth record ──────────────────────────────────────────────────────────


class TruthRecord:
    """
    One (station, date) truth entry with provenance.

    Attributes:
        station:  ICAO code (e.g. "KATL")
        date:     target date string (YYYY-MM-DD)
        temp_f:   settlement high temperature in degF
        provenance: which source provided this value
            ('kalshi' | 'settlement_epochs' | 'ghcn')
    """
    __slots__ = ("station", "date", "temp_f", "provenance")

    def __init__(self, station: str, date: str, temp_f: float, provenance: str):
        self.station = station
        self.date = date
        self.temp_f = temp_f
        self.provenance = provenance

    def to_tuple(self) -> Tuple[str, str]:
        return (self.station, self.date)

    def __repr__(self) -> str:
        return f"TruthRecord({self.station}, {self.date}, {self.temp_f}, prov={self.provenance})"


# ─── HistoricalAdapter ─────────────────────────────────────────────────────


class HistoricalAdapter:
    """
    Loads truth (settlement) and base-forecast data for held-out validation
    of Gamma additive signals.

    Truth precedence:
        1. Kalshi finalized settlements (provenance='kalshi')
        2. Settlement epochs from METAR (provenance='settlement_epochs')
        3. GHCN daily (provenance='ghcn')

    Base forecast precedence (model):
        era5 → ecmwf → gfs → gem → icon
        (variable='temperature_2m_max', lead between 0 and 1 day)
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else _DATA_DIR
        self._validate_dbs()

    # ─── Source availability ───────────────────────────────────────────

    def _validate_dbs(self) -> Dict[str, bool]:
        """Check which databases are available. Logs warnings for missing sources."""
        self._db_status: Dict[str, bool] = {
            "kalshi_settlements": (self.data_dir / "kalshi_settlements.db").is_file(),
            "metar_backfill": (self.data_dir / "metar_backfill.db").is_file(),
            "ghcn_settlements": (self.data_dir / "ghcn_settlements.db").is_file(),
            "nwp_forecasts": (self.data_dir / "nwp_forecasts.db").is_file(),
        }
        for name, ok in self._db_status.items():
            if not ok:
                logger.warning("HistoricalAdapter: %s NOT available at %s",
                               name, self.data_dir / f"{name}.db")
        return self._db_status

    @property
    def available_sources(self) -> Dict[str, bool]:
        """Return dict of {source_name: available} for all data sources."""
        return dict(self._db_status)

    # ─── Truth (settlement) loading ─────────────────────────────────────

    def load_truth(self,
                   stations: Optional[List[str]] = None,
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   ) -> Dict[Tuple[str, str], TruthRecord]:
        """
        Load settlement truth for validation.

        Args:
            stations:  ICAO list. Defaults to SETTLE_STATIONS.
            start_date: YYYY-MM-DD inclusive. Defaults to HELD_OUT_START.
            end_date:   YYYY-MM-DD inclusive. Defaults to HELD_OUT_END.

        Returns:
            {(station, date): TruthRecord} — each record carries a
            'provenance' attribute ('kalshi' | 'settlement_epochs' | 'ghcn').

        Precedence: Kalshi > settlement_epochs > GHCN.  Once a (station, date)
        is filled by a higher-authority source, lower sources are skipped.
        """
        stations = stations or SETTLE_STATIONS
        start = start_date or HELD_OUT_START
        end = end_date or HELD_OUT_END

        truth: Dict[Tuple[str, str], TruthRecord] = {}

        # --- Source 1: Kalshi finalized settlements (authoritative) ---
        if self._db_status.get("kalshi_settlements"):
            count = self._load_kalshi(truth, stations, start, end)
            logger.info("HistoricalAdapter: loaded %d Kalshi truth records", count)

        # --- Source 2: Settlement epochs (fallback) ---
        if self._db_status.get("metar_backfill"):
            count = self._load_settlement_epochs(truth, stations, start, end)
            logger.info("HistoricalAdapter: loaded %d settlement_epochs truth records", count)

        # --- Source 3: GHCN daily (secondary fallback) ---
        if self._db_status.get("ghcn_settlements"):
            count = self._load_ghcn(truth, stations, start, end)
            logger.info("HistoricalAdapter: loaded %d GHCN truth records", count)

        logger.info("HistoricalAdapter: total truth records = %d "
                     "(Kalshi=%d, epochs=%d, GHCN=%d)",
                     len(truth),
                     sum(1 for t in truth.values() if t.provenance == PROV_KALSHI),
                     sum(1 for t in truth.values() if t.provenance == PROV_EPOCH),
                     sum(1 for t in truth.values() if t.provenance == PROV_GHCN))

        return truth

    def _load_kalshi(self,
                     truth: Dict[Tuple[str, str], TruthRecord],
                     stations: List[str],
                     start: str,
                     end: str,
                     ) -> int:
        """Load Kalshi settlements, inserting into truth dict with PROV_KALSHI."""
        db_path = self.data_dir / "kalshi_settlements.db"
        if not db_path.is_file():
            return 0

        count = 0
        station_set = set(stations)
        # Build station placeholders matching the station count
        placeholders = ",".join("?" for _ in station_set)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()

            # Kalshi settlements: source_type='finalized' means the market
            # settled to a known temperature value.
            cur.execute(
                f"""
                SELECT station, target_date, kalshi_temp
                FROM kalshi_settlements
                WHERE source_type = 'finalized'
                  AND kalshi_temp IS NOT NULL
                  AND station IN ({placeholders})
                  AND target_date >= ?
                  AND target_date <= ?
                """,
                list(station_set) + [start, end],
            )

            for row in cur.fetchall():
                st, d, t = str(row[0]), str(row[1]), float(row[2])
                # Sanity filter
                if not (SANITY_MIN_F <= t <= SANITY_MAX_F):
                    continue
                key = (st, d)
                # Kalshi is highest authority — overwrite anything
                truth[key] = TruthRecord(
                    station=st, date=d, temp_f=t, provenance=PROV_KALSHI
                )
                count += 1

            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError) as exc:
            logger.warning("HistoricalAdapter: Kalshi DB error: %s", exc)

        return count

    def _load_settlement_epochs(self,
                                truth: Dict[Tuple[str, str], TruthRecord],
                                stations: List[str],
                                start: str,
                                end: str,
                                ) -> int:
        """
        Load settlement_epochs from metar_backfill.db.

        NOTE: The existing backtest.py::load_truth() queries column names
        (bucket_date, bucket_high_f) that DO NOT EXIST.  This adapter uses
        the correct column names (local_trading_date, settlement_bucket).
        """
        db_path = self.data_dir / "metar_backfill.db"
        if not db_path.is_file():
            return 0

        count = 0
        station_set = set(stations)
        placeholders = ",".join("?" for _ in station_set)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()

            # Correct column names: local_trading_date, settlement_bucket
            # Filter: HIGH market type, closed epoch status, non-null bucket value
            cur.execute(
                f"""
                SELECT station, local_trading_date, settlement_bucket
                FROM settlement_epochs
                WHERE market_type = 'HIGH'
                  AND epoch_status = 'closed'
                  AND settlement_bucket IS NOT NULL
                  AND station IN ({placeholders})
                  AND local_trading_date >= ?
                  AND local_trading_date <= ?
                """,
                list(station_set) + [start, end],
            )

            for row in cur.fetchall():
                st, d, bucket = str(row[0]), str(row[1]), row[2]
                t = float(bucket)  # settlement_bucket is integer °F
                if not (SANITY_MIN_F <= t <= SANITY_MAX_F):
                    continue
                key = (st, d)
                # Only insert if not already filled by a higher-authority source
                if key not in truth:
                    truth[key] = TruthRecord(
                        station=st, date=d, temp_f=t, provenance=PROV_EPOCH
                    )
                    count += 1

            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError) as exc:
            logger.warning("HistoricalAdapter: settlement_epochs query error: %s", exc)

        return count

    def _load_ghcn(self,
                   truth: Dict[Tuple[str, str], TruthRecord],
                   stations: List[str],
                   start: str,
                   end: str,
                   ) -> int:
        """
        Load GHCN daily settlements as secondary fallback.

        GHCN is a NOAA daily-summary dataset, not a Kalshi settlement source,
        but it provides ground-truth high temps for stations with long records.
        """
        db_path = self.data_dir / "ghcn_settlements.db"
        if not db_path.is_file():
            return 0

        count = 0
        station_set = set(stations)
        placeholders = ",".join("?" for _ in station_set)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()

            cur.execute(
                f"""
                SELECT station, date, high_f
                FROM ghcn_settlements
                WHERE high_f IS NOT NULL
                  AND station IN ({placeholders})
                  AND date >= ?
                  AND date <= ?
                """,
                list(station_set) + [start, end],
            )

            for row in cur.fetchall():
                st, d, t = str(row[0]), str(row[1]), float(row[2])
                if not (SANITY_MIN_F <= t <= SANITY_MAX_F):
                    continue
                key = (st, d)
                # Only insert if not already filled
                if key not in truth:
                    truth[key] = TruthRecord(
                        station=st, date=d, temp_f=t, provenance=PROV_GHCN
                    )
                    count += 1

            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError) as exc:
            logger.warning("HistoricalAdapter: GHCN query error: %s", exc)

        return count

    # ─── Base forecast loading ──────────────────────────────────────────

    def load_base_forecasts(self,
                            stations: Optional[List[str]] = None,
                            start_date: Optional[str] = None,
                            end_date: Optional[str] = None,
                            ) -> Dict[str, Dict[Tuple[str, str], float]]:
        """
        Load NWP base forecasts for validation.

        Args:
            stations:  ICAO list. Defaults to SETTLE_STATIONS.
            start_date: YYYY-MM-DD inclusive. Defaults to HELD_OUT_START.
            end_date:   YYYY-MM-DD inclusive. Defaults to HELD_OUT_END.

        Returns:
            {model: {(station, target_date): temp_2m_max_value}}
            Models are filtered to temperature_2m_max with lead ≤ 1 day.
        """
        stations = stations or SETTLE_STATIONS
        start = start_date or HELD_OUT_START
        end = end_date or HELD_OUT_END

        out: Dict[str, Dict[Tuple[str, str], float]] = defaultdict(dict)
        db_path = self.data_dir / "nwp_forecasts.db"

        if not db_path.is_file():
            logger.warning("HistoricalAdapter: NWP DB not found at %s", db_path)
            return dict(out)

        station_set = set(stations)
        placeholders = ",".join("?" for _ in station_set)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()

            cur.execute(
                f"""
                SELECT model, station, target_date, value
                FROM nwp_forecasts
                WHERE variable = 'temperature_2m_max'
                  AND station IN ({placeholders})
                  AND target_date >= ?
                  AND target_date <= ?
                  AND (julianday(target_date) - julianday(fetch_date)) <= 1
                  AND (julianday(target_date) - julianday(fetch_date)) >= 0
                """,
                list(station_set) + [start, end],
            )

            for row in cur.fetchall():
                model, st, d, v = str(row[0]), str(row[1]), str(row[2]), float(row[3])
                out[model][(st, d)] = float(v)

            conn.close()
        except (sqlite3.OperationalError, FileNotFoundError) as exc:
            logger.warning("HistoricalAdapter: NWP query error: %s", exc)

        logger.info("HistoricalAdapter: loaded base forecasts from %d models "
                     "(era5=%d, ecmwf=%d, gfs=%d, gem=%d, icon=%d)",
                     len(out),
                     len(out.get("era5", {})),
                     len(out.get("ecmwf", {})),
                     len(out.get("gfs", {})),
                     len(out.get("gem", {})),
                     len(out.get("icon", {})))

        return dict(out)

    # ─── Best-base helper ───────────────────────────────────────────────

    def pick_base_forecast(self,
                           base_all: Dict[str, Dict[Tuple[str, str], float]],
                           key: Tuple[str, str],
                           ) -> Optional[float]:
        """
        Select the best available base forecast for a (station, date) key.

        Precedence: era5 → ecmwf → gfs → gem → icon.
        This matches the existing backtest.py::pick_base_forecast() but is
        surfaced here so the adapter is self-contained.
        """
        for model in _MODEL_PRECEDENCE:
            model_data = base_all.get(model, {})
            v = model_data.get(key)
            if v is not None:
                return v
        return None

    # ─── Provenance summary ─────────────────────────────────────────────

    def provenance_summary(self,
                           truth: Dict[Tuple[str, str], TruthRecord],
                           ) -> Dict[str, int]:
        """
        Report how many (station, date) pairs come from each source.

        Returns e.g. {"kalshi": 1377, "settlement_epochs": 3793, "ghcn": 0}
        """
        summary: Dict[str, int] = {PROV_KALSHI: 0, PROV_EPOCH: 0, PROV_GHCN: 0}
        for rec in truth.values():
            tag = rec.provenance
            if tag in summary:
                summary[tag] += 1
            else:
                summary[tag] = 1
        return summary


# ─── Module-level convenience ──────────────────────────────────────────────

_default_adapter = None


def get_adapter(data_dir: Optional[Path] = None) -> HistoricalAdapter:
    """
    Get or create a shared HistoricalAdapter instance.

    Callers that need a one-off adapter can construct one directly:
        adapter = HistoricalAdapter()
    """
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = HistoricalAdapter(data_dir=data_dir)
    return _default_adapter


def load_truth(stations=None, start=None, end=None) -> Dict[Tuple[str, str], TruthRecord]:
    """Module-level convenience: load truth via the default adapter."""
    return get_adapter().load_truth(stations=stations, start_date=start, end_date=end)


def load_base_forecasts(stations=None, start=None, end=None) -> Dict[str, Dict[Tuple[str, str], float]]:
    """Module-level convenience: load base forecasts via the default adapter."""
    return get_adapter().load_base_forecasts(stations=stations, start_date=start, end_date=end)


def pick_base_forecast(base_all, key) -> Optional[float]:
    """Module-level convenience: pick best base forecast."""
    return get_adapter().pick_base_forecast(base_all, key)