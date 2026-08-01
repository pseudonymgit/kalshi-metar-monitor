"""
tiers.py — Liquidity tier definitions for microstructure edge penalty (A5).

Tier 1 (8 cities — liquid):     0% edge discount
Tier 2 (6 cities — moderate):  25% edge discount
Tier 3 (6 cities — thin):      50% edge discount

Usage:
    from scripts.sweep.tiers import get_tier, apply_edge_penalty, STATION_TIERS
"""

from typing import Optional, Dict, Tuple

# Liquidity tier mapping: 20 stations, 8+6+6 split
# Based on typical Kalshi weather market liquidity:
#   Tier 1: Major metro areas with highest trading volume
#   Tier 2: Mid-size metro areas with moderate volume
#   Tier 3: Smaller markets with lower volume
STATION_TIERS: Dict[str, int] = {
    # Tier 1 — Liquid (8 cities, 0% discount)
    "KNYC": 1,  # New York City
    "KLAX": 1,  # Los Angeles
    "KMDW": 1,  # Chicago
    "KDCA": 1,  # Washington DC
    "KATL": 1,  # Atlanta
    "KDFW": 1,  # Dallas-Fort Worth
    "KPHL": 1,  # Philadelphia
    "KBOS": 1,  # Boston
    # Tier 2 — Moderate (6 cities, 25% discount)
    "KDEN": 2,  # Denver
    "KLAS": 2,  # Las Vegas
    "KSFO": 2,  # San Francisco
    "KSEA": 2,  # Seattle
    "KMIA": 2,  # Miami
    "KHOU": 2,  # Houston
    # Tier 3 — Thin (6 cities, 50% discount)
    "KPHX": 3,  # Phoenix
    "KMSP": 3,  # Minneapolis
    "KMSY": 3,  # New Orleans
    "KAUS": 3,  # Austin
    "KSAT": 3,  # San Antonio
    "KOKC": 3,  # Oklahoma City
}

# Edge discount by tier
TIER_DISCOUNTS: Dict[int, float] = {
    1: 0.00,  # 0% discount
    2: 0.25,  # 25% discount
    3: 0.50,  # 50% discount
}


def get_tier(station: str) -> int:
    """Get the liquidity tier (1, 2, or 3) for a station. Defaults to Tier 3."""
    return STATION_TIERS.get(station, 3)


def get_tier_discount(station: str) -> float:
    """Get the edge discount fraction for a station's tier."""
    tier = get_tier(station)
    return TIER_DISCOUNTS.get(tier, 0.50)


def apply_edge_penalty(
    raw_edge: float,
    station: str,
    edge_threshold: float = 0.0,
) -> Tuple[float, bool]:
    """
    Apply microstructure edge penalty by liquidity tier.
    
    Args:
        raw_edge: Raw (gross) edge before penalty
        station: ICAO station code
        edge_threshold: Minimum adjusted edge to qualify for position sizing
    
    Returns:
        (adjusted_edge, qualifies): Tuple of adjusted edge and whether it exceeds threshold
    """
    tier = get_tier(station)
    discount = TIER_DISCOUNTS.get(tier, 0.50)
    adjusted_edge = raw_edge * (1.0 - discount)
    qualifies = adjusted_edge > edge_threshold
    return adjusted_edge, qualifies


def get_tier_info(station: str) -> dict:
    """Get full tier information for a station."""
    tier = get_tier(station)
    return {
        "station": station,
        "tier": tier,
        "tier_name": {1: "Liquid", 2: "Moderate", 3: "Thin"}.get(tier, "Unknown"),
        "discount": TIER_DISCOUNTS.get(tier, 0.50),
    }