"""
Core module for Kalshi spread momentum co-signal.
Tracks spread delta and midpoint delta over time, adjusting confidence
based on market behavior consensus.

Part of Phase 7 - Kalshi API Integration.
"""

import os
import time
import requests
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

# In-memory cache for historical spread data
_SPREAD_HISTORY_DB: Dict[str, List[Dict[str, any]]] = {}
_SPREAD_DB_LOCK = threading.RLock()


def get_current_market_state(series_ticker: str, market_type: str, threshold: int) -> Optional[Dict[str, any]]:
    """
    Get current market state (bid, ask, spread, midpoint) for a Kalshi market.
    Uses new API fields 'yes_bid_dollars' and 'yes_ask_dollars'.
    """
    try:
        from datetime import datetime
        today = datetime.now()
        date_str = today.strftime('%y%m%d')
        
        # Construct full ticker for this market
        full_ticker = f"{series_ticker}-{date_str}-{threshold}"
        
        base_url = os.getenv("KALSHI_PUBLIC_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
        url = f"{base_url}/markets/{full_ticker}"
        
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
            
        market_data = response.json().get('market', {})
        
        # Use the new dollar-based pricing fields
        bid_str = market_data.get('yes_bid_dollars')
        ask_str = market_data.get('yes_ask_dollars')
        
        if bid_str is not None and ask_str is not None:
            try:
                bid = float(bid_str)
                ask = float(ask_str)
                
                if bid > ask or bid < 0 or ask > 1:
                    return None  # Invalid state
                    
                spread = ask - bid
                midpoint = (bid + ask) / 2.0
                
                return {
                    'bid': bid,
                    'ask': ask,
                    'spread': spread,
                    'midpoint': midpoint,
                    'timestamp': datetime.now().isoformat(),
                    'last_price': market_data.get('last_price_dollars'),
                    'volume_24h': market_data.get('volume_24h', 0)
                }
            except (ValueError, TypeError):
                return None
        else:
            # Fallback to old format
            bid_raw = market_data.get('yes_bid')
            ask_raw = market_data.get('yes_ask')
            
            if bid_raw is not None and ask_raw is not None:
                bid = bid_raw / 100.0
                ask = ask_raw / 100.0
                if bid > ask or bid < 0 or ask > 1:
                    return None
                    
                spread = ask - bid
                midpoint = (bid + ask) / 2.0
                
                return {
                    'bid': bid,
                    'ask': ask,
                    'spread': spread,
                    'midpoint': midpoint,
                    'timestamp': datetime.now().isoformat(),
                    'last_price': market_data.get('last_price') / 100.0,
                    'volume_24h': market_data.get('volume_24h', 0)
                }
    
    except Exception as e:
        print(f"Error getting market state for {full_ticker}: {str(e)}")
        return None


def store_spread_data(market_key: str, data_point: Dict[str, any]):
    """
    Store spread/midpoint data point in the in-memory database for this market.
    Keep only last 20 data points for performance.
    """
    global _SPREAD_HISTORY_DB
    
    with _SPREAD_DB_LOCK:
        if market_key not in _SPREAD_HISTORY_DB:
            _SPREAD_HISTORY_DB[market_key] = []
        
        _SPREAD_HISTORY_DB[market_key].append(data_point)
        
        # Keep only last 20 data points
        _SPREAD_HISTORY_DB[market_key] = _SPREAD_HISTORY_DB[market_key][-20:]


def get_recent_spread_history(market_key: str, minutes_back: int = 60) -> List[Dict[str, any]]:
    """
    Retrieve recent spread history for a market within the specified time window.
    """
    global _SPREAD_HISTORY_DB
    
    cutoff_time = datetime.now() - timedelta(minutes=minutes_back)
    
    with _SPREAD_DB_LOCK:
        history = _SPREAD_HISTORY_DB.get(market_key, [])
        # Filter by timestamp to get only recent entries
        recent_data = [
            dp for dp in history 
            if datetime.fromisoformat(dp['timestamp'].replace('Z', '+00:00')) > cutoff_time
        ]
        return recent_data


def sample_kalshi_order_book(station: str, market_type: str, threshold: int) -> Optional[Dict[str, any]]:
    """
    Sample the Kalshi order book for a specific market at the current moment.
    This function handles market-specific identification and updates history.
    """
    # Extract station code from station (e.g., "KJFK" -> "JFK")
    station_code = station[1:] if station.startswith('K') else station
    
    # Construct the series ticker based on market type and station code
    series_ticker = f"KX{market_type}{station_code}" if len(station_code) <= 3 else f"KX{market_type}{station_code[-3:].upper()}"
    
    # Handle special longer station codes for some US cities
    code_mappings = {
        "ATLANTA": "ATL",  # Atlanta might appear differently
        "AUSTIN": "AUS",   # Austin
        "CHARLOTTE": "CLT", # Charlotte
    }
    
    # Normalize unusual station naming
    base_station = f"K{station_code.upper()}"
    
    # Standard series ticker
    normalized_st = f"KX{market_type}{station_code.upper()}"
    
    market_key = f"{base_station}-{market_type}-{threshold}"
    
    current_state = get_current_market_state(normalized_st, market_type, threshold)
    
    if current_state:
        store_spread_data(market_key, current_state)
        return current_state
    
    return None


def calculate_spread_and_midpoint_momentum(station: str, market_type: str, threshold: int) -> Tuple[float, float]:
    """
    Calculate the trend in spread and midpoint over time for the given market.
    
    Returns:
        Tuple of (spread_delta, midpoint_delta) representing directional momentum
    """
    market_key = f"{station}-{market_type}-{threshold}"
    
    history = get_recent_spread_history(market_key, minutes_back=60)  # Last hour
    
    if len(history) < 2:
        # Insufficient history, return neutral deltas
        return 0.0, 0.0
    
    # Take latest and earliest available readings
    first_point = history[0]  # Earliest
    last_point = history[-1]  # Latest
    
    try:
        spread_delta = last_point['spread'] - first_point['spread']
        midpoint_delta = last_point['midpoint'] - first_point['midpoint']
    except KeyError:
        # Data format may be incomplete
        return 0.0, 0.0
    
    # Return the momentum values
    return spread_delta, midpoint_delta


def adjust_confidence_with_market_signal(
    station: str, 
    market_type: str, 
    original_confidence: float,
    threshold: int = 80
) -> float:
    """
    Main function to adjust confidence based on market behavior momentum.
    
    If the market moves away from our prediction with conviction, reduce confidence.
    If the market confirms our prediction with narrowing spread and movement toward, increase confidence.
    
    Args:
        station: Station identifier (e.g., 'KJFK')
        market_type: 'HIGH' or 'LOW' market type
        original_confidence: Original signal confidence (0.0 to 1.0)
        threshold: Strike price threshold (e.g., 80 for 80°F contract)
        
    Returns:
        Adjusted confidence after market behavior analysis
    """
    # Sample current state and add to history
    current_state = sample_kalshi_order_book(station, market_type, threshold)
    
    if not current_state:
        # Cannot adjust without market data, return original
        return original_confidence
        
    # Calculate momentum
    spread_delta, midpoint_delta = calculate_spread_and_midpoint_momentum(station, market_type, threshold)
    
    # Determine adjustment based on market behavior and our original signal
    adjustment_factor = 0.0
    
    # If we have a LONG signal (we think temp will be above threshold)
    # - If midpoint is MOVING AWAY from our belief (toward 0.5 instead of toward 1.0) - reduce confidence
    # - If spread is WIDENING (more uncertainty) - consider reducing confidence  
    # - If midpoint is MOVING TOWARD our belief and spread is NARROWING - increase confidence
    
    # The original signal direction is implicit in the confidence:
    # If confidence > 0.5, we're predicting above threshold (LONG for YES)
    # If confidence < 0.5, we're predicting below threshold (SHORT for YES)
    is_long_signal = original_confidence > 0.5
    
    # Calculate adjustments based on momentum
    # Market moving against prediction
    if (is_long_signal and midpoint_delta < 0) or (not is_long_signal and midpoint_delta > 0):
        # Market is moving against our prediction
        if abs(midpoint_delta) > 0.02:  # Significant movement (>2 cents)
            adjustment_factor = -0.15  # Reduce confidence by 15%
    
    # Market confirming prediction
    elif (is_long_signal and midpoint_delta > 0) or (not is_long_signal and midpoint_delta < 0):
        # Market is moving with our prediction
        if abs(midpoint_delta) > 0.02 and spread_delta < 0:  # Confirmation with tightening
            adjustment_factor = 0.12  # Increase confidence by 12%
        elif abs(midpoint_delta) > 0.01:  # Just minor confirmation
            adjustment_factor = 0.05
    
    # High spread indicates uncertainty, reduce confidence slightly
    if current_state['spread'] > 0.08:  # Over 8 cents spread
        adjustment_factor -= 0.05
    elif current_state['spread'] > 0.05:  # Over 5 cents spread
        adjustment_factor -= 0.025
    
    # Apply adjustment
    adjusted_confidence = original_confidence + adjustment_factor
    
    # Maintain bounds (0.01 to 0.99)
    min_conf = 0.01
    max_conf = 0.99
    adjusted_confidence = max(min_conf, min(max_conf, adjusted_confidence))
    
    return adjusted_confidence


def get_complete_market_behavior_analysis(
    station: str, 
    market_type: str, 
    original_confidence: float,
    threshold: int = 80
) -> Dict[str, any]:
    """
    Get complete analysis including raw momentum data, confidence adjustment, and recommendations.
    """
    # First, retrieve or sample current state
    current_state = sample_kalshi_order_book(station, market_type, threshold)
    
    if not current_state:
        return {
            'error': 'Could not retrieve market data',
            'original_confidence': original_confidence,
            'adjusted_confidence': original_confidence,
            'status': 'failure'
        }
    
    # Calculate deltas
    spread_delta, midpoint_delta = calculate_spread_and_midpoint_momentum(station, market_type, threshold)
    
    # Perform adjustment
    adjusted_confidence = adjust_confidence_with_market_signal(
        station, market_type, original_confidence, threshold
    )
    
    # Calculate additional metrics
    confidence_adjustment = adjusted_confidence - original_confidence
    abs_confidence_movement = abs(confidence_adjustment)
    
    # Behavior classification based on spread_delta and midpoint_delta
    behavior_type = classify_market_behavior(spread_delta, midpoint_delta, original_confidence)
    
    return {
        'station': station,
        'market_type': market_type,
        'threshold': threshold,
        'timestamp': datetime.now().isoformat(),
        
        'original_confidence': original_confidence,
        'adjusted_confidence': adjusted_confidence,
        'confidence_adjustment': confidence_adjustment,
        'abs_confidence_movement': abs_confidence_movement,
        
        'spread_delta': spread_delta,
        'midpoint_delta': midpoint_delta,
        'current_spread': current_state['spread'],
        'current_midpoint': current_state['midpoint'],
        
        'market_behavior_type': behavior_type,
        'analysis_recommendation': get_adjustment_recommendation(behavior_type, confidence_adjustment),
        
        'price_action_consistency': calculate_price_action_consistency(
            original_confidence, current_state['midpoint'], midpoint_delta
        ),
        
        'status': 'success'
    }


def classify_market_behavior(spread_delta: float, midpoint_delta: float, original_confidence: float) -> str:
    """
    Classify market behavior based on spread and midpoint movements.
    """
    is_predicting_high = original_confidence > 0.5
    
    if is_predicting_high:
        if midpoint_delta > 0 and spread_delta < 0:  # Moving toward prediction with tightening spread
            return 'CONFIRMATION_STRONG'
        elif midpoint_delta > 0 and spread_delta >= 0:  # Moving toward prediction, neutral spread
            return 'CONFIRMATION_MODERATE'
        elif midpoint_delta < 0:  # Moving away from prediction
            if spread_delta > 0:  # And with widening spread
                return 'CONTRACTION_WITH_CERTAINTY'
            else:
                return 'CONTRARIAN_MOVEMENT'
    else:  # Predicting low/short
        if midpoint_delta < 0 and spread_delta < 0:  # Moving toward prediction with tightening spread
            return 'CONFIRMATION_STRONG'
        elif midpoint_delta < 0 and spread_delta >= 0:  # Moving toward prediction, neutral spread
            return 'CONFIRMATION_MODERATE'
        elif midpoint_delta > 0:  # Moving away from prediction
            if spread_delta > 0:  # And with widening spread
                return 'CONTRACTION_WITH_CERTAINTY'
            else:
                return 'CONTRARIAN_MOVEMENT'
    
    return 'NEUTRAL_OR_INSUFFICIENT_DATA'


def get_adjustment_recommendation(behavior_type: str, confidence_adjustment: float) -> str:
    """
    Get a narrative recommendation based on behavior type and adjustment.
    """
    if behavior_type == 'CONFIRMATION_STRONG':
        return 'Strong market agreement: Consider increasing position size.'
    elif behavior_type == 'CONFIRMATION_MODERATE':
        return 'Moderate market agreement: Confidence slightly bolstered.'
    elif behavior_type == 'CONTRACTION_WITH_CERTAINTY':
        return 'Market disagreeing with high conviction: Consider reducing exposure.'
    elif behavior_type == 'CONTRARIAN_MOVEMENT':
        return 'Market moving oppositely: Consider re-evaluation or reducing confidence.'
    else:
        return 'Insufficient data to determine market sentiment.'


def calculate_price_action_consistency(original_confidence: float, current_midpoint: float, midpoint_delta: float) -> float:
    """
    Calculate how consistent the recent price action is with the original signal.
    Values closer to 1.0 indicate more consistent behavior.
    """
    # Direction prediction: higher confidence means expecting higher midpoint values
    expected_direction = 1 if original_confidence > 0.5 else -1
    actual_direction = 1 if midpoint_delta > 0 else -1 if midpoint_delta < 0 else 0
    
    # Consistency metric
    if expected_direction == actual_direction:
        consistency = 0.8  # High consistency when directions align
    elif expected_direction == -actual_direction:
        consistency = 0.2  # Low consistency when directions oppose
    else:
        consistency = 0.5  # Neutral if no directional change
    
    # Factor in the relative position to fair value (0.5)
    midpoint_distance_from_fair = abs(current_midpoint - 0.5)
    fair_value_alignment = 0.8 if (current_midpoint > 0.5) == (original_confidence > 0.5) else 0.4
    
    final_consistency = (consistency + fair_value_alignment) / 2.0
    return final_consistency


# Periodic cleanup function to prevent memory bloat
def cleanup_old_spread_data(max_age_minutes: int = 120):
    """
    Remove data points older than specified age.
    Should be called periodically.
    """
    global _SPREAD_HISTORY_DB
    
    cutoff_time = datetime.now() - timedelta(minutes=max_age_minutes)
    
    with _SPREAD_DB_LOCK:
        for market_key, data_points in _SPREAD_HISTORY_DB.items():
            _SPREAD_HISTORY_DB[market_key] = [
                dp for dp in data_points 
                if datetime.fromisoformat(dp['timestamp'].replace('Z', '+00:00')) > cutoff_time
            ]