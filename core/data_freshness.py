#!/usr/bin/env python3
"""
Data Freshness Monitor v1.0 — Phase 3.2 Graceful Degradation

Monitors the staleness of data sources (METAR observations, NWP forecasts)
and reports a freshness tier that determines how aggressively the system
can trade.

Tiers:
  - Green:  0-1h    100% positions, all lanes
  - Yellow: 1-2h    100% positions, log warning
  - Orange: 2-6h    50% positions, Sure Thing lane only
  - Red:    >6h     HALT everything, escalate alert

Scripts only — no AI/ML in the freshness loop.
"""

import sqlite3
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class FreshnessTier(Enum):
    """Data freshness tier — determines trading aggressiveness."""
    GREEN = "green"       # 0-1h: Normal, all lanes
    YELLOW = "yellow"     # 1-2h: Warn, all lanes with logging
    ORANGE = "orange"     # 2-6h: Degrade, 50% positions, Sure Thing only
    RED = "red"           # >6h: HALT everything, escalate


# Tier thresholds: (lower_bound_seconds, upper_bound_seconds)
# upper_bound = None means no upper bound (unbounded above)
TIER_BOUNDS: Dict[FreshnessTier, Tuple[float, Optional[float]]] = {
    FreshnessTier.GREEN:  (0, 3600),       # 0-1h
    FreshnessTier.YELLOW: (3600, 7200),    # 1-2h
    FreshnessTier.ORANGE: (7200, 21600),   # 2-6h
    FreshnessTier.RED:    (21600, None),   # >6h
}


# Tier → trading action mapping
TIER_ACTIONS: Dict[FreshnessTier, Dict[str, Any]] = {
    FreshnessTier.GREEN: {
        "position_multiplier": 1.0,        # 100% normal sizing
        "allowed_lanes": ["regular", "sure_thing", "spike_reversion", "goldilocks"],  # A3: added spike_reversion
        "halt": False,
        "log_level": "DEBUG",
    },
    FreshnessTier.YELLOW: {
        "position_multiplier": 1.0,        # 100% normal sizing
        "allowed_lanes": ["regular", "sure_thing", "spike_reversion", "goldilocks"],  # A3: added spike_reversion
        "halt": False,
        "log_level": "WARNING",
    },
    FreshnessTier.ORANGE: {
        "position_multiplier": 0.5,        # 50% position sizing
        "allowed_lanes": ["sure_thing"],   # Sure Thing lane only
        "halt": False,
        "log_level": "WARNING",
    },
    FreshnessTier.RED: {
        "position_multiplier": 0.0,        # No positions
        "allowed_lanes": [],               # All lanes halted
        "halt": True,
        "log_level": "CRITICAL",
    },
}


class DataFreshnessMonitor:
    """
    Monitors data source staleness and returns a freshness tier.

    Checks:
      1. METAR observation freshness (last station observation timestamp)
      2. NWP forecast freshness (last forecast model run timestamp)
      3. Returns the worst tier across all data sources for a station.

    Typical usage:
        monitor = DataFreshnessMonitor(metar_db_path)
        tier = monitor.get_tier("KATL")
        actions = monitor.get_tier_actions(tier)
        if actions["halt"]:
            print("HALTING — data too stale")
    """

    def __init__(self, metar_db_path: str, nwp_db_path: Optional[str] = None):
        """
        Args:
            metar_db_path: Path to SQLite database with METAR observations.
            nwp_db_path: Optional path to NWP forecast database.
        """
        self._metar_db_path = metar_db_path
        self._nwp_db_path = nwp_db_path
        self._now_fn = datetime.now  # Overridable for testing
        self._logger = logging.getLogger(f"{__name__}.DataFreshnessMonitor")

    def set_now_fn(self, fn):
        """Override the time source for testing."""
        self._now_fn = fn

    def get_tier(self, station: str) -> FreshnessTier:
        """
        Get the freshness tier for a station based on all data sources.

        Returns the worst (most degraded) tier across:
          - METAR observation freshness
          - NWP forecast freshness (if available)

        Args:
            station: ICAO station code (e.g. "KATL")

        Returns:
            FreshnessTier enum value
        """
        now = self._now_fn(timezone.utc) if hasattr(self._now_fn, 'tzinfo') else datetime.now(timezone.utc)

        tiers = []

        # Check METAR freshness
        metar_tier = self._check_metar_freshness(station, now)
        tiers.append(metar_tier)

        # Check NWP forecast freshness
        if self._nwp_db_path:
            nwp_tier = self._check_nwp_freshness(station, now)
            tiers.append(nwp_tier)

        # Return the worst tier (most degraded)
        return self._worst_tier(tiers)

    def _check_metar_freshness(self, station: str, now: datetime) -> FreshnessTier:
        """
        Check how fresh the METAR data is for a station.

        Returns the freshness tier based on the time since the most recent
        METAR observation for this station.
        """
        try:
            conn = sqlite3.connect(self._metar_db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            c = conn.cursor()

            # Get the most recent METAR observation timestamp for this station
            c.execute("""
                SELECT MAX(timestamp_utc) as last_obs
                FROM metar_observations
                WHERE station = ?
                AND timestamp_utc IS NOT NULL
            """, (station,))

            row = c.fetchone()
            conn.close()

            if row is None or row[0] is None:
                # No observations at all — RED tier
                self._logger.warning("No METAR obs found for %s", station)
                return FreshnessTier.RED

            last_obs_str = row[0]
            # Parse timestamp — handle both ISO format and naive datetime
            try:
                last_obs = datetime.fromisoformat(last_obs_str)
                if last_obs.tzinfo is None:
                    last_obs = last_obs.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                self._logger.warning("Cannot parse METAR timestamp '%s' for %s", last_obs_str, station)
                return FreshnessTier.RED

            age_seconds = (now - last_obs).total_seconds()
            if age_seconds < 0:
                # Future timestamp — treat as fresh
                age_seconds = 0

            return self._age_to_tier(age_seconds)

        except sqlite3.Error as e:
            self._logger.error("METAR DB error for %s: %s", station, e)
            return FreshnessTier.RED

    def _check_nwp_freshness(self, station: str, now: datetime) -> FreshnessTier:
        """
        Check how fresh the NWP forecast data is for a station.

        Returns the tier based on the most recent forecast model run.
        """
        if not self._nwp_db_path:
            return FreshnessTier.GREEN  # No NWP data source = no degradation

        try:
            conn = sqlite3.connect(self._nwp_db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            c = conn.cursor()

            # Try common NWP table names
            for table in ["nwp_forecasts", "gfs_forecasts", "forecast_data"]:
                try:
                    c.execute(f"""
                        SELECT MAX(run_timestamp) as last_run
                        FROM {table}
                        WHERE station = ?
                        AND run_timestamp IS NOT NULL
                    """, (station,))
                    row = c.fetchone()
                    if row and row[0]:
                        break
                except sqlite3.OperationalError:
                    continue

            conn.close()

            if row is None or row[0] is None:
                # No NWP data — treat as GREEN (no degradation for missing NWP)
                return FreshnessTier.GREEN

            last_run_str = row[0]
            try:
                last_run = datetime.fromisoformat(last_run_str)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return FreshnessTier.GREEN

            age_seconds = (now - last_run).total_seconds()
            if age_seconds < 0:
                age_seconds = 0

            return self._age_to_tier(age_seconds)

        except sqlite3.Error as e:
            self._logger.warning("NWP DB error for %s: %s", station, e)
            return FreshnessTier.GREEN

    def _age_to_tier(self, age_seconds: float) -> FreshnessTier:
        """
        Map an age in seconds to a freshness tier.

        Args:
            age_seconds: Age of the data point in seconds

        Returns:
            FreshnessTier
        """
        for tier, (lower, upper) in TIER_BOUNDS.items():
            if age_seconds >= lower:
                if upper is None or age_seconds < upper:
                    return tier
        return FreshnessTier.RED

    def _worst_tier(self, tiers: List[FreshnessTier]) -> FreshnessTier:
        """
        Return the worst (most degraded) tier from a list.

        Order: RED > ORANGE > YELLOW > GREEN
        """
        severity = [FreshnessTier.RED, FreshnessTier.ORANGE, FreshnessTier.YELLOW, FreshnessTier.GREEN]
        for severe in severity:
            if severe in tiers:
                return severe
        return FreshnessTier.GREEN

    def get_tier_actions(self, tier: FreshnessTier) -> Dict[str, Any]:
        """
        Get the recommended actions for a given tier.

        Returns:
            Dict with keys: position_multiplier, allowed_lanes, halt, log_level
        """
        return TIER_ACTIONS.get(tier, TIER_ACTIONS[FreshnessTier.RED])

    def get_all_station_tiers(self, stations: List[str]) -> Dict[str, FreshnessTier]:
        """
        Get the freshness tier for every station in the list.

        Args:
            stations: List of ICAO station codes

        Returns:
            Dict mapping station -> FreshnessTier
        """
        return {s: self.get_tier(s) for s in stations}

    def get_worst_global_tier(self, stations: List[str]) -> FreshnessTier:
        """
        Get the worst freshness tier across all given stations.

        Useful for system-wide health checks.

        Returns:
            The most degraded FreshnessTier found across all stations
        """
        tiers = [self.get_tier(s) for s in stations]
        return self._worst_tier(tiers)

    def get_freshness_age_seconds(self, station: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Get the raw age of METAR and NWP data for a station.

        Args:
            station: ICAO station code

        Returns:
            (metar_age_seconds, nwp_age_seconds) — either may be None
        """
        now = datetime.now(timezone.utc)
        metar_age = None
        nwp_age = None

        # METAR age
        try:
            conn = sqlite3.connect(self._metar_db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            c = conn.cursor()
            c.execute("""
                SELECT MAX(timestamp_utc) FROM metar_observations
                WHERE station = ? AND timestamp_utc IS NOT NULL
            """, (station,))
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                last_obs = datetime.fromisoformat(row[0])
                if last_obs.tzinfo is None:
                    last_obs = last_obs.replace(tzinfo=timezone.utc)
                metar_age = (now - last_obs).total_seconds()
        except Exception:
            pass

        # NWP age
        if self._nwp_db_path:
            try:
                conn = sqlite3.connect(self._nwp_db_path, timeout=5)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                c = conn.cursor()
                for table in ["nwp_forecasts", "gfs_forecasts", "forecast_data"]:
                    try:
                        c.execute(f"""
                            SELECT MAX(run_timestamp) FROM {table}
                            WHERE station = ? AND run_timestamp IS NOT NULL
                        """, (station,))
                        row = c.fetchone()
                        if row and row[0]:
                            last_run = datetime.fromisoformat(row[0])
                            if last_run.tzinfo is None:
                                last_run = last_run.replace(tzinfo=timezone.utc)
                            nwp_age = (now - last_run).total_seconds()
                            break
                    except sqlite3.OperationalError:
                        continue
                conn.close()
            except Exception:
                pass

        return metar_age, nwp_age


# ─── Module-level convenience function ───────────────────────────────────

def get_freshness_tier(monitor: DataFreshnessMonitor, station: str) -> FreshnessTier:
    """Convenience: get the freshness tier for a station."""
    return monitor.get_tier(station)


def should_halt_trading(monitor: DataFreshnessMonitor, station: str) -> Tuple[bool, str]:
    """
    Convenience: check if trading should be halted for a station.

    Returns:
        (should_halt, reason)
    """
    tier = monitor.get_tier(station)
    if tier == FreshnessTier.RED:
        return True, f"Data freshness RED for {station}: data >6h stale"
    return False, f"Tier {tier.value} — trading OK"


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Quick test with a dummy DB path
    monitor = DataFreshnessMonitor("/tmp/nonexistent.db")
    tier = monitor.get_tier("KATL")
    actions = monitor.get_tier_actions(tier)
    print(f"Tier: {tier.value}")
    print(f"Actions: halt={actions['halt']}, multiplier={actions['position_multiplier']}, lanes={actions['allowed_lanes']}")