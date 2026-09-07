"""
HourlyFusionEngine — LOOP (Log-Odds Opinion Pool) Fusion (v1.0 — 2026-09-07)

Fuses multiple directional signal predictions into a single calibrated
probability using the Log-Odds Opinion Pool (LOOP) method.

LOOP formula (unweighted):
  log_odds_fused = sum(log_odds_i) / sqrt(n_pools)
  mean_probability = expit(log_odds_fused)

where log_odds_i = logit(confidence_i) * direction_sign(direction_i)

Agreement gate: minimum number of pools agreeing on direction
before fusion result is considered valid (prevents low-confidence fusion).

Deterministic math only — no AI/ML.
"""

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.special import expit, logit

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────
DEFAULT_AGREEMENT_THRESHOLD = 2    # Minimum pools agreeing for gate passage
MIN_CONFIDENCE_THRESHOLD = 0.2     # Signals below this confidence are ignored
PROBABILITY_RANGE = (0.01, 0.99)   # Clamp range for probabilities


@dataclass
class FusedResult:
    """Result of fusing multiple signal predictions."""
    direction: str                    # "UP" or "DOWN"
    mean_probability: float           # Calibrated directional probability [0, 1]
    agreement_gate_passed: bool       # True if enough pools agreed
    n_pools_agreeing: int             # Number of pools that agreed on the majority direction
    n_pools_total: int                # Total number of pools with signals
    raw_log_odds: float               # Raw fused log-odds before probability transform
    pool_contributions: Dict[str, float]  # pool_name → contribution to log-odds


# ─── Pool Definitions ─────────────────────────────────────────
# Each pool groups signals by methodology type.
# The fusion engine expects signal names to map to one of these pools.
# When multiple signals map to the same pool, the pool's direction is
# determined by majority vote weighted by confidence.

POOL_NAMES = {"gefs", "heuristic", "intraday", "metar_trend", "frontal", "crossfeed"}


class HourlyFusionEngine:
    """
    LOOP (Log-Odds Opinion Pool) fusion engine for hourly signals.

    Takes a dict of signal predictions (from HourlySignalEvaluator),
    groups them by pool, and fuses via LOOP.

    Usage:
        engine = HourlyFusionEngine()
        result = engine.evaluate(
            signal_predictions={"heuristic": ("up", 0.55), ...},
            station="KATL",
            current_utc_hour=14,
            hour_before_settlement=4,
        )
        # result.direction, result.mean_probability, result.agreement_gate_passed
    """

    def __init__(
        self,
        agreement_threshold: int = DEFAULT_AGREEMENT_THRESHOLD,
        min_confidence: float = MIN_CONFIDENCE_THRESHOLD,
    ):
        self.agreement_threshold = agreement_threshold
        self.min_confidence = min_confidence
        self._pool_weights: Dict[str, float] = {
            # Default equal weights for all pools
            "gefs": 1.0,
            "heuristic": 1.0,
            "intraday": 1.0,
            "metar_trend": 1.0,
            "frontal": 1.0,
            "crossfeed": 1.0,
        }
        logger.debug(f"HourlyFusionEngine initialized: agree={agreement_threshold}")

    # ─── Helpers ──────────────────────────────────────────────

    def _signal_to_log_odds(self, direction: str, confidence: float) -> float:
        """
        Convert a directional signal to signed log-odds.

        'up' signals → positive contribution
        'down' signals → negative contribution

        Formula:
          clamped_conf = clip(confidence, 1e-9, 1 - 1e-9)
          log_odds = logit(clamped_conf)
          sign = +1 for 'up', -1 for 'down'
          return sign * log_odds

        Args:
            direction: "up" or "down"
            confidence: Confidence in [0, 1]

        Returns:
            Signed log-odds value
        """
        if confidence < self.min_confidence:
            return 0.0

        clamped = np.clip(confidence, 1e-9, 1 - 1e-9)
        lo = float(logit(clamped))
        sign = 1.0 if direction.upper() in ("UP", "HIGH", "YES", "1") else -1.0

        return sign * lo

    def _pool_vote(
        self, pool_signals: Dict[str, Tuple[Optional[str], float]]
    ) -> Tuple[Optional[str], float, int]:
        """
        Aggregate multiple signals within a single pool into one direction + confidence.

        Uses confidence-weighted majority voting.

        Args:
            pool_signals: Dict of signal_name → (direction_or_None, confidence) within a pool

        Returns:
            (direction, aggregate_confidence, n_agreeing)
            direction is "up", "down", or None (tie)
            n_agreeing is the number of signals supporting the majority direction
        """
        up_weight = 0.0
        down_weight = 0.0
        n_up = 0
        n_down = 0

        for name, (direction, conf) in pool_signals.items():
            if direction is None or conf < self.min_confidence:
                continue
            if direction.upper() in ("UP", "HIGH", "YES", "1"):
                up_weight += conf
                n_up += 1
            else:
                down_weight += conf
                n_down += 1

        total_weight = up_weight + down_weight
        if total_weight < self.min_confidence:
            return None, 0.0, 0

        if up_weight > down_weight:
            aggregate_conf = up_weight / max(1, n_up)
            return "up", min(1.0, aggregate_conf), n_up
        elif down_weight > up_weight:
            aggregate_conf = down_weight / max(1, n_down)
            return "down", min(1.0, aggregate_conf), n_down
        else:
            # Tie — no signal from this pool
            return None, 0.0, max(n_up, n_down)

    # ─── Main Evaluation ──────────────────────────────────────

    def evaluate(
        self,
        signal_predictions: Dict[str, Tuple[Optional[str], float]],
        station: str = "",
        current_utc_hour: int = 0,
        hour_before_settlement: int = 6,
    ) -> FusedResult:
        """
        Fuse multiple signal predictions using LOOP.

        Steps:
          1. Group signals by pool name (signal_predictions keys are pool names
             when called from evaluate_hour_with_pool_names).
          2. For each pool with a signal, compute log-odds contribution.
          3. Combine via LOOP: sum(log_odds_i) / sqrt(n_pools)
          4. Convert to probability: expit(fused_log_odds)
          5. Check agreement gate: enough pools agreeing on direction?

        Args:
            signal_predictions: Dict of pool_name → (direction_or_None, confidence)
                Direction can be "up"/"down" or None (no signal).
                Confidence is [0, 1].
            station: Station code (for logging, not computation)
            current_utc_hour: Current UTC hour (for logging, not computation)
            hour_before_settlement: Hours before settlement (for logging, not computation)

        Returns:
            FusedResult with direction, probability, and gate status.
        """
        # Filter to signals that actually fired (direction not None)
        fired: Dict[str, Tuple[str, float]] = {}
        for name, (direction, conf) in signal_predictions.items():
            if direction is not None and conf >= self.min_confidence:
                fired[name] = (direction, min(1.0, conf))

        n_fired = len(fired)

        if n_fired == 0:
            logger.debug(f"Fusion: no signals fired for {station}")
            return FusedResult(
                direction="UP",
                mean_probability=0.5,
                agreement_gate_passed=False,
                n_pools_agreeing=0,
                n_pools_total=0,
                raw_log_odds=0.0,
                pool_contributions={},
            )

        # Compute log-odds for each pool (each key is a pool name)
        log_odds_list: List[float] = []
        pool_contributions: Dict[str, float] = {}

        for pool_name, (direction, confidence) in fired.items():
            lo = self._signal_to_log_odds(direction, confidence)
            log_odds_list.append(lo)
            pool_contributions[pool_name] = round(lo, 6)

        # LOOP: sum(log_odds_i) / sqrt(n_pools)
        # Using sqrt(n) normalization to keep variance stable as n increases
        n = len(log_odds_list)
        fused_log_odds = sum(log_odds_list) / math.sqrt(n) if n > 0 else 0.0

        # Convert to probability
        mean_probability = float(expit(fused_log_odds))
        mean_probability = np.clip(mean_probability, PROBABILITY_RANGE[0], PROBABILITY_RANGE[1])

        # Determine direction
        if mean_probability > 0.5:
            direction = "UP"
        elif mean_probability < 0.5:
            direction = "DOWN"
        else:
            direction = "UP"  # Default to UP on tie

        # Agreement gate: count pools agreeing on the majority direction
        majority_dir = direction  # "UP" or "DOWN"
        n_agreeing = sum(
            1 for pool_name, (d, _) in fired.items()
            if (d.upper() in ("UP", "HIGH", "YES", "1") and majority_dir == "UP") or
               (d.upper() in ("DOWN", "LOW", "NO", "0") and majority_dir == "DOWN")
        )
        agreement_gate_passed = n_agreeing >= self.agreement_threshold

        logger.debug(
            f"Fusion {station}: {n_fired} pools, "
            f"dir={direction}, prob={mean_probability:.4f}, "
            f"agree={n_agreeing}/{self.agreement_threshold}"
        )

        return FusedResult(
            direction=direction,
            mean_probability=mean_probability,
            agreement_gate_passed=agreement_gate_passed,
            n_pools_agreeing=n_agreeing,
            n_pools_total=n_fired,
            raw_log_odds=fused_log_odds,
            pool_contributions=pool_contributions,
        )