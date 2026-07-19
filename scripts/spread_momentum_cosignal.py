"""
Spread Momentum Co-signal Detector
=================================

Purpose: Tracks spread widening/narrowing as a position adjustment signal.
When spread widens, reduces position size. When spread narrows, increases position size.
Operates on order book snapshot deltas.

Inputs:
- Orderbook snapshots with bid/ask prices and volumes
- Time-stamped snapshot deltas
- Historical spread momentum patterns

Outputs:
- Spread momentum indicator (-1 to 1 scale)
- Position adjustment recommendation
- Risk adjustment parameters

Usage: python spread_momentum_cosignal.py --station STATION_CODE \
       --previous_snapshot SNAPSHOT_FILE --current_snapshot CURRENT_SNAPSHOT
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Any, Optional
import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.joinpath("core")))
from station_registry import STATIONS
from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
from kalshi_monitor import KalshiMonitor


class SpreadMomentumCoSignal:
    """
    Detects momentum in spread changes as a co-signal for position adjustments.
    When spreads widen, market becomes less liquid and profitable, suggesting
    smaller positions. When spreads narrow, indicating better market efficiency,
    larger positions may be justified.
    """
    
    def __init__(self, lookback_periods: int = 10):
        """
        Initialize spread momentum detection
        
        Args:
            lookback_periods: Number of periods to consider for momentum calculation
        """
        self.lookback_periods = lookback_periods
        self.spread_history = []  # Historical spread values
        self.timestamp_history = []  # Corresponding timestamps
        self.momentum_signal_strength = 0.0  # Current strength of momentum signal
        
    def add_spread_reading(self, timestamp: datetime, bid: float, ask: float) -> None:
        """
        Add a new bid/ask reading to the history
        
        Args:
            timestamp: Time of the reading
            bid: Best bid price
            ask: Best ask price
        """
        spread = ask - bid
        self.spread_history.append(spread)
        self.timestamp_history.append(timestamp)
        
        # Trim history to fit lookback window
        if len(self.spread_history) > self.lookback_periods * 2:  # Keep twice the required history
            excess = len(self.spread_history) - self.lookback_periods * 2
            self.spread_history = self.spread_history[excess:]
            self.timestamp_history = self.timestamp_history[excess:]
    
    def calculate_spread_momentum(self, current_timestamp: datetime = None) -> Dict[str, float]:
        """
        Calculate spread momentum and related metrics
        
        Args:
            current_timestamp: Timestamp to align calculations
        
        Returns:
            Dictionary with momentum metrics
        """
        if len(self.spread_history) < 3:
            return {
                'momentum_signal': 0.0,
                'spread_change_rate': 0.0,
                'momentum_strength': 0.0,
                'average_spread': np.mean(self.spread_history) if self.spread_history else 0.5
            }
        
        # Calculate recent spread changes
        recent_changes = np.diff(self.spread_history[-self.lookback_periods:])
        
        # Calculate momentum - looking at spread acceleration
        if len(recent_changes) < 2:
            momentum = 0.0
        else:
            # Simple trend of the most recent changes (linear regression slope proxy)
            x_vals = np.arange(len(recent_changes))
            momentum = np.polyfit(x_vals, recent_changes, 1)[0] if len(x_vals) > 1 else 0.0
        
        # Normalize momentum to [-1, 1] scale (-1 = rapidly widening, +1 = rapidly narrowing)
        # Using historical baseline spread to normalize
        avg_recent_spread = np.mean(self.spread_history[-self.lookback_periods:])
        avg_initial_spread = np.mean(self.spread_history[:self.lookback_periods]) if len(self.spread_history) >= self.lookback_periods * 2 else np.mean(self.spread_history)
        
        # Relative normalization based on baseline volatility
        baseline_volatility = np.std(self.spread_history) if len(self.spread_history) > 1 else 0.01
        normalized_momentum = momentum / max(baseline_volatility, 0.001)
        normalized_momentum = np.tanh(normalized_momentum * 2)  # Squash to approx .95 range
        
        # Directional component: negative momentum means spread is getting wider
        # (negative = reduce position, positive = potentially increase position)
        spread_trend = recent_changes[-1] if recent_changes.size > 0 else 0.0
        trend_normalized = spread_trend / max(avg_recent_spread, 0.005)  # Normalize by average spread level
        
        return {
            'momentum_signal': normalized_momentum * -1,  # Reverse signs: negative momentum = widening = bad
            'spread_change_rate': trend_normalized,
            'momentum_strength': abs(normalized_momentum),
            'average_spread': avg_recent_spread,
            'spread_volatility': np.std(recent_changes) if len(recent_changes) > 1 else 0.0,
            'sample_count': len(recent_changes)
        }
    
    def get_position_adjustment(self, current_signal_confidence: float, market_conditions: Dict) -> Dict[str, Any]:
        """
        Calculate position adjustment based on spread momentum
        
        Args:
            current_signal_confidence: Underlying signal's confidence level
            market_conditions: Current market conditions dictionary
        
        Returns:
            Position adjustment recommendations
        """
        momentum_metrics = self.calculate_spread_momentum()
        
        # Calculate position adjustment multipliers
        # When spread momentum indicates worsening conditions, decrease position sizes
        base_position_size = current_signal_confidence
        
        # Adjust for current momentum strength
        momentum_multiplier = 1.0 - (momentum_metrics['momentum_strength'] * 0.5)  # At most 50% reduction
        if momentum_metrics['momentum_signal'] < 0:  # Negative signal = spreading out
            # Reduce position significantly in bad momentum conditions
            momentum_multiplier = max(0.2, momentum_multiplier - 0.3)
        
        adjusted_position = base_position_size * max(0.1, momentum_multiplier)
        
        # Additional considerations based on volatility
        high_volatility = momentum_metrics['spread_volatility'] > market_conditions.get('normal_spread_volatility', 0.01) * 1.5
        
        return {
            'original_position': base_position_size,
            'adjusted_position': adjusted_position,
            'adjustment_reason': 'Spread momentum deterioration' if momentum_metrics['momentum_signal'] < -0.3 else 'Stable market',
            'volatility_flag': high_volatility,
            'recommended_action': 'Reduce position' if momentum_metrics['momentum_signal'] < -0.3 else 'Maintain position',
            'metrics': momentum_metrics
        }

    def calculate_orderbook_delta_from_snapshots(self, 
                                               prev_snapshot: Dict[str, Any], 
                                               current_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate orderbook deltas between two snapshots
        
        Args:
            prev_snapshot: Previous orderbook state
            current_snapshot: Current orderbook state
            
        Returns:
            Delta metrics comparing the two states
        """
        # Extract bid and ask from both snapshots (assumes standard format)
        prev_bid = prev_snapshot.get('best_bid', 0.5)
        prev_ask = prev_snapshot.get('best_ask', 0.5)
        curr_bid = current_snapshot.get('best_bid', 0.5)
        curr_ask = current_snapshot.get('best_ask', 0.5)
        
        # Calculate spreads
        prev_spread = prev_ask - prev_bid
        curr_spread = curr_ask - curr_bid
        
        # Record both spreads for historical analysis
        if 'timestamp' in prev_snapshot:
            self.add_spread_reading(prev_snapshot['timestamp'], prev_bid, prev_ask)
        current_time = datetime.utcnow()
        self.add_spread_reading(current_time, curr_bid, curr_ask)
        
        return {
            'bid_change': curr_bid - prev_bid,
            'ask_change': curr_ask - prev_ask,
            'spread_change': curr_spread - prev_spread,
            'midpoint_change': ((curr_ask + curr_bid)/2) - ((prev_ask + prev_bid)/2),
            'spread_widening': curr_spread > prev_spread
        }


def main():
    parser = argparse.ArgumentParser(description='Detect spread momentum co-signals')
    parser.add_argument('--station', type=str, required=True, help='Station code (e.g., KJFK)')
    
    # Add arguments for processing snapshots if provided
    parser.add_argument('--snapshots_file', type=str, help='CSV file with orderbook snapshots')
    
    # Generate mock data if no file provided
    args = parser.parse_args()
    
    cosignal = SpreadMomentumCoSignal(lookback_periods=8)
    
    # If snapshot file provided, use it; otherwise use mock data
    if args.snapshots_file:
        try:
            df = pd.read_csv(args.snapshots_file)
            for _, row in df.iterrows():
                timestamp = datetime.fromisoformat(row['timestamp']) if pd.notna(row['timestamp']) else datetime.utcnow()
                cosignal.add_spread_reading(timestamp, row['bid'], row['ask'])
        except Exception as e:
            print(f"Error reading snapshots file: {e}. Using mock data.")
    
    # Generate mock snapshots if no file
    if not hasattr(df, 'iloc') or df.empty:
        print("Using mock snapshots for demonstration...")
        timestamps = pd.date_range(datetime.now(), periods=15, freq='1min').tolist()
        # Simulate spread fluctuations with some trend
        base_spread = 0.035
        spreads = [base_spread + 0.002 * np.sin(i * 0.5) for i in range(15)]
        bids = [0.48 + 0.02 * np.random.random() for _ in range(15)]
        asks = [bid + spread for bid, spread in zip(bids, spreads)]
        
        for ts, bid, ask in zip(timestamps[:12], bids[:12], asks[:12]):
            cosignal.add_spread_reading(ts, bid, ask)
    
    # Calculate and report metrics
    metrics = cosignal.calculate_spread_momentum()
    
    print(f"Spread Momentum Analysis for {args.station}:")
    print(f"  Momentum Signal: {metrics['momentum_signal']:.3f}")
    print(f"  Average Spread: ${metrics['average_spread']:.3f}")
    print(f"  Spread Volatility: ${metrics['spread_volatility']:.4f}")
    print(f"  Momentum Strength: {metrics['momentum_strength']:.3f}")
    print(f"  Sample Count: {metrics['sample_count']}")
    
    # Simulate market conditions
    market_conds = {
        'normal_spread_volatility': 0.015,
        'is_liquid': metrics['average_spread'] < 0.05
    }
    
    # Show position adjustment with mock signal
    adj_result = cosignal.get_position_adjustment(0.7, market_conds)
    print(f"\nPosition Adjustment Recommendation:")
    print(f"  Original Position Confidence: {adj_result['original_position']:.2f}")
    print(f"  Adjusted Position: {adj_result['adjusted_position']:.2f}")
    print(f"  Recommended Action: {adj_result['recommended_action']}")


if __name__ == "__main__":
    main()