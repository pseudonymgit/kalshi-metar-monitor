#!/usr/bin/env python3
"""
Buy Side Optimizer v1.0 — Phase 6.6 Selecting BUY YES vs BUY NO

For a given direction (UP/DOWN on HIGH/LOW market), determine whether to buy 
YES or NO based on optimal value and cost assessment.

If predicting UP on HIGH: buy YES (predicting HIGH will be > threshold)
If predicting DOWN on HIGH: buy NO (predicting HIGH will be ≤ threshold)
Optimize by comparing premiums: if YES ask > 0.5, buying NO might be cheaper.
"""

import logging
from typing import Dict, Tuple, Optional, Union
from decimal import Decimal

_LOGGER = logging.getLogger(__name__)


def select_buy_side(
    station: str, 
    market_type: str, 
    direction: str, 
    prices: Dict[str, Union[float, int]]
) -> Tuple[str, float, float]:
    """
    Determine optimal side and strike for trading based on prices and prediction.
    
    Args:
        station: Station identifier (e.g., 'KJFK', 'ATL')
        market_type: 'HIGH' or 'LOW' 
        direction: 'UP' or 'DOWN' indicating expected movement
        prices: Dictionary containing price information with at least these keys:
            - 'yes_bid' or 'YES.bid'
            - 'yes_ask' or 'YES.ask'
            - 'no_bid' or 'NO.bid'  
            - 'no_ask' or 'NO.ask'
            - 'strike_price' or 'strike'
    
    Returns:
        Tuple of (side, strike_price, premium) where:
        - side: 'YES' or 'NO'
        - strike_price: Selected strike price for the trade
        - premium: Expected cost per share (probability percentage in dollars)
    """
    # Extract and normalize the price keys
    price_map = {}
    for key, value in prices.items():
        normalized_key = key.replace('.', '_').replace('-', '_').upper()
        price_map[normalized_key] = float(value)
    
    # Handle different possible key formats
    yes_bid = price_map.get('YES_BID') or price_map.get('BID_YES') or prices.get('yes_bid', 0.0)
    yes_ask = price_map.get('YES_ASK') or price_map.get('ASK_YES') or prices.get('yes_ask', 100.0)
    no_bid = price_map.get('NO_BID') or price_map.get('BID_NO') or prices.get('no_bid', 0.0) 
    no_ask = price_map.get('NO_ASK') or price_map.get('ASK_NO') or prices.get('no_ask', 100.0)
    
    # Get strike price - handle multiple possible keys
    strike_keys = ['STRIKE_PRICE', 'STRIKE', 'strike', 'strike_price', 
                   'STRIKEPRICE', 'strikePrice']
    strike = None
    for key in strike_keys:
        if key in prices:
            strike = float(prices[key])
            break
        elif key in price_map:
            strike = price_map[key]
            break
    
    if strike is None:
        raise ValueError(f"No strike price found in prices: {prices}")
    
    # Ensure we have valid decimal prices
    yes_ask_dec = min(max(yes_ask, 0.0), 100.0)
    no_ask_dec = min(max(no_ask, 0.0), 100.0)
    
    # Validate inputs
    if market_type.upper() not in ['HIGH', 'LOW']:
        raise ValueError(f"Invalid market_type: {market_type}, must be 'HIGH' or 'LOW'")
    if direction.upper() not in ['UP', 'DOWN']:
        raise ValueError(f"Invalid direction: {direction}, must be 'UP' or 'DOWN'")
    
    # Log initial information for debug
    _LOGGER.info(
        f"Optimizing for {station} {market_type} -> {direction} "
        f"@ strike {strike}: Y:{yes_ask_dec}/N:{no_ask_dec}"
    )
    
    # Basic decision logic based on direction and market type
    if direction.upper() == 'UP':
        # Expecting the market value to go UP
        if market_type.upper() == 'HIGH':
            # For HIGH markets, UP means temperature will exceed the strike threshold
            # So buy YES if market resolves to 'yes', meaning temp > strike
            expected_side = 'YES'
        else:  # LOW
            # For LOW markets, UP also means we expect the temp measure to increase
            # So buy YES if market resolves to 'yes', meaning low temp of day was above strike
            expected_side = 'YES'
    elif direction.upper() == 'DOWN':
        # Expecting the market value to go DOWN
        if market_type.upper() == 'HIGH':
            # We think temp won't make it above the strike threshold
            # So buy NO since market would resolve "temperature not greater than X"
            expected_side = 'NO'
        else:  # LOW
            # We think the low temp WILL be less than the strike 
            # Actually NO since market resolves "low temp not less than X"
            expected_side = 'NO'
            
    _LOGGER.debug(f"Expected side based on direction: {expected_side}")
    
    # Now perform optimization: decide if we should take the expected side or the opposite
    # based on which costs less (premium minimization)
    
    # If expected side is YES but it's expensive, maybe NO is cheaper for the same outcome
    if expected_side == 'YES' and yes_ask_dec > 0.5:
        # The YES contract is quite expensive (> 50%), check if NO would be better
        # If YES is > 50%, you should consider NOT the proposition (meaning NO)
        if no_ask_dec < yes_ask_dec and no_ask_dec < 0.5:
            optimal_side = 'NO'
            premium_cost = no_ask_dec
            _LOGGER.info(f"Choosing NO ({no_ask_dec}) instead of YES ({yes_ask_dec}) for optimization")
        else:
            # Stick with the expected side if no better value found
            optimal_side = 'YES'
            premium_cost = yes_ask_dec
    elif expected_side == 'NO' and no_ask_dec > 0.5:
        # The NO contract is expensive, check if YES would be better
        if yes_ask_dec < no_ask_dec and yes_ask_dec < 0.5:
            optimal_side = 'YES'
            premium_cost = yes_ask_dec 
            _LOGGER.info(f"Choosing YES ({yes_ask_dec}) instead of NO ({no_ask_dec}) for optimization")
        else:
            # Stick with the expected side
            optimal_side = 'NO'
            premium_cost = no_ask_dec
    else:
        # Expected side is cheap enough, just take it as expected
        optimal_side = expected_side
        premium_cost = yes_ask_dec if expected_side == 'YES' else no_ask_dec
    
    _LOGGER.info(
        f"Final optimization for {station} {market_type} {direction} @ {strike}: " 
        f"{optimal_side} at ${premium_cost:.3f} premium"
    )
    
    return optimal_side, strike, premium_cost


def analyze_buy_side_opportunity(
    station: str,
    market_type: str,
    direction: str,
    prices: Dict[str, Union[float, int]]
) -> Dict[str, Union[str, float, bool]]:
    """
    Detailed analysis of buy side opportunity, providing insight about the decision.
    
    Returns:
        Extended information including original intended side vs optimized choice,
        cost savings, and rationale
    """
    original_expected_side = 'YES' if (
        (direction.upper() == 'UP') or 
        (direction.upper() == 'DOWN' and market_type.upper() in ['HIGH', 'LOW'])
    ) else 'NO'
    
    # Adjust the original expectation based on market type properly
    if direction.upper() == 'UP':
        if market_type.upper() == 'HIGH':
            original_expected_side = 'YES'
        else:
            original_expected_side = 'YES'
    elif direction.upper() == 'DOWN':
        if market_type.upper() == 'HIGH':
            original_expected_side = 'NO'
        else:
            original_expected_side = 'NO'
    
    # Extract pricing info again
    yes_ask = prices.get('yes_ask', prices.get('YES_ASK', prices.get('ASK_YES', 100)))
    no_ask = prices.get('no_ask', prices.get('NO_ASK', prices.get('ASK_NO', 100))) 
    strike = (
        prices.get('strike_price') or 
        prices.get('STRIKE_PRICE') or 
        prices.get('strike') or 
        prices.get('STRIKE')
    )
    
    if strike is None:
        raise ValueError(f"No strike found in prices: {prices}")
    
    # Determine optimal decision using the main function
    optimal_side, optimal_strike, optimized_premium = select_buy_side(station, market_type, direction, prices)
    
    # Get premium for original expected side for comparison
    original_premium = yes_ask if original_expected_side == 'YES' else no_ask
    
    # Calculate potential savings from optimization
    savings = original_premium - optimized_premium
    
    analysis = {
        'station': station,
        'market_type': market_type,
        'direction_prediction': direction.upper(),
        'strike_price': float(strike),
        'original_expected_side': original_expected_side,
        'original_premium_cost': float(original_premium),
        'optimized_side': optimal_side,
        'optimized_premium_cost': optimized_premium,
        'premium_difference': original_premium - optimized_premium,
        'cost_saved_by_optimization': abs(savings) if savings > 0 else 0.0,
        'optimization_performed': optimal_side != original_expected_side,
        'premium_efficiency_ratio': optimized_premium / original_premium if original_premium > 0 else 1.0,
        'rationale': (
            f"Optimized from {original_expected_side} at {original_premium:.3f} to {optimal_side} at {optimized_premium:.3f} "
            f"(saved ${(original_premium - optimized_premium):.3f})"
        ) if savings > 0.005 else (
            f"Took expected side {optimal_side} ({optimized_premium:.3f}), optimization not needed"
        )
    }
    
    return analysis


def get_optimized_trade_decision(
    station: str, 
    market_type: str, 
    direction: str, 
    prices: Dict[str, Union[float, int]],
    quantity: int = 1
) -> Dict[str, Union[str, float, int]]:
    """
    Complete trade decision with optimization and basic trade planning.
    
    Args:
        station: Station identifier
        market_type: HIGH or LOW
        direction: UP or DOWN expected movement  
        prices: Price dictionary
        quantity: Number of shares to buy (default 1)
    
    Returns:
        Complete trade plan including position sizing, cost, and optimization rationale
    """
    optimal_side, strike_price, premium_cost = select_buy_side(station, market_type, direction, prices)
    
    total_cost = premium_cost * quantity
    
    # Perform detailed analysis
    analysis = analyze_buy_side_opportunity(station, market_type, direction, prices)
    
    trade_decision = {
        'action': 'BUY',
        'side': optimal_side,
        'station': station,
        'market_type': market_type,
        'direction_prediction': direction.upper(),
        'strike_price': strike_price,
        'quantity': quantity,
        'premium_per_share': premium_cost,
        'estimated_total_cost': total_cost,
        'position_rationale': analysis['rationale'],
        'optimization_applied': analysis['optimization_performed'],
        'cost_save_amount': analysis['cost_saved_by_optimization']
    }
    
    return trade_decision


def compare_sides_premia(
    yes_premium: float,
    no_premium: float,
    suggested_direction: str
) -> Tuple[str, float]:
    """
    Direct comparison function for two sides given their premia.
    
    Args:
        yes_premium: Premium for YES contract (price in $0-1)
        no_premium: Premium for NO contract (price in $0-1)
        suggested_direction: 'long_yes', 'long_no', or None
    
    Returns:
        Tuple of (preferred_side, premium_cost)
    """
    _LOGGER.debug(f"Side comparison - YES: {yes_premium:.3f}, NO: {no_premium:.3f}, Suggestion: {suggested_direction}")
    
    if suggested_direction == 'long_yes' and yes_premium < no_premium:
        return 'YES', yes_premium
    elif suggested_direction == 'long_no' and no_premium < yes_premium:
        return 'NO', no_premium
    else:
        # Default to the cheaper option regardless of direction suggestion
        if yes_premium <= no_premium:
            return 'YES', yes_premium
        else:
            return 'NO', no_premium


# Compatibility wrapper with older code expectations
def optimize_buy_side(station, market_type, direction, prices):
    """Compatibility alias for select_buy_side"""
    return select_buy_side(station, market_type, direction, prices)


# Backward compatibility for async systems
def aanalyze_buy_side_opportunity(*args, **kwargs):
    """Async-compatible wrapper"""
    return analyze_buy_side_opportunity(*args, **kwargs)


# Example usage and test
if __name__ == "__main__":
    # Example scenario: KATL, HIGH market, prediction UP, current prices suggest YES is expensive
    example_prices = {
        'yes_bid': 85,
        'yes_ask': 87,  # Expensive YES - buying temperature to go high
        'no_bid': 12,
        'no_ask': 15,   # Cheap NO
        'strike_price': 86.0,
        'last_price': 86
    }
    
    print("=== Buy Side Optimization Example ===")
    print("Scenario: Atlanta HIGH temp, expecting temperature to go UP")
    print("Prices: YES ask=87 cents, NO ask=15 cents")
    print("Traditional: would buy YES since expected direction is UP")
    print("Optimization: Is there a cheaper way to align with the forecast?")
    print()
    
    side, strike, premium = select_buy_side(
        "KATL", "HIGH", "UP", example_prices
    )
    
    print(f"Result: Side={side}, Strike=${strike}, Premium={premium}")
    
    analysis = analyze_buy_side_opportunity(
        "KATL", "HIGH", "UP", example_prices
    )
    
    print("\nDetailed Analysis:")
    print(f"  Original expected: {analysis['original_expected_side']} at ${analysis['original_premium_cost']:.2f}")
    print(f"  Optimized choice:  {analysis['optimized_side']} at ${analysis['optimized_premium_cost']:.2f}")
    print(f"  Cost saved: ${analysis['cost_saved_by_optimization']:.2f}")
    print(f"  Rationale: {analysis['rationale']}")
    
    # Another example with a different scenario  
    example2_prices = {
        'yes_bid': 12,
        'yes_ask': 15,   # Cheap YES
        'no_bid': 83,
        'no_ask': 87,    # Expensive NO
        'strike_price': 70.0,
        'last_price': 72
    }
    
    print("\n" + "="*50)
    print("=== Second Example ===")
    print("Scenario: LAX LOW temp, expecting temperature to go DOWN")  
    print("Prices: YES ask=15 cents, NO ask=87 cents")
    print("Traditional: would buy NO since expecting low temp to go lower")
    print("Optimization: Looking for best value alignment.")
    print()
    
    side2, strike2, premium2 = select_buy_side(
        "KLAX", "LOW", "DOWN", example2_prices
    )
    
    print(f"Result: Side={side2}, Strike=${strike2}, Premium={premium2}")
    
    analysis2 = analyze_buy_side_opportunity(
        "KLAX", "LOW", "DOWN", example2_prices
    )
    
    print("\nDetailed Analysis:")
    print(f"  Original expected: {analysis2['original_expected_side']} at ${analysis2['original_premium_cost']:.2f}")
    print(f"  Optimized choice:  {analysis2['optimized_side']} at ${analysis2['optimized_premium_cost']:.2f}")
    print(f"  Cost saved: ${analysis2['cost_saved_by_optimization']:.2f}")  
    print(f"  Rationale: {analysis2['rationale']}")