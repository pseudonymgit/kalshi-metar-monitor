#!/usr/bin/env python3
"""
ADVANCE Signal 1: Spread-Based Entry (<1d)

Detects when the Kalshi market bid/ask spread narrows enough for profitable
entry before settlement. Trades the convergence between market price and
expected settlement value as the spread compresses.

B-Mode compliant. No AI/ML.

Usage:
    from core.signals.spread_based_entry_signal import SpreadBasedEntryDetector

    detector = SpreadBasedEntryDetector()
    result = detector.check(bid=55, ask=57, volume_24h=1500.0,
                            hours_to_settlement=3.5, station="KNYC", bucket_temp_f=84)
    if result["entry"]:
        # execute trade
        pass
"""

import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from .base_signal import BaseSignal

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Default config — overridable per station/bucket
MIN_SPREAD_PROFIT_MARGIN = 0.5  # cents: spread must be < profit after costs
TRANSACTION_COST_CENTS = 1.0     # 1¢ per side round trip
MAX_POSITION_DOLLARS = 200.0
MIN_POSITION_DOLLARS = 5.0
BASE_VOLUME_PCT = 0.02
STOP_LOSS_PCT = 0.30
EXIT_SPREAD_REWIDEN_MULTIPLIER = 1.5  # times entry spread to trigger exit
MIN_HOURS_TO_SETTLEMENT = 1.0
MAX_SPREAD_PERCENTILE = 50  # percentile of historical spread distribution


class SpreadBasedEntryDetector(BaseSignal):
    """Detects profitable spread-based entry opportunities."""

    def __init__(self, db_path: str = None, config: Optional[dict] = None):
        super().__init__(db_path)
        self.config = config or {}
        # Station-specific spread percentiles: {station: {bucket_temp_f: percentile_map}}
        self.spread_history: Dict[str, Dict[int, list]] = {}

    def record_spread(self, station: str, bucket_temp_f: int, spread_cents: int):
        """Record a spread observation for historical percentile computation."""
        if station not in self.spread_history:
            self.spread_history[station] = {}
        if bucket_temp_f not in self.spread_history[station]:
            self.spread_history[station][bucket_temp_f] = []
        self.spread_history[station][bucket_temp_f].append(spread_cents)
        # Keep last 100 observations
        if len(self.spread_history[station][bucket_temp_f]) > 100:
            self.spread_history[station][bucket_temp_f].pop(0)

    def get_spread_percentile(self, station: str, bucket_temp_f: int, spread_cents: int) -> float:
        """Return the percentile rank of current spread against history."""
        hist = self.spread_history.get(station, {}).get(bucket_temp_f, [])
        if not hist:
            return 0.5  # Default to median if no history
        count_below = sum(1 for s in hist if s <= spread_cents)
        return count_below / len(hist)

    @property
    def name(self) -> str:
        """Canonical name for this signal."""
        return "spread_based_entry"

    @property
    def min_lookback(self) -> int:
        """Minimum number of prior days required — needs 5 spread observations for percentile."""
        return 5

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate signal at day index. This is an execution-layer component that
        produces no directional signal from weather data alone. Returns a default
        confidence placeholder until the sweep is revised to pass order book data.

        Returns:
            (None, 0.5) if days has data, (None, 0.0) otherwise.
        """
        if days and len(days) > 0:
            return None, 0.5
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date.
        Execution-layer component; returns placeholder until sweep is revised
        to pass order book data.

        Returns:
            (None, 0.0)
        """
        return None, 0.0

    def check(self, bid: int, ask: int, volume_24h: float,
              hours_to_settlement: float, station: str,
              bucket_temp_f: int) -> Dict:
        """
        Evaluate spread-based entry opportunity.

        Args:
            bid: YES bid price in cents
            ask: YES ask price in cents
            volume_24h: 24-hour dollar volume
            hours_to_settlement: hours until market settlement
            station: Kalshi station code (e.g., "KNYC")
            bucket_temp_f: temperature bucket being evaluated

        Returns:
            Dict with flags: entry, exit, score, reason, units
        """
        result = {
            "entry": False,
            "exit": False,
            "exit_reason": None,
            "score": 0.0,
            "reason": None,
            "units": 0,
            "dollar_value": 0.0,
        }

        # Gate: minimum time to settlement
        if hours_to_settlement < MIN_HOURS_TO_SETTLEMENT:
            result["reason"] = "Too close to settlement"
            return result

        # Gate: minimum volume
        if volume_24h < 500.0:
            result["reason"] = "Insufficient volume"
            return result

        spread = ask - bid
        fair_value = (bid + ask) / 2.0

        # Gate: check if spread is profitable after costs
        profit_margin = (100 - fair_value) / 100.0 * fair_value  # simplified EV
        net_profit = profit_margin - spread - TRANSACTION_COST_CENTS

        if net_profit <= MIN_SPREAD_PROFIT_MARGIN:
            result["reason"] = f"Spread too wide: {spread}¢, net profit {net_profit:.1f}¢"
            return result

        # Spread percentile against historical baseline
        spread_pct = self.get_spread_percentile(station, bucket_temp_f, spread)
        if spread_pct > MAX_SPREAD_PERCENTILE / 100.0:
            result["reason"] = f"Spread {spread}¢ not compressed (pctile={spread_pct:.2f})"
            return result

        # Entry signal
        spread_compression_strength = (1.0 - spread_pct)  # 1.0 = very compressed
        result["entry"] = True
        result["score"] = round(spread_compression_strength, 3)
        result["reason"] = (f"Spread compressed: {spread}¢ (pctile={spread_pct:.2f}), "
                           f"net profit={net_profit:.1f}¢")

        # Position sizing
        base_units = int(BASE_VOLUME_PCT * volume_24h / fair_value * 100)  # contracts
        scalar = min(spread_compression_strength, 1.0)
        units = max(1, int(base_units * scalar))
        dollar_value = min(units * fair_value / 100.0, MAX_POSITION_DOLLARS)
        if dollar_value < MIN_POSITION_DOLLARS:
            dollar_value = 0.0
            units = 0
            result["entry"] = False
            result["reason"] = "Position below minimum"

        result["units"] = units
        result["dollar_value"] = round(dollar_value, 2)
        return result

    def check_exit(self, entry_spread: int, current_ask: int, current_bid: int,
                   bid: int, ask: int, profit_loss_pct: float) -> Dict:
        """
        Evaluate exit condition for a position opened by this signal.

        Args:
            entry_spread: spread at entry (cents)
            current_ask, current_bid: current market prices (cents)
            profit_loss_pct: current P&L as % of entry

        Returns:
            Dict with exit flag and reason
        """
        result = {"exit": False, "reason": None}
        current_spread = ask - bid

        # E1: Spread re-widened
        if current_spread > entry_spread * EXIT_SPREAD_REWIDEN_MULTIPLIER:
            result["exit"] = True
            result["reason"] = "Spread re-widened beyond threshold"
            return result

        # E4: Stop loss
        if profit_loss_pct <= -STOP_LOSS_PCT:
            result["exit"] = True
            result["reason"] = f"Stop loss hit ({profit_loss_pct:.1f}%)"
            return result

        return result