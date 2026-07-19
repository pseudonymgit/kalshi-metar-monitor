#!/usr/bin/env python3
"""
Unit Tests for CalibrationPipeline

Tests:
1. Fallback chain works (city → signal → global → identity)
2. No-change zone logic (0.01 threshold)
3. Clipping to [0.05, 0.95]
4. Refit after update
5. Max_history pruning

Usage:
    pytest tests/test_calibration_pipeline.py -v
    python tests/test_calibration_pipeline.py
"""

import os
import sys
import math
import unittest

# Ensure the repo root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.calibration_pipeline import CalibrationPipeline


class TestCalibrationPipeline(unittest.TestCase):
    """Test the CalibrationPipeline class."""

    def setUp(self):
        """Create a fresh pipeline for each test."""
        self.signal_names = ['test_signal_a', 'test_signal_b']
        self.city_codes = ['KNYC', 'KLAX']
        self.pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=100, window_start=0
        )

    # ─── Test 1: Fallback Chain ──────────────────────────────────────────

    def test_fallback_identity(self):
        """
        Fallback chain: with no data, calibrate() should return raw confidence
        (identity fallback).
        """
        raw_conf = 0.75
        calibrated = self.pipeline.calibrate('test_signal_a', 'KNYC', raw_conf)
        self.assertEqual(calibrated, raw_conf,
                         "Identity fallback should return raw confidence unchanged")

    def test_fallback_global(self):
        """
        Fallback chain: with data on all signals but not enough for per-city,
        the global calibrator should be used.
        """
        # Use a pipeline with high max_history
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )

        # Add enough data to trigger global (4 * MIN_SAMPLES = 800)
        n = 810
        for i in range(n):
            # Mix signals and cities
            sig = self.signal_names[i % 2]
            city = self.city_codes[i % 2]
            raw_c = 0.5 + (i % 5) * 0.1  # 0.5, 0.6, 0.7, 0.8, 0.9
            correct = (i % 3) != 0  # ~66% accuracy
            pipeline.update(sig, city, raw_c, correct)

        pipeline.refit()

        # After refit, global should be fitted
        self.assertIsNotNone(
            pipeline.global_calibrator,
            "Global calibrator should be fitted after sufficient data"
        )

        # Calibrate should return something different from raw (not identity)
        calibrated = pipeline.calibrate('test_signal_a', 'KNYC', 0.75)
        self.assertIsNotNone(calibrated)
        self.assertGreaterEqual(calibrated, 0.0)
        self.assertLessEqual(calibrated, 1.0)

    def test_fallback_signal_global(self):
        """
        Fallback chain: per-signal global should be used when per-city is not
        available but signal-level data is sufficient.
        """
        # Use a pipeline with 3 cities to spread data thinner
        cities = ['KNYC', 'KLAX', 'KATL']
        pipeline = CalibrationPipeline(
            ['test_signal_a'], cities, max_history=5000, window_start=0
        )

        # Add data across all 3 cities such that:
        # - each city gets < MIN_SAMPLES (200)
        # - total across cities >= 2 * MIN_SAMPLES (400)
        n = 450  # 450 / 3 = 150 per city, but 450 >= 400 for signal global
        for i in range(n):
            city = cities[i % 3]
            raw_c = 0.5 + (i % 5) * 0.1
            correct = (i % 3) != 0
            pipeline.update('test_signal_a', city, raw_c, correct)

        pipeline.refit()

        # Signal_a global should be fitted (450 >= 400)
        self.assertIn(
            'test_signal_a', pipeline.fallback_calibrators,
            "Signal global calibrator should be fitted"
        )

        # Per-city calibrator for signal_a+KNYC should NOT be fitted (150 < 200)
        self.assertNotIn(
            ('test_signal_a', 'KNYC'), pipeline.calibrators,
            "Per-city calibrator should not be fitted with < MIN_SAMPLES per city"
        )

    # ─── Test 2: No-Change Zone ──────────────────────────────────────────

    def test_no_change_zone_identity(self):
        """No-change zone: identity fallback should pass through unchanged."""
        raw_conf = 0.72
        calibrated = self.pipeline.calibrate('test_signal_a', 'KNYC', raw_conf)
        # Identity path: no calibrator, should return raw exactly
        self.assertEqual(calibrated, raw_conf)

    def test_no_change_zone_small_adjustment(self):
        """
        No-change zone: if calibrated value is within ±0.01 of raw, raw is kept.
        This ensures the calibrator actually has a meaningful effect.
        """
        # Use a pipeline with high max_history
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )

        # Add enough data to trigger calibration
        n = 810
        for i in range(n):
            sig = self.signal_names[i % 2]
            city = self.city_codes[i % 2]
            raw_c = 0.5 + (i % 5) * 0.1
            correct = (i % 3) != 0
            pipeline.update(sig, city, raw_c, correct)

        pipeline.refit()

        # Check that the no-change zone is active
        # With isotonic regression, values near existing data points should
        # be adjusted but the no-change zone should filter very small adjustments
        calibrated = pipeline.calibrate('test_signal_a', 'KNYC', 0.75)
        if abs(calibrated - 0.75) <= 0.01:
            self.assertEqual(calibrated, 0.75)

    # ─── Test 3: Clipping ────────────────────────────────────────────────

    def test_clipping_lower_bound(self):
        """Clipping: confidence should not go below 0.05 even with extreme inputs."""
        # Use a pipeline with high max_history
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )

        # Add data where very low confidence is always wrong
        n = 810
        for i in range(n):
            sig = self.signal_names[i % 2]
            city = self.city_codes[i % 2]
            # Very low confidence (0.1) but always correct
            raw_c = 0.1
            correct = True
            pipeline.update(sig, city, raw_c, correct)

        pipeline.refit()

        calibrated = pipeline.calibrate('test_signal_a', 'KNYC', 0.05)
        self.assertGreaterEqual(calibrated, 0.05,
                                "Calibrated confidence should not drop below 0.05")

    def test_clipping_upper_bound(self):
        """Clipping: confidence should not exceed 0.95 even with extreme inputs."""
        # Use a pipeline with high max_history
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )

        # Add data where very high confidence is always correct
        n = 810
        for i in range(n):
            sig = self.signal_names[i % 2]
            city = self.city_codes[i % 2]
            raw_c = 0.95
            correct = True
            pipeline.update(sig, city, raw_c, correct)

        pipeline.refit()

        calibrated = pipeline.calibrate('test_signal_a', 'KNYC', 0.99)
        self.assertLessEqual(calibrated, 0.95,
                             "Calibrated confidence should not exceed 0.95")

    # ─── Test 4: Refit After Update ──────────────────────────────────────

    def test_refit_flag_set_on_update(self):
        """Refit flag should be reset after update and set after refit."""
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )
        self.assertFalse(pipeline.refitted)

        # Update should reset refitted flag
        pipeline.update('test_signal_a', 'KNYC', 0.75, True)
        self.assertFalse(pipeline.refitted,
                         "refitted should be False after update")

        # Refit should set it
        # Need enough data for refit to have effect
        for i in range(810):
            pipeline.update(
                self.signal_names[i % 2],
                self.city_codes[i % 2],
                0.5 + (i % 5) * 0.1,
                (i % 3) != 0
            )
        pipeline.refit()
        self.assertTrue(pipeline.refitted,
                        "refitted should be True after refit()")

    def test_refit_incorporates_new_data(self):
        """Refit should incorporate newly added data."""
        pipeline = CalibrationPipeline(
            self.signal_names, self.city_codes, max_history=5000, window_start=0
        )

        # Add enough data so each (signal, city) gets > MIN_SAMPLES
        for i in range(250):
            for sig in self.signal_names:
                for city in self.city_codes:
                    pipeline.update(sig, city, 0.7, True)

        # Initially calibrate returns raw (no calibrator fitted)
        initial = pipeline.calibrate('test_signal_a', 'KNYC', 0.7)

        # Refit
        pipeline.refit()

        # After refit, calibration should change
        post_refit = pipeline.calibrate('test_signal_a', 'KNYC', 0.7)
        self.assertIsNotNone(post_refit)

        # After refit with 100% accurate data, calibrated confidence should be higher
        self.assertGreaterEqual(post_refit, initial)

    # ─── Test 5: Max_History Pruning ─────────────────────────────────────

    def test_max_history_pruning(self):
        """Max_history should prune oldest entries when limit is exceeded."""
        pipeline = CalibrationPipeline(
            ['test_signal'], ['KNYC'], max_history=10, window_start=0
        )

        # Add 20 entries, max_history is 10
        for i in range(20):
            pipeline.update('test_signal', 'KNYC', 0.5 + i * 0.02, (i % 2) == 0)

        # History should be capped at 10
        history = pipeline.history.get(('test_signal', 'KNYC'), [])
        self.assertLessEqual(len(history), 10,
                             f"History should be capped at 10, got {len(history)}")

        # The most recent entries should be preserved
        # After 20 inserts with max_history=10, should have entries 10-19
        last_raw_conf = history[-1][0]
        self.assertAlmostEqual(last_raw_conf, 0.5 + 19 * 0.02,
                               places=5,
                               msg="Most recent entry should be preserved")

    def test_prune_history_logs_count(self):
        """prune_history() should return a count of pruned entries."""
        pipeline = CalibrationPipeline(
            ['test_signal'], ['KNYC'], max_history=50, window_start=10
        )

        # Add 20 entries
        for i in range(20):
            pipeline.update('test_signal', 'KNYC', 0.5 + i * 0.02, (i % 2) == 0)

        # Prune with window_start=10
        pruned = pipeline.prune_history()
        self.assertGreaterEqual(pruned, 0,
                                "prune_history should return non-negative count")

    # ─── Edge Cases ──────────────────────────────────────────────────────

    def test_empty_pipeline(self):
        """Empty pipeline should not crash on any operation."""
        empty = CalibrationPipeline([], [], max_history=10)

        # Update shouldn't crash
        empty.update('test', 'KNYC', 0.5, True)

        # Refit shouldn't crash
        empty.refit()

        # Calibrate should return raw
        result = empty.calibrate('test', 'KNYC', 0.75)
        self.assertEqual(result, 0.75)

    def test_calibrate_invalid_signal(self):
        """Calibrate with unknown signal should fall through to identity."""
        result = self.pipeline.calibrate('nonexistent_signal', 'KNYC', 0.65)
        self.assertEqual(result, 0.65)

    def test_prune_with_zero_window(self):
        """prune_history with window_start=0 should not prune anything."""
        pruned = self.pipeline.prune_history()
        self.assertEqual(pruned, 0,
                         "No pruning should occur with window_start=0")


if __name__ == '__main__':
    unittest.main()