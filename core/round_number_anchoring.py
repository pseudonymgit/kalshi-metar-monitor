"""
Core module for round-number anchoring analysis - compares climatological probability
vs. market-implied probability at round thresholds, identifying arbitrage opportunities.

Part of Phase 7 - Kalshi API Integration.
"""

import os
import math
import requests
import sqlite3
from datetime import datetime, timedelta
from typing import Tuple, Optional


def get_climatological_probability(station: str, date: str, threshold: int, market_type: str) -> float:
    """
    Calculate the climatological probability of reaching a temperature threshold
    based on historical METAR data for the same date/day of year.
    
    Args:
        station: Station identifier (e.g., 'KJFK')
        date: Target date in YYYY-MM-DD format
        threshold: Temperature threshold (e.g., 80, 85, 90)
        market_type: 'HIGH' or 'LOW' market type
        
    Returns:
        Probability (0.0 to 1.0) from historical data
    """
    try:
        # Connect to historical METAR database
        db_path = os.getenv('METAR_DB_PATH', '/var/lib/weather/metars.db')
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        
        try:
            cursor = conn.cursor()
            # Get same month-day historically for the past N years
            target_date = datetime.strptime(date, '%Y-%m-%d')
            month = target_date.month
            day = target_date.day
            
            # Query for historical data - get temperatures from same day/month across multiple years
            if market_type == 'HIGH':
                # For HIGH markets, we want P(temp_high >= threshold)
                query = """
                SELECT DISTINCT date, high_temp 
                FROM daily_temps 
                WHERE station = ? 
                AND substr(date, 5, 2) = ?  -- month portion
                AND substr(date, 7, 2) = ?  -- day portion  
                AND high_temp IS NOT NULL
                LIMIT 20  -- Last 20 years maximum
                """
                cursor.execute(query, (station, f"{month:02d}", f"{day:02d}"))
                
            elif market_type == 'LOW':
                # For LOW markets, we want P(temp_low <= threshold)  
                query = """
                SELECT DISTINCT date, low_temp
                FROM daily_temps 
                WHERE station = ? 
                AND substr(date, 5, 2) = ?  -- month portion
                AND substr(date, 7, 2) = ?  -- day portion
                AND low_temp IS NOT NULL
                LIMIT 20  -- Last 20 years maximum
                """
                cursor.execute(query, (station, f"{month:02d}", f"{day:02d}"))
            else:
                return 0.0
                
            results = cursor.fetchall()
            if len(results) == 0:
                # Return neutral probability if no historical data
                return 0.5
            
            # Count favorable outcomes based on market type
            favor_count = 0
            total_count = 0
            
            for date_found, temp in results:
                if temp is not None:
                    if market_type == 'HIGH' and temp >= threshold:
                        favor_count += 1
                    elif market_type == 'LOW' and temp <= threshold:
                        favor_count += 1
                    total_count += 1
                    
            if total_count > 0:
                return favor_count / total_count
            else:
                return 0.5
                
        finally:
            conn.close()
            
    except Exception as e:
        print(f"Error calculating climatological probability: {str(e)}")
        return 0.5  # Neutral if error


def get_kalshi_midpoint_price(series_ticker: str, market_type: str, threshold: int) -> Optional[float]:
    """
    Get the midpoint price of a Kalshi contract for the specified market type and threshold.
    Uses the new API field 'yes_bid_dollars' and 'yes_ask_dollars'.
    
    Args:
        series_ticker: Series ticker like 'KXHIGHKATL'
        market_type: 'HIGH' or 'LOW'  
        threshold: Strike price threshold like 80, 85, etc.
        
    Returns:
        Midpoint price as float (0.0 to 1.0), or None if not available
    """
    try:
        # Construct individual market ticker
        # Format: KXHIGHKATL-{YYYYMMDD}-{strike}
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
            return None
            
        market_data = response.json()['market']
        
        # Use the new dollar-based pricing fields
        bid_str = market_data.get('yes_bid_dollars')
        ask_str = market_data.get('yes_ask_dollars')
        
        # Convert from strings to float
        if bid_str is not None and ask_str is not None:
            try:
                bid = float(bid_str)
                ask = float(ask_str)
                
                if bid == 0.0 and ask == 0.0:
                    return None  # No active market yet
                    
                # Compute midpoint
                midpoint = (bid + ask) / 2.0
                return max(0.01, min(0.99, midpoint))  # Clamp to realistic bounds
            except (ValueError, TypeError):
                return None
        else:
            # Fallback to old integer fields if new ones missing
            bid = market_data.get('yes_bid')
            ask = market_data.get('yes_ask')
            
            if bid is not None and ask is not None:
                midpoint = (bid / 100 + ask / 100) / 2.0
                return max(0.01, min(0.99, midpoint))
    
        return None
        
    except Exception as e:
        print(f"Error getting Kalshi midpoint for {full_ticker}: {str(e)}")
        return None


def check_round_number_arbitrage(station: str, date: str) -> Tuple[str, float, float]:
    """
    Compare climatological probability vs. market-implied probability at round thresholds,
    identifying arbitrage opportunities.
    
    Args:
        station: Station identifier like 'KJFK'
        date: Target date in YYYY-MM-DD format
        
    Returns:
        Tuple of (direction: 'LONG'|'SHORT'|'NONE', confidence: float, edge: float)
    """
    # Determine what market type this station participates in based on naming convention
    # Extract Kalshi station code from station - for KATL, code might be "TATL", etc.
    station_code = station[1:]  # Remove K prefix 
    
    # Define round thresholds to analyze
    thresholds = [80, 85, 90, 95, 100]  # Only check meaningful thresholds
    
    # For HIGH markets
    total_climat_probs = []
    total_market_probs = []
    total_edges = []
    
    for thres in thresholds:
        # Form series ticker for this station and market type
        high_series_ticker = f"KXHIGH{station_code}"
        low_series_ticker = f"KXLOW{station_code}"
        
        # Get both probabilities
        climat_prob = get_climatological_probability(station, date, thres, 'HIGH')
        market_prob = get_kalshi_midpoint_price(high_series_ticker, 'HIGH', thres)

        if market_prob is not None:
            # Calculate edge: if climatological prob > market_prob, market undervaluing (go LONG)
            # if climatological prob < market_prob, market overvaluing (go SHORT)
            price_diff = climat_prob - market_prob
            edge = abs(price_diff)  # Magnitude of mispricing
            
            if abs(price_diff) > 0.05:  # More than 5% difference triggers action
                if price_diff > 0:
                    # Climatological suggests outcome is MORE likely (LONG)
                    return 'LONG', max(0.5, climat_prob), edge
                else:
                    # Climatological suggests outcome is LESS likely (SHORT)
                    return 'SHORT', 1.0 - max(0.5, market_prob), edge
                
            # Accumulate for overall assessment
            total_climat_probs.append(climat_prob)
            total_market_probs.append(market_prob)
            total_edges.append(edge)
    
    # Same logic for LOW markets
    for thres in thresholds:
        climat_prob = get_climatological_probability(station, date, thres, 'LOW')  
        market_prob = get_kalshi_midpoint_price(f"KXLOW{station_code}", 'LOW', thres)

        if market_prob is not None:
            price_diff = climat_prob - market_prob
            edge = abs(price_diff)
            
            if abs(price_diff) > 0.05:  # 5% threshold
                if price_diff > 0:
                    return 'LONG', max(0.5, climat_prob), edge
                else:
                    return 'SHORT', 1.0 - max(0.5, market_prob), edge
                
            total_climat_probs.append(climat_prob)
            total_market_probs.append(market_prob)
            total_edges.append(edge)
    
    # If no strong single-threshold opportunity, return aggregate view
    if total_climat_probs and total_market_probs:
        avg_climat = sum(total_climat_probs) / len(total_climat_probs)
        avg_market = sum(total_market_probs) / len(total_market_probs)
        avg_edge = sum(total_edges) / len(total_edges) if total_edges else 0
        
        price_diff = avg_climat - avg_market
        if abs(price_diff) > 0.025:  # Smaller aggregate threshold
            if price_diff > 0:
                return 'LONG', max(0.5, avg_climat), avg_edge
            else:
                return 'SHORT', 1.0 - max(0.5, avg_market), avg_edge

    # No significant arbitrage found
    return 'NONE', 0.55, 0.0  # Return neutral with slight house edge