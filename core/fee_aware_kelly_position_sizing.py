#!/usr/bin/env python3
"""
SH3 - Fee-Aware Kelly Position Sizing Module

Implements fractional Kelly position sizing with fee adjustments.
Formula: Kelly fraction = edge / (1 - fee) / variance
Edge estimated from 30-day rolling win rate.

Constraints: max position size ≤ 25% of balance.
"""

import math
from typing import Dict, Any, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class KellySizingConfig:
    """Configuration for fee-aware Kelly position sizing."""
    fraction_kelly: float = 0.5            # 50% fractional Kelly (conservative)  
    fee_rate: float = 0.05                # 5% fee per trade (Kalshi exchange fee)
    max_position_pct: float = 0.25        # Max 25% of balance per trade
    window_days: int = 30                 # Rolling window for win rate calculation
    min_trades_for_kelly: int = 10        # Minimum trades to compute Kelly sizing


class KellyPositionSizer:
    """
    Implements fee-aware Kelly criterion with fractional Kelly sizing.
    
    Kelly formula (adapted for binary outcomes like Kalshi):
    - Edge = Win_probability - Loss_probability = 2*Win_rate - 1
    - Kelly fraction = Edge / (Win_payoff * Win_probability + Loss_size * Loss_probability)
    - For Kalshi: Win_payoff = 0.95x (95c of $1), Loss_size = 1.0x (full stake)
    - Simplified as: Kelly_fraction = edge / variance
    - Where variance = Win_prob * Loss_prob * (Win_payoff + Loss_size)**2
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
        cutoff_date = datetime.now() - timedelta(days=self.config.window_days)
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
    
    def calculate_fee_adjusted_edge(self, win_rate: float) -> float:
        """
        Calculate fee-adjusted edge for Kelly sizing.
        
        Args:
            win_rate: Observed win rate (0.0 to 1.0)
            
        Returns:
            Float edge adjusted for fees (accounting for 0.05 loss factor)
        """
        # Naive edge without fees: win_rate - (1 - win_rate) = 2*win_rate - 1
        naive_edge = 2 * win_rate - 1
        
        # Adjust for fees and imperfect payouts
        # If winning trades yield 95% instead of 100% due to fees, 
        # net profit rate is lowered
        adjusted_edge = naive_edge - (2 * self.config.fee_rate * win_rate)
        # Subtract fee impact on winning trades (2*fee_rate*win_rate)
        # Since winning trades yield less due to fees, and also subtract fee per trade
        
        return adjusted_edge
    
    def calculate_variance_estimate(self, win_rate: float) -> float:
        """
        Estimate variance for Kelly fraction calculation.
        Uses historical variance from realized outcomes.
        """
        if len(self.trade_history) < self.config.min_trades_for_kelly:
            # Default variance for high-conviction scenarios (with fees considered)
            return 0.5  # Conservative variance estimate
        
        # Calculate realized variance from history
        returns = []
        for trade in self.trade_history:
            if trade['outcome']:
                # Win: (net gain percentage) - fee impact
                net_return = 0.95  # Simplifying to 95% of potential win with fees
            else:
                # Loss: full stake loss + fee
                net_return = -1.0
            returns.append(net_return)
        
        if len(returns) == 0:
            return 0.5
            
        # Calculate sample variance
        mean = sum(returns) / len(returns)
        squared_diffs = [(r - mean)**2 for r in returns]
        variance = sum(squared_diffs) / (len(returns) - 1 if len(returns) > 1 else 1)
        
        return max(0.1, variance)  # Set a minimum floor for stability
    
    def compute_kelly_fraction(self, win_rate: float) -> float:
        """
        Compute Kelly fraction based on estimated edge and variance.
        
        Returns NEGATIVE fraction when edge is negative, signaling short opportunity.
        Positive fraction = long position. Negative fraction = short position.
        
        Args:
            win_rate: Estimated win rate from 30-day rolling data
            
        Returns:
            Float Kelly fraction from -0.5 to +0.5
            Positive = go long, Negative = go short
        """
        if len(self.trade_history) < self.config.min_trades_for_kelly:
            # If not enough historical data, scale with a reasonable default
            edge = self.calculate_fee_adjusted_edge(win_rate)
            # Use conservative variance
            variance_estimate = 0.5
        else:
            edge = self.calculate_fee_adjusted_edge(win_rate)
            variance_estimate = self.calculate_variance_estimate(win_rate)
        
        # Check for zero or invalid variance
        if variance_estimate <= 0.0:
            variance_estimate = 0.5  # Default safety value
        
        # Calculate basic Kelly fraction: edge / variance
        # This naturally produces negative values when edge < 0 (win_rate < 0.5)
        kelly_fraction_core = edge / variance_estimate
        
        # Apply fee adjustment: account for fee reducing overall profitability
        # Effective Kelly = Core Kelly / (1 + adjusted_fee_impact)
        effective_kelly = kelly_fraction_core / (1.0 + self.config.fee_rate * 2.0)
        
        # Apply fractional Kelly
        fractional_kelly = effective_kelly * self.config.fraction_kelly
        
        # Cap to reasonable bounds [-0.5, +0.5]
        # Negative values indicate short opportunities — do NOT clip to 0
        fractional_kelly = max(-0.5, min(0.5, fractional_kelly))
        
        return fractional_kelly
    
    def compute_position_size(self, 
                             win_rate: float,
                             confidence: float,
                             total_capital: float) -> Tuple[float, Dict[str, Any]]:
        """
        Compute position size using Kelly criterion with fee adjustment.
        
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
            
        Returns: 
            Tuple of (position_size_usd, metadata_dict)
            position_size_usd is always >= 0 (absolute dollar amount)
            metadata['direction'] indicates 'long' or 'short'
        """
        # Calculate Kelly fraction based on win rate
        kelly_fraction = self.compute_kelly_fraction(win_rate)
        
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
            "conf_level": confidence,
            "capital": total_capital,
            "kelly_fraction_raw": self.compute_kelly_fraction(win_rate),
            "kelly_fraction": kelly_fraction,  # Preserve sign for direction
            "kelly_fraction_abs": abs_kelly,
            "direction": "short" if is_short else "long",
            "position_size_usd": position_size,
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
    Demonstration of Kelly position sizing with fee awareness.
    """
    print("=" * 90)
    print("SH3: Fee-Aware Kelly Position Sizing Demo")
    print("=" * 90)
    print("Simulating Kelly sizing with Kalshi 5% fees, 50% fractional Kelly, 25% max position")
    print()
    
    # Initialize Kelly position sizer
    sizer = KellyPositionSizer()
    
    # Simulate some historical performance
    # Add some winning/losing trades to establish win rate
    for i in range(20):
        sizer.add_result(f"2025-06-{i+1:02d}", 50.0, (i % 3) != 2)  # ~67% win rate
    
    print(f"Added 20 demo trades with ~67% win rate")
    print(f"Rolling current win rate: {sizer.get_rolling_win_rate():.3f}")
    
    # Test various scenarios
    test_scenarios = [
        {"win_rate": 0.60, "name": "Marginal Positive Edge"},
        {"win_rate": 0.65, "name": "Good Edge"},  
        {"win_rate": 0.72, "name": "Strong Edge"},
        {"win_rate": 0.52, "name": "Weak Edge (at threshold)"},
        {"win_rate": 0.48, "name": "Negative Edge (no trade)"},
    ]
    
    capital = 10000.0
    
    print(f"\nCapital: ${capital:,.2f}")
    print("\n" + f"{'Scenario':<25} {'Win Rate':<10} {'Kelly Frac':<12} {'Conf=0.7':<12} {'Conf=0.4':<12}")
    print("-" * 85)
    
    for scenario in test_scenarios:
        win_rate = scenario["win_rate"]
        name = scenario["name"]
        
        # Calculate base Kelly for different confidence levels
        raw_kelly = sizer.compute_kelly_fraction(win_rate)
        
        # Use temporary sizer just for position calculations at different confidences
        temp_sizer = KellyPositionSizer(sizer.config)
        # Copy history to have same context
        temp_sizer.trade_history = sizer.trade_history
        
        pos_high_conf, _ = temp_sizer.compute_position_size(win_rate, 0.7, capital)
        pos_low_conf, _ = temp_sizer.compute_position_size(win_rate, 0.4, capital)
        
        print(f"{name:<25} {win_rate:<10.2f} {raw_kelly:<12.3f} ${pos_high_conf:<11.2f} ${pos_low_conf:<11.2f}")
    
    print(f"\nFee Adjustment Effect:")
    print(f"Raw Kelly (before fee adjustment): calculated from win rate and variance")
    print(f"Fee adjustment: reduces Kelly fraction to account for 5% trading fees per trade") 
    print(f"Fractional Kelly: {sizer.config.fraction_kelly} reduces volatility vs full Kelly")
    print(f"Max position cap: ${capital * sizer.config.max_position_pct:.2f} ({sizer.config.max_position_pct * 100}%)")
    
    return sizer


def main():
    demo_sizer = run_kelly_positioning_demo()
    return demo_sizer


if __name__ == "__main__":
    main()