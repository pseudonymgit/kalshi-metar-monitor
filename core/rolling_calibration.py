#!/usr/bin/env python3
"""
A9 — Rolling 30-Day Calibration for Spike Reversion Confidence

Implements rolling 30-day recalibration for spike reversion confidence scores.
Old calibration data decays out; recent performance weights more heavily.

Key design:
- Rolling window: only the most recent 30 days of data are used
- Time-decay weighting: more recent observations weight more heavily
- Per-station, per-market-type (HIGH/LOW) calibration
- Exponential moving average of accuracy over the rolling window
- Produces an adjusted confidence score based on recent historical accuracy

Usage:
    from core.rolling_calibration import RollingCalibration, CalibratedConfidence

    cal = RollingCalibration(window_days=30)
    cal.record_trade("KATL", "HIGH", "spike_reversion", confidence=0.70, was_correct=True)
    result = cal.get_calibrated_confidence("KATL", "HIGH", raw_confidence=0.70)
    print(result.calibrated_confidence)  # Adjusted based on recent history
"""

import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────

DEFAULT_WINDOW_DAYS = 30
DEFAULT_CACHE_FILE = "data/rolling_calibration_cache.json"
DEFAULT_CACHE_TTL_HOURS = 1
DEFAULT_ALPHA = 0.3  # Exponential smoothing factor (higher = more weight on recent)

# Minimum observations before calibration kicks in
MIN_OBSERVATIONS = 20  # B-Mode R8 Cycle 2.4: increased from 5 for statistical stability

# Fallback when insufficient data
DEFAULT_FALLBACK_CONFIDENCE = 0.50

# Clamp calibrated confidence to this range
MIN_CALIBRATED = 0.10
MAX_CALIBRATED = 0.95


# ─── Result ────────────────────────────────────────────────────

class CalibratedConfidence:
    """
    Result of a rolling calibration query.

    Attributes:
        raw_confidence: Original uncalibrated confidence
        calibrated_confidence: Adjusted confidence score
        historical_accuracy: Rolling accuracy for this station/market
        observations: Number of observations in the rolling window
        window_days: Size of the rolling window
        signal_name: Signal name (e.g. 'spike_reversion')
    """

    def __init__(
        self,
        raw_confidence: float,
        calibrated_confidence: float,
        historical_accuracy: float,
        observations: int,
        window_days: int,
        signal_name: str,
    ):
        self.raw_confidence = raw_confidence
        self.calibrated_confidence = calibrated_confidence
        self.historical_accuracy = historical_accuracy
        self.observations = observations
        self.window_days = window_days
        self.signal_name = signal_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_confidence": round(self.raw_confidence, 4),
            "calibrated_confidence": round(self.calibrated_confidence, 4),
            "historical_accuracy": round(self.historical_accuracy, 4),
            "observations": self.observations,
            "window_days": self.window_days,
            "signal_name": self.signal_name,
        }

    def __repr__(self) -> str:
        return (f"<CalibratedConfidence raw={self.raw_confidence:.3f} "
                f"cal={self.calibrated_confidence:.3f} "
                f"hist_acc={self.historical_accuracy:.3f} "
                f"obs={self.observations}>")


# ─── RollingCalibration ────────────────────────────────────────

class RollingCalibration:
    """
    Rolling 30-day calibration for spike reversion confidence scores.

    Maintains a time-windowed history of trade outcomes per station and
    market type. Uses exponential decay to weight recent observations more
    heavily than older ones.

    The calibration adjusts raw confidence toward the historical accuracy:
    - Station with 70% recent accuracy + raw confidence 0.60 → calibrated ~0.65
    - Station with 45% recent accuracy + raw confidence 0.70 → calibrated ~0.55
    """

    def __init__(
        self,
        window_days: int = DEFAULT_WINDOW_DAYS,
        alpha: float = DEFAULT_ALPHA,
        cache_file: str = DEFAULT_CACHE_FILE,
        cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
    ):
        self.window_days = window_days
        self.alpha = alpha
        self.cache_file = Path(cache_file)
        self.cache_ttl_hours = cache_ttl_hours

        # Trade history: {signal: {station: {market_type: [(timestamp, was_correct, confidence), ...]}}}
        self._trade_history: Dict[str, Dict[str, Dict[str, List[Tuple[float, bool, float]]]]] = {
            "spike_reversion": defaultdict(lambda: defaultdict(list)),
        }
        self._last_saved = 0.0

        # Load from cache
        self._load_cache()

    # ── Public API ────────────────────────────────────────────

    def record_trade(
        self,
        station: str,
        market_type: str,
        signal_name: str = "spike_reversion",
        confidence: float = 0.5,
        was_correct: Optional[bool] = None,
        timestamp: Optional[float] = None,
        trade_time: Optional[datetime] = None,
    ) -> None:
        """
        Record a trade outcome for rolling calibration.

        Args:
            station: Station code
            market_type: 'HIGH' or 'LOW'
            signal_name: Signal name
            confidence: Raw confidence at trade time
            was_correct: True if trade was correct, False if not, None if pending
            timestamp: Unix timestamp (defaults to now)
            trade_time: Alternative datetime object
        """
        if timestamp is None:
            if trade_time is not None:
                timestamp = trade_time.timestamp()
            else:
                timestamp = time.time()

        # Ensure signal entry exists
        if signal_name not in self._trade_history:
            self._trade_history[signal_name] = defaultdict(lambda: defaultdict(list))
        if station not in self._trade_history[signal_name]:
            self._trade_history[signal_name][station] = defaultdict(list)

        self._trade_history[signal_name][station][market_type].append(
            (timestamp, was_correct, confidence)
        )

        # Prune old entries
        self._prune(signal_name, station, market_type)

        # Persist periodically
        self._save_cache()

    def record_outcome(
        self,
        station: str,
        market_type: str,
        signal_name: str = "spike_reversion",
        was_correct: bool = True,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Record the outcome of a previously logged trade.

        Args:
            station: Station code
            market_type: 'HIGH' or 'LOW'
            signal_name: Signal name
            was_correct: True if trade was directionally correct
            timestamp: Unix timestamp of trade (defaults to now)
        """
        self.record_trade(
            station=station,
            market_type=market_type,
            signal_name=signal_name,
            was_correct=was_correct,
            timestamp=timestamp,
        )

    def get_calibrated_confidence(
        self,
        station: str,
        market_type: str = "HIGH",
        raw_confidence: float = 0.5,
        signal_name: str = "spike_reversion",
    ) -> CalibratedConfidence:
        """
        Get the calibrated confidence score for a station and market type.

        Adjusts the raw confidence based on recent historical accuracy.

        Args:
            station: Station code
            market_type: 'HIGH' or 'LOW'
            raw_confidence: Raw uncalibrated confidence [0.0, 1.0]
            signal_name: Signal name

        Returns:
            CalibratedConfidence with adjusted score
        """
        # Get rolling accuracy
        historical_accuracy, observations, recent_trades = self._compute_rolling_accuracy(
            station, market_type, signal_name
        )

        if observations < MIN_OBSERVATIONS:
            # Not enough data — use raw confidence directly (bounded)
            return CalibratedConfidence(
                raw_confidence=raw_confidence,
                calibrated_confidence=max(MIN_CALIBRATED, min(MAX_CALIBRATED, raw_confidence)),
                historical_accuracy=0.50,
                observations=observations,
                window_days=self.window_days,
                signal_name=signal_name,
            )

        # Calibrate: blend raw confidence toward historical accuracy
        # The blend factor depends on how many recent observations we have
        weight = min(1.0, observations / 50.0)  # Saturate at 50 observations
        calibrated = (1.0 - weight) * raw_confidence + weight * historical_accuracy

        # Clamp to valid range
        calibrated = max(MIN_CALIBRATED, min(MAX_CALIBRATED, calibrated))

        return CalibratedConfidence(
            raw_confidence=raw_confidence,
            calibrated_confidence=calibrated,
            historical_accuracy=historical_accuracy,
            observations=observations,
            window_days=self.window_days,
            signal_name=signal_name,
        )

    def get_rolling_accuracy(
        self,
        station: str,
        market_type: str = "HIGH",
        signal_name: str = "spike_reversion",
    ) -> Tuple[float, int]:
        """
        Get the rolling accuracy for a station and market type.

        Args:
            station: Station code
            market_type: 'HIGH' or 'LOW'
            signal_name: Signal name

        Returns:
            (accuracy, observation_count)
        """
        accuracy, count, _ = self._compute_rolling_accuracy(
            station, market_type, signal_name
        )
        return accuracy, count

    def get_all_rolling_accuracies(
        self,
        signal_name: str = "spike_reversion",
    ) -> Dict[str, Dict[str, float]]:
        """
        Get all rolling accuracies across all stations and markets.

        Returns:
            {station: {market_type: accuracy}}
        """
        result = {}
        if signal_name not in self._trade_history:
            return result

        for station, markets in self._trade_history[signal_name].items():
            result[station] = {}
            for market_type, trades in markets.items():
                acc, count = self.get_rolling_accuracy(station, market_type, signal_name)
                if count > 0:
                    result[station][market_type] = round(acc, 4)

        return result

    def clear_history(
        self,
        station: Optional[str] = None,
        market_type: Optional[str] = None,
        signal_name: str = "spike_reversion",
    ) -> None:
        """
        Clear trade history for a station/market, or all if no args.

        Args:
            station: Station code (None = all stations)
            market_type: 'HIGH' or 'LOW' (None = all types)
            signal_name: Signal name
        """
        if signal_name not in self._trade_history:
            return

        if station is None:
            self._trade_history[signal_name].clear()
        elif station in self._trade_history[signal_name]:
            if market_type is None:
                self._trade_history[signal_name][station].clear()
            elif market_type in self._trade_history[signal_name][station]:
                self._trade_history[signal_name][station][market_type].clear()

        self._save_cache()

    # ── Internal ──────────────────────────────────────────────

    def _compute_rolling_accuracy(
        self,
        station: str,
        market_type: str,
        signal_name: str,
    ) -> Tuple[float, int, int]:
        """
        Compute time-decayed rolling accuracy.

        Uses exponential decay weighting where more recent observations
        contribute more to the accuracy estimate.

        Returns:
            (accuracy, observation_count, recent_trade_count)
        """
        if signal_name not in self._trade_history:
            return 0.50, 0, 0
        if station not in self._trade_history[signal_name]:
            return 0.50, 0, 0
        if market_type not in self._trade_history[signal_name][station]:
            return 0.50, 0, 0

        trades = self._trade_history[signal_name][station][market_type]

        # Prune first — ensure only window_days of data
        self._prune(signal_name, station, market_type)
        trades = self._trade_history[signal_name][station][market_type]

        if not trades:
            return 0.50, 0, 0

        # Count completed trades (those with was_correct != None)
        completed = [(ts, correct, conf) for ts, correct, conf in trades if correct is not None]
        if not completed:
            return 0.50, 0, len(trades)

        # Compute time-decayed accuracy using exponential weights
        now = time.time()
        weights = []
        correct_flags = []

        for ts, was_correct, conf in completed:
            age_hours = (now - ts) / 3600.0
            # Weight decays exponentially with age
            # Half-life = window_days / 2 (so data at window edge has ~25% weight)
            half_life_hours = self.window_days * 12  # half the window in hours
            weight = 2.0 ** (-age_hours / half_life_hours)
            weights.append(weight)
            correct_flags.append(1.0 if was_correct else 0.0)

        total_weight = sum(weights)
        if total_weight == 0:
            return 0.50, len(completed), len(trades)

        weighted_accuracy = sum(
            w * c for w, c in zip(weights, correct_flags)
        ) / total_weight

        # Count recent trades (last 7 days) for display
        recent_cutoff = now - (7 * 86400)
        recent_count = sum(1 for ts, _, _ in completed if ts > recent_cutoff)

        return weighted_accuracy, len(completed), recent_count

    def _prune(
        self,
        signal_name: str,
        station: str,
        market_type: str,
    ) -> None:
        """
        Remove trades older than the rolling window.

        Args:
            signal_name: Signal name
            station: Station code
            market_type: 'HIGH' or 'LOW'
        """
        cutoff = time.time() - (self.window_days * 86400)

        if signal_name not in self._trade_history:
            return
        if station not in self._trade_history[signal_name]:
            return
        if market_type not in self._trade_history[signal_name][station]:
            return

        trades = self._trade_history[signal_name][station][market_type]
        self._trade_history[signal_name][station][market_type] = [
            t for t in trades if t[0] >= cutoff
        ]

    def _load_cache(self) -> None:
        """Load calibration history from cache file."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    data = json.load(f)

                raw = data.get("trade_history", {})
                for signal, stations in raw.items():
                    if signal not in self._trade_history:
                        self._trade_history[signal] = defaultdict(lambda: defaultdict(list))
                    for station, markets in stations.items():
                        for market_type, trades in markets.items():
                            self._trade_history[signal][station][market_type] = [
                                (t[0], t[1], t[2]) for t in trades
                            ]

                logger.debug(f"Loaded rolling calibration: {len(raw)} signal groups")
        except Exception as e:
            logger.debug(f"Failed to load rolling calibration cache: {e}")

    def _save_cache(self) -> None:
        """Save calibration history to cache file."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Convert defaultdicts to plain dicts for JSON serialization
            serializable = {}
            for signal, stations in self._trade_history.items():
                serializable[signal] = {}
                for station, markets in stations.items():
                    serializable[signal][station] = {}
                    for market_type, trades in markets.items():
                        serializable[signal][station][market_type] = [
                            [t[0], t[1], t[2]] for t in trades
                        ]

            with open(self.cache_file, "w") as f:
                json.dump({
                    "version": 1,
                    "window_days": self.window_days,
                    "last_updated": time.time(),
                    "trade_history": serializable,
                }, f, indent=2)

            self._last_saved = time.time()
        except Exception as e:
            logger.warning(f"Failed to save rolling calibration cache: {e}")


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the rolling calibration module."""
    cal = RollingCalibration(
        window_days=30,
        cache_file="/tmp/test_rolling_calibration.json",
    )

    # Test 1: No data — returns raw confidence
    r1 = cal.get_calibrated_confidence("KATL", "HIGH", raw_confidence=0.70)
    assert abs(r1.calibrated_confidence - 0.70) < 0.01, f"Expected ~0.70, got {r1.calibrated_confidence}"
    assert r1.observations == 0
    print(f"  Test 1 PASS: {r1}")

    # Test 2: Record 10 correct trades
    now = time.time()
    for i in range(10):
        cal.record_trade(
            "KATL", "HIGH", "spike_reversion",
            confidence=0.70, was_correct=True,
            timestamp=now - (i * 3600),  # Each trade 1 hour apart
        )
    r2 = cal.get_calibrated_confidence("KATL", "HIGH", raw_confidence=0.70)
    assert r2.observations == 10
    assert r2.historical_accuracy > 0.90
    # Calibrated should be between raw and historical
    assert r2.calibrated_confidence > r2.raw_confidence, f"Calibrated should be boosted"
    print(f"  Test 2 PASS: {r2}")

    # Test 3: Mixed outcomes
    cal2 = RollingCalibration(
        window_days=30,
        cache_file="/tmp/test_rolling_calibration2.json",
    )
    now = time.time()
    for i in range(20):
        was_correct = i < 12  # 12/20 = 60%
        cal2.record_trade(
            "KBOS", "HIGH", "spike_reversion",
            confidence=0.65, was_correct=was_correct,
            timestamp=now - (i * 3600),
        )
    r3 = cal2.get_calibrated_confidence("KBOS", "HIGH", raw_confidence=0.65)
    assert r3.observations == 20
    assert abs(r3.historical_accuracy - 0.60) < 0.05, f"Expected ~0.60, got {r3.historical_accuracy}"
    print(f"  Test 3 PASS: {r3}")

    # Test 4: get_rolling_accuracy
    acc, count = cal2.get_rolling_accuracy("KBOS", "HIGH")
    assert count == 20
    assert abs(acc - 0.60) < 0.05
    print(f"  Test 4 PASS: acc={acc:.3f}, count={count}")

    # Test 5: Empty station
    r5 = cal.get_calibrated_confidence("UNKNOWN", "HIGH", raw_confidence=0.50)
    assert r5.observations == 0
    assert abs(r5.calibrated_confidence - 0.50) < 0.01
    print(f"  Test 5 PASS: {r5}")

    # Test 6: Time decay — old trades should have less weight
    cal3 = RollingCalibration(
        window_days=30,
        cache_file="/tmp/test_rolling_calibration3.json",
    )
    now = time.time()
    # Mix of old and new trades
    for i in range(10):
        # Old trades: all wrong (30 days ago)
        cal3.record_trade(
            "KATL", "HIGH", "spike_reversion",
            confidence=0.60, was_correct=False,
            timestamp=now - (31 * 86400) + (i * 3600),
        )
    for i in range(5):
        # Recent trades: all correct (1 day ago)
        cal3.record_trade(
            "KATL", "HIGH", "spike_reversion",
            confidence=0.60, was_correct=True,
            timestamp=now - (1 * 86400) + (i * 3600),
        )
    r6 = cal3.get_calibrated_confidence("KATL", "HIGH", raw_confidence=0.60)
    # Historical accuracy should be biased toward recent (correct) trades
    assert r6.historical_accuracy > 0.50, f"Expected >0.50 (recent bias), got {r6.historical_accuracy}"
    # Old trades at 30d boundary should have been pruned
    assert r6.observations == 5, f"Expected 5 (old pruned), got {r6.observations}"
    print(f"  Test 6 PASS: {r6}")

    # Test 7: Cache persistence
    cal4 = RollingCalibration(
        window_days=30,
        cache_file="/tmp/test_rolling_calibration_cache.json",
    )
    cal4.record_trade("KATL", "HIGH", "spike_reversion", confidence=0.70, was_correct=True)
    cal4.record_trade("KATL", "HIGH", "spike_reversion", confidence=0.70, was_correct=True)
    cal4.record_trade("KATL", "HIGH", "spike_reversion", confidence=0.70, was_correct=False)
    acc_before, cnt_before = cal4.get_rolling_accuracy("KATL", "HIGH")

    cal5 = RollingCalibration(
        window_days=30,
        cache_file="/tmp/test_rolling_calibration_cache.json",
    )
    acc_after, cnt_after = cal5.get_rolling_accuracy("KATL", "HIGH")
    assert cnt_before == cnt_after, f"Cache count mismatch: {cnt_before} vs {cnt_after}"
    assert abs(acc_before - acc_after) < 0.01, f"Cache accuracy mismatch"
    print(f"  Test 7 PASS: cache persistence, acc={acc_after:.3f}, cnt={cnt_after}")

    # Test 8: to_dict
    r8 = cal5.get_calibrated_confidence("KATL", "HIGH", raw_confidence=0.70)
    d = r8.to_dict()
    assert "raw_confidence" in d
    assert "calibrated_confidence" in d
    print(f"  Test 8 PASS: {d}")

    # Clean up test caches
    import glob
    for f in glob.glob("/tmp/test_rolling_calibration*.json"):
        try:
            os.remove(f)
        except OSError:
            pass

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()