"""
Cost Utilities Module for Kalshi Trading

Handles all cost components for trading, especially:
- Bid-ask spread as transaction cost (since Kalshi charges 0 commission)
- Provides a unified get_cost() function to replace hardcoded fee rates

USAGE:
    from core.cost_utils import get_cost
    cost_fraction = get_cost(bid_price=0.48, ask_price=0.52)
"""



def calculate_spread_cost(bid_price: float, ask_price: float) -> float:
    """
    Calculate the cost of transacting based on bid-ask spread.
    This is the effective cost since Kalshi charges 0 commission.
    
    Args:
        bid_price: Best bid price (0.0-1.0)
        ask_price: Best ask price (0.0-1.0)
    
    Returns:
        Cost as fraction of contract value based on bid-ask spread.
        For takers: the cost is typically the full spread in practical terms.
    """
    if bid_price is None or ask_price is None:
        # Fallback to a reasonable assumption if live data not available
        return 0.02  # 2 cent spread assumption

    spread = abs(ask_price - bid_price)
    mid_price = (bid_price + ask_price) / 2 if bid_price != ask_price else bid_price
    
    if mid_price <= 0:
        # If mid price too small, return the spread as percentage
        return spread if spread > 0 else 0.02  # Default 2 cent spread assumption

    spread_as_fraction = spread / mid_price
    
    # As a realistic approximation, the cost per unit trade is half the spread
    # if liquidity is good; in low liquidity it approaches full spread
    # But for modeling purposes we'll return the full spread as cost
    return spread_as_fraction


def get_cost(bid_price: float = None, ask_price: float = None, 
             default_cost: float = 0.02) -> float:
    """
    Unified function to get transaction cost fraction.
    
    Args:
        bid_price: Live bid price (0.0-1.0) - optional (can be None)
        ask_price: Live ask price (0.0-1.0) - optional (can be None) 
        default_cost: Default cost fraction when live data unavailable
    
    Returns:
        Cost fraction as percentage of contract value based on actual spread.
        Returns 0 when both bid and ask are exactly the same (perfect liquidity).
        Returns default_cost when either bid or ask is None.
    """
    if bid_price is None or ask_price is None:
        return default_cost
    
    if bid_price == ask_price:
        # Perfect liquidity (unlikely in practice)
        return 0.0
    
    # Calculate real-time cost based on bid-ask spread
    return calculate_spread_cost(bid_price, ask_price)


def adjust_cost_for_volume(cost_base: float, volume_adjustment: float = 1.0) -> float:
    """
    Adjust base cost based on trade volume.
    
    Args:
        cost_base: Base cost fraction from get_cost()
        volume_adjustment: Multiplier for volume effects (default 1.0 = no effect)
    
    Returns:
        Adjusted cost considering trade size/volume.
    """
    return cost_base * volume_adjustment


def get_effective_cost_for_trade(bid_price: float, ask_price: float, 
                                 direction: str = 'long') -> float:
    """
    Get the effective cost for a specific trade direction.
    
    Args:
        bid_price: Live bid price
        ask_price: Live ask price  
        direction: 'long' (buy at ask) or 'short' (sell at bid)
    
    Returns:
        Cost fraction based on the entry approach and market conditions.
    """
    if direction.lower() == 'long':
        # When buying, you pay the ask price, so effective cost depends on bid-ask
        return calculate_spread_cost(bid_price, ask_price)
    else:  # short
        # When selling, you receive the bid price, similar cost structure
        return calculate_spread_cost(bid_price, ask_price)


if __name__ == '__main__':
    # Examples
    print("Cost calculations:")
    print(f"Standard market (bid=0.48, ask=0.52): {get_cost(0.48, 0.52):.6f}")
    print(f"Liquid market (bid=0.499, ask=0.501): {get_cost(0.499, 0.501):.6f}")
    print(f"Illiquid market (bid=0.45, ask=0.55): {get_cost(0.45, 0.55):.6f}")
    print(f"Missing data (fallback): {get_cost(None, 0.55):.6f}")