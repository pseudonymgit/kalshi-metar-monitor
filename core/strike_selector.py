#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
#

"""
Strike Selector v1.0 — Phase 6.5 Volume-Weighted Strike Selection

Selects optimal strike price based on volume and liquidity metrics from Kalshi API.
Uses market depth and liquidity indicators rather than prediction or AI models.

API endpoint: api.elections.kalshi.com/trade-api/v2
"""

import logging
import requests
import json
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
import math
import time

_LOGGER = logging.getLogger(__name__)

# Cache for order book data (in-memory only for simplicity)
_ORDER_BOOK_CACHE = {}
_CACHE_TIMEOUT = 300  # 5 minutes cache timeout


class StrikeSelector:
    """
    Selects optimal strike price for Kalshi markets based on volume and liquidity.
    Focuses on market depth rather than prediction to find liquid strike prices.
    """
    
    def __init__(self, base_url: str = "https://api.elections.kalshi.com/trade-api/v2", 
                 timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Weather-Engine-Strike-Selector/6.5.0',
            'Accept': 'application/json',
        })
        self._rate_limit_tracker = {}
    
    def _check_rate_limit(self, endpoint: str) -> bool:
        """Check if we're rate limited for an endpoint."""
        now = time.time()
        if endpoint in self._rate_limit_tracker:
            last_call, delay = self._rate_limit_tracker[endpoint]
            if now - last_call < delay:
                return True  # Rate limited
        return False
    
    def _update_rate_limit(self, endpoint: str, delay: float = 1.0):
        """Update rate limit tracking for an endpoint."""
        self._rate_limit_tracker[endpoint] = (time.time(), delay)
    
    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make an authenticated request to the Kalshi API."""
        url = f"{self.base_url}{endpoint}"
        
        # Rate limiting
        if self._check_rate_limit(endpoint):
            delay_needed = 1.0 - (time.time() - self._rate_limit_tracker[endpoint][0])
            if delay_needed > 0:
                time.sleep(delay_needed)
        
        try:
            response = self.session.request(method, url, params=params, timeout=self.timeout)
            self._update_rate_limit(endpoint, 1.0 if response.status_code == 200 else 0.1)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                _LOGGER.warning(f"Rate limited (429) for endpoint {endpoint}, retrying after delay...")
                time.sleep(5)
                return None
            else:
                _LOGGER.error(f"Kalshi API request failed: {response.status_code} - {response.text}")
                return None
        except requests.RequestException as e:
            _LOGGER.error(f"Request failed to {url}: {str(e)}")
            return None
        except Exception as e:
            _LOGGER.error(f"Unexpected error in API request to {url}: {str(e)}")
            return None
    
    def _get_cache_key(self, series_ticker: str, strike_price: int, market_type: str) -> str:
        """Generate a cache key for order book data."""
        return f"{series_ticker}:{strike_price}:{market_type}"
    
    def _get_cached_order_book(self, cache_key: str) -> Optional[Dict]:
        """Get cached order book data if not expired."""
        if cache_key in _ORDER_BOOK_CACHE:
            data, timestamp = _ORDER_BOOK_CACHE[cache_key]
            if time.time() - timestamp < _CACHE_TIMEOUT:
                return data
            else:
                del _ORDER_BOOK_CACHE[cache_key]
        return None
    
    def _set_cache_order_book(self, cache_key: str, data: Dict):
        """Cache order book data with timestamp."""
        _ORDER_BOOK_CACHE[cache_key] = (data, time.time())
    
    def _validate_market_params(self, station: str, market_type: str, direction: str) -> bool:
        """Validate market parameters are supported."""
        if not market_type.upper() in ['HIGH', 'LOW']:
            return False
        if not direction.upper() in ['UP', 'DOWN']:
            return False
        if not station:
            return False
        return True
    
    def _get_station_code(self, station_icao: str) -> str:
        """
        Convert ICAO station code to Kalshi station code.
        For weather markets, Kalshi uses K + station code convention (KATL, KLAX, etc.)
        
        This can be enhanced with a comprehensive mapping.
        """
        # For weather contracts, use ICAO directly but ensure correct format
        icao = station_icao.upper()
        
        if not icao.startswith('K') and len(icao) == 4:
            # Assume US station, prepend K if not present
            return icao
        elif len(icao) == 3:
            # For stations that don't have K prefix, prepend it (this could be refined with a lookup)
            if icao in ['JFK', 'LGA', 'EWR']:
                return f"K{icao}"
            else:
                # Generic approach - most weather contracts in CONUS have K prefix
                return f"K{icao}"
        else:
            return icao
    
    def _get_series_ticker(self, market_type: str, station_icao: str) -> str:
        """Get series ticker for a given market type and station."""
        # For weather temperature markets
        # Convention: TEMPXX-{YYYYMMDD} where XX is HIGH/LOW, {YYYYMMDD} is settlement date 
        # But for current market selection we need series with "current" date
        # Simplified: We'll look for markets with matching station and type
        
        station_code = self._get_station_code(station_icao)
        market_prefix = market_type[:4].upper()  # Take first 4 chars: HIGH->HIGH, LOW->LOW  
        
        # Kalshi market pattern for weather is like: WX{MARKET}{STATION}
        # e.g., WXHIGHKATL, WXLOWKATL
        # But this might vary - let's make more flexible based on typical patterns
        # Common patterns for Kalshi weather markets include station code + type
        return f"TEMP{market_prefix}{station_code}"
    
    def get_available_markets(self, station_icao: str, market_type: str = "both") -> List[Dict[str, Any]]:
        """
        Get available markets for a particular station.
        market_type can be "HIGH", "LOW", or "both"
        """
        base_params = {
            'limit': 100,  # Get as many as possible for search
        }
        
        # Add market category filters - Kalshi has categories like weather,
        # so we can filter to increase relevance to weather contracts
        ticker_filter = self._get_station_code(station_icao)
        
        response = self._make_request("GET", "/markets", base_params)
        if not response:
            return []
        
        markets = response.get('markets', [])
        
        filtered_markets = []
        for mkt in markets:
            # Look for markets that match our criteria
            event_ticker_upper = mkt.get('event_ticker', '').upper()
            series_ticker_upper = mkt.get('series_ticker', '').upper()
            
            condition = (
                (ticker_filter in event_ticker_upper or ticker_filter in series_ticker_upper) and
                ('WEATHER' in event_ticker_upper or 'TEM' in event_ticker_upper or
                 'WX' in event_ticker_upper or 'TEMP' in event_ticker_upper) and
                mkt.get('status') == 'open'  # Only get open markets
            )

            if market_type.lower() != 'both':
                market_type_upper = market_type.upper() 
                condition = condition and (market_type_upper in event_ticker_upper or market_type_upper in series_ticker_upper)
    
            if condition:
                # Validate the market has proper structure
                if 'yes_bid' in mkt and 'yes_ask' in mkt:
                    filtered_markets.append(mkt)
        
        return filtered_markets
    
    def get_order_book(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get the current order book for a market."""
        # First try cache
        cache_key = self._get_cache_key(market_id, 0, "order_book")
        cached = self._get_cached_order_book(cache_key)
        if cached:
            return cached
        
        endpoint = f"/markets/{market_id}/orderbook"
        response = self._make_request("GET", endpoint)
        
        if response:
            self._set_cache_order_book(cache_key, response) 
            return response
        return None
    
    def calculate_strike_score(self, strike_data: Dict[str, Any]) -> float:
        """
        Calculate a liquidity score for a strike based on:
        - Available volume
        - Spread tightness
        - Order imbalances that might indicate movement
        """
        bid_size = strike_data.get('yes_bid_size', 0) + strike_data.get('no_bid_size', 0)
        ask_size = strike_data.get('yes_ask_size', 0) + strike_data.get('no_ask_size', 0)
        total_volume = bid_size + ask_size
        
        # Calculate spreads
        yes_spread = strike_data.get('yes_ask', 100) - strike_data.get('yes_bid', 0)
        no_spread = strike_data.get('no_ask', 100) - strike_data.get('no_bid', 0)
        avg_spread = (yes_spread + no_spread) / 2.0
        
        # Normalize spread (higher scores for tighter spreads, capped)
        # Max penalty for spreads wider than ~20 cents
        normalized_spread_factor = max(0.1, 1.0 - min(avg_spread / 20.0, 1.0))
        
        # Use total volume as base liquidity indicator
        # Log volume to reduce skew from very large trades
        if total_volume > 0:
            liquidity_factor = math.log10(total_volume + 1)  # +1 to avoid log(0)
        else:
            liquidity_factor = 0.1  # Low but non-zero
        
        # Calculate a composite score
        score = liquidity_factor * normalized_spread_factor
        
        return score, total_volume, avg_spread
    
    def select_best_strike(self, station: str, market_type: str, direction: str) -> Optional[int]:
        """
        Select the optimal strike price for a given direction.
        
        This implementation focuses on market depth and liquidity rather than
        trying to predict which strike will be "correct".
        
        Args:
            station: Station identifier (e.g., "ATL", "KATL")  
            market_type: "HIGH" or "LOW"
            direction: "UP" or "DOWN" indicating predicted movement
        Returns:
            Best strike price in the specified direction or None if no good option
        """
        if not self._validate_market_params(station, market_type, direction):
            _LOGGER.error(f"Invalid market params: station={station}, market_type={market_type}, direction={direction}")
            return None
            
        # Find markets for station and type
        markets = self.get_available_markets(station, market_type)
        
        if not markets:
            _LOGGER.warning(f"No markets found for station {station} and type {market_type}")
            return None
        
        # Sort markets to find the most appropriate one (by settlement soonest, etc.)
        # Sort by closes_at timestamp (nearest to farthest)
        sorted_markets = sorted(markets, key=lambda m: m.get('close_time', ''))
        
        # For each market, look at the order book to select strikes based on liquidity
        best_score = -1
        best_strike = None
        best_market = None
        
        for market in sorted_markets[:3]:  # Only examine top 3 markets
            market_id = market.get('id')
            if not market_id:
                continue
                
            order_book = self.get_order_book(market_id)
            if not order_book:
                continue
            
            # Process each side of order book for potential strike selection
            strikes_info = []
            
            # This is a simplified view; real order book might be formatted differently
            # depending on how Kalshi structures it
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            
            # Or it might be structured as levels per strike
            # Let's try parsing by individual strikes in the market's strike range
            for level in order_book.get('levels', []):
                # Assuming levels contain strike-specific info
                # In reality this might vary based on Kalshi's exact API structure
                strike = level.get('strike_price') or level.get('strike')
                if not strike:
                    continue
                    
                level_data = {
                    'strike': strike,
                    'yes_bid': level.get('yes_bid', 0),
                    'yes_ask': level.get('yes_ask', 100),
                    'no_bid': level.get('no_bid', 0),
                    'no_ask': level.get('no_ask', 100),
                    'yes_bid_size': level.get('yes_bid_size', 0),
                    'yes_ask_size': level.get('yes_ask_size', 0),
                    'no_bid_size': level.get('no_bid_size', 0),
                    'no_ask_size': level.get('no_ask_size', 0),
                }
                
                score, volume, spread = self.calculate_strike_score(level_data)
                
                # Consider direction - for UP direction, might prefer strikes slightly higher
                # for DOWN direction, strikes slightly lower - but primarily use liquidity
                if direction.upper() == 'UP':
                    # Slightly bias towards strikes where there are buyers (yes_bid) relative to sellers  
                    if level_data['yes_bid_size'] > level_data['no_ask_size']:
                        score *= 1.1
                elif direction.upper() == 'DOWN':
                    # Slightly bias toward strikes where there are no buyers relative to YES sellers
                    if level_data['no_bid_size'] > level_data['yes_ask_size']:
                        score *= 1.1
                
                strikes_info.append((strike, score, volume, spread))
        
            # If didn't find strikes in level format, look for market directly
            if not strikes_info:
                # Use market-level data
                current_yes_price = (market.get('yes_bid', 0) + market.get('yes_ask', 100)) / 2
                current_no_price = (market.get('no_bid', 0) + market.get('no_ask', 100)) / 2
                
                market_strike = market.get('strike_price') or market.get('limit')
                if market_strike:
                    # Create synthetic data for this market based on liquidity indicators
                    vol = market.get('volume', market.get('total_matched_shares', 0))
                    level_data = {
                        'strike': market_strike,
                        'yes_bid': market.get('yes_bid', 0),
                        'yes_ask': market.get('yes_ask', 100),
                        'no_bid': market.get('no_bid', 0),
                        'no_ask': market.get('no_ask', 100),
                        'yes_bid_size': market.get('yes_bid_size', market.get('latest_bid_amount', 0)),
                        'yes_ask_size': market.get('yes_ask_size', market.get('latest_offer_amount', 0)),
                        'no_bid_size': market.get('no_bid_size', 0),
                        'no_ask_size': market.get('no_ask_size', 0),
                        'volume': vol
                    }
                    
                    score, volume, spread = self.calculate_strike_score(level_data)
                    
                    # Adjust score based on our price expectation
                    if direction.upper() == 'UP' and current_yes_price > 60:  # Trending up
                        score *= 1.05
                    elif direction.upper() == 'DOWN' and current_yes_price < 40:  # Trending down
                        score *= 1.05
                    
                    strikes_info.append((
                        level_data['strike'], 
                        score, 
                        max(volume, level_data.get('volume', 0)), 
                        spread
                    ))
        
            # Select the best from this market
            for strike, score, volume, spread in strikes_info:
                # Only consider strikes that make sense for the direction
                # For UP in HIGH: want strikes somewhat above current expectation
                # For DOWN in HIGH: want strikes somewhat below current expectation
                # For UP in LOW: want strikes somewhat above current expectation 
                # For DOWN in LOW: want strikes somewhat below current expectiation
                current_expected = (market.get('yes_bid', 50) + market.get('yes_ask', 50)) / 2
                
                # Score adjustment for directional alignment - only minor adjustment 
                # since liquidity should dominate
                if (
                    (direction.upper() == 'UP' and current_expected > 40) or
                    (direction.upper() == 'DOWN' and current_expected < 60)
                ):
                    score *= 1.02  # Minor adjustment for alignment
                
                if score > best_score:
                    best_score = score
                    best_strike = strike
                    best_market = market
            
        if best_strike:
            _LOGGER.info(
                f"Selected best strike {best_strike} (score: {best_score:.2f}, vol: {strikes_info[0][2] if strikes_info else 'N/A'}) "
                f"for {station} {market_type} {direction} on market {best_market.get('id', 'unknown') if best_market else 'none'}"
            )
        
        return best_strike
    
    def select_best_strike_comprehensive(self, station: str, market_type: str, direction: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Enhanced strike selection that returns additional information.
        
        Returns:
            Tuple of (strike_price, metadata) or None if no appropriate strike found
        """
        if not self._validate_market_params(station, market_type, direction):
            return None
            
        best_strike = self.select_best_strike(station, market_type, direction)
        
        if not best_strike:
            return None
            
        # Get detailed market info for the selected strike
        markets = self.get_available_markets(station, market_type)
        
        # Find and return detailed info about the best market
        for market in markets:
            if market.get('strike_price') == best_strike or str(best_strike) in market.get('title', ''):
                # Also try to find order book data for this specific strike
                ob = self.get_order_book(market.get('id', ''))
                
                meta = {
                    'selected_strike': best_strike,
                    'market_id': market.get('id'),
                    'market_title': market.get('title'),
                    'current_yes_price': (market.get('yes_bid', 0) + market.get('yes_ask', 100)) / 2,
                    'spread_cents': abs(market.get('yes_ask', 100) - market.get('yes_bid', 0)),
                    'liquidity_indicator': market.get('volume', market.get('total_matched_shares', 0)),
                    'settlement_time': market.get('close_time', 'Unknown'),
                    'event_ticker': market.get('event_ticker'),
                    'series_ticker': market.get('series_ticker'),
                    'order_book_depth_data': ob if ob else None,
                    'direction_hint': direction,
                    'station': station,
                    'market_type': market_type,
                }
                return (best_strike, meta)
        
        # If we couldn't get detailed market info, return basic selection with limited metadata
        return (best_strike, {
            'selected_strike': best_strike,
            'station': station,
            'market_type': market_type,
            'direction_hint': direction,
        })


def get_strike_selector(api_base_url: str = "https://api.elections.kalshi.com/trade-api/v2") -> StrikeSelector:
    """Get a configured StrikeSelector instance."""
    return StrikeSelector(base_url=api_base_url)


def select_best_strike(station: str, market_type: str, direction: str) -> Optional[int]:
    """
    Convenience function for selecting the best strike price.
    
    Args:
        station: Station identifier (e.g., "ATL", "KATL")  
        market_type: "HIGH" or "LOW"
        direction: "UP" or "DOWN" indicating predicted movement
    Returns:
        Best strike price or None
    """
    selector = get_strike_selector()
    result = selector.select_best_strike(station, market_type, direction)
    return result


async def aselect_best_strike(station: str, market_type: str, direction: str) -> Optional[int]:
    """Async wrapper for select_best_strike to satisfy any code expecting async."""
    return select_best_strike(station, market_type, direction)


# Test and demo
def demo_select_best_strike(station: str = "KATL", market_type: str = "HIGH", direction: str = "UP"):
    """Demo function to exercise the strike selection logic."""
    print(f"Demonstrating strike selection: {station} {market_type} expected {direction}")
    
    selector = get_strike_selector()
    result = selector.select_best_strike(station, market_type, direction)
    
    if result:
        print(f"Selected strike: {result}")
        
        # Also run comprehensive version to get more details
        comp_result = selector.select_best_strike_comprehensive(station, market_type, direction)
        if comp_result:
            strike, meta = comp_result
            print(f"Detailed selection: Strike {strike}")
            print("Meta:", json.dumps(meta, indent=2, default=str))
    else:
        print(f"No suitable strike found for {station} {market_type} {direction}")


if __name__ == "__main__":
    # Test the selection
    demo_select_best_strike("KATL", "HIGH", "UP")