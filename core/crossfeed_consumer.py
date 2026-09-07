"""
Crossfeed Consumer — PWW Whale Detection Modulation (2026-09-06)

Reads whalewatch_crossfeed.db produced by the Polymarket WhaleWatch daemon and
applies a probability bump to the analytical forecast when whale activity is
detected for a matching station.

Design:
  - Fail-graceful: if the crossfeed DB is absent or unreachable, `is_available`
    returns False and all modulation is skipped.
  - Modulation is a +0.5pp to +2.0pp bump depending on anomaly conviction level.
  - Reads unconsumed signals only; marks consumed after processing.

Usage:
    from core.crossfeed_consumer import CrossfeedConsumer
    pww = CrossfeedConsumer()
    if pww.is_available:
        mod_prob, meta = pww.consume_and_modulate(
            station_icao="KNYC",
            analytical_prob=0.72,
            direction="UP",
        )
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
CROSSFEED_DB = (
    REPO_ROOT / "polymarket-whalewatch" / "data" / "whalewatch_crossfeed.db"
)

# Modulation parameters: probability bump in percentage points by conviction level
BUMP_PP_BY_LEVEL = {
    "SUSPECTED": 0.5,
    "DETECTED": 1.0,
    "HIGH_CONVICTION": 2.0,
}

DEFAULT_BUMP_PP = 0.5  # fallback if level is unknown


class CrossfeedConsumer:
    """
    Consumes PWW whale-detection signals from the crossfeed DB.

    Each call to consume_and_modulate() reads the most recent unconsumed
    signal matching the station, applies a probability bump, and marks it
    consumed so it's not applied again.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else CROSSFEED_DB
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def is_available(self) -> bool:
        """True if the crossfeed DB exists and is readable."""
        try:
            return self._db_path.exists() and self._db_path.stat().st_size > 0
        except (OSError, PermissionError):
            return False

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        """Lazy-open a read-only connection to the crossfeed DB."""
        if not self.is_available:
            return None
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            except sqlite3.OperationalError:
                logger.warning("PWW crossfeed DB not readable")
                return None
        return self._conn

    def consume_and_modulate(
        self,
        station_icao: str,
        analytical_prob: float,
        direction: str = "UP",
    ) -> Tuple[float, Optional[Dict]]:
        """
        Look up the most recent unconsumed PWW signal for a station and
        apply a probability bump.

        Args:
            station_icao: ICAO station code (e.g. 'KNYC', 'KLAX').
            analytical_prob: The base analytical probability in [0, 1].
            direction: 'UP' or 'DOWN' — used for sanity; if the whale direction
                       opposes the analytical direction, bump is halved.

        Returns:
            (modulated_probability, meta_dict) where meta_dict contains:
                - bump_pp: the applied bump in percentage points
                - signal_id: the consumed signal row id (or None)
                - direction: whale direction from the signal
                - anomaly_score: anomaly severity
                - detection_level: SUSPECTED / DETECTED / HIGH_CONVICTION
            If no unconsumed signal is found, returns (analytical_prob, None).
        """
        conn = self._get_connection()
        if conn is None:
            return analytical_prob, None

        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, anomaly_score, detection_level, direction, price_at_detection
                FROM whalewatch_signals
                WHERE station_icao = ?
                  AND crossfeed_eligible = 1
                  AND consumed = 0
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                (station_icao.upper(),),
            )
            row = cur.fetchone()
            if row is None:
                return analytical_prob, None

            signal_id, anomaly_score, det_level, whale_dir, price_at_det = row

            # Map conviction level to bump
            bump_pp = BUMP_PP_BY_LEVEL.get(str(det_level).upper(), DEFAULT_BUMP_PP)

            # Direction sanity: if whale is betting against our direction, halve the bump
            direction_u = direction.upper() in ("UP", "BUY", "YES", "HIGH")
            whale_up = str(whale_dir).upper() in ("YES", "UP", "BUY", "HIGH", "1")
            if direction_u != whale_up:
                bump_pp *= 0.5
                logger.debug(
                    "PWW crossfeed %s: whale=%s vs analytical=%s → half bump (%.2fpp)",
                    station_icao, whale_dir, direction, bump_pp,
                )

            # Apply bump
            mod_prob = max(0.01, min(0.99, analytical_prob + bump_pp / 100.0))

            # Mark consumed
            cur.execute(
                "UPDATE whalewatch_signals SET consumed = 1, "
                "consumed_at = ?, consumer_note = ? WHERE id = ?",
                (
                    datetime.now(timezone.utc).isoformat(),
                    f"modulated {station_icao} +{bump_pp:.1f}pp → {mod_prob:.4f}",
                    signal_id,
                ),
            )
            conn.commit()

            meta = {
                "bump_pp": bump_pp,
                "signal_id": signal_id,
                "direction": whale_dir,
                "anomaly_score": anomaly_score,
                "detection_level": det_level,
            }

            logger.info(
                "PWW crossfeed: %s %s anomaly=%.2f %s +%.1fpp → %.4f",
                station_icao, whale_dir, anomaly_score, det_level, bump_pp, mod_prob,
            )

            return mod_prob, meta

        except sqlite3.Error as e:
            logger.warning("PWW crossfeed DB error for %s: %s", station_icao, e)
            return analytical_prob, None

    def close(self) -> None:
        """Close the DB connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def __del__(self):
        self.close()