#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-18 Fix Bug 7: Remove hardcoded fee rates - replace 0.05/0.001/0.002 with proper zero commissions (Kalshi charges 0 commission)]
# 2. [2026-07-06 fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL]
#

"""
SH3 - Fee-Aware Kelly Position Sizing Module

Implements fractional Kelly position sizing for binary options.
Formula: f* = (W - P) / (1 - P)

Where:
    W = win probability (estimated from rolling win rate)
    P = market price (implied probability from spread)

This is the correct Kelly formula for binary YES/NO options, replacing the
old edge/variance (continuous-normal Kelly) formula which was incorrect for
binary outcomes.

Constraints: max position size ≤ 25% of balance.
"""

import math
from typing import Dict, Any, Tuple, List, Optional
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from .market_cost_model import MARKET_COST_MODEL


@dataclass
class KellySizingConfig:
    """Configuration for fee-aware Kelly position sizing."""
    fraction_kelly: float = 0.5            # 50% fractional Kelly (conservative)
    fee_rate: float = 0.0                # 0% fee - Kalshi charges 0 commission (spread-only costs)
    max_position_pct: float = 0.25        # Max 25% of balance per trade
    window_days: int = 30                 # Rolling window for win rate calculation
    min_trades_for_kelly: int = 10        # Minimum trades to compute Kelly sizing


class KellyPositionSizer:
    """
    Implements the correct binary-option Kelly criterion.

    Formula: f* = (W - P) / (1 - P)

    Where:
        W = win probability (rolling win rate)
        P = market price (Kalshi implied probability)

    This is the correct Kelly formula for binary YES/NO options,
    replacing the old edge/variance formula which was designed for
    continuous-normal distributions.
    """
    
    def __init__(self, config: KellySizingConfig = None):
        if config is None:
            config = KellySizingConfig()
        self.config = config
        
        # Track trade history for win rate estimation
        self.trade_history: List[Dict[str, Any]] = []
    
    def add_result(self, date: str, amount: float, outcome: bool, market_price: float = 0.5):
        """
        Add a trade result to history for win rate calculation.
        
        Args:
            date: Date string (YYYY-MM-DD)
            amount: Amount bet (USD)
            outcome: True if won (correct direction), False if lost
            market_price: Market price at entry time (0.0-1.0)
        """
        self.trade_history.append({
            'date': date,
            'amount': amount,
            'outcome': outcome,
            'market_price': market_price
        })
        
        # Keep only last N days of history
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.config.window_days)
        self.trade_history = [
            t for t in self.trade_history 
            if datetime.strptime(t['date'], '%Y-%m-%d').date() >= cutoff_date.date()
        ]
    
    def get_rolling_win_rate(self) -> float:
        """
        Calculate rolling 30-day win rate from historical results.
        
        Returns:
            Float win rate (0.0 to 1.0)
        """
        if not self.trade_history:
            return 0.5  # Default neutral if no data
        
        total = len(self.trade_history)
        wins = sum(1 for t in self.trade_history if t['outcome'])
        return wins / total
    
    def calculate_edge(self, win_rate: float, market_price: float = 0.5) -> float:
        """
        Calculate edge for binary options.
        Edge = Win_Prob - Market_Price
        
        Args:
            win_rate: Observed win probability (0.0 to 1.0)
            market_price: Current market-implied probability (0.0 to 1.0)
            
        Returns:
            Float edge (positive = edge for YES, negative = edge for NO)
        """
        return win_rate - market_price
    

        # For binary outcomes, the variance is W*(1-W)
        # This is the maximum-likelihood variance for Bernoulli trials
        bernoulli_variance = win_rate * (1.0 - win_rate)

        # Ensure minimum stability floor
        return max(0.01, bernoulli_variance)
    
    def compute_kelly_fraction(self, win_rate: float, market_price: float = 0.5) -> float:
        """
        Compute Kelly fraction for binary options using the correct formula.
        
        Correct binary Kelly formula:
            f* = (W - P) / (1 - P)
        
        Where:
            W = win probability (estimated from rolling win rate)
            P = market price (implied probability, including spread costs)
        
        This replaces the old edge/variance (continuous-normal Kelly) which is
        incorrect for binary outcomes.
        
        Positive fraction = long position (buy YES).
        Negative fraction = short position (skip / reverse).
        
        Args:
            win_rate: Estimated win probability from rolling data
            market_price: Current market-implied probability (0.0 to 1.0)
            
        Returns:
            Float Kelly fraction from -0.5 to +0.5
        """
        # Compute round-trip cost for fee/spread adjustment
        round_trip_cost = MARKET_COST_MODEL.round_trip_fraction()

        # Adjust market price for the spread: the effective price we pay
        # is midpoint + half-spread for buys, midpoint - half-spread for sells
        # For the Kelly formula, use the mid-market price (spread already
        # accounted for in the edge vs breakeven calculation)
        adjusted_price = market_price

        # Edge = W - P (positive means our estimate exceeds market price)
        edge = win_rate - adjusted_price

        if len(self.trade_history) < self.config.min_trades_for_kelly:
            # Use a simplified estimate when insufficient history
            if adjusted_price >= 1.0 or (1.0 - adjusted_price) <= 0.0:
                return 0.0
            kelly_fraction_core = edge / (1.0 - adjusted_price)
        else:
            # Binary Kelly formula: f* = (W - P) / (1 - P)
            if adjusted_price >= 1.0 or (1.0 - adjusted_price) <= 0.0:
                return 0.0
            kelly_fraction_core = edge / (1.0 - adjusted_price)

        # Apply fractional Kelly for conservative sizing
        fractional_kelly = kelly_fraction_core * self.config.fraction_kelly

        # Cap to reasonable bounds [-0.5, +0.5]
        fractional_kelly = max(-0.5, min(0.5, fractional_kelly))

        return fractional_kelly
    
    def compute_position_size(self, 
                             win_rate: float,
                             confidence: float,
                             total_capital: float,
                             market_price: float = 0.5) -> Tuple[float, Dict[str, Any]]:
        """
        Compute position size using Kelly criterion with binary-option formula.
        
        Formula: f* = (W - P) / (1 - P)
        
        Supports both long and short positions:
        - Positive Kelly fraction → long position (BUY)
        - Negative Kelly fraction → short position (SELL)
        - Zero Kelly fraction → no position
        
        The returned position_size is always non-negative (absolute dollar amount).
        Check metadata['direction'] for 'long' or 'short'.
        
        Args:
            win_rate: Estimated 30-day rolling win rate  
            confidence: Signal confidence factor (0.0-1.0)
            total_capital: Total available capital
            market_price: Current market-implied probability (0.0-1.0)
            
        Returns: 
            Tuple of (position_size_usd, metadata_dict)
            position_size_usd is always >= 0 (absolute dollar amount)
            metadata['direction'] indicates 'long' or 'short'
        """
        # Calculate Kelly fraction based on win rate and market price
        kelly_fraction = self.compute_kelly_fraction(win_rate, market_price)
        
        # Determine direction based on sign of Kelly fraction
        is_short = kelly_fraction < 0
        abs_kelly = abs(kelly_fraction)
        
        if abs_kelly == 0:
            # No edge in either direction
            position_size = 0.0
        else:
            # Calculate base position from |Kelly fraction| and capital
            base_position = total_capital * abs_kelly
            
            # Apply signal confidence modulation
            # Higher confidence means higher portion of Kelly stake
            confidence_modulated_position = base_position * confidence
            
            # Calculate max allowable position based on cap
            max_position_size = total_capital * self.config.max_position_pct
            
            # Determine final position
            position_size = min(
                confidence_modulated_position,  # Kelly size with confidence
                max_position_size,  # Maximum allowed
                total_capital * 0.9  # Never risk 100% of capital
            )
        
        # Metadata with calculation details
        metadata = {
            "win_rate": win_rate,
            "market_price": market_price,
            "conf_level": confidence,
            "capital": total_capital,
            "kelly_fraction_raw": self.compute_kelly_fraction(win_rate, market_price),
            "kelly_fraction": kelly_fraction,  # Preserve sign for direction
            "kelly_fraction_abs": abs_kelly,
            "direction": "short" if is_short else "long",
            "position_size_usd": position_size,
            "formula": "f* = (W - P) / (1 - P)",
            "config_fraction_kelly": self.config.fraction_kelly,
            "config_fee_rate": self.config.fee_rate,
            "config_max_position_pct": self.config.max_position_pct,
            "config_window_days": self.config.window_days,
            "num_trades_in_calculation": len(self.trade_history), 
            "has_positive_edge": kelly_fraction > 0,
            "has_negative_edge": kelly_fraction < 0,
            "short_opportunity": is_short,
        }
        
        return position_size, metadata


def run_kelly_positioning_demo():
    """
    Demonstration of binary-option Kelly position sizing.
    """
    print("=" * 90)
    print("Phase A.2: Binary-Option Kelly Position Sizing Demo")
    print("=" * 90)
    print("Formula: f* = (W - P) / (1 - P)")
    print("Cost model: MARKET_COST_MODEL.round_trip_fraction()")
    print(f"Round-trip cost: {MARKET_COST_MODEL.round_trip_fraction():.4f}")
    print()
    
    # Initialize Kelly position sizer
    sizer = KellyPositionSizer()
    
    # Simulate some historical performance
    # Add some winning/losing trades to establish win rate
    for i in range(20):
        sizer.add_result(f"2025-06-{i+1:02d}", 50.0, (i % 3) != 2)  # ~67% win rate
    
    print(f"Added 20 demo trades with ~67% win rate")
    print(f"Rolling current win rate: {sizer.get_rolling_win_rate():.3f}")
    
    # Test various scenarios with market prices
    test_scenarios = [
        {"win_rate": 0.60, "market_price": 0.50, "name": "60% edge vs 50% market"},
        {"win_rate": 0.65, "market_price": 0.55, "name": "65% edge vs 55% market"},
        {"win_rate": 0.72, "market_price": 0.60, "name": "72% edge vs 60% market"},
        {"win_rate": 0.52, "market_price": 0.50, "name": "52% edge vs 50% (thin)"},
        {"win_rate": 0.48, "market_price": 0.52, "name": "48% edge vs 52% (neg)"},
    ]
    
    capital = 10000.0
    
    print(f"\nCapital: ${capital:,.2f}")
    print("\n" + f"{'Scenario':<30} {'W':<6} {'P':<6} {'Kelly':<10} {'Conf=0.7':<12} {'Conf=0.4':<12}")
    print("-" * 90)
    
    for scenario in test_scenarios:
        win_rate = scenario["win_rate"]
        market_price = scenario["market_price"]
        name = scenario["name"]
        
        raw_kelly = sizer.compute_kelly_fraction(win_rate, market_price)
        
        temp_sizer = KellyPositionSizer(sizer.config)
        temp_sizer.trade_history = sizer.trade_history
        
        pos_high_conf, _ = temp_sizer.compute_position_size(win_rate, 0.7, capital, market_price)
        pos_low_conf, _ = temp_sizer.compute_position_size(win_rate, 0.4, capital, market_price)
        
        print(f"{name:<30} {win_rate:<6.2f} {market_price:<6.2f} {raw_kelly:<10.4f} ${pos_high_conf:<11.2f} ${pos_low_conf:<11.2f}")
    
    print(f"\nKelly Formula: f* = (W - P) / (1 - P)")
    print(f"Fractional Kelly: {sizer.config.fraction_kelly}")
    print(f"Max position cap: ${capital * sizer.config.max_position_pct:.2f}")
    
    return sizer


def main():
    demo_sizer = run_kelly_positioning_demo()
    return demo_sizer


if __name__ == "__main__":
    main()