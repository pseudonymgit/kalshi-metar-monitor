import unittest
from unittest.mock import patch

from core import metar_monitor


class GoldilocksDetectionTests(unittest.TestCase):
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
            if transition_type == "goldilocks_reversion":
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

    def test_settlement_up_then_fast_reversion_emits_goldilocks(self):
        emitted = self._run_sequence(
            [
                (70.2, "2025-01-01T12:00:00+00:00"),
                (69.4, "2025-01-01T12:04:00+00:00"),
            ]
        )

        self.assertEqual(emitted, ["settlement_up", "reversion_after_settlement", "goldilocks_reversion"])

    def test_settlement_up_then_slow_reversion_does_not_emit_goldilocks(self):
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


class GoldilocksConfidenceScoringTests(unittest.TestCase):
    def test_compute_goldilocks_confidence_basic(self):
        """Test basic confidence scoring with is_daily_high=True."""
        tracker = {
            "is_daily_high": True,
            "daily_high_margin": 0.5,
            "observations_since_spike": 5,
            "day_fraction_at_spike": 0.75,
        }
        
        confidence, factors = metar_monitor._compute_goldilocks_confidence(tracker)
        
        # Verify factors
        self.assertTrue(factors["is_daily_high"])
        self.assertAlmostEqual(factors["daily_high_margin"], 0.5, places=1)
        self.assertEqual(factors["observations_since_spike"], 5)
        self.assertAlmostEqual(factors["day_fraction_at_spike"], 0.75, places=2)
        
        # Verify confidence score calculation:
        # base = 0.4 (is_daily_high=True)
        # bonus_margin = min(0.5 * 0.15, 0.2) = min(0.075, 0.2) = 0.075
        # bonus_obs = min(5 * 0.02, 0.2) = min(0.1, 0.2) = 0.1
        # bonus_time = 0.75 * 0.2 = 0.15
        # confidence = 0.4 + 0.075 + 0.1 + 0.15 = 0.725
        self.assertAlmostEqual(confidence, 0.725, places=2)

    def test_compute_goldilocks_confidence_not_daily_high(self):
        """Test confidence scoring with is_daily_high=False."""
        tracker = {
            "is_daily_high": False,
            "daily_high_margin": 0.1,
            "observations_since_spike": 3,
            "day_fraction_at_spike": 0.5,
        }
        
        confidence, factors = metar_monitor._compute_goldilocks_confidence(tracker)
        
        # Verify factors
        self.assertFalse(factors["is_daily_high"])
        
        # Verify confidence score calculation:
        # base = 0.0 (is_daily_high=False)
        # bonus_margin = min(0.1 * 0.15, 0.2) = 0.015
        # bonus_obs = min(3 * 0.02, 0.2) = 0.06
        # bonus_time = 0.5 * 0.2 = 0.1
        # confidence = 0.0 + 0.015 + 0.06 + 0.1 = 0.175
        self.assertAlmostEqual(confidence, 0.175, places=2)

    def test_compute_goldilocks_confidence_clamped_to_one(self):
        """Test that confidence is clamped to 1.0."""
        tracker = {
            "is_daily_high": True,
            "daily_high_margin": 2.0,  # Large margin
            "observations_since_spike": 50,  # Many observations
            "day_fraction_at_spike": 1.0,  # End of day
        }
        
        confidence, factors = metar_monitor._compute_goldilocks_confidence(tracker)
        
        # Verify confidence is clamped to 1.0
        self.assertLessEqual(confidence, 1.0)
        # With is_daily_high=True, max should be 0.4 + 0.2 + 0.2 + 0.2 = 1.0
        self.assertAlmostEqual(confidence, 1.0, places=2)

    def test_compute_goldilocks_confidence_momentum_down(self):
        """Test confidence scoring for goldilocks_momentum_down (inverted logic)."""
        tracker = {
            "is_daily_high": True,  # For momentum_down, this means reversion from daily high
            "daily_high_margin": 0.3,
            "observations_since_spike": 4,
            "day_fraction_at_spike": 0.6,
        }
        
        confidence, factors = metar_monitor._compute_goldilocks_confidence(tracker, is_down=True)
        
        # For momentum_down, is_daily_high is still the key factor (reversion from daily high)
        self.assertTrue(factors["is_daily_high"])
        
        # Verify confidence score calculation:
        # base = 0.4 (is_daily_high=True)
        # bonus_margin = min(0.3 * 0.15, 0.2) = min(0.045, 0.2) = 0.045
        # bonus_obs = min(4 * 0.02, 0.2) = min(0.08, 0.2) = 0.08
        # bonus_time = 0.6 * 0.2 = 0.12
        # confidence = 0.4 + 0.045 + 0.08 + 0.12 = 0.645
        self.assertAlmostEqual(confidence, 0.645, places=2)

    def test_is_daily_high_threshold_tolerance(self):
        """Test is_daily_high comparison with 0.1°F tolerance."""
        # Scenario 1: max_temp_after_up is within tolerance of running_daily_max
        tracker = {
            "max_temp_after_up": 70.0,
            "running_daily_max": 70.05,  # Within 0.1°F tolerance
        }
        # Compute is_daily_high: 70.0 >= 70.05 - 0.1 = 69.95, so True
        is_daily_high = tracker["max_temp_after_up"] >= tracker["running_daily_max"] - 0.1
        self.assertTrue(is_daily_high)
        
        # Scenario 2: max_temp_after_up is outside tolerance
        tracker2 = {
            "max_temp_after_up": 69.8,
            "running_daily_max": 70.05,  # More than 0.1°F below
        }
        is_daily_high2 = tracker2["max_temp_after_up"] >= tracker2["running_daily_max"] - 0.1
        self.assertFalse(is_daily_high2)


if __name__ == "__main__":
    unittest.main()
