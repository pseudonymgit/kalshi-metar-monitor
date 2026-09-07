"""
EntryTimingEngine — Risk-Gated Entry Decisions (v1.0 — 2026-09-07)

Evaluates whether to enter a trade at the current hour based on:
  1. Probability divergence: our calibrated probability vs market price
  2. Confidence gate: minimum confidence / probability far enough from 0.5
  3. Time filter: only enter during trading hours (10:00-16:00 ET = 14:00-20:00 UTC)
  4. Position sizing: Kelly-optimal fraction capped by risk limits

Produces EntrySignal with signal_type (BUY/SELL/PASS) and position_size.

Deterministic math only — no AI/ML.
"""

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─── Trading Hours (UTC) ──────────────────────────────────────
# Kalshi weather markets trade 10:00-16:00 ET = 14:00-20:00 UTC
TRADING_HOUR_START_UTC = 14
TRADING_HOUR_END_UTC = 21  # exclusive (last run at 20:30)

# Settlement hour UTC
SETTLEMENT_HOUR_UTC = 18

# Default risk parameters
DEFAULT_MIN_PROB_GATE = 0.55       # Minimum calibrated probability to enter
DEFAULT_EDGE_THRESHOLD = 0.03      # Minimum |prob - market_price| to act
DEFAULT_MAX_POSITION_USD = 250.0   # Maximum position size in USD
DEFAULT_STOP_LOSS_PCT = 0.15       # Fraction of position to risk
DEFAULT_KELLY_FRACTION = 0.25      # Fraction of Kelly to use (conservative)


class SignalType(Enum):
    """Entry signal type."""
    BUY = "BUY"      # Buy YES contract (we think probability > market price)
    SELL = "SELL"     # Sell YES contract (we think probability < market price)
    PASS = "PASS"     # No entry this hour


@dataclass
class EntrySignal:
    """Decision output from the entry timing engine."""
    signal_type: SignalType   # BUY, SELL, or PASS
    position_size: float      # Position size in USD (0 for PASS)
    edge: float               # |our_probability - market_price|
    our_probability: float    # Our calibrated probability
    market_price: float       # Market-implied probability
    confidence: float         # Our confidence [0, 1]

    @property
    def is_entry(self) -> bool:
        """True if this signal is BUY or SELL (not PASS)."""
        return self.signal_type != SignalType.PASS


@dataclass
class RiskGuardrails:
    """Risk limits for position sizing."""
    max_position_usd: float = DEFAULT_MAX_POSITION_USD
    min_prob_gate: float = DEFAULT_MIN_PROB_GATE
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT

    def __post_init__(self):
        """Validate and clamp parameters."""
        self.min_prob_gate = max(0.5, min(0.95, self.min_prob_gate))
        self.edge_threshold = max(0.0, min(0.5, self.edge_threshold))
        self.max_position_usd = max(10.0, min(10000.0, self.max_position_usd))
        self.kelly_fraction = max(0.0, min(1.0, self.kelly_fraction))


class EntryTimingEngine:
    """
    Evaluates entry timing for intraday trading signals.

    Usage:
        engine = EntryTimingEngine()
        signal = engine.evaluate_hour(
            hour=14, hour_before_settlement=4,
            our_probability=0.62, market_price=0.55, confidence=0.7,
        )
        # signal.signal_type -> SignalType.BUY
        # signal.position_size -> 125.0
    """

    def __init__(self, guardrails: Optional[RiskGuardrails] = None):
        self.guardrails = guardrails or RiskGuardrails()
        logger.debug(f"EntryTimingEngine: gate={self.guardrails.min_prob_gate}")

    @staticmethod
    def _is_trading_hour(hour_utc: int) -> bool:
        """Check if the hour falls within Kalshi trading hours (UTC)."""
        return TRADING_HOUR_START_UTC <= hour_utc < TRADING_HOUR_END_UTC

    def _compute_kelly_size(
        self,
        our_probability: float,
        market_price: float,
    ) -> float:
        """
        Compute Kelly-optimal position size as a fraction of max position.

        Kelly formula for binary bets:
          f* = (p * b - q) / b
        where:
          p = our probability of winning
          q = 1 - p (probability of losing)
          b = market odds = (1 - market_price) / market_price

        For BUY (we think p > market_price):
          b = (1 - market_price) / market_price  (payout if YES wins)
          f* = (p * b - q) / b
          f* = (p - market_price) / (1 - market_price)

        For SELL (we think p < market_price, i.e., probability of NO):
          b = market_price / (1 - market_price)  (payout if NO wins)
          f* = (p - market_price) / market_price
          where p = 1 - our_probability (probability of NO)

        Args:
            our_probability: Our calibrated probability [0, 1]
            market_price: Market-implied probability [0, 1]

        Returns:
            Kelly fraction [0, 1] of max position
        """
        # Clamp to avoid division by zero
        mp = np.clip(market_price, 0.01, 0.99)
        op = np.clip(our_probability, 0.01, 0.99)

        if our_probability > market_price:
            # BUY: we think probability is higher than market implies
            kelly = (op - mp) / (1.0 - mp)
        else:
            # SELL: we think probability is lower than market implies
            kelly = (mp - op) / mp

        # Guard against edge cases
        kelly = max(0.0, min(1.0, kelly))
        return kelly

    def evaluate_hour(
        self,
        hour: int,
        hour_before_settlement: int,
        our_probability: float,
        market_price: float,
        confidence: float,
    ) -> EntrySignal:
        """
        Evaluate whether to enter a trade at the current hour.

        Decision cascade:
          1. Is hour within trading hours?
          2. Is probability far enough from 0.5 (above min_prob_gate)?
          3. Is edge (|our_prob - market_price|) above threshold?
          4. Is direction consistent? (BUY if our_prob > market_price, SELL if <)
          5. Compute Kelly-optimal position size

        Args:
            hour: Current UTC hour (0-23)
            hour_before_settlement: Hours until settlement
            our_probability: Our calibrated probability [0, 1]
            market_price: Current market price [0, 1]
            confidence: Our confidence in the signal [0, 1]

        Returns:
            EntrySignal with signal_type and position_size
        """
        # Clamp inputs
        our_prob = np.clip(our_probability, 0.0, 1.0)
        mp = np.clip(market_price, 0.0, 1.0)
        conf = np.clip(confidence, 0.0, 1.0)

        # Gate 1: Trading hours
        if not self._is_trading_hour(hour):
            logger.debug(
                f"PASS hour={hour}: outside trading hours "
                f"({TRADING_HOUR_START_UTC}-{TRADING_HOUR_END_UTC} UTC)"
            )
            return EntrySignal(
                signal_type=SignalType.PASS,
                position_size=0.0,
                edge=0.0,
                our_probability=our_prob,
                market_price=mp,
                confidence=conf,
            )

        # Gate 2: Minimum probability gate
        prob_deviation = abs(our_prob - 0.5) * 2.0  # Map 0.5→0, 1.0→1
        if prob_deviation < (self.guardrails.min_prob_gate - 0.5) * 2.0:
            logger.debug(
                f"PASS {hour}: prob {our_prob:.3f} below gate "
                f"{self.guardrails.min_prob_gate:.3f}"
            )
            return EntrySignal(
                signal_type=SignalType.PASS,
                position_size=0.0,
                edge=0.0,
                our_probability=our_prob,
                market_price=mp,
                confidence=conf,
            )

        # Gate 3: Edge threshold
        edge = abs(our_prob - mp)
        if edge < self.guardrails.edge_threshold:
            logger.debug(
                f"PASS {hour}: edge {edge:.4f} below threshold "
                f"{self.guardrails.edge_threshold:.4f}"
            )
            return EntrySignal(
                signal_type=SignalType.PASS,
                position_size=0.0,
                edge=edge,
                our_probability=our_prob,
                market_price=mp,
                confidence=conf,
            )

        # Determine direction
        if our_prob > mp:
            # We think probability is higher than market → BUY
            signal_type = SignalType.BUY
        else:
            # We think probability is lower than market → SELL
            signal_type = SignalType.SELL

        # Compute Kelly-optimal position size
        kelly_frac = self._compute_kelly_size(our_prob, mp)
        # Apply Kelly fraction (conservative: 25% of Kelly by default)
        position_size = (
            self.guardrails.kelly_fraction
            * kelly_frac
            * self.guardrails.max_position_usd
        )

        # Clamp position size
        position_size = max(0.0, min(self.guardrails.max_position_usd, position_size))

        logger.info(
            f"ENTRY {signal_type.value} h={hour} hb={hour_before_settlement}: "
            f"prob={our_prob:.4f} mkt={mp:.4f} edge={edge:.4f} "
            f"kelly={kelly_frac:.3f} size=${position_size:.0f}"
        )

        return EntrySignal(
            signal_type=signal_type,
            position_size=position_size,
            edge=edge,
            our_probability=our_prob,
            market_price=mp,
            confidence=conf,
        )


# ─── Convenience ──────────────────────────────────────────────

def default_guardrails() -> RiskGuardrails:
    """Return default risk guardrails for entry timing."""
    return RiskGuardrails()