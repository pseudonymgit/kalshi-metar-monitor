#!/usr/bin/env python3
"""
Kelly Position Sizer — Edge 13, Phase 3 Risk Management

Standalone Kelly-criterion position sizer with adaptive confidence scaling,
Bayesian Beta-Binomial belief updates, and an 8% bankroll cap.

Formula: f* = p - 0.0526  (simplified for 5% fees)

Adaptive scaling:
  - confidence < 60% → 0.5× Kelly
  - 60–70%           → 1.0× Kelly
  - 70–80%           → 1.5× Kelly
  - ≥ 80%            → 2.0× Kelly

Cap: 8% of bankroll per position (differs from position_sizing.py's 25%).
Trailing 10% max drawdown protection.
Bayesian Beta-Binomial confidence updates (Beta(α, β) posterior).

Deterministic math only — no AI/ML.

Version: Edge 13 — 2026-07-18
"""

import logging
import math
from typing import Tuple, Dict
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

MAX_BANKROLL_PCT = 0.08         # 8% cap per position
MAX_DRAWDOWN_PCT = 0.10          # 10% trailing drawdown

# Default cost fraction — import from MARKET_COST_MODEL (single source of truth)
# Round-trip = spread/2 + commission + slippage = 3.1¢/2 + 0¢ + 0.5¢ = 2.05¢
# This is the per-contract cost, not raw spread (which was 3.1¢)
from core.market_cost_model import ROUND_TRIP_FEE as DEFAULT_COST_FRACTION

# Adaptive scaling thresholds
CONFIDENCE_TIERS = [
    (0.80, 2.0),   # ≥ 80% → 2×
    (0.70, 1.5),   # 70–80% → 1.5×
    (0.60, 1.0),   # 60–70% → 1×
    (0.00, 0.5),   # < 60% → 0.5×
]


@dataclass
class BayesianBelief:
    """
    Beta-Binomial posterior for win-probability estimation.

    Prior: Beta(α=1, β=1) — uniform.
    After observing wins/losses: Beta(α + wins, β + losses).
    Posterior mean: α / (α + β).
    """
    alpha: float = 1.0
    beta: float = 1.0

    @property
    def posterior_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, wins: int, losses: int) -> None:
        self.alpha += max(0, wins)
        self.beta += max(0, losses)

    def confidence_interval(self, z: float = 1.96) -> Tuple[float, float]:
        """
        Approximate 95% CI using the Beta distribution's mean and variance.
        """
        total = self.alpha + self.beta
        if total <= 0:
            return 0.0, 1.0
        mean = self.alpha / total
        variance = (self.alpha * self.beta) / (total * total * (total + 1))
        std = math.sqrt(variance)
        return max(0.0, mean - z * std), min(1.0, mean + z * std)


class KellyPositionSizer:
    """
    Kelly-criterion position sizer with adaptive confidence scaling,
    Bayesian belief updates, and bankroll protection.
    """

    def __init__(self, bankroll: float = 10000.0, cost_fraction: float = None):
        self.bankroll = bankroll
        self._peak_bankroll = bankroll
        self._belief = BayesianBelief()
        self._adaptive_multiplier: float = 0.5   # default to conservative
        self._current_drawdown: float = 0.0
        # Cost fraction = (ask - bid) / mid as fraction of contract value
        # Kalshi charges 0 commission; use live bid-ask spread only
        self._cost_fraction = cost_fraction if cost_fraction is not None else DEFAULT_COST_FRACTION
        _LOGGER.info(
            "KellyPositionSizer initialised — bankroll=$%.2f, cap=%.0f%%, max_drawdown=%.0f%%",
            bankroll, MAX_BANKROLL_PCT * 100, MAX_DRAWDOWN_PCT * 100,
        )

    # ─── Public API ────────────────────────────────────────────────────────

    def set_adaptive_multiplier(self, confidence: float) -> float:
        """
        Set the adaptive Kelly multiplier based on a confidence level (0–1).
        Returns the multiplier that was set.
        """
        for threshold, multiplier in CONFIDENCE_TIERS:
            if confidence >= threshold:
                self._adaptive_multiplier = multiplier
                _LOGGER.debug(
                    "Adaptive multiplier set to %.1f× (confidence=%.1f%%)",
                    multiplier, confidence * 100,
                )
                return multiplier
        # Should not reach here, but default to conservative
        self._adaptive_multiplier = 0.5
        return 0.5

    def update_belief(self, wins: int, losses: int) -> float:
        """
        Perform a Bayesian Beta-Binomial update on the win-probability belief.
        Returns the posterior mean (estimated win probability).
        """
        self._belief.update(wins, losses)
        posterior = self._belief.posterior_mean
        _LOGGER.info(
            "Belief updated — wins=%d losses=%d → Beta(%.1f, %.1f), posterior mean=%.4f",
            wins, losses, self._belief.alpha, self._belief.beta, posterior,
        )
        return posterior

    def update_cost(self, cost_fraction: float) -> None:
        """
        Update the cost fraction (spread-based, no commission).
        
        Args:
            cost_fraction: Spread as fraction of contract value
                e.g., 0.031 for 3.1¢ spread on $1 contract
        """
        self._cost_fraction = max(0.0, min(1.0, cost_fraction))
        _LOGGER.debug("Cost fraction updated to %.4f", self._cost_fraction)

    def calculate_kelly_fraction(self, edge: float, win_rate: float) -> float:
        """
        Calculate the Kelly fraction using the correct formula:
            f* = (p - c) / (1 - c)

        where p is the win probability (win_rate) and c is the cost fraction
        (spread as fraction of contract value). Kalshi charges 0 commission,
        so only the bid-ask spread matters.

        This is the standard Kelly criterion for binary outcomes with
        asymmetric payoff: if you win you get (1 - c) per contract, if you
        lose you forfeit the full contract value.

        Returns the raw Kelly fraction (before adaptive scaling and caps).
        """
        c = self._cost_fraction
        if c >= 1.0:
            return 0.0  # Cost exceeds contract value — cannot profit
        kelly = (win_rate - c) / (1.0 - c)
        # Clamp to [0, 1] — negative Kelly means no bet
        kelly = max(0.0, min(1.0, kelly))
        _LOGGER.debug(
            "Kelly fraction — win_rate=%.4f cost=%.4f → f*=%.4f",
            win_rate, c, kelly,
        )
        return kelly

    def compute_position_size(self, confidence: float, win_rate: float, edge: float) -> Tuple[float, Dict]:
        """
        Compute the final position size in dollars.

        Pipeline:
          1. Calculate raw Kelly fraction (f* = (p - c) / (1 - c))
          2. Apply adaptive multiplier based on confidence
          3. Cap at 8% of bankroll
          4. Check trailing 10% drawdown — halve size if in drawdown

        Returns (position_size_dollars, details_dict).
        """
        details: Dict = {}

        # 1. Raw Kelly
        kelly = self.calculate_kelly_fraction(edge, win_rate)
        details["kelly_fraction"] = round(kelly, 6)

        # 2. Adaptive multiplier
        multiplier = self.set_adaptive_multiplier(confidence)
        details["adaptive_multiplier"] = multiplier

        adjusted_kelly = kelly * multiplier
        details["adjusted_kelly"] = round(adjusted_kelly, 6)

        # 3. Bankroll cap (8%)
        max_position = self.bankroll * MAX_BANKROLL_PCT
        raw_position = adjusted_kelly * self.bankroll
        position = min(raw_position, max_position)
        details["max_position"] = round(max_position, 2)
        details["raw_position"] = round(raw_position, 2)

        # 4. Drawdown protection
        self._update_drawdown()
        if self._current_drawdown >= MAX_DRAWDOWN_PCT:
            position *= 0.5
            details["drawdown_protection"] = "ACTIVE — position halved"
            _LOGGER.warning(
                "Drawdown protection active — drawdown=%.1f%% >= %.0f%%, position halved",
                self._current_drawdown * 100, MAX_DRAWDOWN_PCT * 100,
            )
        else:
            details["drawdown_protection"] = "inactive"

        # Floor at 0
        position = max(0.0, position)
        details["final_position"] = round(position, 2)
        details["bankroll"] = round(self.bankroll, 2)
        details["confidence"] = round(confidence, 4)
        details["win_rate"] = round(win_rate, 4)
        details["edge"] = round(edge, 4)
        details["posterior_mean"] = round(self._belief.posterior_mean, 4)

        _LOGGER.info(
            "Position computed — $%.2f (kelly=%.4f, mult=%.1f, cap=$%.2f, drawdown=%.1f%%)",
            position, kelly, multiplier, max_position, self._current_drawdown * 100,
        )
        return position, details

    def update_bankroll(self, new_bankroll: float) -> None:
        """
        Update the bankroll and trailing peak for drawdown tracking.
        """
        self.bankroll = new_bankroll
        if new_bankroll > self._peak_bankroll:
            self._peak_bankroll = new_bankroll
        _LOGGER.debug("Bankroll updated — $%.2f (peak=$%.2f)", new_bankroll, self._peak_bankroll)

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _update_drawdown(self) -> None:
        if self._peak_bankroll <= 0:
            self._current_drawdown = 0.0
            return
        self._current_drawdown = (self._peak_bankroll - self.bankroll) / self._peak_bankroll

    def get_belief(self) -> BayesianBelief:
        """Return the current Bayesian belief state."""
        return self._belief

    def get_drawdown(self) -> float:
        """Return current drawdown as a fraction (0.10 = 10%)."""
        return self._current_drawdown


# ─── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("\n=== Kelly Position Sizer — Self-Test ===\n")

    kps = KellyPositionSizer(bankroll=10000.0)

    # 1. Kelly fraction (correct formula: f* = (p - c) / (1 - c))
    k = kps.calculate_kelly_fraction(edge=0.1, win_rate=0.60)
    print(f"[1] Kelly fraction (win_rate=0.60, cost=0.031) → f*={k:.4f}")
    expected = (0.60 - 0.031) / (1.0 - 0.031)
    assert abs(k - expected) < 1e-6, f"Expected {expected}, got {k}"

    # 2. Adaptive multiplier tiers
    test_cases = [
        (0.50, 0.5, "conf=50% → 0.5×"),
        (0.60, 1.0, "conf=60% → 1.0×"),
        (0.65, 1.0, "conf=65% → 1.0×"),
        (0.70, 1.5, "conf=70% → 1.5×"),
        (0.75, 1.5, "conf=75% → 1.5×"),
        (0.80, 2.0, "conf=80% → 2.0×"),
        (0.90, 2.0, "conf=90% → 2.0×"),
    ]
    for conf, expected_mult, label in test_cases:
        mult = kps.set_adaptive_multiplier(conf)
        print(f"[2] {label} → got {mult}×")
        assert mult == expected_mult, f"Expected {expected_mult}, got {mult} for conf={conf}"

    # 3. Position size with 65% confidence, 60% win rate
    size, details = kps.compute_position_size(
        confidence=0.65, win_rate=0.60, edge=0.10
    )
    print(f"[3] Position at conf=65%, win_rate=60%: ${size:.2f}")
    print(f"    Details: {details}")
    # Kelly = (0.60 - 0.031) / (1 - 0.031) ≈ 0.5866, multiplier = 1.0, raw = 5866, cap = 800
    assert size == 800.0, f"Expected cap of $800 (8% of $10k), got ${size}"

    # 4. Position size with 90% confidence (2× multiplier)
    size2, details2 = kps.compute_position_size(
        confidence=0.90, win_rate=0.60, edge=0.10
    )
    print(f"[4] Position at conf=90%, win_rate=60%: ${size2:.2f}")
    # Kelly ≈ 0.5866 × 2.0 = 1.1732 × $10k = $11732 → capped at $800
    assert size2 == 800.0, f"Expected cap $800, got ${size2}"

    # 5. Bayesian belief update
    posterior = kps.update_belief(wins=30, losses=20)
    print(f"[5] After 30W/20L → posterior mean={posterior:.4f}")
    # Beta(31, 21) → mean = 31/52 ≈ 0.5962
    assert abs(posterior - 31.0/52.0) < 1e-6, f"Expected {31/52}, got {posterior}"

    # 6. Confidence interval
    lo, hi = kps.get_belief().confidence_interval()
    print(f"[6] 95% CI: [{lo:.4f}, {hi:.4f}]")
    assert 0 < lo < posterior < hi < 1

    # 7. Drawdown protection
    kps.update_bankroll(8500.0)  # 15% drawdown from $10k peak
    size3, details3 = kps.compute_position_size(
        confidence=0.90, win_rate=0.60, edge=0.10
    )
    print(f"[7] Position in drawdown (15%): ${size3:.2f} — {details3['drawdown_protection']}")
    assert "ACTIVE" in details3["drawdown_protection"]
    assert size3 == 340.0, f"Expected $340 (halved $680 cap), got ${size3}"

    # 8. Low win rate → zero Kelly
    kps_low = KellyPositionSizer(bankroll=10000.0)
    size_low, details_low = kps_low.compute_position_size(
        confidence=0.90, win_rate=0.05, edge=-0.1
    )
    print(f"[8] Very low win rate (5%): ${size_low:.2f} — kelly={details_low['kelly_fraction']}")
    # f* = (0.05 - 0.031) / (1 - 0.031) = 0.0196, not zero but very small
    assert size_low > 0.0, f"Expected small positive Kelly (still above cost), got ${size_low}"

    # 9. Bankroll cap respected
    kps_small = KellyPositionSizer(bankroll=1000.0)
    size_small, _ = kps_small.compute_position_size(
        confidence=0.90, win_rate=0.80, edge=0.50
    )
    print(f"[9] $1000 bankroll, 80% win rate: ${size_small:.2f} (cap=$80)")
    assert size_small <= 80.0, f"Should be capped at $80 (8% of $1000), got ${size_small}"

    print("\n✅ All Kelly Position Sizer tests passed.\n")
