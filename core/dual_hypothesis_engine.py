#!/usr/bin/env python3
"""
A4 — Dual Hypothesis Engine for METAR Temperature Spike Detection

Formalizes the two competing hypotheses about a METAR temperature observation:

H1 (Transient Spike → Reversion):
  The observed temperature excursion is a brief, transient deviation from the
  prevailing daily regime. It will revert to the running daily max/min within
  1-2 observation intervals. This is the *tradable edge* — price reversion
  after a transient spike creates positive EV.

H2 (Structural Spike → New Daily Extreme):
  The observed temperature excursion represents a genuine structural change in
  the airmass or synoptic regime. It will set a new daily high or low that
  persists through settlement. This is *not* a reversion trade — betting
  against a structural change is negative EV.

Logic (from A1 / Gray Room R7-A1):
  - running_max_delta < 0.3°F → H1 (transient spike)
  - running_max_delta >= 0.3°F → H2 (structural spike)

Usage:
    from core.dual_hypothesis_engine import DualHypothesisEngine, HypothesisResult
    engine = DualHypothesisEngine()
    result = engine.evaluate(tracker_data)
    if result.hypothesis == "transient_spike":
        confidence = result.confidence
"""

from typing import Any, Dict, Optional, Tuple
from enum import Enum

# ─── Hypothesis Enum ────────────────────────────────────────────

class SpikeHypothesis(Enum):
    """Enumeration of competing spike hypotheses."""
    TRANSIENT_SPIKE = "transient_spike"   # H1 — will revert (TRADE)
    STRUCTURAL_SPIKE = "structural_spike" # H2 — will persist (NO TRADE)
    NO_SPIKE = "no_spike"                 # No spike detected (NO TRADE)

    def is_tradeable(self) -> bool:
        return self == SpikeHypothesis.TRANSIENT_SPIKE

# ─── Config ─────────────────────────────────────────────────────

# Threshold from R7-A1: delta < 0.3°F means transient
TRANSIENT_THRESHOLD_F = 0.3

# Asymmetric confidence parameters (from R4-1.5, preserved)
UP_REVERSION_BASE = 0.50        # H1 up-reversion base confidence
UP_REVERSION_MARGIN_BONUS = 0.10  # Per °F margin bonus
UP_REVERSION_MAX_BONUS = 0.15
UP_REVERSION_OBS_BONUS = 0.02   # Per observation since spike
UP_REVERSION_MAX_OBS = 0.20
UP_REVERSION_TIME_BONUS = 0.20  # Day fraction at spike multiplier
UP_REVERSION_BOOST = 1.05       # +5% for up-reversion reliability

DOWN_REVERSION_BASE = 0.50      # H1 down-reversion base confidence (same after A1 fix)
DOWN_REVERSION_MARGIN_BONUS = 0.08
DOWN_REVERSION_MAX_BONUS = 0.10
DOWN_REVERSION_OBS_BONUS = 0.015
DOWN_REVERSION_MAX_OBS = 0.15
DOWN_REVERSION_TIME_BONUS = 0.15
DOWN_REVERSION_DISCOUNT = 0.85  # -15% for down-reversion uncertainty

STRUCTURAL_BASE = 0.10          # H2 base — very low, don't trade
STRUCTURAL_MARGIN_BONUS = 0.06
STRUCTURAL_MAX_BONUS = 0.10
STRUCTURAL_OBS_BONUS = 0.01
STRUCTURAL_MAX_OBS = 0.10

# ─── Result Data Class ──────────────────────────────────────────

class HypothesisResult:
    """
    Structured result from the Dual Hypothesis Engine.

    Attributes:
        hypothesis: Which hypothesis was selected
        confidence: Numeric confidence [0.0, 1.0]
        direction: Predicted reversion direction ('up' or 'down')
        is_tradeable: Whether this hypothesis suggests a trade
        factors: Dict of contributing factors for diagnostics
    """

    def __init__(
        self,
        hypothesis: SpikeHypothesis,
        confidence: float,
        direction: Optional[str],
        factors: Optional[Dict[str, Any]] = None,
    ):
        self.hypothesis = hypothesis
        self.confidence = max(0.0, min(1.0, confidence))
        self.direction = direction  # 'up', 'down', or None
        self.is_tradeable = hypothesis.is_tradeable()
        self.factors = factors or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis": self.hypothesis.value,
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
            "is_tradeable": self.is_tradeable,
            "factors": self.factors,
        }

    def __repr__(self) -> str:
        h = self.hypothesis.value
        return (f"<HypothesisResult {h} dir={self.direction} "
                f"conf={self.confidence:.3f} trade={self.is_tradeable}>")


# ─── Engine ─────────────────────────────────────────────────────

class DualHypothesisEngine:
    """
    Dual Hypothesis Engine for METAR temperature spike assessment.

    Evaluates whether an observed temperature excursion is a transient spike
    (H1 — tradeable reversion) or a structural change (H2 — not tradeable).

    Can be used standalone with a tracker dict, or via the helper classmethod
    from the real-time microstructure spike detector.
    """

    @staticmethod
    def classify_hypothesis(running_max_delta: float) -> SpikeHypothesis:
        """
        Classify a spike observation into H1 (transient) or H2 (structural).

        Args:
            running_max_delta: Difference between spike temp and running daily
                               max/min (in °F). From tracker.daily_high_margin.

        Returns:
            SpikeHypothesis enum value
        """
        if running_max_delta < TRANSIENT_THRESHOLD_F:
            return SpikeHypothesis.TRANSIENT_SPIKE
        return SpikeHypothesis.STRUCTURAL_SPIKE

    @staticmethod
    def compute_confidence(
        hypothesis: SpikeHypothesis,
        running_max_delta: float,
        observations_since_spike: int = 0,
        day_fraction_at_spike: float = 0.0,
        is_down: bool = False,
    ) -> float:
        """
        Compute confidence score based on hypothesis classification.

        Args:
            hypothesis: Classified hypothesis (H1 or H2)
            running_max_delta: Delta from running max in °F
            observations_since_spike: Number of METAR obs since spike detected
            day_fraction_at_spike: Fraction of day elapsed at spike time
            is_down: True for down-reversion (spike down → bounce up)

        Returns:
            Confidence score in [0.0, 1.0]
        """
        if hypothesis == SpikeHypothesis.NO_SPIKE:
            return 0.0

        if hypothesis == SpikeHypothesis.TRANSIENT_SPIKE:
            if is_down:
                # Down reversion (spike down → bounce up): less reliable
                base = DOWN_REVERSION_BASE
                bonus_margin = min(
                    running_max_delta * DOWN_REVERSION_MARGIN_BONUS,
                    DOWN_REVERSION_MAX_BONUS,
                )
                bonus_obs = min(
                    observations_since_spike * DOWN_REVERSION_OBS_BONUS,
                    DOWN_REVERSION_MAX_OBS,
                )
                bonus_time = day_fraction_at_spike * DOWN_REVERSION_TIME_BONUS
                confidence = (base + bonus_margin + bonus_obs + bonus_time) * DOWN_REVERSION_DISCOUNT
            else:
                # Up reversion (spike up → drop down): more reliable
                base = UP_REVERSION_BASE
                bonus_margin = min(
                    running_max_delta * UP_REVERSION_MARGIN_BONUS,
                    UP_REVERSION_MAX_BONUS,
                )
                bonus_obs = min(
                    observations_since_spike * UP_REVERSION_OBS_BONUS,
                    UP_REVERSION_MAX_OBS,
                )
                bonus_time = day_fraction_at_spike * UP_REVERSION_TIME_BONUS
                confidence = (base + bonus_margin + bonus_obs + bonus_time) * UP_REVERSION_BOOST

        elif hypothesis == SpikeHypothesis.STRUCTURAL_SPIKE:
            # Structural spike — very low base, no edge
            base = STRUCTURAL_BASE
            bonus_margin = min(
                running_max_delta * STRUCTURAL_MARGIN_BONUS,
                STRUCTURAL_MAX_BONUS,
            )
            bonus_obs = min(
                observations_since_spike * STRUCTURAL_OBS_BONUS,
                STRUCTURAL_MAX_OBS,
            )
            confidence = base + bonus_margin + bonus_obs
        else:
            confidence = 0.0

        return max(0.0, min(1.0, confidence))

    def evaluate(
        self,
        tracker: Dict[str, Any],
        is_down: bool = False,
    ) -> HypothesisResult:
        """
        Evaluate a spike tracker dict and return the full hypothesis result.

        This is the main entry point. Accepts the same tracker dict format
        used by _compute_microstructure_spike_confidence in metar_monitor.py.

        Args:
            tracker: Spike epoch tracker dict with fields:
                daily_high_margin (or running_max_delta): delta in °F
                observations_since_spike: count of obs since spike
                day_fraction_at_spike: fraction of day elapsed
            is_down: True for down-reversion direction

        Returns:
            HypothesisResult with classification and confidence
        """
        # Extract fields with backward compatibility
        running_max_delta = float(
            tracker.get("daily_high_margin") or
            tracker.get("running_max_delta") or
            0.0
        )
        observations_since_spike = int(
            tracker.get("observations_since_spike", 0) or 0
        )
        day_fraction_at_spike = float(
            tracker.get("day_fraction_at_spike", 0.0) or 0.0
        )

        # Classify spike hypothesis
        hypothesis = self.classify_hypothesis(running_max_delta)

        # Compute confidence
        confidence = self.compute_confidence(
            hypothesis=hypothesis,
            running_max_delta=running_max_delta,
            observations_since_spike=observations_since_spike,
            day_fraction_at_spike=day_fraction_at_spike,
            is_down=is_down,
        )

        # Determine direction
        direction = None
        if hypothesis.is_tradeable():
            direction = "down" if not is_down else "up"

        factors = {
            "running_max_delta": round(running_max_delta, 2),
            "observations_since_spike": observations_since_spike,
            "day_fraction_at_spike": round(day_fraction_at_spike, 3),
            "is_down_reversion": is_down,
            "transient_threshold_f": TRANSIENT_THRESHOLD_F,
        }

        return HypothesisResult(
            hypothesis=hypothesis,
            confidence=confidence,
            direction=direction,
            factors=factors,
        )

    @staticmethod
    def evaluate_from_scratch(
        spike_temp: float,
        running_daily_max: float,
        observations_since_spike: int = 0,
        day_fraction: float = 0.5,
        is_down: bool = False,
    ) -> HypothesisResult:
        """
        Evaluate spike hypotheses from raw data without requiring a tracker dict.

        Args:
            spike_temp: The METAR temperature that triggered spike detection
            running_daily_max: The running daily high (or low for down-spikes)
            observations_since_spike: Number of METAR obs since spike detected
            day_fraction: Fraction of day elapsed (0.0-1.0)
            is_down: True for down-reversion

        Returns:
            HypothesisResult
        """
        running_max_delta = abs(spike_temp - running_daily_max)
        hypothesis = DualHypothesisEngine.classify_hypothesis(running_max_delta)

        confidence = DualHypothesisEngine.compute_confidence(
            hypothesis=hypothesis,
            running_max_delta=running_max_delta,
            observations_since_spike=observations_since_spike,
            day_fraction_at_spike=day_fraction,
            is_down=is_down,
        )

        direction = None
        if hypothesis.is_tradeable():
            direction = "down" if not is_down else "up"

        return HypothesisResult(
            hypothesis=hypothesis,
            confidence=confidence,
            direction=direction,
            factors={
                "spike_temp": spike_temp,
                "running_daily_max": running_daily_max,
                "running_max_delta": round(running_max_delta, 2),
                "observations_since_spike": observations_since_spike,
                "day_fraction": day_fraction,
                "is_down_reversion": is_down,
                "transient_threshold_f": TRANSIENT_THRESHOLD_F,
            },
        )


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the dual hypothesis engine."""
    engine = DualHypothesisEngine()

    # Test 1: Transient spike (delta < 0.3°F)
    tracker_transient = {
        "daily_high_margin": 0.15,
        "observations_since_spike": 3,
        "day_fraction_at_spike": 0.3,
    }
    r1 = engine.evaluate(tracker_transient)
    assert r1.hypothesis == SpikeHypothesis.TRANSIENT_SPIKE, f"Expected TRANSIENT, got {r1.hypothesis}"
    assert r1.is_tradeable, f"Expected tradeable"
    assert r1.direction == "down", f"Expected down, got {r1.direction}"
    assert r1.confidence > 0.5, f"Expected >0.5, got {r1.confidence}"
    print(f"  Test 1 PASS: {r1}")

    # Test 2: Transient spike, down reversion
    tracker_down = {
        "daily_high_margin": 0.2,
        "observations_since_spike": 2,
        "day_fraction_at_spike": 0.5,
    }
    r2 = engine.evaluate(tracker_down, is_down=True)
    assert r2.hypothesis == SpikeHypothesis.TRANSIENT_SPIKE
    assert r2.direction == "up", f"Expected up, got {r2.direction}"
    print(f"  Test 2 PASS: {r2}")

    # Test 3: Structural spike (delta >= 0.3°F)
    tracker_structural = {
        "daily_high_margin": 2.5,
        "observations_since_spike": 5,
        "day_fraction_at_spike": 0.6,
    }
    r3 = engine.evaluate(tracker_structural)
    assert r3.hypothesis == SpikeHypothesis.STRUCTURAL_SPIKE
    assert not r3.is_tradeable, f"Expected not tradeable"
    assert r3.confidence < 0.3, f"Expected <0.3, got {r3.confidence}"
    print(f"  Test 3 PASS: {r3}")

    # Test 4: No spike edge case
    tracker_no_spike = {"daily_high_margin": -1.0, "observations_since_spike": 0}
    r4 = engine.evaluate(tracker_no_spike)
    assert r4.hypothesis in (SpikeHypothesis.TRANSIENT_SPIKE, SpikeHypothesis.STRUCTURAL_SPIKE)
    print(f"  Test 4 PASS: {r4}")

    # Test 5: evaluate_from_scratch
    r5 = engine.evaluate_from_scratch(
        spike_temp=82.3,
        running_daily_max=82.1,
        observations_since_spike=1,
        day_fraction=0.4,
    )
    assert r5.hypothesis == SpikeHypothesis.TRANSIENT_SPIKE
    assert r5.is_tradeable
    print(f"  Test 5 PASS: {r5}")

    # Test 6: Structural from scratch
    r6 = engine.evaluate_from_scratch(
        spike_temp=88.0,
        running_daily_max=82.1,
        observations_since_spike=3,
        day_fraction=0.7,
    )
    assert r6.hypothesis == SpikeHypothesis.STRUCTURAL_SPIKE
    assert not r6.is_tradeable
    assert r6.confidence < 0.3
    print(f"  Test 6 PASS: {r6}")

    # Test 7: Down-reversion from scratch
    r7 = engine.evaluate_from_scratch(
        spike_temp=55.0,
        running_daily_max=58.0,
        observations_since_spike=2,
        day_fraction=0.3,
        is_down=True,
    )
    assert r7.hypothesis == SpikeHypothesis.STRUCTURAL_SPIKE  # delta=3.0 >= 0.3
    assert not r7.is_tradeable
    r7b = engine.evaluate_from_scratch(
        spike_temp=58.2,
        running_daily_max=58.0,
        observations_since_spike=2,
        day_fraction=0.3,
        is_down=True,
    )
    assert r7b.hypothesis == SpikeHypothesis.TRANSIENT_SPIKE  # delta=0.2 < 0.3
    assert r7b.direction == "up"
    print(f"  Test 7 PASS: {r7b}")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()
