#!/usr/bin/env python3
"""
Adaptive Confidence Thresholds — Per-Signal Per-City Dynamic Threshold Manager

Tracks rolling 30-day accuracy per signal per city and adjusts the confidence
threshold at which signals are accepted for trading.

Logic:
  - If accuracy > 70%: lower threshold by 0.1 (not below 0.3)
  - If accuracy < 50%: raise threshold by 0.1 (not above 0.9)
  - If 50-70%: keep current threshold
  - Threshold adjustment happens daily (not per-trade)
  - State persisted to data/adaptive_thresholds.json

Usage:
    from core.adaptive_thresholds import AdaptiveThresholdManager
    thresholds = AdaptiveThresholdManager()
    thresholds.update_all(signal_history)
    trade_threshold = thresholds.get_threshold("gaussian", "KNYC")
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_THRESHOLD = 0.5
ADJUSTMENT_STEP = 0.1
MIN_THRESHOLD = 0.3
MAX_THRESHOLD = 0.9
HIGH_ACCURACY_BAR = 0.70   # Above this -> lower threshold
LOW_ACCURACY_BAR = 0.50    # Below this -> raise threshold
ROLLING_WINDOW_DAYS = 30   # Rolling accuracy window

# Path for persistence
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_THRESHOLDS_PATH = os.path.join(_BASE, "data", "adaptive_thresholds.json")


class AdaptiveThresholdManager:
    """
    Manages per-signal per-city adaptive confidence thresholds.

    Thresholds are adjusted daily based on the rolling 30-day accuracy
    of each signal+city combination.
    """

    def __init__(self, thresholds_path: str = None):
        self.thresholds_path = thresholds_path or _DEFAULT_THRESHOLDS_PATH
        self.thresholds: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._last_adjustment_date: Optional[str] = None
        self._load()

    def _load(self):
        """Load persisted threshold state from disk."""
        try:
            if os.path.exists(self.thresholds_path):
                with open(self.thresholds_path, 'r') as f:
                    data = json.load(f)
                self.thresholds = defaultdict(dict, data.get('thresholds', {}))
                self._last_adjustment_date = data.get('last_adjustment_date')
                _logger.info(
                    f"Loaded adaptive thresholds for "
                    f"{sum(len(cities) for cities in self.thresholds.values())} "
                    f"signal+city combinations"
                )
        except Exception as e:
            _logger.warning(f"Failed to load adaptive thresholds: {e}")
            self.thresholds = defaultdict(dict)
            self._last_adjustment_date = None

    def _save(self):
        """Persist threshold state to disk."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.thresholds_path), exist_ok=True)
            with open(self.thresholds_path, 'w') as f:
                json.dump({
                    'thresholds': dict(self.thresholds),
                    'last_adjustment_date': self._last_adjustment_date,
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
            _logger.debug(f"Saved adaptive thresholds to {self.thresholds_path}")
        except Exception as e:
            _logger.error(f"Failed to save adaptive thresholds: {e}")

    def get_threshold(self, signal_name: str, city: str) -> float:
        """
        Get the current adaptive threshold for a signal+city combination.

        Falls back to DEFAULT_THRESHOLD if no adaptive threshold exists.

        Args:
            signal_name: Signal name (e.g., 'gaussian', 'pressure_delta')
            city: City code (e.g., 'KNYC', 'KLAX')

        Returns:
            Current threshold value (0.3-0.9)
        """
        return self.thresholds.get(signal_name, {}).get(city, DEFAULT_THRESHOLD)

    def set_threshold(self, signal_name: str, city: str, threshold: float):
        """
        Set a threshold for a signal+city combination and persist.

        Args:
            signal_name: Signal name
            city: City code
            threshold: Threshold value (clamped to [MIN_THRESHOLD, MAX_THRESHOLD])
        """
        threshold = max(MIN_THRESHOLD, min(MAX_THRESHOLD, threshold))
        self.thresholds[signal_name][city] = threshold
        self._save()

    def update_all(
        self,
        signal_history: Dict[Tuple[str, str], List[bool]],
        today: str = None,
    ) -> Dict[str, Dict[str, float]]:
        """
        Update all thresholds based on rolling 30-day accuracy.

        Call this once per day (not per-trade).

        Args:
            signal_history: dict mapping (signal_name, city) -> list of bools
                           (was_correct) for the last 30 days
            today: ISO date string for the adjustment day. If None, uses today UTC.

        Returns:
            dict mapping signal_name -> {city: adjusted_threshold}
        """
        if today is None:
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # Skip if already adjusted today
        if self._last_adjustment_date == today:
            _logger.debug(f"Thresholds already adjusted for {today}, skipping")
            return self.get_all_thresholds()

        adjustments_made = 0

        for (signal_name, city), correctness_list in signal_history.items():
            if len(correctness_list) < 5:  # Need minimum samples
                continue

            # Use only last ROLLING_WINDOW_DAYS observations
            recent = correctness_list[-ROLLING_WINDOW_DAYS:]
            accuracy = sum(1 for c in recent if c) / len(recent)

            current = self.get_threshold(signal_name, city)

            if accuracy > HIGH_ACCURACY_BAR:
                # Signal is performing well: lower the threshold
                new_threshold = max(MIN_THRESHOLD, current - ADJUSTMENT_STEP)
                if new_threshold != current:
                    self.thresholds[signal_name][city] = new_threshold
                    adjustments_made += 1
                    _logger.debug(
                        f"AdaptiveThreshold: {signal_name}@{city} "
                        f"accuracy={accuracy:.2%} > 70%, "
                        f"threshold {current:.2f} -> {new_threshold:.2f}"
                    )

            elif accuracy < LOW_ACCURACY_BAR:
                # Signal is underperforming: raise the threshold
                new_threshold = min(MAX_THRESHOLD, current + ADJUSTMENT_STEP)
                if new_threshold != current:
                    self.thresholds[signal_name][city] = new_threshold
                    adjustments_made += 1
                    _logger.debug(
                        f"AdaptiveThreshold: {signal_name}@{city} "
                        f"accuracy={accuracy:.2%} < 50%, "
                        f"threshold {current:.2f} -> {new_threshold:.2f}"
                    )

            # If 50-70%: keep current threshold (no action needed)

        self._last_adjustment_date = today
        self._save()

        if adjustments_made > 0:
            _logger.info(
                f"AdaptiveThreshold: adjusted {adjustments_made} thresholds "
                f"on {today}"
            )

        return self.get_all_thresholds()

    def get_all_thresholds(self) -> Dict[str, Dict[str, float]]:
        """
        Get all current thresholds.

        Returns:
            dict mapping signal_name -> {city: threshold}
        """
        return dict(self.thresholds)

    def get_last_adjustment_date(self) -> Optional[str]:
        """Get the date of the last threshold adjustment."""
        return self._last_adjustment_date

    def reset(self):
        """Reset all thresholds to defaults."""
        self.thresholds = defaultdict(dict)
        self._last_adjustment_date = None
        self._save()
        _logger.info("Adaptive thresholds reset to defaults")