#!/usr/bin/env python3
"""
A8 — Station Rank Selective

Per-station selection for spike reversion trades. Only trades stations where
the spike reversion signal historically exceeds 58% directional accuracy.
Stations below threshold are skipped.

Design:
- Maintains a persistent accuracy registry (JSON cache)
- Tracks per-station, per-market-type (HIGH/LOW) accuracy
- Rolling window: most recent N trades per station
- Threshold: 58% directional accuracy (from Gray Room R7 gate)
- Results are cached for configurable TTL

Usage:
    from core.station_rank_selective import StationRankSelective, StationRankResult

    ranker = StationRankSelective()
    result = ranker.is_eligible("KATL", "HIGH")
    if result.eligible:
        # Proceed with trade
        confidence = result.adjusted_confidence  # Rank-adjusted confidence
    else:
        # Skip station — reason in result.reason
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.sqlite_utils import get_sqlite_connection
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────

# Accuracy threshold from Gray Room R7: ≥ 58% directional accuracy
DEFAULT_ACCURACY_THRESHOLD = 0.58

# Configuration defaults
DEFAULT_CACHE_TTL_HOURS = 24
DEFAULT_CACHE_FILE = "data/station_rank_cache.json"
DEFAULT_ROLLING_WINDOW = 50  # Number of recent trades to consider

# Confidence boost for high-accuracy stations
BOOST_ABOVE_65 = 1.05   # +5% for stations > 65%
BOOST_ABOVE_70 = 1.10   # +10% for stations > 70%
BOOST_ABOVE_75 = 1.15   # +15% for stations > 75%

# Discount for low-accuracy-but-passing stations
DISCOUNT_58_60 = 0.95   # -5% for stations between 58-60%


# ─── Result ────────────────────────────────────────────────────

class StationRankResult:
    """
    Result of a station rank eligibility check.

    Attributes:
        eligible: True if station passes accuracy threshold
        accuracy: Historical directional accuracy for this station/market
        adjusted_confidence: Confidence multiplier based on rank
        trades_evaluated: Number of trades in the rolling window
        threshold: The accuracy threshold used
        market_type: HIGH or LOW
        reason: Reason string if not eligible
    """

    def __init__(
        self,
        eligible: bool,
        accuracy: float = 0.0,
        adjusted_confidence: float = 1.0,
        trades_evaluated: int = 0,
        threshold: float = DEFAULT_ACCURACY_THRESHOLD,
        market_type: str = "HIGH",
        reason: str = "",
    ):
        self.eligible = eligible
        self.accuracy = accuracy
        self.adjusted_confidence = adjusted_confidence
        self.trades_evaluated = trades_evaluated
        self.threshold = threshold
        self.market_type = market_type
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "accuracy": round(self.accuracy, 4),
            "adjusted_confidence": round(self.adjusted_confidence, 4),
            "trades_evaluated": self.trades_evaluated,
            "threshold": self.threshold,
            "market_type": self.market_type,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        return (f"<StationRankResult {self.market_type} "
                f"eligible={self.eligible} acc={self.accuracy:.3f} "
                f"trades={self.trades_evaluated}>")


# ─── StationRankSelective ──────────────────────────────────────

class StationRankSelective:
    """
    Per-station rank-selective gate for spike reversion trades.

    Tracks historical directional accuracy per station and market type,
    and gates trades based on the 58% threshold.
    """

    def __init__(
        self,
        accuracy_threshold: float = DEFAULT_ACCURACY_THRESHOLD,
        rolling_window: int = DEFAULT_ROLLING_WINDOW,
        cache_file: str = DEFAULT_CACHE_FILE,
        cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
        db_path: Optional[str] = None,
    ):
        self.accuracy_threshold = accuracy_threshold
        self.rolling_window = rolling_window
        self.cache_file = Path(cache_file)
        self.cache_ttl_hours = cache_ttl_hours
        self.db_path = db_path

        # In-memory accuracy registry: {station: {market_type: accuracy}}
        self._accuracy_registry: Dict[str, Dict[str, float]] = {}
        self._trade_count_registry: Dict[str, Dict[str, int]] = {}
        self._last_loaded = 0.0

        # Load from cache on init
        self._load_cache()

    # ── Public API ────────────────────────────────────────────

    def is_eligible(
        self,
        station: str,
        market_type: str = "HIGH",
    ) -> StationRankResult:
        """
        Check if a station is eligible for spike reversion trading.

        Args:
            station: Station code (e.g. 'KATL')
            market_type: 'HIGH' or 'LOW'

        Returns:
            StationRankResult with eligibility and adjusted confidence
        """
        # Refresh cache if stale
        self._refresh_if_stale()

        # Get accuracy for this station/market
        accuracy = self._get_accuracy(station, market_type)
        trades = self._trade_count_registry.get(station, {}).get(market_type, 0)

        # Compute adjusted confidence
        adjusted_confidence = self._compute_adjusted_confidence(accuracy)

        # Check eligibility
        if trades < 5:
            # Not enough data — defer to global average
            return StationRankResult(
                eligible=True,
                accuracy=accuracy,
                adjusted_confidence=0.95,  # Slight discount for uncertainty
                trades_evaluated=trades,
                market_type=market_type,
                reason="insufficient_data_global_default",
            )

        if accuracy >= self.accuracy_threshold:
            return StationRankResult(
                eligible=True,
                accuracy=accuracy,
                adjusted_confidence=adjusted_confidence,
                trades_evaluated=trades,
                market_type=market_type,
                reason="pass",
            )

        return StationRankResult(
            eligible=False,
            accuracy=accuracy,
            adjusted_confidence=adjusted_confidence,
            trades_evaluated=trades,
            threshold=self.accuracy_threshold,
            market_type=market_type,
            reason=f"accuracy_{accuracy:.3f}_below_threshold_{self.accuracy_threshold}",
        )

    def get_accuracy(self, station: str, market_type: str = "HIGH") -> float:
        """Get historical accuracy for a station and market type."""
        return self._get_accuracy(station, market_type)

    def get_all_accuracies(self) -> Dict[str, Dict[str, float]]:
        """Get all station accuracies."""
        return dict(self._accuracy_registry)

    def update_accuracy(
        self,
        station: str,
        market_type: str,
        was_correct: bool,
    ) -> None:
        """
        Update the rolling accuracy for a station with a new trade outcome.

        Args:
            station: Station code
            market_type: 'HIGH' or 'LOW'
            was_correct: True if the trade was directional correct
        """
        if station not in self._accuracy_registry:
            self._accuracy_registry[station] = {}
            self._trade_count_registry[station] = {}

        if market_type not in self._accuracy_registry[station]:
            self._accuracy_registry[station][market_type] = 0.50  # Start at 50%
            self._trade_count_registry[station][market_type] = 0

        # Rolling update: weighted moving average
        count = self._trade_count_registry[station][market_type]
        current_acc = self._accuracy_registry[station][market_type]

        if count >= self.rolling_window:
            # Rolling window full — exponential decay
            alpha = 2.0 / (self.rolling_window + 1)
            new_acc = current_acc * (1 - alpha) + (1.0 if was_correct else 0.0) * alpha
        else:
            # Accumulating: simple running average
            new_acc = (current_acc * count + (1.0 if was_correct else 0.0)) / (count + 1)

        self._accuracy_registry[station][market_type] = new_acc
        self._trade_count_registry[station][market_type] = count + 1

        # Persist to cache
        self._save_cache()

    def compute_from_backtest(
        self,
        db_path: Optional[str] = None,
        signal_name: str = "spike_reversion",
    ) -> None:
        """
        Compute per-station accuracy from backtest results in the database.

        Args:
            db_path: Path to paper trading DB (defaults to self.db_path)
            signal_name: Signal name to filter by
        """
        db = db_path or self.db_path
        if not db:
            logger.warning("No db_path provided for backtest accuracy computation")
            return

        try:
            conn = get_sqlite_connection(db)
            cur = conn.cursor()

            for market_type in ("HIGH", "LOW"):
                cur.execute("""
                    SELECT station,
                           CAST(SUM(CASE WHEN correct THEN 1 ELSE 0 END) AS REAL) / COUNT(*) as accuracy,
                           COUNT(*) as trades
                    FROM (
                        SELECT t.station, t.signal_direction,
                               CASE WHEN (t.signal_direction = 'up' AND s.settlement_bucket > s.prev_settlement_bucket)
                                    OR (t.signal_direction = 'down' AND s.settlement_bucket < s.prev_settlement_bucket)
                                    THEN 1 ELSE 0 END as correct
                        FROM trades t
                        JOIN settlement_epochs s ON t.epoch_id = s.epoch_id
                        WHERE t.functionality = ? AND t.market_type = ?
                    )
                    GROUP BY station
                """, (signal_name, market_type))

                for row in cur.fetchall():
                    station, accuracy, trades = row
                    if station not in self._accuracy_registry:
                        self._accuracy_registry[station] = {}
                        self._trade_count_registry[station] = {}
                    self._accuracy_registry[station][market_type] = accuracy
                    self._trade_count_registry[station][market_type] = trades

            conn.close()
            self._save_cache()
            logger.info(f"Loaded per-station accuracies from backtest DB: "
                        f"{len(self._accuracy_registry)} stations")

        except Exception as e:
            logger.warning(f"Failed to compute accuracies from backtest DB: {e}")

    # ── Internal ──────────────────────────────────────────────

    def _get_accuracy(self, station: str, market_type: str) -> float:
        """Get accuracy for a station/market, defaulting to 0.5."""
        return self._accuracy_registry.get(station, {}).get(market_type, 0.50)

    def _compute_adjusted_confidence(self, accuracy: float) -> float:
        """
        Compute a confidence multiplier based on station accuracy rank.

        High-accuracy stations get a confidence boost.
        Low-accuracy-but-passing stations get a discount.
        """
        if accuracy >= 0.75:
            return BOOST_ABOVE_75
        elif accuracy >= 0.70:
            return BOOST_ABOVE_70
        elif accuracy >= 0.65:
            return BOOST_ABOVE_65
        elif accuracy >= self.accuracy_threshold and accuracy < 0.60:
            return DISCOUNT_58_60
        return 1.0

    def _refresh_if_stale(self) -> None:
        """Reload from cache if TTL has expired."""
        if time.time() - self._last_loaded > self.cache_ttl_hours * 3600:
            self._load_cache()

    def _load_cache(self) -> None:
        """Load the accuracy registry from cache file."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                self._accuracy_registry = data.get("accuracy_registry", {})
                self._trade_count_registry = data.get("trade_count_registry", {})
                self._last_loaded = data.get("timestamp", 0.0)
                logger.debug(f"Loaded station rank cache: {len(self._accuracy_registry)} stations")
        except Exception as e:
            logger.debug(f"Failed to load station rank cache: {e}")

    def _save_cache(self) -> None:
        """Save the accuracy registry to cache file."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump({
                    "timestamp": time.time(),
                    "accuracy_registry": self._accuracy_registry,
                    "trade_count_registry": self._trade_count_registry,
                }, f, indent=2)
            self._last_loaded = time.time()
        except Exception as e:
            logger.warning(f"Failed to save station rank cache: {e}")

    def get_eligible_stations(
        self,
        stations: List[str],
        market_type: str = "HIGH",
    ) -> List[str]:
        """
        Filter a list of stations to only those eligible for trading.

        Args:
            stations: List of station codes to check
            market_type: 'HIGH' or 'LOW'

        Returns:
            List of eligible station codes
        """
        return [
            s for s in stations
            if self.is_eligible(s, market_type).eligible
        ]


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the station rank selective module."""
    ranker = StationRankSelective(
        cache_file="/tmp/test_station_rank_cache.json",
    )

    # Test 1: Insufficient data — should pass with default confidence
    r1 = ranker.is_eligible("KATL", "HIGH")
    assert r1.eligible, f"Expected eligible (insufficient data fallback), got {r1}"
    assert r1.trades_evaluated == 0
    print(f"  Test 1 PASS: {r1}")

    # Test 2: Update accuracy and check
    for _ in range(10):
        ranker.update_accuracy("KATL", "HIGH", was_correct=True)
    r2 = ranker.is_eligible("KATL", "HIGH")
    assert r2.eligible, f"Expected eligible (100% accuracy), got {r2}"
    assert r2.accuracy > 0.95
    assert r2.adjusted_confidence > 1.10  # Should get boost
    print(f"  Test 2 PASS: {r2}")

    # Test 3: Low accuracy — should be ineligible
    ranker2 = StationRankSelective(
        accuracy_threshold=0.58,
        cache_file="/tmp/test_station_rank_cache2.json",
    )
    for _ in range(20):
        ranker2.update_accuracy("KATL", "HIGH", was_correct=False)
        ranker2.update_accuracy("KATL", "HIGH", was_correct=True)
    # 10/20 = 50% — below threshold
    r3 = ranker2.is_eligible("KATL", "HIGH")
    assert not r3.eligible, f"Expected not eligible (50% accuracy), got {r3}"
    print(f"  Test 3 PASS: {r3}")

    # Test 4: Borderline accuracy (58% — exactly at threshold)
    ranker3 = StationRankSelective(
        accuracy_threshold=0.58,
        cache_file="/tmp/test_station_rank_cache3.json",
    )
    for i in range(50):
        was_correct = i < 29  # 29/50 = 58%
        ranker3.update_accuracy("KATL", "HIGH", was_correct=was_correct)
    r4 = ranker3.is_eligible("KATL", "HIGH")
    assert r4.eligible, f"Expected eligible (58% accuracy), got {r4}"
    assert abs(r4.accuracy - 0.58) < 0.02, f"Expected ~0.58, got {r4.accuracy}"
    print(f"  Test 4 PASS: {r4}")

    # Test 5: get_eligible_stations
    ranker4 = StationRankSelective(
        accuracy_threshold=0.58,
        cache_file="/tmp/test_station_rank_cache4.json",
    )
    for i in range(10):
        ranker4.update_accuracy("KATL", "HIGH", was_correct=True)
        ranker4.update_accuracy("KBOS", "HIGH", was_correct=False)
        ranker4.update_accuracy("KATL", "LOW", was_correct=True)
        ranker4.update_accuracy("KBOS", "LOW", was_correct=False)

    eligible_high = ranker4.get_eligible_stations(["KATL", "KBOS"], "HIGH")
    assert "KATL" in eligible_high, f"Expected KATL in eligible HIGH"
    assert "KBOS" not in eligible_high, f"Expected KBOS not in eligible HIGH"
    print(f"  Test 5 PASS: eligible HIGH={eligible_high}")

    eligible_low = ranker4.get_eligible_stations(["KATL", "KBOS"], "LOW")
    assert "KATL" in eligible_low, f"Expected KATL in eligible LOW"
    assert "KBOS" not in eligible_low, f"Expected KBOS not in eligible LOW"
    print(f"  Test 6 PASS: eligible LOW={eligible_low}")

    # Test 7: Cache persistence
    ranker5 = StationRankSelective(
        cache_file="/tmp/test_station_rank_cache5.json",
    )
    for i in range(10):
        ranker5.update_accuracy("KATL", "HIGH", was_correct=True)
    acc_before = ranker5.get_accuracy("KATL", "HIGH")

    ranker6 = StationRankSelective(
        cache_file="/tmp/test_station_rank_cache5.json",
    )
    acc_after = ranker6.get_accuracy("KATL", "HIGH")
    assert abs(acc_before - acc_after) < 0.01, f"Cache failed: {acc_before} vs {acc_after}"
    print(f"  Test 7 PASS: cache persistence, acc={acc_after:.3f}")

    # Clean up test cache files
    import glob
    for f in glob.glob("/tmp/test_station_rank_cache*.json"):
        try:
            os.remove(f)
        except OSError:
            pass

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()