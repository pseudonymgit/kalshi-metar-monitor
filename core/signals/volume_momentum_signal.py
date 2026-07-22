#!/usr/bin/env python3
"""
Volume Momentum Signal — Market Micro Structure Signal.

Detects momentum from volume dynamics:
- Volume spikes confirm or contradict price/signal direction
- High volume + high spread = informed flow (strong signal)
- High volume + tight spread = sentiment-driven flow (weak signal)
- Low volume + wide spread = noise (ignore)

This is a confirmation / contradiction signal that modulates
confidence on existing directional signals.

Version: 1.0 — 2026-07-22 (Phase 19.3)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from core.market_cost_model import MARKET_COST_MODEL
from core.signals.base_signal import BaseSignal

_LOGGER = logging.getLogger(__name__)

# Volume thresholds (contracts)
LOW_VOLUME_THRESHOLD = 50
HIGH_VOLUME_THRESHOLD = 500

# Spread thresholds (fraction of $1)
TIGHT_SPREAD_THRESHOLD = 0.02
WIDE_SPREAD_THRESHOLD = 0.04

# Volume baseline for spike detection
DEFAULT_VOLUME_BASELINE = 200  # Average daily volume for a typical weather contract


class VolumeMomentumSignal(BaseSignal):
    """
    Market Micro Signal: Volume Momentum.

    Evaluates whether current volume conditions confirm or contradict
    a directional trading signal.

    This is a confidence modulation signal:
    - Volume spike + wide spread = informed liquidity takers → CONFIRM
    - Volume spike + tight spread = sentiment-driven → WEAKEN
    - Low volume = no confirmation → NEUTRAL
    """

    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._signal_name = "volume_momentum"
        self._signal_min_lookback = 1
        self.cost_model = MARKET_COST_MODEL

    @property
    def name(self) -> str:
        return self._signal_name

    @property
    def min_lookback(self) -> int:
        return self._signal_min_lookback

    def evaluate(self, idx: int, days: list) -> tuple:
        """
        Evaluate volume momentum conditions.

        Returns standard (direction, confidence) tuple for BaseSignal interface.
        direction = None (modulation signal), confidence = volume-based confidence.
        """
        result = self.evaluate_raw(idx, days)
        signal = result.get('signal', 0.0)
        confidence = result.get('confidence', 0.0)
        return None, min(float(confidence), 1.0)

    def evaluate_raw(self, idx: int, days: list) -> Dict[str, any]:
        """
        Evaluate volume momentum conditions.

        Returns:
            dict with:
                - signal: float (-1 to 1), positive = confirms directional signal
                - confidence: float (0-1)
                - metadata: dict with volume details
        """
        timestamp = datetime.now(timezone.utc)
        ticker = self._get_ticker_for_idx(idx)
        depth = None
        volume = DEFAULT_VOLUME_BASELINE
        spread = 0.031

        if ticker:
            depth = self.cost_model.get_market_depth_snapshot(ticker)
            if depth:
                volume = depth.total_volume
                spread = depth.spread

        signal, confidence, details = self._score_volume_environment(
            volume=volume,
            spread=spread,
            depth=depth,
        )

        return {
            'signal': signal,
            'confidence': confidence,
            'metadata': {
                'signal_name': self.name,
                'volume': volume,
                'volume_baseline': DEFAULT_VOLUME_BASELINE,
                'volume_ratio': volume / max(DEFAULT_VOLUME_BASELINE, 1),
                'spread': spread,
                'depth_score': depth.implied_depth_score if depth else None,
                'timestamp': timestamp.isoformat(),
                **details,
            }
        }

    def _score_volume_environment(
        self,
        volume: int,
        spread: float,
        depth: Optional[any],
    ) -> Tuple[float, float, Dict]:
        """
        Score volume environment on [-1, 1] scale.

        Positive = confirms directional trading signal.
        Negative = contradicts / weakens signal.

        Quadrants:
        - High volume + wide spread → informed flow (+0.5 to +0.8)
        - High volume + tight spread → sentiment flow (-0.2 to -0.5)
        - Low volume + wide spread → noise (-0.3 to -0.6)
        - Low volume + tight spread → neutral to slightly positive (0.0 to +0.3)
        """
        details = {}

        vol_ratio = volume / max(DEFAULT_VOLUME_BASELINE, 1)
        is_high_volume = volume >= HIGH_VOLUME_THRESHOLD
        is_low_volume = volume <= LOW_VOLUME_THRESHOLD
        is_wide_spread = spread >= WIDE_SPREAD_THRESHOLD
        is_tight_spread = spread <= TIGHT_SPREAD_THRESHOLD

        if is_high_volume and is_wide_spread:
            # Informed flow: volume + wide spread = liquidity takers
            vol_factor = min(1.0, vol_ratio * 0.3)
            spread_factor = min(1.0, (spread - WIDE_SPREAD_THRESHOLD) * 20)
            signal = 0.5 + 0.3 * (vol_factor + spread_factor) / 2.0
            details['volume_regime'] = 'informed_flow'
            details['is_informed'] = True
            confidence_base = 0.7
        elif is_high_volume and is_tight_spread:
            # Sentiment-driven flow: volume + tight = crowd following
            vol_factor = min(1.0, vol_ratio * 0.2)
            signal = -0.2 - 0.3 * vol_factor
            details['volume_regime'] = 'sentiment_flow'
            details['is_informed'] = False
            confidence_base = 0.5
        elif is_low_volume and is_wide_spread:
            # Noise: low volume, wide spread = no one trading
            signal = -0.3 - 0.3 * min(1.0, (spread - WIDE_SPREAD_THRESHOLD) * 10)
            details['volume_regime'] = 'noise'
            details['is_informed'] = False
            confidence_base = 0.4
        else:
            # Normal volume, normal spread = neutral
            # Slightly positive if volume is somewhat elevated
            normalized_vol = min(1.0, max(0, vol_ratio - 0.5) * 2.0)
            signal = 0.0 + 0.3 * normalized_vol
            details['volume_regime'] = 'normal'
            details['is_informed'] = not is_low_volume
            confidence_base = 0.5

        # Confidence adjustment from depth
        if depth and depth.implied_depth_score > 0.5:
            confidence_base += 0.15
        elif depth and depth.implied_depth_score < 0.3:
            confidence_base -= 0.1

        confidence = min(0.95, max(0.1, confidence_base))
        details['confidence_base'] = round(confidence_base, 3)

        return round(signal, 4), round(confidence, 3), details

    def _get_ticker_for_idx(self, idx: int):
        """Get the ticker for a given index. Override in subclass."""
        return None