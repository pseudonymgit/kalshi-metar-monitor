#!/usr/bin/env python3
"""
Unit Tests for PlattCalibrationPipeline (Phase 1 calibration).

Tests:
1. PlattCalibrator — fit, transform, regularization
2. Climate group mapping
3. Signal family mapping
4. 7-level fallback cascade
5. 10-bin diagnostics
6. Dual-method divergence detection
7. Serialization round-trip
8. Migration script
9. Phase 2: SynopticRegimeDetector
10. Phase 2: Season utilities
11. Phase 2: Regime/seasons-aware calibration + fallback
12. Phase 2: nowcast_path
13. Phase 2: check_drift

Usage:
    pytest tests/test_platt_calibration.py -v
    python tests/test_platt_calibration.py
"""

import os
import sys
import json
import tempfile
import unittest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.platt_calibration import (
    PlattCalibrator, PlattCalibrationPipeline,
    CLIMATE_GROUPS, STATION_TO_GROUP, GEFS_SIGNALS, HEURISTIC_SIGNALS,
    get_climate_group, get_signal_family,
    MIN_SAMPLES, DIRECTIONS, MARKET_TYPES, SIGNAL_FAMILIES,
    logit_transform, compute_bin_diagnostics, check_platt_bin_divergence,
    REGULARIZATION_THRESHOLD,
    # Phase 2
    SEASONS, SEASON_MONTHS, SYNOPTIC_REGIMES,
    get_season_from_month, get_season_from_date,
    SynopticRegimeDetector, DRIFT_ALPHA_THRESHOLD, DRIFT_BETA_THRESHOLD,
)


class TestPlattCalibrator(unittest.TestCase):
    """Test the core PlattCalibrator class."""

    def test_identity_when_no_data(self):
        """Calibrator with no data should return identity (α=1, β=0)."""
        cal = PlattCalibrator()
        logits = np.array([0.0, 1.0, 2.0])
        result = cal.transform(logits)
        expected = 1 / (1 + np.exp(-logits))
        np.testing.assert_array_almost_equal(result, expected)

    def test_fit_perfectly_calibrated(self):
        """If outcomes perfectly match raw confidences, α≈1, β≈0."""
        np.random.seed(42)
        n = 500
        raw_confs = np.random.uniform(0.55, 0.95, n)
        logits = logit_transform(raw_confs)
        # True outcomes are exactly the raw confidences
        outcomes = np.random.binomial(1, raw_confs)

        cal = PlattCalibrator.from_data(logits, outcomes)
        self.assertAlmostEqual(cal.alpha, 1.0, delta=0.3)
        self.assertAlmostEqual(cal.beta, 0.0, delta=0.3)
        self.assertEqual(cal.n, n)
        self.assertTrue(cal.fitted)

    def test_fit_overconfident_system(self):
        """If signal is overconfident, α > 1 or β < 0."""
        n = 500
        raw_confs = np.random.uniform(0.55, 0.95, n)
        logits = logit_transform(raw_confs)
        # Actual win rate ~0.45, but signal says 0.8
        outcomes = np.random.binomial(1, 0.45 * np.ones(n))

        cal = PlattCalibrator.from_data(logits, outcomes)
        # Should detect overconfidence: β < 0 (negative = shift to lower probabilities)
        # or α > 1 (steeper curve). Bounds: α∈[0.01,5], β∈[-3,3]
        self.assertTrue(cal.beta < 0.0 or cal.alpha > 1.1,
                        f"Expected β < 0 or α > 1 for overconfident system, got α={cal.alpha:.3f}, β={cal.beta:.3f}")

    def test_regularization_small_n(self):
        """For n < 200, regularization should pull toward identity."""
        n = 80  # Well within regularization range
        raw_confs = np.ones(n) * 0.75
        logits = logit_transform(raw_confs)
        outcomes = np.ones(n)  # 100% correct, all 0.75

        cal = PlattCalibrator.from_data(logits, outcomes)
        # With n=80 and regularization, α should be < 1.0 (pulled toward identity)
        # and β should be near 0 (pulled toward identity)
        # 100% correct at 0.75 should give α > 1, β > 0 without regularization
        # With regularization, these should be closer to identity
        self.assertLess(abs(cal.beta), 2.0,
                        f"Regularization should bound β, got {cal.beta:.3f}")

    def test_regularization_large_n(self):
        """For n >= 200, regularization should have negligible effect."""
        n = 500
        raw_confs = np.random.uniform(0.55, 0.95, n)
        logits = logit_transform(raw_confs)
        outcomes = np.random.binomial(1, 0.5 * np.ones(n))

        cal = PlattCalibrator.from_data(logits, outcomes)
        # Should converge to MLE without being pulled to identity
        self.assertNotEqual(cal.alpha, 1.0)

    def test_transform_monotonic(self):
        """Platt transform should be monotonic in raw confidence."""
        cal = PlattCalibrator(alpha=0.85, beta=-0.12, n=200)
        logits = logit_transform(np.array([0.55, 0.65, 0.75, 0.85, 0.95]))
        results = cal.transform(logits)
        for i in range(len(results) - 1):
            self.assertGreaterEqual(results[i + 1], results[i])

    def test_transform_range(self):
        """Platt output should be in (0, 1)."""
        cal = PlattCalibrator(alpha=0.85, beta=-0.12, n=200)
        logits = logit_transform(np.linspace(0.51, 0.99, 50))
        results = cal.transform(logits)
        self.assertTrue(np.all(results > 0.0))
        self.assertTrue(np.all(results < 1.0))

    def test_to_from_dict(self):
        """Round-trip serialization."""
        cal = PlattCalibrator(alpha=0.85, beta=-0.12, n=300)
        d = cal.to_dict()
        restored = PlattCalibrator.from_dict(d)
        self.assertEqual(restored.alpha, 0.85)
        self.assertEqual(restored.beta, -0.12)
        self.assertEqual(restored.n, 300)


class TestClimateGroups(unittest.TestCase):
    """Test climate group mapping."""

    def test_all_stations_mapped(self):
        """Every station in the 20-station set should map to a group."""
        all_stations = [
            "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
            "KLAS", "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC",
            "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
        ]
        for s in all_stations:
            group = get_climate_group(s)
            self.assertIn(group, CLIMATE_GROUPS,
                          f"Station {s} should map to a known group, got {group}")

    def test_each_group_has_stations(self):
        """Each climate group should have at least one station."""
        for group, stations in CLIMATE_GROUPS.items():
            self.assertGreater(len(stations), 0, f"Group {group} has no stations")

    def test_unknown_station_falls_back(self):
        """Unknown station should fall back to 'interior'."""
        self.assertEqual(get_climate_group("ZZZZ"), "interior")


class TestSignalFamilies(unittest.TestCase):
    """Test signal family mapping."""

    def test_known_signals_mapped(self):
        """Known GEFS/heuristic signals should map correctly."""
        for sig in GEFS_SIGNALS:
            self.assertEqual(get_signal_family(sig), "gefs")
        for sig in HEURISTIC_SIGNALS:
            self.assertEqual(get_signal_family(sig), "heuristic")

    def test_unknown_signal_defaults_heuristic(self):
        """Unknown signal should default to heuristic."""
        self.assertEqual(get_signal_family("unknown_signal"), "heuristic")


class TestBinDiagnostics(unittest.TestCase):
    """Test 10-bin diagnostic computation."""

    def test_compute_bin_diagnostics(self):
        """Bin diagnostics should produce valid bin structure."""
        n = 200
        raw_confs = np.random.uniform(0.55, 0.95, n)
        outcomes = np.random.binomial(1, 0.55, n)

        # Fit a calibrator
        logits = logit_transform(raw_confs)
        cal = PlattCalibrator.from_data(logits, outcomes)

        bins, ece, brier = compute_bin_diagnostics(raw_confs, outcomes, cal)

        self.assertEqual(len(bins), 10)
        self.assertGreaterEqual(ece, 0.0)
        self.assertLessEqual(ece, 1.0)
        self.assertGreaterEqual(brier, 0.0)

        # Check bin structure
        for bin_entry in bins:
            self.assertIn("conf_low", bin_entry)
            self.assertIn("conf_high", bin_entry)
            self.assertIn("n", bin_entry)

    def test_dual_method_divergence_ok(self):
        """Well-calibrated data should not trigger divergence flag."""
        n = 500
        raw_confs = np.random.uniform(0.55, 0.95, n)
        # Perfectly calibrated: outcomes = raw_confs
        outcomes = np.random.binomial(1, raw_confs)

        logits = logit_transform(raw_confs)
        cal = PlattCalibrator.from_data(logits, outcomes)

        ok = check_platt_bin_divergence(cal, raw_confs, outcomes)
        self.assertTrue(ok)


class TestPlattCalibrationPipeline(unittest.TestCase):
    """Test the full calibration pipeline."""

    def setUp(self):
        self.pipeline = PlattCalibrationPipeline()

    def test_record_outcome(self):
        """Recording an outcome should populate all 6 storage levels."""
        self.pipeline.record_outcome(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, was_correct=True,
        )

        # Should have entries in L1-L6
        self.assertIn("up_HIGH_heuristic_continental", self.pipeline._data)
        self.assertIn("up_HIGH_heuristic", self.pipeline._data)
        self.assertIn("up_HIGH", self.pipeline._data)
        self.assertIn("up", self.pipeline._data)
        self.assertIn("up_heuristic", self.pipeline._data)
        self.assertIn("_global", self.pipeline._data)

    def test_refit_with_sufficient_data(self):
        """With enough data, L1 calibrators should be fitted."""
        # Add 100 records per cell (50 * 2 directions * 2 markets * 2 families * 5 groups =)
        target_per_cell = 60  # Above MIN_SAMPLES=50
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]  # Use first station in each group
                        for _ in range(target_per_cell):
                            raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                            outcome = np.random.binomial(1, 0.55)
                            self.pipeline.record_outcome(
                                station=station, direction=direction,
                                market_type=mt, signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                raw_conf=raw_conf, was_correct=bool(outcome),
                            )

        self.pipeline.refit()

        # Check that L1 calibrators are populated
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group in CLIMATE_GROUPS.keys():
                        key = f"{direction}_{mt}_{fam}_{group}"
                        if key in self.pipeline._calibrators:
                            cal = self.pipeline._calibrators[key]
                            self.assertGreater(cal.n, 0)

        # Should have at least some calibrators
        self.assertGreater(len(self.pipeline._calibrators), 0)

    def test_7_level_fallback(self):
        """Fallback cascade should work: L1 → L2 → ... → L7."""
        # Only add data to L6 (_global) — L1-L5 should be empty
        for _ in range(200):
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="gaussian", raw_conf=0.75, was_correct=True,
            )

        self.pipeline.refit()

        # Should have global calibrator
        self.assertIn("_global", self.pipeline._calibrators)

        # Calibrate should use global fallback
        result = self.pipeline.calibrate(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75,
        )
        self.assertAlmostEqual(result, 0.75, delta=0.3)

    def test_identity_fallback(self):
        """With no data at all, should return raw confidence (L7 identity)."""
        result = self.pipeline.calibrate(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75,
        )
        self.assertEqual(result, 0.75)

    def test_serialization_round_trip(self):
        """Save and load should preserve calibrators."""
        # Add some data
        for _ in range(200):
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="gaussian", raw_conf=0.75, was_correct=True,
            )
            self.pipeline.record_outcome(
                station="KLAX", direction="down", market_type="LOW",
                signal_name="forecast_disagreement", raw_conf=0.65, was_correct=False,
            )

        self.pipeline.refit()

        # Save to JSON string
        d = self.pipeline.to_json_dict()

        # Verify structure
        self.assertIn("_metadata", d)
        self.assertEqual(d["_metadata"]["version"], 3)
        self.assertIn("calibrators", d)
        self.assertIn("_fallback", d)
        self.assertIn("diagnostics", d)
        self.assertIn("per_station_ece", d["diagnostics"])

        # Save to temp file and reload
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(d, f)
            tmp_path = f.name

        try:
            loaded = PlattCalibrationPipeline.load(tmp_path)
            self.assertTrue(loaded.refitted)
            self.assertIn("_global", loaded._calibrators)
        finally:
            os.unlink(tmp_path)

    def test_gefs_vs_heuristic_separation(self):
        """GEFS and heuristic signals should use different calibration keys."""
        # Add data for both families
        for _ in range(200):
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="forecast_disagreement", raw_conf=0.85, was_correct=True,
            )
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="gaussian", raw_conf=0.65, was_correct=False,
            )

        self.pipeline.refit()

        # GEFS and heuristic should have different keys
        gefs_key = "up_HIGH_gefs"
        heur_key = "up_HIGH_heuristic"

        # At least one should be in calibrators or fallback
        all_keys = set(self.pipeline._calibrators.keys())

        # The families should be tracked separately
        self.assertIn("up_HIGH_gefs_continental", self.pipeline._data)
        self.assertIn("up_HIGH_heuristic_continental", self.pipeline._data)

    def test_market_type_split(self):
        """HIGH and LOW markets should be tracked separately."""
        self.pipeline.record_outcome(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, was_correct=True,
        )
        self.pipeline.record_outcome(
            station="KNYC", direction="up", market_type="LOW",
            signal_name="gaussian", raw_conf=0.65, was_correct=False,
        )

        self.assertIn("up_HIGH_heuristic_continental", self.pipeline._data)
        self.assertIn("up_LOW_heuristic_continental", self.pipeline._data)


class TestMigrationScript(unittest.TestCase):
    """Test the v2→v3 migration script."""

    def test_approximate_logistic_from_bins(self):
        """Approximate logistic from bins should produce valid alpha/beta."""
        from scripts.migrate_calibration_v3 import approximate_logistic_from_bins

        bins = {
            "0.50-0.55": {"win_rate": 0.52, "n": 50},
            "0.55-0.60": {"win_rate": 0.54, "n": 40},
            "0.60-0.65": {"win_rate": 0.58, "n": 45},
            "0.65-0.70": {"win_rate": 0.62, "n": 35},
            "0.70-0.75": {"win_rate": 0.65, "n": 50},
            "0.75-0.80": {"win_rate": 0.70, "n": 40},
            "0.80-0.85": {"win_rate": 0.75, "n": 45},
            "0.85-0.90": {"win_rate": 0.80, "n": 35},
            "0.90-0.95": {"win_rate": 0.85, "n": 50},
            "0.95-1.00": {"win_rate": 0.90, "n": 40},
        }
        result = approximate_logistic_from_bins(bins, 430)
        # Reasonable alpha/beta for data that tracks diagonal
        self.assertGreater(result["alpha"], 0.0)
        self.assertIsInstance(result["alpha"], float)
        self.assertIsInstance(result["beta"], float)
        self.assertEqual(result["n"], 430)

    def test_approximate_logistic_sparse_bins(self):
        """With sparse bins, should return near-identity."""
        from scripts.migrate_calibration_v3 import approximate_logistic_from_bins

        bins = {
            "0.50-0.55": {"win_rate": None, "n": 2},
            "0.55-0.60": {"win_rate": None, "n": 1},
        }
        result = approximate_logistic_from_bins(bins, 3)
        self.assertEqual(result["alpha"], 1.0)
        self.assertEqual(result["beta"], 0.0)


class TestSeasonUtilities(unittest.TestCase):
    """Phase 2: Season dimension utilities."""

    def test_get_season_from_month(self):
        """Each month maps to the correct season."""
        winter_months = [12, 1, 2]
        spring_months = [3, 4, 5]
        summer_months = [6, 7, 8]
        fall_months = [9, 10, 11]

        for m in winter_months:
            self.assertEqual(get_season_from_month(m), "winter")
        for m in spring_months:
            self.assertEqual(get_season_from_month(m), "spring")
        for m in summer_months:
            self.assertEqual(get_season_from_month(m), "summer")
        for m in fall_months:
            self.assertEqual(get_season_from_month(m), "fall")

    def test_get_season_from_date(self):
        """ISO date strings map to correct seasons."""
        self.assertEqual(get_season_from_date("2026-01-15"), "winter")
        self.assertEqual(get_season_from_date("2026-04-10"), "spring")
        self.assertEqual(get_season_from_date("2026-07-20"), "summer")
        self.assertEqual(get_season_from_date("2026-10-05"), "fall")
        # ISO 8601 datetime form
        self.assertEqual(get_season_from_date("2026-12-01T12:00:00Z"), "winter")

    def test_get_season_from_date_bad_input(self):
        """Malformed date should not raise."""
        self.assertIsInstance(get_season_from_date(""), str)
        self.assertIsInstance(get_season_from_date("not-a-date"), str)


class TestSynopticRegimeDetector(unittest.TestCase):
    """Phase 2: METAR-based regime detection."""

    def test_quiescent(self):
        """Stable conditions → quiescent."""
        regime = SynopticRegimeDetector.detect_regime(
            pressure_tendency_3h=0.2, wind_direction_shift=10.0,
            cloud_cover_pct=0.1, temp_trend_3h=0.5,
        )
        self.assertEqual(regime, "quiescent")

    def test_frontal_pressure_wind(self):
        """Pressure surge + wind shift → frontal."""
        regime = SynopticRegimeDetector.detect_regime(
            pressure_tendency_3h=-2.0, wind_direction_shift=60.0,
            cloud_cover_pct=0.7,
        )
        self.assertEqual(regime, "frontal")

    def test_frontal_pressure_alone(self):
        """Strong pressure tendency alone → frontal."""
        regime = SynopticRegimeDetector.detect_regime(
            pressure_tendency_3h=2.0, wind_direction_shift=10.0,
        )
        self.assertEqual(regime, "frontal")

    def test_cold_advection(self):
        """Rising pressure + temperature drop → cold_advection."""
        regime = SynopticRegimeDetector.detect_regime(
            pressure_tendency_3h=2.0, wind_direction_shift=20.0,
            temp_trend_3h=-4.0,
        )
        self.assertEqual(regime, "cold_advection")

    def test_convective(self):
        """Falling pressure + high cloud cover → convective."""
        regime = SynopticRegimeDetector.detect_regime(
            pressure_tendency_3h=-2.0, cloud_cover_pct=0.8,
        )
        self.assertEqual(regime, "convective")

    def test_unknown_insufficient_data(self):
        """No usable signals → unknown."""
        regime = SynopticRegimeDetector.detect_regime()
        self.assertEqual(regime, "unknown")

    def test_from_metar_record(self):
        """dict-based detection works with expected keys."""
        record = {
            "pressure_tendency_3h": -2.5,
            "wind_dir_shift_3h": 55.0,
            "cloud_cover_pct": 0.75,
            "temp_trend_3h": -2.0,
        }
        self.assertEqual(
            SynopticRegimeDetector.detect_regime_from_metar_record(record),
            "frontal",
        )

    def test_known_regime_labels(self):
        """Detector output is always a known regime label."""
        cases = [
            dict(pressure_tendency_3h=0.0),
            dict(pressure_tendency_3h=2.5),
            dict(pressure_tendency_3h=-2.5, cloud_cover_pct=0.9),
            dict(pressure_tendency_3h=2.5, temp_trend_3h=-5.0),
            dict(wind_direction_shift=80.0),
            dict(cloud_cover_pct=0.7),
            dict(pressure_tendency_3h=-0.5, cloud_cover_pct=0.2),
        ]
        for c in cases:
            r = SynopticRegimeDetector.detect_regime(**c)
            self.assertIn(r, SYNOPTIC_REGIMES, f"regime {r} not in known set")


class TestPhase2RegimeSeasonCalibration(unittest.TestCase):
    """Phase 2: regime-aware + season-aware calibration."""

    def setUp(self):
        self.pipeline = PlattCalibrationPipeline()

    def _seed_regime_data(self, target_per_cell=60, regimes=("quiescent", "frontal")):
        """Seed data so regime cells have >= MIN_SAMPLES."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]
                        for regime in regimes:
                            for _ in range(target_per_cell):
                                raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                                outcome = np.random.binomial(1, 0.55)
                                self.pipeline.record_outcome(
                                    station=station, direction=direction,
                                    market_type=mt,
                                    signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                    raw_conf=raw_conf, was_correct=bool(outcome),
                                    regime=regime,
                                )

    def test_regime_recording_populates_data(self):
        """Records with regime label land in regime-specific bucket."""
        self.pipeline.record_outcome(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, was_correct=True,
            regime="frontal",
        )
        key = "up_HIGH_heuristic_continental_frontal"
        self.assertIn(key, self.pipeline._data)
        self.assertEqual(len(self.pipeline._data[key]), 1)

    def test_season_recording_populates_data(self):
        """Records with season label land in season-specific bucket."""
        self.pipeline.record_outcome(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, was_correct=True,
            season="winter",
        )
        key = "up_HIGH_heuristic_continental_winter"
        self.assertIn(key, self.pipeline._data)
        self.assertEqual(len(self.pipeline._data[key]), 1)

    def test_regime_fit_populates_regime_calibrators(self):
        """Regime cells with enough data get fitted calibrators."""
        self._seed_regime_data()
        self.pipeline.refit()
        self.assertGreater(len(self.pipeline._regime_calibrators), 0)

    def test_season_fit_populates_season_calibrators(self):
        """Season cells with enough data get fitted calibrators."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]
                        for season in ("winter", "summer"):
                            for _ in range(60):
                                raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                                outcome = np.random.binomial(1, 0.55)
                                self.pipeline.record_outcome(
                                    station=station, direction=direction,
                                    market_type=mt,
                                    signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                    raw_conf=raw_conf, was_correct=bool(outcome),
                                    season=season,
                                )
        self.pipeline.refit()
        self.assertGreater(len(self.pipeline._season_calibrators), 0)

    def test_regime_calibrate_preferred_over_phase1(self):
        """With regime data present, calibrate() uses the regime curve."""
        self._seed_regime_data()
        self.pipeline.refit()

        result = self.pipeline.calibrate(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, regime="frontal",
        )
        # Regime curve should exist and be used
        key = "up_HIGH_heuristic_continental_frontal"
        if key in self.pipeline._regime_calibrators:
            expected = self.pipeline._regime_calibrators[key].transform(
                logit_transform(np.array([0.75])))[0]
            self.assertAlmostEqual(result, float(np.clip(expected, 0.0, 1.0)), delta=1e-6)

    def test_regime_fallback_to_phase1(self):
        """Unknown regime falls back to Phase 1 calibration (no crash)."""
        # Seed only Phase 1 data
        for _ in range(200):
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="gaussian", raw_conf=0.75, was_correct=True,
            )
        self.pipeline.refit()

        result = self.pipeline.calibrate(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, regime="unknown",
        )
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)

    def test_season_calibrate_uses_season_curve(self):
        """Season-specific calibration is applied when season provided."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]
                        for _ in range(60):
                            raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                            outcome = np.random.binomial(1, 0.55)
                            self.pipeline.record_outcome(
                                station=station, direction=direction,
                                market_type=mt,
                                signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                raw_conf=raw_conf, was_correct=bool(outcome),
                                season="winter",
                            )
        self.pipeline.refit()

        result = self.pipeline.calibrate(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75, season="winter",
        )
        key = "up_HIGH_heuristic_continental_winter"
        if key in self.pipeline._season_calibrators:
            expected = self.pipeline._season_calibrators[key].transform(
                logit_transform(np.array([0.75])))[0]
            self.assertAlmostEqual(result, float(np.clip(expected, 0.0, 1.0)), delta=1e-6)

    def test_regime_and_season_serialization_round_trip(self):
        """Regime/season calibrators survive save → load."""
        self._seed_regime_data()
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]
                        for _ in range(60):
                            raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                            outcome = np.random.binomial(1, 0.55)
                            self.pipeline.record_outcome(
                                station=station, direction=direction,
                                market_type=mt,
                                signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                raw_conf=raw_conf, was_correct=bool(outcome),
                                season="winter",
                            )
        self.pipeline.refit()

        d = self.pipeline.to_json_dict()
        self.assertIn("_regime_calibrators", d)
        self.assertIn("_season_calibrators", d)
        self.assertIn("phase2", d["_metadata"])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(d, f)
            tmp_path = f.name

        try:
            loaded = PlattCalibrationPipeline.load(tmp_path)
            self.assertEqual(
                len(loaded._regime_calibrators),
                len(self.pipeline._regime_calibrators),
            )
            self.assertEqual(
                len(loaded._season_calibrators),
                len(self.pipeline._season_calibrators),
            )
        finally:
            os.unlink(tmp_path)

    def test_batch_recording_8_tuple(self):
        """8-tuple batch records include regime and season."""
        records = [
            ("KNYC", "up", "HIGH", "gaussian", 0.75, True, "frontal", "winter"),
            ("KLAX", "down", "LOW", "forecast_disagreement", 0.65, False, "quiescent", "summer"),
        ]
        self.pipeline.record_outcomes_batch(records)
        self.assertIn("up_HIGH_heuristic_continental_frontal", self.pipeline._data)
        self.assertIn("down_LOW_gefs_coastal_warm_quiescent", self.pipeline._data)
        self.assertIn("up_HIGH_heuristic_continental_winter", self.pipeline._data)
        self.assertIn("down_LOW_gefs_coastal_warm_summer", self.pipeline._data)


class TestNowcastPath(unittest.TestCase):
    """Phase 2: Nowcasting integration."""

    def setUp(self):
        self.pipeline = PlattCalibrationPipeline()

    def test_nowcast_path_returns_valid_probability(self):
        """nowcast_path returns a probability in [0,1]."""
        metar_record = {
            "pressure_tendency_3h": -2.0,
            "wind_dir_shift_3h": 50.0,
            "cloud_cover_pct": 0.7,
            "temp_trend_3h": -1.0,
        }
        result = self.pipeline.nowcast_path(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75,
            metar_record=metar_record,
        )
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)

    def test_nowcast_path_uses_regime_calibration(self):
        """With regime data seeded, nowcast uses regime curve."""
        # Seed regime-specific data
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group, stations in CLIMATE_GROUPS.items():
                        station = stations[0]
                        for _ in range(60):
                            raw_conf = 0.75 + np.random.uniform(-0.1, 0.1)
                            outcome = np.random.binomial(1, 0.55)
                            self.pipeline.record_outcome(
                                station=station, direction=direction,
                                market_type=mt,
                                signal_name=list(GEFS_SIGNALS)[0] if fam == "gefs" else "gaussian",
                                raw_conf=raw_conf, was_correct=bool(outcome),
                                regime="frontal",
                            )
        self.pipeline.refit()

        metar_record = {
            "pressure_tendency_3h": -2.5,
            "wind_dir_shift_3h": 60.0,
            "cloud_cover_pct": 0.8,
            "temp_trend_3h": -2.0,
        }
        result = self.pipeline.nowcast_path(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75,
            metar_record=metar_record,
        )
        key = "up_HIGH_heuristic_continental_frontal"
        if key in self.pipeline._regime_calibrators:
            expected = self.pipeline._regime_calibrators[key].transform(
                logit_transform(np.array([0.75])))[0]
            self.assertAlmostEqual(result, float(np.clip(expected, 0.0, 1.0)), delta=1e-6)

    def test_nowcast_path_with_season_from_date(self):
        """Season derived from metar_record date when not provided."""
        metar_record = {
            "pressure_tendency_3h": 0.1,
            "wind_dir_shift_3h": 5.0,
            "cloud_cover_pct": 0.1,
            "temp_trend_3h": 0.2,
            "date_utc": "2026-01-15T12:00:00Z",
        }
        result = self.pipeline.nowcast_path(
            station="KNYC", direction="up", market_type="HIGH",
            signal_name="gaussian", raw_conf=0.75,
            metar_record=metar_record,
        )
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)


class TestCheckDrift(unittest.TestCase):
    """Phase 2: Drift diagnostics."""

    def setUp(self):
        self.pipeline = PlattCalibrationPipeline()

    def _seed_phase1(self, n=200, win_rate=0.55):
        for _ in range(n):
            self.pipeline.record_outcome(
                station="KNYC", direction="up", market_type="HIGH",
                signal_name="gaussian", raw_conf=0.75,
                was_correct=bool(np.random.binomial(1, win_rate)),
            )

    def test_no_prior_state_returns_empty(self):
        """Without prior state, drift report is empty and safe."""
        report = self.pipeline.check_drift(None)
        self.assertEqual(report["drifted_cells"], [])
        self.assertEqual(report["total_cells"], 0)

    def test_identical_state_no_drift(self):
        """Same α/β → no drift flags."""
        self._seed_phase1()
        self.pipeline.refit()
        prior = self.pipeline.to_json_dict()

        report = self.pipeline.check_drift(prior)
        self.assertEqual(report["drifted_cells"], [])

    def test_drift_detected_on_alpha_change(self):
        """Large α change (>0.5) is flagged."""
        self._seed_phase1()
        self.pipeline.refit()
        prior = self.pipeline.to_json_dict()

        # Simulate a shift: flip α by +1.0 in a cell
        current = self.pipeline.to_json_dict()
        target_key = None
        for key in current["calibrators"]:
            target_key = key
            break
        self.assertIsNotNone(target_key)

        prior["calibrators"][target_key]["alpha"] = current["calibrators"][target_key]["alpha"] + 1.0
        report = self.pipeline.check_drift(prior)

        flagged = [c for c in report["drifted_cells"] if c["key"] == target_key]
        self.assertTrue(any(c["alpha_change"] > DRIFT_ALPHA_THRESHOLD for c in flagged))

    def test_drift_detected_on_beta_change(self):
        """Large β change (>0.3) is flagged."""
        self._seed_phase1()
        self.pipeline.refit()
        prior = self.pipeline.to_json_dict()

        current = self.pipeline.to_json_dict()
        target_key = None
        for key in current["calibrators"]:
            target_key = key
            break
        self.assertIsNotNone(target_key)

        prior["calibrators"][target_key]["beta"] = current["calibrators"][target_key]["beta"] + 0.5
        report = self.pipeline.check_drift(prior)

        flagged = [c for c in report["drifted_cells"] if c["key"] == target_key]
        self.assertTrue(any(c["beta_change"] > DRIFT_BETA_THRESHOLD for c in flagged))

    def test_small_change_not_flagged(self):
        """Sub-threshold changes are not flagged."""
        self._seed_phase1()
        self.pipeline.refit()
        prior = self.pipeline.to_json_dict()

        current = self.pipeline.to_json_dict()
        target_key = None
        for key in current["calibrators"]:
            target_key = key
            break
        self.assertIsNotNone(target_key)

        prior["calibrators"][target_key]["alpha"] = current["calibrators"][target_key]["alpha"] + 0.05
        report = self.pipeline.check_drift(prior)

        flagged = [c for c in report["drifted_cells"] if c["key"] == target_key]
        self.assertEqual(flagged, [])


if __name__ == "__main__":
    unittest.main()