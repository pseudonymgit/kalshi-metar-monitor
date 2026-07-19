"""
Order Flow Imbalance Detector
=============================

Purpose: Calculates the ratio of buyer-initiated vs seller-initiated trades
to detect imbalances in market sentiment. Derived by analyzing trade direction
relative to midpoint pricing in orderbook delta snapshots.

Inputs:
- Trade-by-trade transaction data
- Orderbook snapshots between trades
- Midpoint price changes for trade direction attribution

Outputs:
- Order flow imbalance ratio (+ = buying pressure, - = selling pressure)
- Trading signal based on detected imbalance
- Imbalance confidence

Usage: python order_flow_imbalance.py --station STATION_CODE \
      --trade_data PATH_TO_TRADES [--threshold RATIO_THRESHOLD]
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


class OrderFlowImbalance:
    """
    Detects buy/sell imbalance by analyzing trade direction relative to the 
    theoretical market midpoint at execution time. Buys above midpoint indicate 
    aggression to fill, sells below midpoint indicate selling pressure.
    """
    
    def __init__(self, lookback_period: str = "15min"):
        """
        Initialize with time window for order flow analysis
        
        Args:
            lookback_period: Time window to aggregate flow data (e.g., '15min', '1h')
        """
        self.lookback_period = pd.Timedelta(lookback_period)
        self.trade_history = []  # Stores processed trades with direction
        self.imbalance_history = []  # Historical imbalance values
        self.volume_by_direction = {"buy": [], "sell": []}
    
    def add_trade(self, timestamp: datetime, price: float, size: int, 
                  bid: float, ask: float) -> Dict[str, Any]:
        """
        Add a trade to the history and determine if it was initiated by a buyer or seller
        
        Args:
            timestamp: Trade execution time
            price: Trade execution price
            size: Trade size (quantity)
            bid: Last known bid
            ask: Last known ask
            
        Returns:
            Trade details with direction assignment
        """
        midpoint = (bid + ask) / 2.0
        size = abs(size)  # Size should always be positive
        
        # Determine if this appears to be buyer- or seller-initiated
        if price > midpoint:
            direction = "buy"
            explanation = f"Trade @${price:.3f} above midpoint @${midpoint:.3f}"
        elif price < midpoint:
            direction = "sell"
            explanation = f"Trade @${price:.3f} below midpoint @${midpoint:.3f}"
        else:
            direction = "neutral"
            explanation = f"Trade @${price:.3f} @ midpoint @${midpoint:.3f}"
            
        # For neutral trades, try to infer direction from recent pattern
        if direction == "neutral":
            # Look at recent trades at same price level
            recent_same_level = [
                t for t in self.trade_history[-10:]
                if abs(t['price'] - price) < 0.001 and t['timestamp'] > (pd.Timestamp(timestamp) - pd.Timedelta(minutes=5))
            ]
            
            # Most recent non-neutral nearby trade likely gives direction
            while recent_same_level and direction == "neutral":
                prev_trade = recent_same_level.pop()
                if prev_trade['direction'] in ['buy', 'sell']:
                    direction = prev_trade['direction']
                    explanation += f", inferred from nearby price movement"
                    break
            
            # Default to buy if unclear
            if direction == "neutral":
                direction = "buy"
                explanation += ", defaulted to buy (no clear direction)"
        
        trade_detail = {
            "timestamp": pd.Timestamp(timestamp),
            "price": price,
            "size": size,
            "bid": bid,
            "ask": ask,
            "midpoint": midpoint,
            "direction": direction,
            "explained": explanation,
            "imbalance_impact": size if direction == "buy" else -size
        }
        
        self.trade_history.append(trade_detail)
        
        # Store volume by direction for later imbalancing
        self.volume_by_direction[direction].append({
            'timestamp': pd.Timestamp(timestamp),
            'volume': size
        })
        
        # Clean old data beyond lookback period
        self._cleanup_old_trades()
        
        return trade_detail
    
    def _cleanup_old_trades(self):
        """
        Remove trades older than lookback period from history
        """
        cutoff = pd.Timestamp.utcnow() - self.lookback_period
        
        # Filter trade history
        self.trade_history = [t for t in self.trade_history if t['timestamp'] >= cutoff]
        
        # Filter volume histories
        for d in ['buy', 'sell']:
            self.volume_by_direction[d] = [v for v in self.volume_by_direction[d] if v['timestamp'] >= cutoff]
    
    def calculate_imbalance(self, window_ms: int = 900000) -> Dict[str, Any]:
        """
        Calculate recent order flow imbalance
        
        Args:
            window_ms: Window width in milliseconds
            
        Returns:
            Imbalance metrics
        """
        window = pd.Timedelta(milliseconds=window_ms)
        
        now = pd.Timestamp.utcnow()
        cutoff = now - window
        
        # Get recent trades within window
        recent_trades = [t for t in self.trade_history if t['timestamp'] >= cutoff]
        
        if not recent_trades:
            return {
                'imbalance_ratio': 0.0,
                'buy_volume': 0.0,
                'sell_volume': 0.0,
                'total_volume': 0.0,
                'imbalance_indicator': 0.0,
                'is_reliable': False,
                'trades_in_window': 0
            }
        
        # Separate and sum volumes by direction
        buy_volumes = [t['size'] for t in recent_trades if t['direction'] == 'buy']
        sell_volumes = [t['size'] for t in recent_trades if t['direction'] == 'sell']
        
        buy_total = sum(buy_volumes)
        sell_total = sum(sell_volumes)
        
        total_volume = buy_total + sell_total
        if total_volume <= 0:
            return {
                'imbalance_ratio': 0.0,
                'buy_volume': 0.0,
                'sell_volume': 0.0,
                'total_volume': 0.0,
                'imbalance_indicator': 0.0,
                'is_reliable': False,
                'trades_in_window': 0
            }
        
        # Calculate imbalance ratio (positive = buying pressure, negative = selling)
        imbalance = (buy_total - sell_total) / total_volume if total_volume > 0 else 0.0
        
        # Smooth the imbalance to prevent overreacting to very few trades
        num_trades = len(recent_trades)
        reliability_scalar = min(1.0, num_trades / 10.0)  # Only reliable with 10+ trades
        
        smoothed_imbalance = imbalance * reliability_scalar
        
        return {
            'imbalance_ratio': imbalance,
            'buy_volume': buy_total,
            'sell_volume': sell_total,
            'total_volume': total_volume,
            'imbalance_indicator': smoothed_imbalance,
            'is_reliable': reliability_scalar > 0.5,  # At least 5 trades for reliability
            'trades_in_window': num_trades,
            'reliability_score': reliability_scalar
        }
    
    def get_trading_signal(self, threshold: float = 0.1) -> Dict[str, Any]:
        """
        Generate a trading signal based on order flow imbalance
        
        Args:
            threshold: Minimum imbalance level to trigger a signal
            
        Returns:
            Trading signal with strength and confidence
        """
        imbalance_info = self.calculate_imbalance()
        
        imbalance_val = imbalance_info['imbalance_indicator']
        
        # Determine signal direction and strength
        if abs(imbalance_val) < threshold:
            signal_type = "neutral"
            signal_strength = 0.0
        elif imbalance_val > 0:
            signal_type = "buy"
            signal_strength = min(1.0, abs(imbalance_val) / threshold)  # Cap maximum signal strength
        else:
            signal_type = "sell"
            signal_strength = min(1.0, abs(imbalance_val) / threshold)  # Cap maximum signal strength
        
        # Add confidence based on reliability factors
        volume_confidence = min(1.0, imbalance_info['total_volume'] / 100.0)  # Normalize on 100 contracts
        reliability_confidence = imbalance_info['reliability_score']
        overall_confidence = min(1.0, (volume_confidence + reliability_confidence) / 2.0)
        
        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength * overall_confidence,
            'confidence': overall_confidence,
            'imbalance_raw_value': imbalance_info['imbalance_ratio'],
            'buy_sell_ratio': imbalance_info.get('buy_volume', 0) / (imbalance_info.get('sell_volume', 1) + 1e-8),
            'volume_pressure': 'buying' if imbalance_val > 0 else 'selling',
            'details': imbalance_info
        }
    
    def process_snapshot_deltas(self, initial_snapshot: Dict, trades: List[Dict]) -> List[Dict[str, Any]]:
        """
        Process orderbook changes along with associated trades
        
        Args:
            initial_snapshot: Initial orderbook state
            trades: List of trades that occurred between snapshots
            
        Returns:
            List of processed trades with attribution
        """
        processed_trades = []
        
        for trade in trades:
            # Use the snapshot's bid/ask for the first trade
            bid = initial_snapshot.get('bid', trade.get('price', 0.5))
            ask = initial_snapshot.get('ask', trade.get('price', 0.5))
            
            processed = self.add_trade(
                timestamp=trade['timestamp'], 
                price=trade['price'], 
                size=trade['size'],
                bid=bid,
                ask=ask
            )
            processed_trades.append(processed)
            
            # Update the snapshot for the next trade if needed
            if 'post_trade_bid' in trade and 'post_trade_ask' in trade:
                initial_snapshot['bid'] = trade['post_trade_bid']
                initial_snapshot['ask'] = trade['post_trade_ask']
        
        return processed_trades


def main():
    parser = argparse.ArgumentParser(description='Detect order flow imbalance')
    parser.add_argument('--station', type=str, required=True, help='Station code (e.g., KJFK)')
    parser.add_argument('--threshold', type=float, default=0.1, help='Minimum imbalance threshold for signal')
    parser.add_argument('--window_minutes', type=int, default=15, help='Minutes for imbalance window')
    
    args = parser.parse_args()
    
    # Initialize imbalance detector
    of_detector = OrderFlowImbalance(lookback_period=f"{args.window_minutes}min")
    
    # Generate mock trade history for demonstration
    print("Simulating order flow data for demonstration...")
    
    start_time = pd.Timestamp.utcnow() - pd.Timedelta(hours=1)
    times = pd.date_range(start_time, periods=50, freq='2min')
    
    # Simulate some trading with periodic imbalance (more buying pressure)
    np.random.seed(42)  # For reproducible demo
    base_price = 0.50
    bid = base_price - 0.005
    ask = base_price + 0.005
    
    for i, t in enumerate(times):
        # Determine if this trade is buy or sell based on random with bias
        trade_direction = np.random.choice(['buy_b', 'sell_s'], 
                                          p=[0.65, 0.35])  # Bias towards buying pressure
        
        if trade_direction == 'buy_b':
            price = ask  # Buyer takes offer
        else:
            price = bid  # Seller hits bid
        
        # Random volume
        size = np.random.randint(5, 25)
        
        trade = {
            'timestamp': t,
            'price': price,
            'size': size
        }
        
        # Add to detector
        result = of_detector.add_trade(t, price, size, bid, ask)
    
    # Calculate final metrics
    imbalance_info = of_detector.calculate_imbalance(window_ms=args.window_minutes*60*1000)
    signal = of_detector.get_trading_signal(threshold=args.threshold)
    
    print(f"Order Flow Imbalance Analysis for {args.station}:")
    print(f"  Imbalance Ratio: {imbalance_info['imbalance_ratio']:.3f}")
    print(f"  Buy Volume: {imbalance_info['buy_volume']:.1f}")
    print(f"  Sell Volume: {imbalance_info['sell_volume']:.1f}")
    print(f"  Total Volume: {imbalance_info['total_volume']:.1f}")
    print(f"  Trades in Window: {imbalance_info['trades_in_window']}")
    print(f"  Is Reliable: {imbalance_info['is_reliable']}")
    
    print(f"\nTrading Signal:")
    print(f"  Type: {signal['signal_type']}")
    print(f"  Strength: {signal['signal_strength']:.3f}")
    print(f"  Confidence: {signal['confidence']:.3f}")
    print(f"  Volume Pressure: {signal['volume_pressure']}")
    
    if abs(imbalance_info['imbalance_ratio']) > args.threshold:
        print(f"  >> SIGNIFICANT IMBALANCE DETECTED - {signal['signal_type'].upper()} OPPORTUNITY")


if __name__ == "__main__":
    main()