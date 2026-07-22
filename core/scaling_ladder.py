#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Scaling Ladder — Edge 17, Phase 3 Risk Management

Tiered position-size scaling based on performance metrics and market conditions.

Tiers:
  TIER1  — $10/trade  (starting)
  TIER2  — $25/trade
  TIER3  — $50/trade
  TIER4  — $100/trade
  FROZEN — $0/trade   (halted)

Upward scaling criteria:
  TIER1 → TIER2:  3 consecutive wins + Sharpe ≥ 1.2  (≥ 50 trades)
  TIER2 → TIER3:  5 consecutive wins + Sortino ≥ 1.5 (≥ 150 trades)
  TIER3 → TIER4:  7 consecutive wins + Calmar ≥ 2.0   (≥ 400 trades)

Reverse scaling:
  2 consecutive losses → down 1 tier
  Drawdown > 10% → down 1 tier
  VIX > 25 → −1 tier, VIX > 35 → freeze

Deterministic math only — no AI/ML.

Version: Edge 17 — 2026-07-18
"""

import logging
from typing import Tuple, List
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ─── Tier definitions ──────────────────────────────────────────────────────

TIERS = [
    "FROZEN",
    "TIER1",
    "TIER2",
    "TIER3",
    "TIER4",
]

TIER_POSITION_SIZES = {
    "FROZEN": 0.0,
    "TIER1": 10.0,
    "TIER2": 25.0,
    "TIER3": 50.0,
    "TIER4": 100.0,
}

# Index mapping for easy navigation
TIER_INDEX = {name: idx for idx, name in enumerate(TIERS)}
STARTING_TIER = "TIER1"

# ─── Promotion thresholds ──────────────────────────────────────────────────

PROMOTION_CRITERIA = {
    # from_tier: (wins_streak, metric_name, metric_threshold, min_trades)
    "TIER1": ("win_streak", 3, "sharpe", 1.2, 50),
    "TIER2": ("win_streak", 5, "sortino", 1.5, 150),
    "TIER3": ("win_streak", 7, "calmar", 2.0, 400),
}

# ─── Reverse scaling thresholds ────────────────────────────────────────────

LOSS_STREAK_DEMOTE = 2
DRAWDOWN_DEMOTE_PCT = 10.0      # percent
VIX_DEMOTE_THRESHOLD = 25.0
VIX_FREEZE_THRESHOLD = 35.0
DRAWDOWN_FREEZE_PCT = 25.0      # extreme drawdown → freeze


@dataclass
class PerformanceMetrics:
    """Rolling performance metrics used by the scaling ladder."""
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    drawdown_pct: float = 0.0


class ScalingLadder:
    """
    Manages tier-based position scaling with forward and reverse criteria.
    """

    def __init__(self):
        self._tier: str = STARTING_TIER
        self._win_streak: int = 0
        self._loss_streak: int = 0
        self._total_trades: int = 0
        self._vix: float = 0.0
        self._metrics = PerformanceMetrics()
        self._frozen: bool = False
        _LOGGER.info("ScalingLadder initialised at %s ($%.0f)", self._tier, TIER_POSITION_SIZES[self._tier])

    # ─── Public API ────────────────────────────────────────────────────────

    def get_current_tier(self) -> str:
        """Return current tier name."""
        return self._tier

    def get_position_limit(self) -> float:
        """Return position size for the current tier."""
        return TIER_POSITION_SIZES[self._tier]

    def record_trade_result(self, pnl: float, is_profitable: bool) -> None:
        """
        Update internal streak counters based on the latest trade result.
        """
        self._total_trades += 1
        if is_profitable:
            self._win_streak += 1
            self._loss_streak = 0
        else:
            self._loss_streak += 1
            self._win_streak = 0
        _LOGGER.debug(
            "Trade recorded — pnl=%.2f profitable=%s win_streak=%d loss_streak=%d total=%d",
            pnl, is_profitable, self._win_streak, self._loss_streak, self._total_trades,
        )

    def update_metrics(self, sharpe: float, sortino: float, calmar: float, drawdown_pct: float) -> None:
        """Update rolling performance metrics."""
        self._metrics = PerformanceMetrics(
            sharpe=sharpe,
            sortino=sortino,
            calmar=calmar,
            drawdown_pct=drawdown_pct,
        )
        _LOGGER.debug(
            "Metrics updated — sharpe=%.2f sortino=%.2f calmar=%.2f drawdown=%.1f%%",
            sharpe, sortino, calmar, drawdown_pct,
        )

    def set_vix_level(self, vix_value: float) -> None:
        """Set the current VIX level for volatility-based adjustments."""
        self._vix = vix_value
        _LOGGER.debug("VIX set to %.2f", vix_value)

    def evaluate(self) -> Tuple[str, float, str]:
        """
        Evaluate all promotion / demotion / freeze conditions and return
        the resulting tier, position limit, and a human-readable reason.
        """
        reason_parts: List[str] = []

        # ── Freeze checks (highest priority) ──
        if self._vix > VIX_FREEZE_THRESHOLD:
            self._tier = "FROZEN"
            reason = f"VIX {self._vix:.1f} > {VIX_FREEZE_THRESHOLD} → freeze"
            _LOGGER.warning("Scaling freeze: %s", reason)
            return self._tier, self.get_position_limit(), reason

        if self._metrics.drawdown_pct >= DRAWDOWN_FREEZE_PCT:
            self._tier = "FROZEN"
            reason = f"Drawdown {self._metrics.drawdown_pct:.1f}% ≥ {DRAWDOWN_FREEZE_PCT}% → freeze"
            _LOGGER.warning("Scaling freeze: %s", reason)
            return self._tier, self.get_position_limit(), reason

        # ── Demotion checks ──
        if self._tier != "FROZEN" and self._tier != "TIER1":
            demoted = False
            if self._loss_streak >= LOSS_STREAK_DEMOTE:
                self._demote()
                reason_parts.append(f"{LOSS_STREAK_DEMOTE} consecutive losses → demote")
                demoted = True

            if self._metrics.drawdown_pct > DRAWDOWN_DEMOTE_PCT:
                self._demote()
                reason_parts.append(f"Drawdown {self._metrics.drawdown_pct:.1f}% > {DRAWDOWN_DEMOTE_PCT}% → demote")
                demoted = True

            if self._vix > VIX_DEMOTE_THRESHOLD:
                self._demote()
                reason_parts.append(f"VIX {self._vix:.1f} > {VIX_DEMOTE_THRESHOLD} → demote")
                demoted = True

            if demoted:
                reason = "; ".join(reason_parts)
                _LOGGER.info("Demoted: %s → %s", reason, self._tier)
                return self._tier, self.get_position_limit(), reason

        # ── Promotion checks ──
        if self._tier in PROMOTION_CRITERIA:
            criteria = PROMOTION_CRITERIA[self._tier]
            _, required_streak, metric_name, metric_threshold, min_trades = criteria

            metric_value = getattr(self._metrics, metric_name, 0.0)

            if (
                self._win_streak >= required_streak
                and metric_value >= metric_threshold
                and self._total_trades >= min_trades
            ):
                old_tier = self._tier
                self._promote()
                reason = (
                    f"Promoted {old_tier} → {self._tier} "
                    f"(wins={self._win_streak}, {metric_name}={metric_value:.2f} "
                    f">={metric_threshold}, trades={self._total_trades} >= {min_trades})"
                )
                _LOGGER.info("Promoted: %s", reason)
                return self._tier, self.get_position_limit(), reason

        # ── No change ──
        return self._tier, self.get_position_limit(), "No tier change"

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _promote(self) -> None:
        """Move up one tier."""
        idx = TIER_INDEX[self._tier]
        if idx < len(TIERS) - 1:
            self._tier = TIERS[idx + 1]

    def _demote(self) -> None:
        """Move down one tier (floor at TIER1, FROZEN is handled separately)."""
        idx = TIER_INDEX[self._tier]
        if idx > TIER_INDEX["TIER1"]:
            self._tier = TIERS[idx - 1]
        elif self._tier != "FROZEN":
            self._tier = "TIER1"


# ─── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ladder = ScalingLadder()

    print("\n=== Scaling Ladder — Self-Test ===\n")

    # 1. Initial state
    assert ladder.get_current_tier() == "TIER1"
    assert ladder.get_position_limit() == 10.0
    print(f"[1] Initial tier: {ladder.get_current_tier()} @ ${ladder.get_position_limit()}")

    # 2. Promotion TIER1 → TIER2
    for _ in range(3):
        ladder.record_trade_result(1.0, True)
    ladder.update_metrics(sharpe=1.3, sortino=0.0, calmar=0.0, drawdown_pct=0.0)
    # Need ≥50 trades
    for _ in range(50):
        ladder.record_trade_result(0.5, True)
    # Streak reset by 50 wins, need 3 consecutive again
    ladder._win_streak = 3  # restore streak after filling trade count
    tier, limit, reason = ladder.evaluate()
    print(f"[2] After 3-win + sharpe≥1.2 + 50 trades: {tier} @ ${limit} — {reason}")
    assert tier == "TIER2", f"Expected TIER2, got {tier}"

    # 3. Promotion TIER2 → TIER3
    ladder._win_streak = 5
    ladder._total_trades = 150
    ladder.update_metrics(sharpe=1.0, sortino=1.6, calmar=0.0, drawdown_pct=0.0)
    tier, limit, reason = ladder.evaluate()
    print(f"[3] After 5-win + sortino≥1.5 + 150 trades: {tier} @ ${limit} — {reason}")
    assert tier == "TIER3", f"Expected TIER3, got {tier}"

    # 4. Promotion TIER3 → TIER4
    ladder._win_streak = 7
    ladder._total_trades = 400
    ladder.update_metrics(sharpe=1.0, sortino=1.0, calmar=2.1, drawdown_pct=0.0)
    tier, limit, reason = ladder.evaluate()
    print(f"[4] After 7-win + calmar≥2.0 + 400 trades: {tier} @ ${limit} — {reason}")
    assert tier == "TIER4", f"Expected TIER4, got {tier}"

    # 5. Demotion: 2 consecutive losses
    ladder._loss_streak = 2
    ladder._win_streak = 0
    tier, limit, reason = ladder.evaluate()
    print(f"[5] After 2 consecutive losses: {tier} @ ${limit} — {reason}")
    assert tier == "TIER3", f"Expected TIER3, got {tier}"

    # 6. VIX freeze
    ladder.set_vix_level(40.0)
    tier, limit, reason = ladder.evaluate()
    print(f"[6] VIX=40: {tier} @ ${limit} — {reason}")
    assert tier == "FROZEN", f"Expected FROZEN, got {tier}"
    assert limit == 0.0

    # 7. VIX demote (not freeze)
    ladder2 = ScalingLadder()
    ladder2._tier = "TIER3"
    ladder2.set_vix_level(28.0)
    ladder2._loss_streak = 0
    ladder2._win_streak = 0
    ladder2._metrics = PerformanceMetrics(sharpe=1, sortino=1, calmar=1, drawdown_pct=0)
    tier, limit, reason = ladder2.evaluate()
    print(f"[7] VIX=28 from TIER3: {tier} @ ${limit} — {reason}")
    assert tier == "TIER2", f"Expected TIER2 (VIX demote), got {tier}"

    # 8. Drawdown demote
    ladder3 = ScalingLadder()
    ladder3._tier = "TIER3"
    ladder3._loss_streak = 0
    ladder3._win_streak = 0
    ladder3._vix = 0
    ladder3._metrics = PerformanceMetrics(sharpe=1, sortino=1, calmar=1, drawdown_pct=12.0)
    tier, limit, reason = ladder3.evaluate()
    print(f"[8] Drawdown=12% from TIER3: {tier} @ ${limit} — {reason}")
    assert tier == "TIER2", f"Expected TIER2 (drawdown demote), got {tier}"

    print("\n✅ All Scaling Ladder tests passed.\n")
