#!/usr/bin/env python3
"""
core/continuous_kelly.py — Continuous Kelly Position Sizing

Replaces discrete 3-tier sizing ($10/$20/$50) with continuous Kelly criterion
for optimal capital allocation in binary weather markets.

Key formulas:
  f* = (p * b - q) / b    (standard Kelly for binary outcomes)
  f* = (p - q / b)         (simplified: b = 1/(1-P) - 1 for direct YES)

With fractional Kelly and fee-aware edge calculation.

B-Mode compliant. No AI/ML.
"""

import math
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ─── Defaults ──────────────────────────────────────────────────

DEFAULT_FRACTIONAL_FACTOR = 0.25   # Fractional Kelly: 25% of full Kelly
DEFAULT_CAPITAL_BASE = 50000.0     # $50,000 total betting bank
DEFAULT_DRAWDOWN_HALVE = 0.10      # Halve Kelly after 10% drawdown
DEFAULT_DRAWDOWN_STOP = 0.20       # Stop trading after 20% drawdown
DEFAULT_MAX_STATION_PCT = 0.25     # Max 25% of capital per station
DEFAULT_MAX_SIGNAL_PCT = 0.15      # Max 15% of capital per signal lane
KALSHI_FEE_RATE = 0.07             # 0.07% of notional = ceil(0.07 * P * (1-P) * 100) / 100


# ─── State ─────────────────────────────────────────────────────

@dataclass
class KellyState:
    """Persistent Kelly state tracking drawdown and capital allocation."""
    initial_capital: float = DEFAULT_CAPITAL_BASE
    current_capital: float = DEFAULT_CAPITAL_BASE
    peak_capital: float = DEFAULT_CAPITAL_BASE
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    kelly_multiplier: float = 1.0       # Reduced during drawdown
    station_allocations: Dict[str, float] = field(default_factory=dict)
    signal_allocations: Dict[str, float] = field(default_factory=dict)
    trade_count: int = 0

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown as fraction of peak capital."""
        if self.peak_capital <= 0:
            return 0.0
        return (self.peak_capital - self.current_capital) / self.peak_capital

    @property
    def is_stopped(self) -> bool:
        """True if drawdown exceeds stop threshold."""
        return self.drawdown_pct >= DEFAULT_DRAWDOWN_STOP

    def record_trade(self, pnl: float, station: str, signal_name: str):
        """Record a completed trade and update state."""
        self.total_pnl += pnl
        self.current_capital += pnl
        self.trade_count += 1

        if pnl > 0:
            self.consecutive_losses = 0
            if self.current_capital > self.peak_capital:
                self.peak_capital = self.current_capital
        else:
            self.consecutive_losses += 1

        # Update drawdown multiplier
        dd = self.drawdown_pct
        if dd >= DEFAULT_DRAWDOWN_STOP:
            self.kelly_multiplier = 0.0
        elif dd >= DEFAULT_DRAWDOWN_HALVE:
            self.kelly_multiplier = 0.5
        else:
            self.kelly_multiplier = 1.0

        # Track station/signal exposure
        self.station_allocations[station] = (
            self.station_allocations.get(station, 0) + pnl
        )
        self.signal_allocations[signal_name] = (
            self.signal_allocations.get(signal_name, 0) + pnl
        )

    def reset(self, capital: float = DEFAULT_CAPITAL_BASE):
        """Reset to initial state."""
        self.initial_capital = capital
        self.current_capital = capital
        self.peak_capital = capital
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.kelly_multiplier = 1.0
        self.station_allocations = {}
        self.signal_allocations = {}
        self.trade_count = 0


# ─── Fee calculation ───────────────────────────────────────────

def kalshi_fee(contracts: int, price: float) -> float:
    """Kalshi exchange fee per trade side.
    Formula: ceil(0.07 * P * (1-P) * 100) / 100 per contract.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(KALSHI_FEE_RATE * contracts * price * (1.0 - price))


# ─── Edge calculation ──────────────────────────────────────────

def kelly_calculate_edge(
    pred_price: float,
    fair_price: float,
    fee_rate: float = KALSHI_FEE_RATE,
    direction: str = "up",
) -> float:
    """
    Calculate edge for a binary weather market.

    For 'up' direction (buy YES):
        edge = (pred_price - fair_price) / fair_price  (fractional advantage)

    For 'down' direction (buy NO):
        edge = (fair_price - pred_price) / (1 - fair_price)  (fractional advantage)

    Subtracts fee rate from edge.

    Args:
        pred_price: Our predicted probability (0-1)
        fair_price: Market price (0-1)
        fee_rate: Kalshi fee rate fraction
        direction: 'up' (buy YES) or 'down' (buy NO)

    Returns:
        Edge as fraction (0 = no edge, negative = no edge)
    """
    if direction == "up":
        # Buy YES: profit = 1 - entry_price if correct, lose entry_price if wrong
        if fair_price >= 1.0 or fair_price <= 0.0:
            return 0.0
        edge = (pred_price - fair_price) / fair_price
    else:
        # Buy NO: profit = entry_price if correct, lose 1 - entry_price if wrong
        if (1.0 - fair_price) <= 0.0:
            return 0.0
        edge = (fair_price - pred_price) / (1.0 - fair_price)

    # Subtract fee
    fee_frac = fee_rate * fair_price * (1.0 - fair_price)
    edge -= fee_frac

    return max(0.0, edge)


# ─── Kelly fraction ────────────────────────────────────────────

def kelly_fraction(
    edge: float,
    confidence: float,
    kelly_frac: float = DEFAULT_FRACTIONAL_FACTOR,
    max_contracts: int = 1000,
    market_price: float = 0.5,
    capital: float = DEFAULT_CAPITAL_BASE,
    station: str = "",
    signal_name: str = "",
    state: Optional[KellyState] = None,
) -> int:
    """
    Compute position size using continuous Kelly criterion.

    For binary markets:
      f* = (p * b - q) / b
      where:
        p = probability of winning (confidence)
        q = 1 - p
        b = odds received on the bet

    For YES (direction='up'):
      b = (1 / market_price) - 1  (if market_price is the cost to enter)

    For NO (direction='down'):
      b = market_price / (1 - market_price)

    Args:
        edge: Edge from kelly_calculate_edge() (0 to 1)
        confidence: Our calibrated confidence (0 to 1)
        kelly_frac: Fractional Kelly factor (default 0.25)
        max_contracts: Maximum position size in contracts
        market_price: Current market price (0-1)
        capital: Current betting capital
        station: Station code for concentration limits
        signal_name: Signal name for concentration limits
        state: Optional persistent KellyState for drawdown tracking

    Returns:
        Number of contracts (integer)
    """
    if edge <= 0.0 or confidence <= 0.5:
        return 0

    # Compute Kelly fraction
    if market_price <= 0.0 or market_price >= 1.0:
        return 0

    # Kelly formula for binary bet
    # f* = (p * b - q) / b
    # where b = (1 / price) - 1 for YES bets
    b = (1.0 / market_price) - 1.0
    q = 1.0 - confidence
    kelly = (confidence * b - q) / b

    # Apply fractional Kelly
    kelly *= kelly_frac

    # Apply drawdown multiplier
    multiplier = 1.0
    if state is not None:
        multiplier = state.kelly_multiplier
        if state.is_stopped:
            return 0
        # Consecutive loss penalty
        if state.consecutive_losses >= 3:
            multiplier *= 0.5
        if state.consecutive_losses >= 5:
            multiplier *= 0.5

    kelly *= multiplier

    # Convert to contracts
    dollar_amount = kelly * capital
    n_contracts = max(1, int(dollar_amount / market_price))

    # Apply station concentration limit
    if station and state is not None:
        station_exposure = abs(state.station_allocations.get(station, 0.0))
        max_station = capital * DEFAULT_MAX_STATION_PCT
        if station_exposure + dollar_amount > max_station:
            available = max_station - station_exposure
            n_contracts = min(n_contracts, max(1, int(available / market_price)))

    # Apply signal concentration limit
    if signal_name and state is not None:
        signal_exposure = abs(state.signal_allocations.get(signal_name, 0.0))
        max_signal = capital * DEFAULT_MAX_SIGNAL_PCT
        if signal_exposure + dollar_amount > max_signal:
            available = max_signal - signal_exposure
            n_contracts = min(n_contracts, max(1, int(available / market_price)))

    # Cap at max contracts
    n_contracts = min(n_contracts, max_contracts)

    return n_contracts


# ─── Fee-aware Kelly (wrapper) ─────────────────────────────────

def fee_aware_kelly(
    pred_price: float,
    market_price: float,
    confidence: float,
    direction: str,
    kelly_frac: float = DEFAULT_FRACTIONAL_FACTOR,
    max_contracts: int = 1000,
    capital: float = DEFAULT_CAPITAL_BASE,
    station: str = "",
    signal_name: str = "",
    state: Optional[KellyState] = None,
) -> int:
    """
    Full pipeline: calculate edge (with fee) → Kelly fraction → contracts.

    Args:
        pred_price: Our predicted probability
        market_price: Current market price
        confidence: Calibrated confidence
        direction: 'up' or 'down'
        kelly_frac: Fractional Kelly factor
        max_contracts: Max position size
        capital: Current capital
        station: Station code
        signal_name: Signal name
        state: KellyState for drawdown/concentration tracking

    Returns:
        Number of contracts
    """
    edge = kelly_calculate_edge(pred_price, market_price, direction=direction)
    return kelly_fraction(
        edge, confidence, kelly_frac, max_contracts,
        market_price, capital, station, signal_name, state
    )


# ─── Testing ───────────────────────────────────────────────────

def test():
    """Quick self-test."""
    state = KellyState()

    # Test 1: Strong edge, high confidence
    n = fee_aware_kelly(
        pred_price=0.85, market_price=0.65, confidence=0.82,
        direction="up", capital=50000.0, state=state
    )
    assert n > 0, f"Should have positive contracts: {n}"
    print(f"Test 1 (strong edge): {n} contracts")

    # Test 2: No edge → 0 contracts
    n = fee_aware_kelly(
        pred_price=0.50, market_price=0.50, confidence=0.55,
        direction="up", capital=50000.0, state=state
    )
    assert n == 0, f"Should be 0 contracts: {n}"
    print(f"Test 2 (no edge): {n} contracts")

    # Test 3: Drawdown stop
    state.peak_capital = 100000.0
    state.current_capital = 75000.0  # 25% drawdown — exceeds stop
    n = fee_aware_kelly(
        pred_price=0.85, market_price=0.65, confidence=0.82,
        direction="up", capital=state.current_capital, state=state
    )
    assert n == 0, f"Should be stopped: {n}"
    print(f"Test 3 (drawdown stop): {n} contracts")

    print("All tests passed")


if __name__ == "__main__":
    test()