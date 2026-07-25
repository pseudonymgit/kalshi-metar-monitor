#!/usr/bin/env python3
"""
Market Cost Model — Single source of truth for Kalshi market cost assumptions.

Provides a centralized cost model for spread, slippage, and commission
used across the paper trading engine, fee-aware filter, and Kelly sizer.

Per Kalshi fee structure:
- Commission: 0% (Kalshi charges no commission on weather markets)
- Spread: dynamically fetched from live bid/ask via Kalshi API
- Slippage: estimated based on market depth and position size

Enhancements (Phase 19.1):
- Dynamic spread: fetches real bid/ask from Kalshi API instead of hardcoded 0.5¢
- Market depth analysis: estimates liquidity from market volume data
- Slippage model: random fill price within spread, partial fill probability based on volume
- Fallback: uses measured mean spread (3.1¢) when API is unavailable

Usage:
    from core.market_cost_model import MARKET_COST_MODEL
    cost = MARKET_COST_MODEL.round_trip_fraction()
    spread = MARKET_COST_MODEL.get_dynamic_spread("KXHIGHKDEN")

Version: 2.0 — 2026-07-22 (Phase 19.1)
"""

import math
import random
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# ─── Default / Fallback Constants ────────────────────────────────────────

MEASURED_MEAN_SPREAD = 0.031  # 3.1¢ measured mean across all markets
DEFAULT_SLIPPAGE = 0.005       # 0.5¢ fallback slippage
DEFAULT_COMMISSION = 0.0       # Kalshi charges no commission

# ─── Round-trip fee (single source of truth) ───────────────────────────────────
# Round-trip = spread/2 + commission + slippage = 3.1¢/2 + 0¢ + 0.5¢ = 2.05¢
ROUND_TRIP_FEE = (MEASURED_MEAN_SPREAD / 2.0) + DEFAULT_COMMISSION + DEFAULT_SLIPPAGE

# Slippage model parameters
SLIPPAGE_SPREAD_FRACTION = 0.5  # Expected slippage = spread * this fraction
PARTIAL_FILL_THRESHOLD_VOLUME = 100   # Below this many contracts, partial fills likely
MIN_FILL_RATE = 0.3             # Minimum fill fraction when liquidity is thin
MAX_FILL_RATE = 1.0             # Perfect fill when liquid


@dataclass
class MarketDepthSnapshot:
    """Snapshot of market depth for a given contract."""
    yes_bid: float       # Dollar price (0-1)
    yes_ask: float       # Dollar price (0-1)
    spread: float        # yes_ask - yes_bid in dollar terms
    bid_volume: int      # Estimated volume at bid (contracts)
    ask_volume: int      # Estimated volume at ask (contracts)
    total_volume: int    # Total liquidity (contracts)
    last_price: Optional[float] = None
    implied_depth_score: float = 0.5  # 0 (illiquid) to 1 (deep)


@dataclass
class SlippageEstimate:
    """Result of a slippage estimate for a given position size."""
    expected_fill_price: float     # Expected average fill price (0-1)
    expected_slippage_cost: float  # Slippage in dollar terms per contract
    fill_probability: float        # Probability of full fill (0-1)
    expected_fill_fraction: float  # Expected fraction of order filled
    partial_fill_risk: str         # "low", "medium", "high"


class MarketCostModel:
    """
    Centralized Kalshi market cost model with dynamic bid/ask integration.

    Attributes:
        spread: Default/fallback spread (fraction of $1)
        commission: Commission per contract (fraction of $1, 0 for Kalshi)
        slippage: Default/fallback slippage (fraction of $1)
    """

    def __init__(
        self,
        spread: float = MEASURED_MEAN_SPREAD,
        commission: float = DEFAULT_COMMISSION,
        slippage: float = DEFAULT_SLIPPAGE,
    ):
        self.spread = spread
        self.commission = commission
        self.slippage = slippage
        # In-memory cache for bid/ask data: ticker -> MarketDepthSnapshot
        self._depth_cache: Dict[str, MarketDepthSnapshot] = {}
        self._cache_ttl_seconds: int = 300  # 5 minute cache

    # ─── Core cost calculations ──────────────────────────────────────────

    def round_trip_fraction(self) -> float:
        """
        Return the round-trip cost as a fraction of notional.
        Round-trip = spread/2 + commission + slippage (buy-and-hold-to-settlement).
        """
        return (self.spread / 2.0) + self.commission + self.slippage

    def one_way_cost(self) -> float:
        """Return the one-way cost per contract (fraction of $1)."""
        return (self.spread / 2.0) + self.slippage

    def to_dict(self) -> dict:
        return {
            'spread': self.spread,
            'commission': self.commission,
            'slippage': self.slippage,
            'round_trip_fraction': self.round_trip_fraction(),
        }

    # ─── Dynamic spread from Kalshi API ──────────────────────────────────

    def get_dynamic_spread(
        self,
        ticker: str,
        force_refresh: bool = False,
    ) -> float:
        """
        Fetch the live bid-ask spread for a ticker from the Kalshi API.

        Args:
            ticker: Full market ticker (e.g. 'KXHIGHKDEN-260722-85')
            force_refresh: If True, bypass cache

        Returns:
            Spread as fraction of $1 (e.g. 0.031 for 3.1¢).
            Falls back to self.spread if API unavailable.
        """
        depth = self._fetch_market_depth(ticker, force_refresh)
        if depth is not None:
            return depth.spread
        return self.spread

    def get_bid_ask(
        self,
        ticker: str,
        force_refresh: bool = False,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Get real-time bid and ask prices for a ticker.

        Returns:
            Tuple of (bid, ask) as fractions of $1, or (None, None) on failure.
        """
        depth = self._fetch_market_depth(ticker, force_refresh)
        if depth is not None:
            return depth.yes_bid, depth.yes_ask
        return None, None

    def get_market_depth_snapshot(
        self,
        ticker: str,
        force_refresh: bool = False,
    ) -> Optional[MarketDepthSnapshot]:
        """Get a full depth snapshot for a ticker."""
        return self._fetch_market_depth(ticker, force_refresh)

    # ─── Market depth analysis ───────────────────────────────────────────

    def estimate_liquidity_depth(self, ticker: str) -> float:
        """
        Estimate market depth on a 0-1 scale.

        Uses:
        1. Live bid/ask volume if available
        2. Total volume from market data
        3. Implied depth score as fallback

        Returns:
            Depth score: 0 (illiquid) to 1 (deep market)
        """
        depth = self._fetch_market_depth(ticker)
        if depth is not None:
            return depth.implied_depth_score
        return 0.3  # Conservative fallback

    def estimate_slippage(
        self,
        ticker: str,
        position_size_contracts: float = 1.0,
        is_buy: bool = True,
    ) -> SlippageEstimate:
        """
        Estimate slippage for a given position size.

        Models:
        - Random fill price uniformly distributed within the spread
        - Partial fill probability inversely proportional to position / depth ratio
        - Market impact: larger orders push price further

        Args:
            ticker: Full market ticker
            position_size_contracts: Number of contracts
            is_buy: True for buying (pays ask), False for selling (receives bid)

        Returns:
            SlippageEstimate with expected fill price and risk assessment
        """
        depth = self._fetch_market_depth(ticker)

        if depth is None:
            # No live data: use default spread + random jitter
            current_mid = 0.5
            half_spread = self.spread / 2.0
            if is_buy:
                ref_price = current_mid + half_spread
            else:
                ref_price = current_mid - half_spread

            expected_fill = ref_price
            slippage_cost = self.slippage
            fill_prob = 0.9
            fill_frac = 0.9
            risk = "medium"
        else:
            spread = depth.spread
            bid = depth.yes_bid
            ask = depth.yes_ask
            mid = (bid + ask) / 2.0

            # Estimate market depth (in contract-equivalent units)
            est_depth = max(depth.total_volume, PARTIAL_FILL_THRESHOLD_VOLUME)

            # Position-to-depth ratio — drives impact
            impact_ratio = position_size_contracts / max(est_depth, 1.0)

            # Slippage: fill price is randomly distributed within spread
            # For buy: fill between mid and ask (possibly beyond if large order)
            if is_buy:
                # Wide end of spread for buyer
                fill_center = ask
                # Market impact pushes price beyond ask for large orders
                impact_penalty = impact_ratio * spread * 2.0
                max_fill = ask + impact_penalty
                # Random uniform between mid and max_fill (skewed toward worse)
                raw_fill = mid + random.uniform(spread * 0.3, spread * 0.7 + impact_penalty)
                expected_fill = min(raw_fill, 1.0)
            else:
                # Wide end of spread for seller
                fill_center = bid
                impact_penalty = impact_ratio * spread * 2.0
                min_fill = bid - impact_penalty
                raw_fill = mid - random.uniform(spread * 0.3, spread * 0.7 + impact_penalty)
                expected_fill = max(raw_fill, 0.0)

            slippage_cost = abs(expected_fill - mid)

            # Partial fill probability
            if impact_ratio >= 1.0:
                # Order larger than estimated depth — high chance of partial fill
                fill_prob = max(MIN_FILL_RATE, 1.0 - impact_ratio)
                fill_frac = max(MIN_FILL_RATE, 1.0 / (1.0 + impact_ratio))
                risk = "high"
            elif impact_ratio >= 0.3:
                fill_prob = 0.7 + 0.3 * (1.0 - impact_ratio / 0.3)
                fill_frac = 0.85 + 0.15 * (1.0 - impact_ratio / 0.3)
                risk = "medium"
            else:
                fill_prob = 0.95
                fill_frac = 1.0
                risk = "low"

        return SlippageEstimate(
            expected_fill_price=round(expected_fill, 4),
            expected_slippage_cost=round(slippage_cost, 5),
            fill_probability=round(fill_prob, 3),
            expected_fill_fraction=round(fill_frac, 3),
            partial_fill_risk=risk,
        )

    def estimate_total_cost(
        self,
        ticker: str,
        position_size_contracts: float = 1.0,
        is_buy: bool = True,
    ) -> Dict[str, float]:
        """
        Comprehensive cost estimate including spread, slippage, and commission.

        Returns:
            dict with keys: spread_cost, slippage_cost, commission_cost, total_cost,
                            expected_fill_price, fill_probability
        """
        slippage_est = self.estimate_slippage(ticker, position_size_contracts, is_buy)
        depth = self._fetch_market_depth(ticker)
        live_spread = depth.spread if depth else self.spread

        spread_cost_per = live_spread / 2.0
        commission_per = self.commission

        return {
            'spread_cost': round(spread_cost_per, 5),
            'slippage_cost': round(slippage_est.expected_slippage_cost, 5),
            'commission_cost': round(commission_per, 5),
            'total_cost_per_contract': round(
                spread_cost_per + slippage_est.expected_slippage_cost + commission_per, 5
            ),
            'expected_fill_price': slippage_est.expected_fill_price,
            'fill_probability': slippage_est.fill_probability,
            'expected_fill_fraction': slippage_est.expected_fill_fraction,
            'partial_fill_risk': slippage_est.partial_fill_risk,
        }

    # ─── Internal: Kalshi API depth fetch ────────────────────────────────

    def _fetch_market_depth(
        self,
        ticker: str,
        force_refresh: bool = False,
    ) -> Optional[MarketDepthSnapshot]:
        """
        Fetch market depth from Kalshi API.

        Uses the public /markets/{ticker} endpoint which returns:
        - yes_bid / yes_ask (dollar format with _dollars suffix)
        - volume metrics
        """
        if not force_refresh and ticker in self._depth_cache:
            return self._depth_cache[ticker]

        try:
            import requests
            base_url = "https://api.elections.kalshi.com/trade-api/v2"
            url = f"{base_url}/markets/{ticker}"
            resp = requests.get(url, timeout=10)

            if resp.status_code != 200:
                _LOGGER.warning("kalshi_depth_fetch_failed ticker=%s status=%d", ticker, resp.status_code)
                return None

            data = resp.json()
            market = data.get("market") or data

            # Extract bid/ask in dollar format
            yes_bid = None
            yes_ask = None

            for suffix in ("_dollars", ""):
                bid_raw = market.get(f"yes_bid{suffix}")
                ask_raw = market.get(f"yes_ask{suffix}")
                if bid_raw is not None and ask_raw is not None:
                    try:
                        bid = float(bid_raw)
                        ask = float(ask_raw)
                        if bid > 1 or ask > 1:
                            bid /= 100.0
                            ask /= 100.0
                        if 0 < bid <= ask <= 1:
                            yes_bid = bid
                            yes_ask = ask
                            break
                    except (TypeError, ValueError):
                        continue

            if yes_bid is None or yes_ask is None:
                return None

            spread = yes_ask - yes_bid

            # Estimate volume from available data
            volume_24h = market.get("volume", 0) or market.get("volume_24h", 0)
            open_interest = market.get("open_interest", 0) or 0
            no_volume = market.get("no_volume", 0) or 0
            yes_volume = market.get("yes_volume", 0) or 0

            # Kalshi doesn't expose order book depth directly,
            # so we infer from 24h volume and open interest
            est_total_volume = max(
                int(volume_24h) if volume_24h else 0,
                int(open_interest) if open_interest else 0,
                int(no_volume) + int(yes_volume),
                1,
            )

            # Infer bid/ask depth proportionally from spread
            # Tighter spread implies deeper market
            tightness_factor = max(0, 1.0 - spread * 20)  # 0.01 spread → 0.8, 0.05 spread → 0.0
            volume_factor = min(1.0, est_total_volume / 1000.0)
            implied_depth = 0.3 + 0.4 * tightness_factor + 0.3 * volume_factor

            bid_volume_est = max(1, int(est_total_volume * 0.4 * implied_depth))
            ask_volume_est = max(1, int(est_total_volume * 0.4 * implied_depth))

            # Last price
            last_price = None
            for key in ("last_price_dollars", "last_price"):
                lp = market.get(key)
                if lp is not None:
                    try:
                        lp_val = float(lp)
                        if lp_val > 1:
                            lp_val /= 100.0
                        last_price = lp_val
                        break
                    except (TypeError, ValueError):
                        continue

            snapshot = MarketDepthSnapshot(
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                spread=round(spread, 5),
                bid_volume=bid_volume_est,
                ask_volume=ask_volume_est,
                total_volume=est_total_volume,
                last_price=last_price,
                implied_depth_score=round(implied_depth, 3),
            )

            self._depth_cache[ticker] = snapshot
            return snapshot

        except Exception as e:
            _LOGGER.warning("kalshi_depth_exception ticker=%s error=%s", ticker, e)
            return None

    def clear_cache(self) -> None:
        """Clear the bid/ask depth cache."""
        self._depth_cache.clear()


# ─── Helper functions for backward-compatible single-call usage ──────────

def get_dynamic_spread(ticker: str) -> float:
    """Convenience: get live spread for a ticker using the default model."""
    return MARKET_COST_MODEL.get_dynamic_spread(ticker)


def estimate_slippage(ticker: str, contracts: float = 1.0, is_buy: bool = True) -> SlippageEstimate:
    """Convenience: estimate slippage using the default model."""
    return MARKET_COST_MODEL.estimate_slippage(ticker, contracts, is_buy)


# Default singleton instance with measured mean spread of 3.1¢
MARKET_COST_MODEL = MarketCostModel()
