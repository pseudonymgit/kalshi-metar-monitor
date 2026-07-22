#!/usr/bin/env python3
"""
ESDR Signal — Ensemble Spread Divergence Rate

ESDR (Ensemble Spread Divergence Rate) measures the rate at which ensemble
member forecasts diverge over time. Widening spread at hour 2 predicts
reversion/error by hour 6.

This is a STOP-LOSS signal, not an entry signal. It is used to:
- Actively reduce position size when ensemble confidence degrades
- Trigger early exit when spread exceeds climatological normal thresholds

Logic:
- Requires 31-member ensemble (HGEFS or GEFS) with individual member forecasts
- Compute spread (IQR or StdDev) at forecast hour 2 and hour 6
- If spread(hour 6) / spread(hour 2) > 1.5x → 60% chance of reversion
- Strongest signal: spread > 1.5x climatological normal → 60% chance of next 6h being wrong

NOTE: This signal requires full ensemble member data (31 members) which is not
currently available in the NWP database. Only the ensemble mean is stored.
This class is a stub ready for activation when HGEFS data becomes available.
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
import math

from .base_signal import BaseSignal, validate_signal
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

logger = logging.getLogger(__name__)

NWP_DB_DEFAULT = "data/nwp_forecasts.db"


class EsdrSignal(BaseSignal):
    """
    ESDR (Ensemble Spread Divergence Rate) Signal.

    Measures ensemble spread divergence to detect forecast degradation
    and trigger stop-loss or position reduction.

    This is a STOP-LOSS signal that returns negative confidence (indicating
    the prediction should be weakened) when spread is widening abnormally.
    """

    def __init__(self, db_path: str = None, nwp_db_path: str = None):
        super().__init__(db_path)
        self.nwp_db_path = nwp_db_path or self._resolve_nwp_db()

    def _resolve_nwp_db(self):
        if os.environ.get('NWP_DB_PATH'):
            return Path(os.environ['NWP_DB_PATH']).absolute()
        alt = Path(__file__).resolve().parent.parent.parent / NWP_DB_DEFAULT
        if alt.exists():
            return alt
        return Path(NWP_DB_DEFAULT).resolve()

    @property
    def name(self) -> str:
        return "esdr"

    @property
    def min_lookback(self) -> int:
        return 0

    @validate_signal
    def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
        """Standard evaluate interface — not applicable. Use evaluate_for_station()."""
        return None, 0.0

    def evaluate_for_station(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection = None,
        market_type: str = 'HIGH'
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate ESDR for a specific station.

        Checks if ensemble spread is widening abnormally. Returns a signal
        with direction=None (no direction) and a confidence value that
        represents the strength of the divergence warning.

        A high confidence indicates the existing prediction is likely wrong
        and positions should be reduced.

        Args:
            station: Station code
            date: ISO date string
            conn: Optional SQLite connection
            market_type: 'HIGH' or 'LOW'

        Returns:
            (None, divergence_confidence) where divergence_confidence > 0.5
            indicates positions should be reduced/closed.
            Returns (None, 0.0) if no divergence detected.
        """
        # Check if full ensemble data is available
        own_conn = conn is None
        if own_conn:
            try:
                conn = get_sqlite_connection(self.nwp_db_path) if os.path.exists(self.nwp_db_path) else None
            except Exception:
                conn = None

        if conn is None:
            return None, 0.0

        try:
            cur = conn.cursor()

            # Check if we have multi-member ensemble data
            cur.execute("""
                SELECT COUNT(DISTINCT member_index)
                FROM nwp_forecasts
                WHERE station = ? AND model = 'ensemble'
                  AND member_index IS NOT NULL AND member_index != 0
            """, (station,))
            member_count = cur.fetchone()[0]

            if member_count < 10:
                # Not enough ensemble members for meaningful ESDR
                logger.debug(f"ESDR: Only {member_count} ensemble members for {station} — insufficient")
                return None, 0.0

            # Get forecast target dates
            cur.execute("""
                SELECT DISTINCT target_date
                FROM nwp_forecasts
                WHERE station = ? AND model = 'ensemble'
                  AND variable = 'temperature_2m_max'
                  AND member_index IS NOT NULL
                ORDER BY target_date ASC
                LIMIT 7
            """, (station,))
            target_dates = [r[0] for r in cur.fetchall()]

            if len(target_dates) < 4:
                return None, 0.0

            # Find the target date that matches or is closest to our date
            target_date = None
            for td in target_dates:
                if td >= date:
                    target_date = td
                    break

            if target_date is None:
                target_date = target_dates[0]

            # Get spread at early forecast (hour ~2 = first available forecast)
            # and later forecast (hour ~6)
            target_idx = target_dates.index(target_date)

            # Get ensemble member values for target dates
            spreads = []
            for td in target_dates[:min(4, len(target_dates))]:
                cur.execute("""
                    SELECT value
                    FROM nwp_forecasts
                    WHERE station = ? AND model = 'ensemble'
                      AND target_date = ?
                      AND variable = 'temperature_2m_max'
                      AND member_index IS NOT NULL AND member_index != 0
                """, (station, td))
                values = [r[0] for r in cur.fetchall() if r[0] is not None]

                if len(values) >= 10:
                    # Compute IQR as spread measure
                    sorted_vals = sorted(values)
                    q1 = sorted_vals[len(sorted_vals) // 4]
                    q3 = sorted_vals[3 * len(sorted_vals) // 4]
                    iqr = q3 - q1
                    spreads.append({'date': td, 'iqr': iqr, 'mean': sum(values) / len(values)})

            if len(spreads) < 3:
                return None, 0.0

            # Check if spread is widening: compare early vs later
            early_spread = spreads[0]['iqr']
            late_spread = spreads[-1]['iqr']

            if early_spread <= 0:
                return None, 0.0

            spread_ratio = late_spread / early_spread

            # ESDR trigger: spread ratio > 1.5x
            if spread_ratio > 1.5:
                # Spread widening — signal divurgence
                divergence_confidence = min(0.6 + (spread_ratio - 1.5) * 0.2, 0.85)
                logger.info(f"ESDR: {station} spread ratio {spread_ratio:.2f}x — "
                           f"divergence warning (confidence={divergence_confidence:.2f})")
                # Return None direction + divergence confidence as a special signal
                # The direction is None because ESDR is a stop-loss signal, not direction
                return None, divergence_confidence

            return None, 0.0

        finally:
            if own_conn and conn:
                conn.close()

    def get_reduction_factor(self, station: str, date: str, conn: sqlite3.Connection = None) -> float:
        """
        Get the position reduction factor based on ESDR.

        Returns:
            0.0 = no reduction, 0.5 = reduce by 50%, 1.0 = reduce by 100% (close)
        """
        direction, confidence = self.evaluate_for_station(station, date, conn)
        if direction is None and confidence > 0.5:
            # Map confidence to reduction factor
            if confidence > 0.75:
                return 1.0  # Full close
            elif confidence > 0.6:
                return 0.5  # Half position
            else:
                return 0.25  # Quarter position
        return 0.0


def test_signal():
    """Quick test."""
    signal = EsdrSignal()
    print(f"Name: {signal.name}")
    print(f"NWP DB: {signal.nwp_db_path}")
    print(f"NWP DB exists: {os.path.exists(signal.nwp_db_path)}")
    print("Test complete.")


if __name__ == "__main__":
    test_signal()