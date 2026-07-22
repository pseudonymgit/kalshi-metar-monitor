#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Low Liquidity Traps Filter (v1.0) - Edge 3

Risk/execution filter that identifies low-liquidity markets where small capital can move prices.
It flags them as traps, not opportunities according to the spec:

- If all 4 volume snapshots (10am/1pm/4pm/7pm local) < $15K AND spread > 1.5¢ → LOW_LIQ_TRAP
- Also flag if volume_24h < $50K consistently
- Signal fires as a risk gate: block trading on this market, don't produce a directional prediction
- Win rate target if ever tested: ≥55%

This module implements the logic to prevent trading in illiquid markets that may trap
the trading capital due to excessive slippage and adverse selection.

Version: v1.0 2026-07-18
"""

import logging
from typing import Tuple, Dict, Any, Optional
from datetime import datetime, timedelta, timezone

# Set up logger for liquidity trap detection
_LOGGER = logging.getLogger(__name__)


class LowLiquidityTrapFilter:
    """
    Detects low-liquidity traps in markets to prevent adverse trading conditions.
    Implements the business logic for identifying potentially dangerous markets
    where low liquidity could lead to poor fills and adverse selection.
    """
    
    def __init__(self,
                 volume_threshold_usd: float = 15000,  # $15K per snapshot
                 volume_24h_threshold_usd: float = 50000,  # $50K over 24h
                 spread_threshold_cents: float = 1.5):  # 1.5 cents
        """
        Initialize the low liquidity trap filter.
        
        Args:
            volume_threshold_usd: Threshold for individual snapshots ($15K default)
            volume_24h_threshold_usd: Threshold for 24hr volume ($50K default)
            spread_threshold_cents: Spread threshold in US cents (1.5¢ default)
        """
        self.volume_threshold_usd = volume_threshold_usd
        self.volume_24h_threshold_usd = volume_24h_threshold_usd
        self.spread_threshold_cents = spread_threshold_cents

    def is_low_liquidity(self, 
                        station: str, 
                        market_type: str, 
                        date: str, 
                        volume_data: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Determines if a market presents a low-liquidity trap based on volume and spread data.
        
        Args:
            station: ICAO station code
            market_type: Market type (HIGH or LOW)
            date: Trade date in YYYY-MM-DD format
            volume_data: Dictionary containing volume information including:
                        - snapshots: Dict with timestamp keys containing volume data
                        - volume_24h: 24-hour volume value
                        - spread: Current spread in decimal format (e.g., 0.025 for 2.5 cents)
        
        Returns:
            Tuple of (is_trap: bool, reason: str, details: dict)
            - is_trap: True if this is considered a low-liquidity trap
            - reason: Explanation of why marked as trap
            - details: Additional metadata about the analysis
        """
        snapshot_volumes = volume_data.get('snapshots', {})
        volume_24h = volume_data.get('volume_24h')
        spread_decimal = volume_data.get('spread')

        details = {
            'station': station,
            'market_type': market_type,
            'date': date,
            'volume_24h': volume_24h,
            'snapshots': snapshot_volumes,
            'spread_decimal': spread_decimal
        }

        # Check if spread is provided and exceeds threshold
        spread_exceeds_threshold = False
        if spread_decimal is not None:
            # Convert decimal spread (0.025) to cents (2.5)
            spread_cents = spread_decimal * 100
            details['spread_cents'] = spread_cents
            spread_exceeds_threshold = spread_cents > self.spread_threshold_cents
        
        # Check the primary condition: all 4 volume snapshots < $15K AND spread > 1.5¢
        snapshot_times = ['10am', '1pm', '4pm', '7pm']
        snapshots_below_threshold = 0
        snapshot_volumes_found = 0
        snapshots_details = {}

        for snap_time in snapshot_times:
            # Try various possible representations of the time
            possible_keys = [
                snap_time,
                f"{snap_time}_local",
                f"{snap_time.replace('am', 'AM').replace('pm', 'PM')}",
                f"{snap_time}_vol",
                f"volume_{snap_time}",
                f"vol_{snap_time}",
                snap_time.upper(),
                snap_time.replace('am', 'a').replace('pm', 'p'),
                snap_time.replace('am', ' A.M.').replace('pm', ' P.M.')
            ]

            snapshot_value = None
            for key in possible_keys:
                if key.lower() in snapshot_volumes:
                    snapshot_value = snapshot_volumes[key.lower()]
                    break
                elif key in snapshot_volumes:
                    snapshot_value = snapshot_volumes[key]
                    break
            
            if snapshot_value is not None:
                snapshot_volumes_found += 1
                snapshots_details[snap_time] = snapshot_value
                if isinstance(snapshot_value, (int, float)) and snapshot_value < self.volume_threshold_usd:
                    snapshots_below_threshold += 1

        # Mark as trap if ALL 4 snapshots exist AND are below threshold AND spread > 1.5¢
        has_low_volume_snapshots_and_high_spread = (
            snapshot_volumes_found == 4 and 
            snapshots_below_threshold == 4 and 
            spread_exceeds_threshold
        )
        
        details['snapshot_volumes_found'] = snapshot_volumes_found
        details['snapshots_below_threshold'] = snapshots_below_threshold
        details['snapshots_details'] = snapshots_details

        if has_low_volume_snapshots_and_high_spread:
            reason = f"LOW_LIQ_TRAP: All 4 snapshots < ${self.volume_threshold_usd:,} AND spread > {self.spread_threshold_cents}¢"
            _LOGGER.info(
                "low_liquidity_trap_detected station=%s market=%s reason=%s",
                station, market_type, reason
            )
            return True, reason, details

        # Check secondary condition: 24h volume < $50K
        has_low_24h_volume = (
            volume_24h is not None and 
            isinstance(volume_24h, (int, float)) and
            volume_24h < self.volume_24h_threshold_usd
        )

        if has_low_24h_volume:
            reason = f"LOW_VOLUMNE_ALERT: 24h volume ${volume_24h:,.2f} < ${self.volume_24h_threshold_usd:,}"
            _LOGGER.info(
                "low_24h_volume_alert station=%s market=%s reason=%s",
                station, market_type, reason
            )
            return True, reason, details

        # No trap detected
        details['trap_detected'] = False
        return False, "LIQUID_MARKET: Passes all liquidity tests", details
    
    def analyze_market_liquidity(self, 
                                station: str, 
                                market_type: str, 
                                date: str,
                                price_metadata: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Analyze market liquidity using price metadata that may come from kalshi_price_fetcher.
        
        Args:
            station: ICAO station code
            market_type: Market type (HIGH or LOW)
            date: Trade date in YYYY-MM-DD format
            price_metadata: Metadata as received from kalshi_price_fetcher containing:
                           - volume_24h: 24-hour volume
                           - yes_bid: Bid price
                           - yes_ask: Ask price (to calculate spread)

        Returns:
            Tuple of (is_trap: bool, reason: str, details: dict)
        """
        # Calculate spread from bid/ask if available
        spread_decimal = None
        if price_metadata.get('yes_bid') is not None and price_metadata.get('yes_ask') is not None:
            try:
                bid = float(price_metadata.get('yes_bid'))
                ask = float(price_metadata.get('yes_ask'))
                # Ensure normalization to decimal (0-1 range)
                if bid > 1:
                    bid /= 100
                if ask > 1:
                    ask /= 100
                spread_decimal = ask - bid
            except (ValueError, TypeError):
                # If calculation fails, continue with spread=None
                pass

        # Prepare volume data dict from metadata
        volume_data = {
            'volume_24h': price_metadata.get('volume_24h', 0),
            'spread': spread_decimal,
            'snapshots': {}  # No snapshot data from live price fetch
        }

        return self.is_low_liquidity(station, market_type, date, volume_data)


# ─── Standalone Test Function ───────────────────────────────────────────────

def test_low_liquidity_trap_filter():
    """Test the LowLiquidityTrapFilter with sample data."""
    import json
    
    print("Testing LowLiquidityTrapFilter...\n")
    
    # Create filter instance
    filter_obj = LowLiquidityTrapFilter(
        volume_threshold_usd=15000,
        volume_24h_threshold_usd=50000,
        spread_threshold_cents=1.5
    )
    
    # Test 1: Trap condition - all snapshots low volume + wide spread
    snap_data_trap_1 = {
        'snapshots': {
            '10am': 10000,
            '1pm': 12000,
            '4pm': 8000,
            '7pm': 11000
        },
        'volume_24h': 45000,
        'spread': 0.025  # 2.5 cents (> 1.5¢)
    }
    
    is_trap_1, reason_1, details_1 = filter_obj.is_low_liquidity('KDEN', 'HIGH', '2026-07-18', snap_data_trap_1)
    print(f"Test 1: Snap-based trap detection")
    print(f"  Result: {'TRAP' if is_trap_1 else 'PASS'} - {reason_1}")
    print(f"  Details: snapshots_above_thresh={details_1['snapshots_below_threshold']}/4, 24h_vol=${details_1['volume_24h']:,}, spread={details_1.get('spread_cents')}¢\n")
    
    # Test 2: Trap condition - 24h volume too low
    snap_data_trap_2 = {
        'snapshots': {},  # No snapshot data
        'volume_24h': 30000,  # Less than 50K threshold
        'spread': 0.01  # Narrow spread, but 24h vol is too low
    }
    
    is_trap_2, reason_2, details_2 = filter_obj.is_low_liquidity('KLAX', 'HIGH', '2026-07-18', snap_data_trap_2)
    print(f"Test 2: 24h-volume-based trap detection")
    print(f"  Result: {'TRAP' if is_trap_2 else 'PASS'} - {reason_2}")
    print(f"  Details: volume_24h=${details_2['volume_24h']:,}\n")
    
    # Test 3: Normal market - high volume and narrow spread
    snap_data_normal = {
        'snapshots': {
            '10am': 25000,
            '1pm': 30000,
            '4pm': 28000,
            '7pm': 35000
        },
        'volume_24h': 120000,
        'spread': 0.010  # 1.0 cent (< 1.5¢)
    }
    
    is_trap_3, reason_3, details_3 = filter_obj.is_low_liquidity('KNYC', 'HIGH', '2026-07-18', snap_data_normal)
    print(f"Test 3: Normal market detection")
    print(f"  Result: {'TRAP' if is_trap_3 else 'PASS'} - {reason_3}")
    print(f"  Details: snapshots_all_good=<{details_3['snapshots_below_threshold']}/4, 24h_vol=${details_3['volume_24h']:,}, spread={details_3.get('spread_cents')}¢\n")

    # Test 4: Price metadata based analysis
    metadata_data = {
        'volume_24h': 35000,  # Below 50K threshold
        'yes_bid': 0.45,      # Bid price
        'yes_ask': 0.48       # Ask price, spread = 0.03 (3¢ > 1.5¢ threshold)
    }
    
    is_trap_4, reason_4, details_4 = filter_obj.analyze_market_liquidity('KMIA', 'HIGH', '2026-07-18', metadata_data)
    print(f"Test 4: Metadata-based analysis (bid/ask)")
    print(f"  Result: {'TRAP' if is_trap_4 else 'PASS'} - {reason_4}")
    print(f"  Details: spread={details_4.get('spread_cents', 'N/A')}¢, 24h_vol=${details_4['volume_24h']:,}\n")


if __name__ == "__main__":
    test_low_liquidity_trap_filter()