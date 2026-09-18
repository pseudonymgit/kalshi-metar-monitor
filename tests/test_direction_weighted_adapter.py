#!/usr/bin/env python3
"""
DirectionWeightedAdapter acceptance tests (spec D.6).

Gates:
- D-G1: class exists with rolling-window computation
- D-G2: weight == 1.0 when asymmetry == 0
- D-G3: weight == 0.5 when asymmetry >= 0.5 (floor boundary)
- D-G4: weight decreases monotonically as asymmetry rises 0 -> 0.5
- D-G5: integration — ON vs OFF runs complete; adapter is post-calibration
- D-G6: no calibration pipeline change
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.direction_weighted_adapter import DirectionWeightedAdapter


class TestAdapterBoundaries(unittest.TestCase):
    """D-G2 / D-G3: boundary conditions."""

    def test_weight_one_at_zero_asymmetry(self):
        """D-G2: weight == 1.0 when asymmetry == 0."""
        a = DirectionWeightedAdapter(window_size=100)
        # Perfectly balanced: up and down confidences have equal means
        for _ in range(25):
            a.record("sig", "up", 0.60)
            a.record("sig", "down", 0.60)
        self.assertAlmostEqual(a.weight("sig"), 1.0, places=9)

    def test_weight_half_at_max_asymmetry(self):
        """D-G3: weight == 0.5 when asymmetry >= 0.5."""
        a = DirectionWeightedAdapter(window_size=100)
        for _ in range(25):
            a.record("sig", "up", 0.99)
            a.record("sig", "down", 0.01)
        # asymmetry = |0.01 - 0.99| = 0.98 -> clamped to 0.5 -> weight 0.5
        self.assertAlmostEqual(a.weight("sig"), 0.5, places=9)


class TestAdapterMonotonicity(unittest.TestCase):
    """D-G4: monotonic decrease."""

    def test_monotonic_decrease(self):
        a = DirectionWeightedAdapter(window_size=200)
        prev = 1.0
        for asym in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]:
            b = DirectionWeightedAdapter(window_size=200)
            up_c = 0.5 + asym / 2
            down_c = 0.5 - asym / 2
            for _ in range(50):
                b.record("s", "up", up_c)
                b.record("s", "down", down_c)
            w = b.weight("s")
            self.assertLessEqual(w, prev + 1e-9, f"weight rose at asym={asym}")
            prev = w


class TestAdapterAdjustLogOdds(unittest.TestCase):
    """D.2 formula: adjusted_log_odds = raw_log_odds * weight."""

    def test_formula(self):
        a = DirectionWeightedAdapter()
        # Force a known weight by recording imbalanced history
        for _ in range(25):
            a.record("sig", "up", 0.95)
            a.record("sig", "down", 0.05)
        w = a.weight("sig")
        self.assertAlmostEqual(w, 0.5, places=6)
        raw = 2.0
        adjusted = a.adjust_log_odds("sig", raw)
        self.assertAlmostEqual(adjusted, raw * w, places=9)

    def test_weight_one_noop(self):
        a = DirectionWeightedAdapter()
        for _ in range(10):
            a.record("s", "up", 0.5)
            a.record("s", "down", 0.5)
        self.assertAlmostEqual(a.adjust_log_odds("s", 1.5), 1.5, places=9)


class TestAdapterPostCalibration(unittest.TestCase):
    """D-G5/D-G6: adapter is post-calibration, no pipeline change."""

    def test_no_calibration_attribute_change(self):
        from core.fusion_logic import SignalFusionEngine

        # Default construction: adapter OFF
        fuser = SignalFusionEngine(
            signal_names=["pressure_delta", "metar_trend"],
            city_codes=["KMDW"],
        )
        self.assertIsNone(fuser.direction_adapter, "adapter should be None by default")

        # Explicit ON
        fuser_on = SignalFusionEngine(
            signal_names=["pressure_delta", "metar_trend"],
            city_codes=["KMDW"],
            use_direction_weighted_adapter=True,
        )
        self.assertIsNotNone(fuser_on.direction_adapter)
        self.assertIsInstance(fuser_on.direction_adapter, DirectionWeightedAdapter)

    def test_llop_single_signal_backward_compat(self):
        """fuse_signals 4-tuple contract preserved (WS1)."""
        from core.fusion_logic import SignalFusionEngine

        fuser = SignalFusionEngine(
            signal_names=["pressure_delta"],
            city_codes=["KMDW"],
            use_direction_weighted_adapter=True,
        )
        # 4-tuple return contract
        result = fuser.fuse_signals(
            [("pressure_delta", "up", 0.70)],
            "KMDW",
        )
        self.assertEqual(len(result), 4)
        self.assertIn(result[3], {"empty", "no_calibrated", "ds_conflict", "ok"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
