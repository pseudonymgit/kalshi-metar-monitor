#!/usr/bin/env python3
"""
NWP + METAR dT/dt Fusion Signal

Combines GFS direction (from NwpDirectSignal) with METAR rate of change
(from MetarDtdtSignal) using Bayesian log-odds fusion.

Logic:
- If GFS says UP and dT/dt is positive → confirm, high confidence
- If GFS says UP but dT/dt is negative → contradict, low confidence
- If GFS says DOWN and dT/dt is negative → confirm, high confidence
- If GFS says DOWN but dT/dt is positive → contradict, low confidence

Fusion formula: Bayesian log-odds
- Prior odds from GFS direction confidence
- Likelihood ratio from METAR dT/dt trend
- Posterior confidence = combined log-odds → sigmoid
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
import logging
import math
import os
from pathlib import Path
from datetime import datetime, timedelta

from .base_signal import BaseSignal, validate_signal
from .nwp_direct_signal import NwpDirectSignal
from .metar_dtdt_signal import MetarDtdtSignal

logger = logging.getLogger(__name__)

METAR_DB_DEFAULT = "data/metar_backfill.db"


class NwpDtdtFusionSignal(BaseSignal):
    """
    NWP + METAR dT/dt Fusion Signal.

    Combines NWP model direction with METAR observed temperature trend
    using Bayesian log-odds fusion for more robust short-term predictions.
    """

    # Bayesian prior: base rate of GFS directional accuracy
    # Used as the prior probability when no signal is available
    BAYESIAN_PRIOR = 0.55

    def __init__(self, db_path: str = None, nwp_db_path: str = None):
        super().__init__(db_path)
        self._nwp_db_path = nwp_db_path or self._resolve_nwp_db()
        self._nwp_signal = NwpDirectSignal(db_path=self._nwp_db_path)
        self._metar_dtdt = MetarDtdtSignal(db_path=db_path)

    def _resolve_nwp_db(self):
        if os.environ.get('NWP_DB_PATH'):
            return Path(os.environ['NWP_DB_PATH']).absolute()
        alt = Path(__file__).resolve().parent.parent.parent.parent / "data" / "nwp_forecasts.db"
        if alt.exists():
            return alt
        return Path("data/nwp_forecasts.db").resolve()

    @property
    def name(self) -> str:
        return "nwp_dtdt_fusion"

    @property
    def min_lookback(self) -> int:
        return 0

    @staticmethod
    def _log_odds_to_probability(log_odds: float) -> float:
        """Convert log-odds to probability: p = 1 / (1 + exp(-log_odds))."""
        return 1.0 / (1.0 + math.exp(-log_odds))

    @staticmethod
    def _probability_to_log_odds(probability: float) -> float:
        """Convert probability to log-odds: log(p / (1-p))."""
        p = max(0.001, min(0.999, probability))
        return math.log(p / (1.0 - p))

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
        Evaluate NWP + METAR dT/dt fusion signal.

        Gets GFS direction from NwpDirectSignal and METAR trend from
        MetarDtdtSignal, then fuses them using Bayesian log-odds.

        Args:
            station: Station code
            date: ISO date string
            conn: Optional SQLite connection to metar DB
            market_type: 'HIGH' or 'LOW'

        Returns:
            (direction, fused_confidence) or (None, 0.0)
        """
        # Get GFS direction from NWP signal
        nwp_direction, nwp_confidence = self._nwp_signal.evaluate_for_station(
            station, date, conn=conn, market_type=market_type
        )

        # Get METAR dT/dt direction
        metar_direction, metar_confidence = self._metar_dtdt.evaluate_for_station(
            station, date, conn=conn, market_type=market_type
        )

        # If neither signal fires, return no signal
        if nwp_direction is None and metar_direction is None:
            return None, 0.0

        # If only one signal fires, use it with reduced confidence
        if nwp_direction is None:
            return metar_direction, metar_confidence * 0.7
        if metar_direction is None:
            return nwp_direction, nwp_confidence * 0.8

        # Both signals fired — perform Bayesian fusion

        # Convert NWP confidence to log-odds (prior)
        prior_log_odds = self._probability_to_log_odds(
            self.BAYESIAN_PRIOR + (nwp_confidence - 0.5) * 0.3
        )

        # Determine likelihood ratio from METAR agreement/contradiction
        directions_agree = (nwp_direction == metar_direction)

        if directions_agree:
            # Agreement: strong confirmation
            # Likelihood ratio based on METAR confidence
            # LR = 1 + metar_confidence (ranges from 1.0 to 2.0)
            likelihood_ratio = 1.0 + metar_confidence
            log_likelihood = math.log(likelihood_ratio)
        else:
            # Contradiction: weaken the signal
            # Likelihood ratio = 1 - metar_confidence * 0.5 (ranges from 0.5 to 1.0)
            likelihood_ratio = max(0.1, 1.0 - metar_confidence * 0.5)
            log_likelihood = math.log(likelihood_ratio)

        # Posterior log-odds = prior + log(likelihood)
        posterior_log_odds = prior_log_odds + log_likelihood

        # Convert back to probability/confidence
        fused_confidence = self._log_odds_to_probability(posterior_log_odds)

        # The fused direction is the NWP direction (since it's the primary signal)
        # The confidence is adjusted by the fusion
        direction = nwp_direction

        # Clamp confidence to reasonable range
        fused_confidence = max(0.05, min(0.95, fused_confidence))

        # If agreement is strong, boost confidence; if contradiction, reduce
        if directions_agree:
            # Boost: confidence is already high from fusion
            pass
        else:
            # Contradiction: either flip direction (if metar confidence is very high)
            # or reduce confidence significantly
            if metar_confidence > 0.7 and nwp_confidence < 0.4:
                # METAR confidence outweighs NWP — flip direction
                direction = metar_direction
                fused_confidence = metar_confidence * 0.8
            elif metar_confidence > 0.5:
                # Strong contradiction — keep NWP direction but low confidence
                fused_confidence = min(fused_confidence, 0.3)
            else:
                # Weak contradiction
                fused_confidence = min(fused_confidence, 0.4)

        return direction, fused_confidence


def test_signal():
    """Quick test."""
    signal = NwpDtdtFusionSignal()
    print(f"Name: {signal.name}")
    print("Test complete.")


if __name__ == "__main__":
    test_signal()