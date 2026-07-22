#!/usr/bin/env python3
"""
Spread-Based Entry Signal — Market Micro Structure Signal.

Detects informed trading opportunities from spread dynamics:
- Widening spreads often indicate informed traders pulling liquidity
- Narrowing spreads indicate consensus / low uncertainty
- Spread-to-volume ratio helps filter noise

Core insight: When bid-ask spreads widen without a clear news catalyst,
it's often because informed participants are reducing their footprint
(making markets less liquid). This is a contrarian entry signal.

Version: 1.0 — 2026-07-22 (Phase 19.3)
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from core.market_cost_model import MARKET_COST_MODEL
from core.signals.base_signal import BaseSignal

_LOGGER = logging.getLogger(__name__)

# Default thresholds
SPREAD_WIDENING_THRESHOLD = 0.04   # 4¢ — threshold for "wide" spread
SPREAD_NARROW_THRESHOLD = 0.02     # 2¢ — threshold for "narrow" spread
BASELINE_SPREAD = 0.031            # Mean measured spread
LOOKBACK_MINUTES = 30              # How far back to compare spreads


class SpreadBasedEntrySignal(BaseSignal):
    """
    Market Micro Signal: Spread-Based Entry.

    Evaluates whether current spread conditions favor entry:
    - Widening spreads → signal strength HIGH (liquidity providers pulling back,
      potential for mean reversion in spread)
    - Narrowing spreads → signal strength LOW (consensus, less edge)
    - Spread-to-volume ratio filters noise

    This is a gating / confidence modulation signal, not a directional signal.
    It tells you WHEN to trade, not WHICH direction.
    """

    def __init__(
        self,
        db_path: str,
        widening_threshold: float = SPREAD_WIDENING_THRESHOLD,
        narrow_threshold: float = SPREAD_NARROW_THRESHOLD,
    ):
        super().__init__(db_path)
        self.name = "spread_based_entry"
        self.min_lookback = 1  # Real-time signal, no lookback needed
        self.widening_threshold = widening_threshold
        self.narrow_threshold = narrow_threshold
        self.cost_model = MARKET_COST_MODEL

    def evaluate(self, idx: int, days: int) -> Dict[str, any]:
        """
        Evaluate spread conditions for the given index.

        Returns a dict with:
            - signal: float (-1 to 1), positive = favorable entry conditions
            - confidence: float (0-1)
            - metadata: dict with spread details
        """
        # Get the timestamp for this bar
        timestamp = self._get_timestamp_for_idx(idx)
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        # Try to get live spread from Kalshi API for the ticker
        ticker = self._get_ticker_for_idx(idx)
        spread = BASELINE_SPREAD  # fallback
        depth = None

        if ticker:
            depth = self.cost_model.get_market_depth_snapshot(ticker)
            if depth:
                spread = depth.spread

        # Score the spread environment
        signal, confidence, details = self._score_spread_environment(
            current_spread=spread,
            depth=depth,
            ticker=ticker,
        )

        return {
            'signal': signal,
            'confidence': confidence,
            'metadata': {
                'signal_name': self.name,
                'spread': spread,
                'baseline_spread': BASELINE_SPREAD,
                'spread_ratio': spread / max(BASELINE_SPREAD, 1e-8),
                'depth_score': depth.implied_depth_score if depth else None,
                'bid_volume': depth.bid_volume if depth else None,
                'ask_volume': depth.ask_volume if depth else None,
                'widening_threshold': self.widening_threshold,
                'narrow_threshold': self.narrow_threshold,
                'timestamp': timestamp.isoformat(),
                **details,
            }
        }

    def _score_spread_environment(
        self,
        current_spread: float,
        depth: Optional[any],
        ticker: Optional[str],
    ) -> Tuple[float, float, Dict]:
        """
        Score the spread environment on a [-1, 1] scale.

        Positive = favorable entry conditions.
        Negative = unfavorable (tight spread, no edge from micro structure).

        Logic:
        - Spread > 4¢ (widening): signal = +0.4 to +0.7 (informed traders active)
        - Spread < 2¢ (tight): signal = -0.3 to -0.5 (consensus, no micro edge)
        - Spread 2-4¢ (normal): signal = 0.0 to +0.3 (neutral to slightly favorable)
        - Depth score adjusts confidence: deeper markets = more reliable signal
        """
        details = {}

        # Spread ratio vs baseline
        spread_ratio = current_spread / max(BASELINE_SPREAD, 1e-8)

        # Base signal from spread width
        if current_spread >= self.widening_threshold:
            # Widening spread — informed traders pulling liquidity
            # Stronger signal as spread widens, but diminishing returns
            excess = current_spread - self.widening_threshold
            signal = min(0.7, 0.4 + excess * 5.0)
            details['spread_regime'] = 'widening'
            details['widening_excess'] = round(excess, 4)
        elif current_spread <= self.narrow_threshold:
            # Tight spread — consensus, low edge opportunity
            deficit = self.narrow_threshold - current_spread
            signal = max(-0.5, -0.3 - deficit * 10.0)
            details['spread_regime'] = 'tight'
            details['tightness_deficit'] = round(deficit, 4)
        else:
            # Normal spread — slightly favorable
            normalized = (current_spread - self.narrow_threshold) / (
                self.widening_threshold - self.narrow_threshold
            )
            signal = 0.0 + normalized * 0.3
            details['spread_regime'] = 'normal'
            details['spread_percentile'] = round(normalized, 3)

        # Confidence adjustment
        if depth:
            # Higher depth score → more reliable signal
            base_confidence = 0.5 + 0.3 * depth.implied_depth_score
            # Volume asymmetry: big difference between bid/ask volume = more signal
            if depth.bid_volume > 0 and depth.ask_volume > 0:
                vol_ratio = min(depth.bid_volume, depth.ask_volume) / max(
                    depth.bid_volume, depth.ask_volume, 1
                )
                asymmetry_boost = (1.0 - vol_ratio) * 0.2
                base_confidence += asymmetry_boost
                details['volume_asymmetry'] = round(1.0 - vol_ratio, 3)
        else:
            base_confidence = 0.5  # Neutral when no depth data

        confidence = min(0.95, max(0.1, base_confidence))
        details['base_confidence'] = round(base_confidence, 3)

        return round(signal, 4), round(confidence, 3), details

    def _get_timestamp_for_idx(self, idx: int):
        """Get the timestamp for a given index. Override in subclass."""
        return None

    def _get_ticker_for_idx(self, idx: int):
        """Get the ticker for a given index. Override in subclass."""
        return None