"""
Core module for spread-adjusted net edge calibration.
Calculates net edge after accounting for bid-ask spread costs.

Part of Phase 7 - Kalshi API Integration.
"""

import os
import requests
from typing import Tuple, Dict, Any


def get_kalshi_spread(series_ticker: str, market_type: str, threshold: int) -> float:
    """
    Get the current spread of a Kalshi contract for the specified market type and threshold.
    Uses the new API fields 'yes_bid_dollars' and 'yes_ask_dollars'.
    
    Args:
        series_ticker: Series ticker like 'KXHIGHKATL'
        market_type: 'HIGH' or 'LOW'
        threshold: Strike price threshold like 80, 85, etc.
        
    Returns:
        Spread as float (e.g., 0.03 for 3-cent spread)
    """
    try:
        # Construct individual market ticker
        from datetime import datetime
        today = datetime.now()
        date_str = today.strftime('%y%m%d')
        
        # Example: KXHIGHKATL-270120-80
        full_ticker = f"{series_ticker}-{date_str}-{threshold}"
        
        # Kalshi API endpoint for individual markets
        base_url = os.getenv("KALSHI_PUBLIC_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
        url = f"{base_url}/markets/{full_ticker}"
        
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return 0.05  # Default to 5 cent spread if no data
            
        market_data = response.json()['market']
        
        # Use the new dollar-based pricing fields
        bid_str = market_data.get('yes_bid_dollars')
        ask_str = market_data.get('yes_ask_dollars')
        
        # Convert from strings to float
        if bid_str is not None and ask_str is not None:
            try:
                bid = float(bid_str)
                ask = float(ask_str)
                
                # Return the spread
                if 0 <= bid <= 1 and 0 <= ask <= 1 and bid <= ask:
                    spread = ask - bid
                    return max(0.005, spread)  # Minimum 0.5 cent
            except (ValueError, TypeError):
                pass
                
        # Fallback to old integer fields if new ones missing
        bid_raw = market_data.get('yes_bid')
        ask_raw = market_data.get('yes_ask')
        
        if bid_raw is not None and ask_raw is not None:
            bid = bid_raw / 100.0
            ask = ask_raw / 100.0
            if 0 <= bid <= 1 and 0 <= ask <= 1 and bid <= ask:
                return max(0.005, ask - bid)
    
        return 0.05  # Default to 5 cent spread
        
    except Exception as e:
        print(f"Error getting Kalshi spread for {full_ticker}: {str(e)}")
        return 0.05  # Default to 5 cent spread


def calculate_net_edge(signal_confidence: float, spread: float) -> float:
    """
    Calculate net edge after accounting for spread costs.
    
    Formula: net_edge = signal_confidence - 0.5 - (spread / 2)
    Subtract half-spread as cost component. If negative, don't trade.
    
    Args:
        signal_confidence: Our confidence in the signal (0.0 to 1.0)
        spread: Market spread as decimal (e.g., 0.03 for 3 cents)
        
    Returns:
        Net edge after spread, can be negative
    """
    # Calculate net edge
    fair_value = 0.5
    distance_from_fair = signal_confidence - fair_value
    spread_cost = spread / 2.0  # Approximate cost of entering via market order
    
    net_edge = distance_from_fair - spread_cost
    return net_edge


DEFAULT_MIN_NET_EDGE = 0.02  # 2% minimum net edge


def calibrate_signal_with_spread(
    signal_confidence: float,
    series_ticker: str,
    market_type: str,
    threshold: int,
    min_net_edge: float = DEFAULT_MIN_NET_EDGE
) -> Dict[str, Any]:
    """
    2D calibrator that adjusts signal based on spread.
    
    Args:
        signal_confidence: Original signal confidence (0.0 to 1.0)
        series_ticker: Series ticker for market lookup
        market_type: 'HIGH' or 'LOW'
        threshold: Strike price
        min_net_edge: Minimum acceptable net edge
        
    Returns:
        Dict with calibration results and whether to trade
    """
    # Get market spread
    spread = get_kalshi_spread(series_ticker, market_type, threshold)
    
    # Calculate net edge
    net_edge = calculate_net_edge(signal_confidence, spread)
    
    # Decide if trade is worth it
    should_trade = net_edge >= min_net_edge
    
    # Adjust confidence based on liquidity conditions
    # Lower confidence when spreads are wide
    if spread > 0.05:  # High spread (> 5 cents)
        adjusted_confidence = max(0.5, signal_confidence)  # Reduce extreme confidence
    elif spread < 0.02:  # Low spread (< 2 cents)
        adjusted_confidence = signal_confidence  # Boost very slightly
    else:
        adjusted_confidence = signal_confidence
    
    return {
        'original_confidence': signal_confidence,
        'adjusted_confidence': adjusted_confidence,
        'net_edge': net_edge,
        'spread': spread,
        'min_net_edge': min_net_edge,
        'should_trade': should_trade,
        'calibration_timestamp': datetime.now().isoformat()
    }


def apply_spread_calibration(signals: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply spread calibration to a collection of signals.
    
    Args:
        signals: Dictionary with signal information including direction/confidence
        
    Returns:
        Updated signals dictionary with calibrated values
    """
    calibrated_signals = {}
    
    for signal_name, signal_data in signals.items():
        if 'direction' in signal_data and 'confidence' in signal_data:
            direction = signal_data['direction']
            confidence = signal_data.get('confidence', 0.5)
            series_ticker = signal_data.get('series_ticker', '')
            market_type = signal_data.get('market_type', 'HIGH')
            threshold = signal_data.get('threshold', 80)  # Default threshold
            
            if series_ticker:
                calibration = calibrate_signal_with_spread(
                    confidence, series_ticker, market_type, threshold
                )
                
                calibrated_signals[signal_name] = {
                    **signal_data,
                    'original_confidence': confidence,
                    'calibrated_confidence': calibration['adjusted_confidence'],
                    'net_edge': calibration['net_edge'],
                    'should_trade': calibration['should_trade'],
                    'spread': calibration['spread']
                }
            else:
                # If no series ticker available, keep original signal data
                calibrated_signals[signal_name] = signal_data
        else:
            calibrated_signals[signal_name] = signal_data
    
    return calibrated_signals


# Helper function to integrate with paper trading engine
def get_kalshi_series_ticker(station: str) -> str:
    """
    Helper to construct the Kalshi series ticker for a station.
    E.g., 'KJFK' -> 'KXHIGHKJFK' or 'KXLOWKJFK'
    """
    station_code = station[1:] if station.startswith('K') else station
    # This is just a default - in practice, we'd look this up
    return f"KXHIGH{station_code}"  # Default to HIGH series


if __name__ == "__main__":
    # Test the functions when run directly (for development only)
    print("Testing spread calibration...")
    result = calibrate_signal_with_spread(
        signal_confidence=0.65,
        series_ticker="KXHIGHKATL",
        market_type="HIGH", 
        threshold=80
    )
    print(f"Calibration result: {result}")