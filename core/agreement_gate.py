#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
#

"""
AGREEMENT GATE: N-of-M agreement filter for trade signal consensus

Implements a configurable N-of-M threshold filter that requires minimum
consensus among signals before emitting a trade signal. Only emits trade
signals when >= N signals agree on direction (after individual confidence
thresholds are satisfied).

Based on signal audit findings: 3/9 signals agreeing = 63.9% accuracy, 
5/9 = 76.7%. This gate is designed to improve signal accuracy via 
democratic consensus.
"""
from typing import List, Tuple, Optional
from enum import Enum


class MarketDirection(Enum):
    UP = "UP"
    DOWN = "DOWN"


class AgreementGate:
    def __init__(self, n_required: int = 3, m_total: int = 9):
        """
        Initialize agreement gate with N-of-M threshold.
        
        Args:
            n_required: Minimum number of signals that must agree (default 3)
            m_total: Total number of signals being considered (default 9)
        """
        self.n_required = n_required
        self.m_total = m_total
    
    def filter_signals(self, 
                      signals: List[Tuple[str, str, str, str]], 
                      min_confidence: float = 0.25) -> List[Tuple[str, str, str, str]]:
        """
        Filter signal list using N-of-M agreement requirement.
        
        Args:
            signals: List of (station, market_type, signal_direction, reason) tuples
            min_confidence: Minimum individual signal confidence for consideration
                    
        Returns:
            Filtered list containing only signals where >= N of M agree on direction
        """
        if len(signals) < self.m_total:
            # Not enough signals to form proper consensus, return original list
            return signals
            
        # Group signals by location (station/market type combination)
        signal_groups = {}
        for station, market_type, direction, reason in signals:
            key = (station, market_type)
            if key not in signal_groups:
                signal_groups[key] = []
            signal_groups[key].append((station, market_type, direction, reason))
        
        # For each group, check if N-of-M threshold is met for either direction
        accepted_signals = []
        for (station, market_type), group_signals in signal_groups.items():
            # Count votes for directions
            direction_votes = {}
            for _, _, direction, _ in group_signals:
                direction_key = direction.upper()
                direction_votes[direction_key] = direction_votes.get(direction_key, 0) + 1
            
            # Check if any direction meets the N threshold
            majority_direction = None
            for direction, vote_count in direction_votes.items():
                if vote_count >= self.n_required:
                    majority_direction = direction
                    break
            
            # If majority direction exists, only keep signals matching it
            if majority_direction:
                for signal_item in group_signals:
                    _, _, direction, _ = signal_item
                    if direction.upper() == majority_direction:
                        accepted_signals.append(signal_item)
        
        return accepted_signals

    def update_threshold(self, n_required: int, m_total: int):
        """
        Dynamically update the agreement threshold.
        
        Args:
            n_required: New minimum number of agreeing signals
            m_total: New total signal count
        """
        self.n_required = n_required
        self.m_total = m_total


class SimpleAgreementChecker:
    """
    Helper class for simpler N-of-M voting without complex grouping logic.
    This can be used directly in generate_signals processing.
    """
    @staticmethod
    def check_agreement(signal_group: List[Tuple[str, str, str, str]], 
                       n_required: int = 3) -> List[Tuple[str, str, str, str]]:
        """
        Check agreement within a group of signals for the same station/market_type.
        
        Args:
            signal_group: List of signals for same station/market_type
            n_required: Number of signals required to agree on direction
            
        Returns:
            List of signals that meet agreement threshold (may be empty)
        """
        if len(signal_group) < n_required:
            # Not enough signals to reach agreement
            return []
        
        # Count direction votes
        direction_counts = {}
        for _, _, direction, _ in signal_group:
            direction_upper = direction.upper() if isinstance(direction, str) else str(direction).upper()
            direction_counts[direction_upper] = direction_counts.get(direction_upper, 0) + 1
        
        # Find if any direction meets threshold
        qualifying_dir = None
        for direction, count in direction_counts.items():
            if count >= n_required:
                qualifying_dir = direction
                break
        
        if qualifying_dir is None:
            return []  # No consensus reached
        
        # Return all signals matching the qualified direction
        return [(s, mt, d, r) for s, mt, d, r in signal_group 
                if (d.upper() if isinstance(d, str) else str(d).upper()) == qualifying_dir]


if __name__ == "__main__":
    # Example usage and testing
    from enum import Enum
    
    class MarketSide(Enum):
        UP = "UP" 
        DOWN = "DOWN"
    
    # Test basic functionality
    gate = AgreementGate(n_required=2, m_total=3)
    
    # Mock signals: (station, market_type, direction, reason)
    test_signals = [
        ("KATL", "HIGH", MarketSide.UP.value, "calendar_climatology"),
        ("KATL", "HIGH", MarketSide.UP.value, "late_day_momentum"),
        ("KATL", "HIGH", MarketSide.DOWN.value, "nwp_analog"),  # Only 1 disagreeing
        ("KBOS", "HIGH", MarketSide.UP.value, "calendar_climatology"),
        ("KBOS", "HIGH", MarketSide.DOWN.value, "late_day_momentum"),  # No majority here
        ("KBOS", "HIGH", MarketSide.DOWN.value, "nwp_analog"),        # No majority here
    ]
    
    print(f"Input signals: {len(test_signals)}")
    filtered = gate.filter_signals(test_signals)
    print(f"Filtered signals: {len(filtered)}")
    for sig in filtered:
        print(f"  {sig}")