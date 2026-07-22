#!/usr/bin/env python3
"""
Market Cost Model — Single source of truth for Kalshi market cost assumptions.

Provides a centralized cost model for spread, slippage, and commission
used across the paper trading engine, fee-aware filter, and Kelly sizer.

Per Kalshi fee structure:
- Commission: 0% (Kalshi charges no commission on weather markets)
- Spread: 3.1¢ per contract (measured mean across all markets)
- Slippage: 0.5¢ per contract (estimated)

Usage:
    from core.market_cost_model import MARKET_COST_MODEL
    cost = MARKET_COST_MODEL.round_trip_fraction()
    spread = MARKET_COST_MODEL.spread

Version: 1.0 — 2026-07-22
"""


class MarketCostModel:
    """
    Centralized Kalshi market cost model.

    Attributes:
        spread: Bid-ask spread per contract (fraction of $1, e.g. 0.031 = 3.1¢)
        commission: Commission per contract (fraction of $1, 0 for Kalshi)
        slippage: Estimated slippage per contract (fraction of $1)
    """

    def __init__(self, spread: float = 0.031, commission: float = 0.0, slippage: float = 0.005):
        self.spread = spread
        self.commission = commission
        self.slippage = slippage

    def round_trip_fraction(self) -> float:
        """
        Return the round-trip cost as a fraction of notional.
        Round-trip = spread/2 + commission + slippage (buy-and-hold-to-settlement).
        """
        return (self.spread / 2.0) + self.commission + self.slippage

    def one_way_cost(self) -> float:
        """Return the one-way cost per contract (fraction of $1)."""
        return (self.spread / 2.0) + self.slippage

    def to_dict(self) -> dict:
        return {
            'spread': self.spread,
            'commission': self.commission,
            'slippage': self.slippage,
            'round_trip_fraction': self.round_trip_fraction(),
        }


# Default singleton instance with measured mean spread of 3.1¢
MARKET_COST_MODEL = MarketCostModel()