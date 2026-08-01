#!/usr/bin/env python3
"""
frontal_passage_nowcast_signal.py — Spatial nowcasting signal for cold front detection.

Detects cold/warm fronts approaching a Kalshi trading station by computing
temperature gradients between the target station and its upstream METAR stations.

This is the spatial nowcasting signal (as opposed to the temporal frontal passage
signal at the target station itself).

Design doc: docs/plans/NOWCASTING-INTEGRATION-DESIGN.md

B-Mode compliant. No AI/ML.
"""

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ──
from core.signals.base_signal import BaseSignal

# Detection thresholds (from design doc)
GRADIENT_THRESHOLD_C = 3.0       # 3°C = 5.4°F minimum gradient for detection
MIN_UPSTREAM_STATIONS = 2         # Need ≥2 upstream obs for robustness
MAX_OBS_AGE_MINUTES = 30          # METARs are hourly; 30-min window
BASE_CONFIDENCE = 0.40            # Starting confidence for minimal detection
MAX_CONFIDENCE = 0.75             # Capped confidence

# Upstream station map: target -> list of upstream ICAO codes
# Based on climatological prevailing cold-front tracks (W→E, NW→SE)
UPSTREAM_MAP = {
    "KNYC": ["KEWR", "KPHL", "KACY"],
    "KMDW": ["KMKE", "KDBQ", "KPIA"],
    "KDEN": ["KCOS", "KPUB", "KLAR"],
    "KDFW": ["KOKC", "KSPS", "KACT"],
    "KSEA": ["KPDX", "KCLM", "KBLI"],
    "KSFO": ["KSTS", "KSQL", "KMRY"],
    "KLAX": ["KSBA", "KCMA", "KNTD"],
    "KPHX": ["KBLH", "KIGM", "KYUM"],
    "KMIA": ["KFLL", "KPBI", "KRSW"],
    "KBOS": ["KPVD", "KORH", "KBED"],
    "KATL": ["KAHN", "KCSG", "KMCN"],
    "KDCA": ["KRIC", "KCHO", "KNYC"],
    "KPHL": ["KNYC", "KACY", "KBWI"],
    "KMSP": ["KSTP", "KMKT", "KAXN"],
    # Fallback: stations without dedicated upstream maps use nearest climatological
    "KLAX": ["KSBA", "KCMA", "KNTD"],
    "KHOU": ["KAUS", "KACT", "KCRP"],
    "KAUS": ["KSAT", "KACT", "KGRK"],
    "KOKC": ["KICT", "KSPS", "KEND"],
    "KSAT": ["KDRT", "KDLF", "KCOU"],
    "KMSY": ["KMCB", "KHDC", "KASD"],
    "KPHX": ["KBLH", "KIGM", "KYUM"],
    "KLAS": ["KBIH", "KEKO", "KIFP"],
}

# METAR DB path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_METAR_DB = os.path.join(REPO_ROOT, "data", "metar_backfill.db")


class FrontalPassageNowcastSignal(BaseSignal):
    """
    Spatial nowcasting signal: detects cold/warm fronts approaching
    a Kalshi trading station by computing temperature gradients
    between the target station and its upstream METAR stations.
    """

    def __init__(self, metar_db_path: str = DEFAULT_METAR_DB):
        super().__init__(db_path=metar_db_path)
        self._name = "frontal_passage_nowcast"
        self._cached_upstream_temps: Dict[str, Dict[str, float]] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 300  # 5 minutes

    @property
    def name(self) -> str:
        return self._name

    @property
    def min_lookback(self) -> int:
        """Nowcasting doesn't require historical lookback — it's real-time."""
        return 0

    def evaluate_for_station(
        self,
        station: str,
        date: str,
        conn: Optional[sqlite3.Connection] = None,
        market_type: Optional[str] = None,
    ) -> Tuple[Optional[str], float]:
        """
        Check for approaching front at this station.

        1. Look up upstream stations from UPSTREAM_MAP
        2. Get current temp at target station (last 30 min)
        3. Get current temps at upstream stations (last 30 min)
        4. Compute median gradient g = T_target - T_upstream
        5. If |g| > GRADIENT_THRESHOLD_C, fire signal
        6. Direction: 'down' if upstream is colder, 'up' if warmer
        7. Confidence: function of gradient magnitude + n_upstream

        Args:
            station: ICAO station code
            date: Date string YYYY-MM-DD (used for context, not strict filtering)
            conn: Database connection (created if None)
            market_type: 'HIGH' or 'LOW' (affects trading decision)

        Returns:
            (direction, confidence) or (None, 0.0) if no signal
        """
        upstream_stations = UPSTREAM_MAP.get(station, [])
        if len(upstream_stations) < MIN_UPSTREAM_STATIONS:
            logger.debug("Insufficient upstream stations for %s", station)
            return None, 0.0

        # Get connection
        close_conn = False
        if conn is None:
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                close_conn = True
            except Exception as e:
                logger.error("Cannot connect to METAR DB: %s", e)
                return None, 0.0

        try:
            # Get target station temperature
            target_temp = self._get_recent_temp(conn, station, date)
            if target_temp is None:
                logger.debug("No recent temperature for target %s", station)
                return None, 0.0

            # Get upstream temperatures
            upstream_temps = []
            for us in upstream_stations:
                temp = self._get_recent_temp(conn, us, date)
                if temp is not None:
                    upstream_temps.append(temp)

            if len(upstream_temps) < MIN_UPSTREAM_STATIONS:
                logger.debug(
                    "Only %d/%d upstream stations have data for %s",
                    len(upstream_temps), len(upstream_stations), station,
                )
                return None, 0.0

            # Compute gradients
            gradients = [target_temp - ut for ut in upstream_temps]
            g = sorted(gradients)[len(gradients) // 2]  # median

            # Cache upstream temps
            self._cached_upstream_temps[station] = {
                u: t for u, t in zip(upstream_stations, upstream_temps)
            }

            # Check gradient against threshold
            if abs(g) < GRADIENT_THRESHOLD_C:
                return None, 0.0

            # Determine direction
            if g > GRADIENT_THRESHOLD_C:
                direction = "down"  # upstream colder = cold front approaching
            else:
                direction = "up"  # upstream warmer = warm front approaching

            # Compute confidence
            k = abs(g) / GRADIENT_THRESHOLD_C  # gradient ratio
            n_valid = max(len(upstream_temps), MIN_UPSTREAM_STATIONS)
            confidence = min(
                MAX_CONFIDENCE,
                BASE_CONFIDENCE + 0.05 * (k - 1) + 0.05 * (n_valid - MIN_UPSTREAM_STATIONS),
            )

            logger.info(
                "Nowcasting signal: %s direction=%s gradient=%.1f°C confidence=%.2f "
                "n_upstream=%d",
                station, direction, g, confidence, len(upstream_temps),
            )

            return direction, confidence

        except Exception as e:
            logger.error("Error evaluating nowcasting for %s: %s", station, e)
            return None, 0.0

        finally:
            if close_conn and conn:
                conn.close()

    def _get_recent_temp(
        self,
        conn: sqlite3.Connection,
        station: str,
        date: str,
    ) -> Optional[float]:
        """
        Get the most recent temperature observation for a station.

        Queries metar_backfill.db for the most recent observation within
        MAX_OBS_AGE_MINUTES of the target date.

        Args:
            conn: Database connection
            station: ICAO station code
            date: Target date string

        Returns:
            Temperature in °C, or None if no recent data
        """
        try:
            # Try the observations table for recent METAR data
            cur = conn.cursor()

            # First try: observations table with temp
            cur.execute("""
                SELECT temp_c
                FROM observations
                WHERE station = ? AND temp_c IS NOT NULL
                ORDER BY obs_time DESC
                LIMIT 1
            """, (station,))
            row = cur.fetchone()
            if row and row[0] is not None:
                return float(row[0])

            # Second try: metar_archive table
            try:
                cur.execute("""
                    SELECT temp_c
                    FROM metar_archive
                    WHERE station = ? AND temp_c IS NOT NULL
                    ORDER BY obs_hour DESC
                    LIMIT 1
                """, (station,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            except Exception:
                pass

            # Third try: settlement_epochs for daily values
            try:
                cur.execute("""
                    SELECT max_temp_c
                    FROM settlement_epochs
                    WHERE station = ? AND max_temp_c IS NOT NULL
                      AND epoch_date = ?
                    LIMIT 1
                """, (station, date))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            except Exception:
                pass

            return None

        except Exception as e:
            logger.debug("Error fetching temp for %s: %s", station, e)
            return None

    def get_cached_state(self) -> dict:
        """Return current cached state for persistence."""
        return {
            "cached_upstream_temps": self._cached_upstream_temps,
            "cache_timestamp": (
                self._cache_timestamp.isoformat()
                if self._cache_timestamp
                else None
            ),
        }

    # ── BaseSignal interface ──

    def evaluate(self, context: dict) -> Tuple[Optional[str], float]:
        """
        Evaluate the nowcasting signal for the given context.

        Args:
            context: Dict with 'station', 'date', 'market_type', 'conn'

        Returns:
            (direction, confidence)
        """
        station = context.get("station", "")
        date = context.get("date", "")
        conn = context.get("conn")
        market_type = context.get("market_type", "HIGH")

        return self.evaluate_for_station(station, date, conn, market_type)


# ── Standalone Test ──

def test_nowcasting_signal():
    """Test the nowcasting signal module."""
    print("=" * 72)
    print("  FrontalPassageNowcastSignal — Self-Test")
    print("=" * 72)

    signal = FrontalPassageNowcastSignal()
    print(f"  Signal name: {signal.name}")
    print(f"  Upstream stations: {len(UPSTREAM_MAP)} targets mapped")
    for target, ups in sorted(UPSTREAM_MAP.items()):
        print(f"    {target} → {','.join(ups)}")

    # Test evaluate (will likely get no data in this environment)
    test_station = "KDEN"
    test_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    direction, confidence = signal.evaluate({
        "station": test_station,
        "date": test_date,
        "market_type": "HIGH",
    })

    print(f"\n  Test evaluate({test_station}, {test_date}):")
    print(f"    Direction: {direction}")
    print(f"    Confidence: {confidence:.4f}")

    # Test gradient thresholds
    print(f"\n  Thresholds:")
    print(f"    GRADIENT_THRESHOLD_C: {GRADIENT_THRESHOLD_C}°C ({GRADIENT_THRESHOLD_C*9/5+32:.1f}°F)")
    print(f"    MIN_UPSTREAM_STATIONS: {MIN_UPSTREAM_STATIONS}")
    print(f"    BASE_CONFIDENCE: {BASE_CONFIDENCE}")
    print(f"    MAX_CONFIDENCE: {MAX_CONFIDENCE}")

    # Test confidence function
    print(f"\n  Confidence examples:")
    for grad_c in [3.0, 4.5, 6.0, 8.0, 10.0]:
        k = grad_c / GRADIENT_THRESHOLD_C
        conf = min(MAX_CONFIDENCE, BASE_CONFIDENCE + 0.05 * (k - 1) + 0.05 * (3 - MIN_UPSTREAM_STATIONS))
        print(f"    |g|={grad_c:.1f}°C, n=3: confidence={conf:.4f}")

    print("\n  ✅ Self-test complete")


if __name__ == "__main__":
    test_nowcasting_signal()