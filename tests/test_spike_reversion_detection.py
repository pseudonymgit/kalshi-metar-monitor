import unittest
from unittest.mock import patch

from core import metar_monitor


class SpikeReversionDetectionTests(unittest.TestCase):
    def setUp(self):
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()

    def _run_sequence(self, observations):
        state = {
            "last_observed_integer": 69,
            "running_daily_max": 69.9,
            "last_settlement_bucket": 69,
            "last_instant_bucket": 69,
        }
        emitted = []

        def fake_read_temperature_state(_icao):
            return dict(state)

        def fake_commit_temperature_state(*, icao, curr_floor, running_daily_max, settlement_bucket, instant_bucket):
            del icao
            state["last_observed_integer"] = curr_floor
            state["running_daily_max"] = running_daily_max
            state["last_settlement_bucket"] = settlement_bucket
            state["last_instant_bucket"] = instant_bucket

        def fake_emit_transition_if_changed(**kwargs):
            transition_type = kwargs["transition_type"]
            if transition_type == "microstructure_spike_reversion":
                emitted.append(transition_type)
            elif transition_type and (kwargs["instant_changed"] or kwargs["settlement_changed"]):
                emitted.append(transition_type)
            return None

        with patch("core.metar_monitor._maybe_daily_reset_local", return_value=None), \
            patch("core.metar_monitor.read_temperature_state", side_effect=fake_read_temperature_state), \
            patch("core.metar_monitor.commit_temperature_state", side_effect=fake_commit_temperature_state), \
            patch("core.metar_monitor.emit_transition_if_changed", side_effect=fake_emit_transition_if_changed):
            for temp_f, obs_time in observations:
                metar_monitor._process_temperature_event(
                    icao="KDEN",
                    temp_f=temp_f,
                    obs_time=obs_time,
                    cfg={"webhook": ""},
                    last_temp_f=temp_f,
                    allow_alert_delivery=False,
                )

        return emitted

    def test_settlement_up_then_fast_reversion_emits_spike_reversion(self):
        emitted = self._run_sequence(
            [
                (70.2, "2025-01-01T12:00:00+00:00"),
                (69.4, "2025-01-01T12:04:00+00:00"),
            ]
        )

        self.assertEqual(emitted, ["settlement_up", "reversion_after_settlement", "microstructure_spike_reversion"])

    def test_settlement_up_then_slow_reversion_does_not_emit_spike_reversion(self):
        emitted = self._run_sequence(
            [
                (70.2, "2025-01-01T12:00:00+00:00"),
                (69.4, "2025-01-01T12:06:00+00:00"),
            ]
        )

        self.assertEqual(emitted, ["settlement_up", "reversion_after_settlement"])

    def test_replay_sequence_is_deterministic(self):
        observations = [
            (70.2, "2025-01-01T12:00:00+00:00"),
            (69.4, "2025-01-01T12:04:00+00:00"),
        ]

        first_run = self._run_sequence(observations)
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        second_run = self._run_sequence(observations)

        self.assertEqual(first_run, second_run)


class MicrostructureSpikeConfidenceScoringTests(unittest.TestCase):
    """Tests for the R7-A1 confidence inversion fix.

    The edge exists ONLY when the spike is transient (does NOT set the daily max).
    Key metric: running_max_delta (daily_high_margin) = how much the spike
    pushed above the running daily max at the time of the spike.

    - running_max_delta < 0.3°F → transient spike → base=0.50 (strong signal)
    - running_max_delta >= 0.3°F → spike set new daily max → base=0.10 (weak signal)

    This inverts the previous logic where is_daily_high=True gave base=0.40.
    """

    def test_transient_spike_high_confidence(self):
        """R7-A1: Transient spike (small delta) gets HIGH confidence."""
        # Spike barely exceeded running max (0.1°F margin) → transient
        tracker = {
            "daily_high_margin": 0.1,  # running_max_delta < 0.3 → transient
            "observations_since_spike": 5,
            "day_fraction_at_spike": 0.75,
            "is_daily_high": True,  # backward compat field (doesn't drive logic)
        }

        confidence, factors = metar_monitor._compute_microstructure_spike_confidence(tracker)

        # Transient spike → base=0.50
        self.assertTrue(factors["is_transient_spike"])
        self.assertAlmostEqual(factors["asymmetric_base"], 0.50, places=2)
        # Confidence should be high: 0.50 + bonus_margin + bonus_obs + bonus_time * 1.05
        # bonus_margin = min(0.1 * 0.10, 0.15) = 0.01
        # bonus_obs = min(5 * 0.02, 0.20) = 0.10
        # bonus_time = 0.75 * 0.20 = 0.15
        # raw = 0.50 + 0.01 + 0.10 + 0.15 = 0.76
        # * 1.05 = 0.798
        self.assertGreater(confidence, 0.70)
        self.assertAlmostEqual(confidence, 0.798, places=2)

    def test_structural_spike_low_confidence(self):
        """R7-A1: Structural spike (large delta, sets daily max) gets LOW confidence."""
        # Spike significantly exceeded running max (2.0°F margin) → NOT transient
        tracker = {
            "daily_high_margin": 2.0,  # running_max_delta >= 0.3 → structural
            "observations_since_spike": 5,
            "day_fraction_at_spike": 0.75,
            "is_daily_high": True,
        }

        confidence, factors = metar_monitor._compute_microstructure_spike_confidence(tracker)

        # Structural spike → base=0.10
        self.assertFalse(factors["is_transient_spike"])
        self.assertAlmostEqual(factors["asymmetric_base"], 0.10, places=2)
        # Confidence should be low: 0.10 + bonus_margin + bonus_obs + bonus_time * 1.05
        # bonus_margin = min(2.0 * 0.10, 0.15) = 0.15
        # bonus_obs = min(5 * 0.02, 0.20) = 0.10
        # bonus_time = 0.75 * 0.20 = 0.15
        # raw = 0.10 + 0.15 + 0.10 + 0.15 = 0.50
        # * 1.05 = 0.525
        self.assertLess(confidence, 0.60)
        self.assertAlmostEqual(confidence, 0.525, places=2)

    def test_confidence_inversion_transient_beats_structural(self):
        """R7-A1: KEY TEST — transient spike MUST score higher than structural spike.

        This is the core inversion fix. Before R7-A1, is_daily_high=True
        (structural spike) got base=0.40, while is_daily_high=False got base=0.0.
        Now, transient spike (small delta) gets base=0.50, structural gets base=0.10.
        """
        transient_tracker = {
            "daily_high_margin": 0.1,  # transient
            "observations_since_spike": 3,
            "day_fraction_at_spike": 0.5,
        }
        structural_tracker = {
            "daily_high_margin": 2.0,  # structural (sets new daily max)
            "observations_since_spike": 3,
            "day_fraction_at_spike": 0.5,
        }

        transient_conf, _ = metar_monitor._compute_microstructure_spike_confidence(transient_tracker)
        structural_conf, _ = metar_monitor._compute_microstructure_spike_confidence(structural_tracker)

        # CRITICAL: Transient MUST score higher than structural
        self.assertGreater(transient_conf, structural_conf,
                          "R7-A1 inversion fix: transient spike confidence must exceed structural spike confidence")

    def test_margin_threshold_boundary(self):
        """R7-A1: Test the 0.3°F threshold boundary between transient and structural."""
        # Just below threshold → transient
        tracker_below = {
            "daily_high_margin": 0.29,
            "observations_since_spike": 1,
            "day_fraction_at_spike": 0.5,
        }
        # At threshold → structural
        tracker_at = {
            "daily_high_margin": 0.3,
            "observations_since_spike": 1,
            "day_fraction_at_spike": 0.5,
        }

        conf_below, factors_below = metar_monitor._compute_microstructure_spike_confidence(tracker_below)
        conf_at, factors_at = metar_monitor._compute_microstructure_spike_confidence(tracker_at)

        self.assertTrue(factors_below["is_transient_spike"])
        self.assertFalse(factors_at["is_transient_spike"])
        self.assertGreater(conf_below, conf_at)

    def test_down_reversion_discount_applied(self):
        """R7-A1: Down-reversion signals get 15% discount on top of base logic."""
        tracker = {
            "daily_high_margin": 0.1,  # transient
            "observations_since_spike": 5,
            "day_fraction_at_spike": 0.75,
        }

        conf_up, _ = metar_monitor._compute_microstructure_spike_confidence(tracker, is_down=False)
        conf_down, _ = metar_monitor._compute_microstructure_spike_confidence(tracker, is_down=True)

        # Down reversion should be lower due to 0.85 discount vs 1.05 boost
        self.assertLess(conf_down, conf_up)

    def test_confidence_clamped_to_one(self):
        """Test that confidence is clamped to 1.0."""
        # Maximum possible: transient + large bonuses
        tracker = {
            "daily_high_margin": 0.1,  # transient
            "observations_since_spike": 50,  # Many observations
            "day_fraction_at_spike": 1.0,  # End of day
        }

        confidence, factors = metar_monitor._compute_microstructure_spike_confidence(tracker)

        self.assertLessEqual(confidence, 1.0)

    def test_zero_delta_confidence(self):
        """R7-A1: Zero delta (spike exactly at running max) → transient, base=0.50."""
        tracker = {
            "daily_high_margin": 0.0,
            "observations_since_spike": 2,
            "day_fraction_at_spike": 0.5,
        }

        confidence, factors = metar_monitor._compute_microstructure_spike_confidence(tracker)

        self.assertTrue(factors["is_transient_spike"])
        self.assertAlmostEqual(factors["asymmetric_base"], 0.50, places=2)
        # Should have moderate confidence
        self.assertGreater(confidence, 0.50)

    def test_backward_compat_fields_preserved(self):
        """R7-A1: backward-compat fields are preserved in confidence_factors."""
        tracker = {
            "daily_high_margin": 0.1,
            "is_daily_high": True,
            "is_daily_low": False,
            "observations_since_spike": 3,
            "day_fraction_at_spike": 0.5,
        }

        _, factors = metar_monitor._compute_microstructure_spike_confidence(tracker)

        # New fields present
        self.assertIn("is_transient_spike", factors)
        self.assertIn("running_max_delta", factors)
        # Backward-compat fields preserved
        self.assertIn("is_daily_high", factors)
        self.assertIn("is_daily_low", factors)
        self.assertIn("is_daily_extreme", factors)
        self.assertEqual(factors["is_daily_high"], True)
        self.assertEqual(factors["is_daily_low"], False)


if __name__ == "__main__":
    unittest.main()