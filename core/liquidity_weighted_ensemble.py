# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
#


"""
Core module for liquidity-weighted ensemble voting.
Weights each signal's vote by its accuracy multiplied by market liquidity.

Part of Phase 7 - Kalshi API Integration.
"""

import os
import requests
import math
from datetime import datetime, timezone
from typing import Dict, List, Tuple


# Load signal accuracy from audit file
SIGNAL_ACCURACY = {
    'calendar_climatology': 0.6935,
    'gaussian': 0.6666,
    'pressure_delta': 0.6074,
    'forecast_disagreement': 0.6495,
    'wind_direction_shift': 0.5886,
    'persistence': 0.5095,
    # Other signals not used in ensemble, excluded for performance
}


def get_kalshi_liquidity_metrics(series_ticker: str, market_type: str) -> Dict[str, float]:
    """
    Get liquidity metrics from Kalshi API including 24h volume and spread.
    Uses the new 'yes_bid_dollars'/'yes_ask_dollars' API fields.
    
    Args:
        series_ticker: Series ticker like 'KXHIGHKATL'
        market_type: 'HIGH', 'LOW', or 'HOURLY'
        
    Returns:
        Dict containing liquidity indicators
    """
    try:
        base_url = os.getenv("KALSHI_PUBLIC_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
        url = f"{base_url}/markets?series={series_ticker}&limit=20"
        
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            return {'volume_24h': 0.0, 'avg_daily_volume': 50.0, 'spread': 0.05}
        
        data = response.json()
        markets = data.get('markets', [])
        
        if not markets:
            return {'volume_24h': 0.0, 'avg_daily_volume': 50.0, 'spread': 0.05}
        
        # Aggregate liquidity measures
        total_volume = 0.0
        spread_sum = 0.0
        spread_count = 0
        active_markets = 0
        
        for market in markets:
            # Filter by market type if specified
            if market_type == 'HIGH' and 'HIGH' not in market.get('ticker', '').upper():
                continue
            if market_type == 'LOW' and 'LOW' not in market.get('ticker', '').upper():
                continue
            elif market_type == 'HIGH' and 'HIGH' not in market.get('ticker', '').upper() and 'LOW' not in market.get('ticker', '').upper():
                # For mixed type, just take the market regardless
                pass
                
            volume_24h = market.get('volume_24h', 0)
            total_volume += volume_24h
            active_markets += 1
            
            # Calculate spread from dollar amounts
            bid_dollars = market.get('yes_bid_dollars')
            ask_dollars = market.get('yes_ask_dollars')
            
            if bid_dollars is not None and ask_dollars is not None:
                try:
                    bid = float(bid_dollars)
                    ask = float(ask_dollars)
                    if bid <= ask:
                        spread = ask - bid
                        if spread >= 0:
                            spread_sum += spread
                            spread_count += 1
                except (ValueError, TypeError):
                    pass
            else:
                # Fallback to old format
                bid = market.get('yes_bid')
                ask = market.get('yes_ask')
                if bid is not None and ask is not None:
                    spread = (ask - bid) / 100.0
                    if spread >= 0:
                        spread_sum += spread
                        spread_count += 1
                        
        avg_spread = spread_sum / spread_count if spread_count > 0 else 0.05
        avg_daily_vol = total_volume / len(markets) if markets else 50.0
        
        return {
            'volume_24h': total_volume,
            'avg_daily_volume': avg_daily_vol,
            'spread': avg_spread,
            'active_markets': active_markets
        }
    
    except Exception as e:
        print(f"Error retrieving liquidity metrics for {series_ticker}: {str(e)}")
        return {'volume_24h': 0.0, 'avg_daily_volume': 50.0, 'spread': 0.05}


def get_liquidity_weighted_vote(station: str, date: str, signals: Dict[str, Dict]) -> Tuple[str, float]:
    """
    Generate liquidity-weighted ensemble vote based on individual signal performances,
    weighted by signal accuracy and market liquidity.
    
    Args:
        station: Station identifier like 'KJFK'
        date: Trading date in YYYY-MM-DD format
        signals: Dict of signal name -> signal data (includes direction, confidence)
        
    Returns:
        Tuple of (direction: str, confidence: float) with liquidity-weighted averaging
    """
    if not signals:
        return 'NONE', 0.55
    
    # Extract station code from station name (e.g., KJFK -> JFK)
    station_code = station[1:] if station.startswith('K') else station
    
    # Form series ticker from station and market type
    # Assume we'll trade both HIGH and LOW markets for this station
    series_ticker = f"KXHIGH{station_code}"  # Default to HIGH series for liquidity
    liquidity_metrics = get_kalshi_liquidity_metrics(series_ticker, 'HIGH')
    
    # Calculate weights based on signal accuracy and market liquidity
    liquidity_factor = 1.0 - liquidity_metrics.get('spread', 0.05) / 2.0  # Inverse to spread
    
    votes = []
    weights = []
    
    for sig_name, sig_data in signals.items():
        if sig_name not in SIGNAL_ACCURACY:
            # Skip signals not in our accuracy report
            continue
            
        if 'confidence' not in sig_data or 'direction' not in sig_data:
            continue
            
        # Get signal metrics
        confidence = sig_data['confidence']
        direction = sig_data['direction']
        
        # Skip neutral signals
        if direction.upper() == 'NONE':
            continue
            
        # Get signal accuracy
        signal_accuracy = SIGNAL_ACCURACY[sig_name]
        
        # Calculate final weight: accuracy * liquidity_factor (avoid overconfidence)
        weight = signal_accuracy * liquidity_factor
        
        votes.append({'direction': direction, 'confidence': confidence})
        weights.append(weight)
    
    if not votes or not weights:
        return 'NONE', 0.55
    
    # Apply weighting and compute aggregated result
    total_weight = sum(weights)
    if total_weight == 0:
        return 'NONE', 0.55
    
    # Normalize weights
    norm_weights = [w / total_weight for w in weights]
    
    # Calculate direction bias (LONG=+1, SHORT=-1, NEUTRAL=0)
    direction_votes = []
    conf_sum = 0.0
    weighted_conf_sum = 0.0
    
    for i, vote in enumerate(votes):
        direction = vote['direction']
        confidence = vote['confidence']
        weight = norm_weights[i]
        
        conf_sum += confidence
        weighted_conf_sum += confidence * weight
        
        if direction.upper() in ['LONG', 'BUY', 'UP']:
            direction_votes.append(1)  # LONG vote
        elif direction.upper() in ['SHORT', 'SELL', 'DOWN']:
            direction_votes.append(-1)  # SHORT vote
        else:
            direction_votes.append(0)  # Neutral
    
    # Calculate weighted majority direction
    direction_score = sum(dir * wgt for dir, wgt in zip(direction_votes, norm_weights))
    avg_confidence = weighted_conf_sum
    
    # Map direction_score to final direction and confidence
    final_confidence = abs(direction_score) * avg_confidence
    
    if direction_score > 0:
        final_direction = 'LONG'
        # Boost confidence slightly if multiple signals agree
        final_confidence = min(0.95, max(0.55, final_confidence + 0.05))
    elif direction_score < 0:
        final_direction = 'SHORT'
        final_confidence = min(0.95, max(0.55, final_confidence + 0.05))
    else:
        final_direction = 'NONE'
        final_confidence = 0.55  # House edge
    
    return final_direction, min(0.95, final_confidence)


def get_signal_weight(
    signal_name: str, 
    liquidity_metrics: Dict[str, float]
) -> float:
    """
    Calculate weight for an individual signal based on accuracy and liquidity.
    
    Args:
        signal_name: Name of signal (as in SIGNAL_ACCURACY dict)
        liquidity_metrics: Pre-fetched liquidity metrics
        
    Returns:
        Weight value for the signal (0.0 to 1.0)
    """
    accuracy = SIGNAL_ACCURACY.get(signal_name, 0.5)
    liquidity_factor = 1.0 - liquidity_metrics.get('spread', 0.05) / 2.0
    volume_factor = min(1.0, liquidity_metrics.get('avg_daily_volume', 50.0) / 200.0)  # Normalize volume scale
    
    # Combine factors with accuracy being most important
    weight = accuracy * liquidity_factor * (0.7 + 0.3 * volume_factor)  # Volume adds stability
    
    return max(0.01, weight)  # Minimum weight for all signals


def generate_ensemble_prediction(
    station: str,
    date: str, 
    individual_signals: Dict[str, Dict[str, any]]
) -> Dict[str, any]:
    """
    High-level function to generate complete ensemble prediction with all metrics.
    
    Args:
        station: Station code
        date: Trading date
        individual_signals: Individual signal outputs with confidence/direction
        
    Returns:
        Dictionary with ensemble decision and metrics
    """
    # Get weights from liquidity metrics
    station_code = station[1:] if station.startswith('K') else station
    series_ticker = f"KXHIGH{station_code}"
    liquidity_metrics = get_kalshi_liquidity_metrics(series_ticker, 'HIGH')
    
    # Generate weighted vote
    direction, confidence = get_liquidity_weighted_vote(station, date, individual_signals)
    
    # Prepare detailed metrics
    signal_weights = {}
    for sig_name in individual_signals.keys():
        if sig_name in SIGNAL_ACCURACY:
            signal_weights[sig_name] = get_signal_weight(sig_name, liquidity_metrics)
    
    return {
        'station': station,
        'date': date,
        'ensemble_direction': direction,
        'ensemble_confidence': confidence,
        'liquidity_metrics': liquidity_metrics,
        'individual_signal_weights': signal_weights,
        'final_decision': direction if confidence > 0.60 else 'NONE',
        'decision_confidence': confidence if direction != 'NONE' else 0.0,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }