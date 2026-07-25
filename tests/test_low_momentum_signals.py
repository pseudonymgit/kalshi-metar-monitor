"""Test Layer 2: LOW Momentum Signals (L2-T1)."""

import unittest
from unittest.mock import patch

from core import metar_monitor


class LowMomentumSignalTests(unittest.TestCase):
    """Test LOW momentum signal emissions (near_boundary_momentum_down, microstructure_spike_momentum_down)."""

    def setUp(self):
        metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
        metar_monitor._SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER.clear()
        metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        metar_monitor._MISSING_LADDER_DEDUPE.clear()
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

    def _run_sequence(self, observations, initial_state=None):
        """Run a sequence of observations and return emitted signals."""
        state = {
            "last_observed_integer": 69,
            "running_daily_max": 69.9,
            "last_settlement_bucket": 69,
            "last_instant_bucket": 69,
        }
        emitted = []

        if initial_state:
            state.update(initial_state)

        def fake_read_temperature_state(_icao):
            return dict(state)

        def fake_commit_temperature_state(*, icao, curr_floor, running_daily_max, settlement_bucket, instant_bucket):
            del icao
            state["last_observed_integer"] = curr_floor
            state["running_daily_max"] = running_daily_max
            state["last_settlement_bucket"] = settlement_bucket
            state["last_instant_bucket"] = instant_bucket

        def fake_emit_transition_if_changed(**kwargs):
            return {
                "station": kwargs.get("station"),
                "timestamp_utc": kwargs.get("metadata", {}).get("obs_time"),
                "transition_event_id": 1,
            }

        def fake_emit_signal_alert(*, station, obs_time, temp_f, signal_context, cfg):
            del cfg
            # Capture full signal context including confidence scoring data
            emitted.append(
                {
                    "station": station,
                    "obs_time": obs_time,
                    "temp_f": temp_f,
                    "signal_type": signal_context.get("signal_type"),
                    "confidence": signal_context.get("confidence"),
                    "confidence_factors": signal_context.get("confidence_factors"),
                }
            )

        with patch("core.metar_monitor._maybe_daily_reset_local", return_value=None), \
            patch("core.metar_monitor.read_temperature_state", side_effect=fake_read_temperature_state), \
            patch("core.metar_monitor.commit_temperature_state", side_effect=fake_commit_temperature_state), \
            patch("core.metar_monitor.emit_transition_if_changed", side_effect=fake_emit_transition_if_changed), \
            patch("core.metar_monitor.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}}), \
            patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}}), \
            patch("core.metar_monitor._emit_signal_alert", side_effect=fake_emit_signal_alert), \
            patch("core.metar_monitor._persist_signal_state", return_value=None):  # Mock persistence to avoid file I/O
            for temp_f, obs_time in observations:
                result = metar_monitor._process_temperature_event(
                    icao="KDEN",
                    temp_f=temp_f,
                    obs_time=obs_time,
                    cfg={"webhook": ""},
                    last_temp_f=temp_f,
                    allow_alert_delivery=False,
                )
                runtime = metar_monitor.get_latest_station_signal_runtime("KDEN").get("KDEN", {})
                print(f"  Obs {temp_f}@{obs_time}: signal_type={runtime.get('signal_type')}, suppression={runtime.get('suppression_reason')}")

        return emitted

    def test_near_boundary_momentum_down_emits_for_downward_transition(self):
        """L2-T1a: near_boundary_momentum_down should emit for downward transitions approaching boundary."""
        # The signal only triggers when temperature is within 0.10 of the next lower integer boundary
        # AND there's a downward transition (instant_down or reversion_after_settlement)
        # AND there's sustained downward momentum in the observation window
        print("Testing with temperatures that approach 68 boundary...")
        emitted = self._run_sequence(
            [
                (69.10, "2025-01-01T12:00:00+00:00"),  # floor=69, first in window
                (69.07, "2025-01-01T12:00:10+00:00"),  # floor=69, second in window
                (68.05, "2025-01-01T12:00:20+00:00"),  # floor=68, instant_down triggers, distance=0.05 - within threshold!
            ]
        )
        
        signal_types = [row["signal_type"] for row in emitted]
        print(f"Emitted signals: {signal_types}")
        self.assertIn("near_boundary_momentum_down", signal_types)

    def test_microstructure_spike_momentum_down_emits_for_reversion_after_settlement(self):
        """L2-T1b: microstructure_spike_reversion should emit after settlement up followed by downward reversion."""
        # Need to set up initial state to have a previous settlement bucket
        initial_state = {
            "last_settlement_bucket": 70,
            "last_observed_integer": 70,
            "running_daily_max": 70.5,
        }
        emitted = self._run_sequence(
            [
                (70.0, "2025-01-01T12:00:00+00:00"),  # floor=70, no transition (same as previous)
                (71.3, "2025-01-01T12:00:10+00:00"),  # floor=71, settlement_up
                (69.5, "2025-01-01T12:00:20+00:00"),  # floor=69, reversion_after_settlement
            ],
            initial_state=initial_state,
        )
        
        signal_types = [row["signal_type"] for row in emitted]
        print(f"First sequence signals: {signal_types}")
        # microstructure_spike_reversion should emit after reversion below threshold
        self.assertIn("microstructure_spike_reversion", signal_types)

    def test_low_momentum_signal_cooldown_per_signal_type(self):
        """L2-T4: Cooldown should reset per signal type, not globally."""
        # First emit near_boundary_momentum_down by setting up proper conditions
        emitted1 = self._run_sequence(
            [
                (69.10, "2025-01-01T12:00:00+00:00"),
                (69.07, "2025-01-01T12:00:10+00:00"),
                (68.05, "2025-01-01T12:00:20+00:00"),  # Within 0.10 of 68
            ]
        )
        
        signal_types1 = [row["signal_type"] for row in emitted1]
        print(f"First sequence signals: {signal_types1}")
        self.assertIn("near_boundary_momentum_down", signal_types1)
        
        # Run second sequence after cooldown window - should still be suppressed
        emitted2 = self._run_sequence(
            [
                (68.85, "2025-01-01T12:02:00+00:00"),  # 2 minutes later, within cooldown
            ]
        )
        
        runtime = metar_monitor.get_latest_station_signal_runtime("KDEN").get("KDEN", {})
        # Cooldown should still be active
        self.assertIn("cooldown", runtime.get("suppression_reason", "").lower())

    def test_low_momentum_signal_replay_determinism(self):
        """Test that LOW momentum signals are replay-deterministic."""
        observations = [
            (69.10, "2025-01-01T12:00:00+00:00"),
            (69.07, "2025-01-01T12:00:10+00:00"),
            (68.05, "2025-01-01T12:00:20+00:00"),
        ]

        first = self._run_sequence(observations)
        self.setUp()
        second = self._run_sequence(observations)

        self.assertEqual(first, second)

    def test_near_boundary_momentum_down_distance_threshold(self):
        """Test that near_boundary_momentum_down requires distance from boundary <= 0.10."""
        # 68.85 is 0.15 away from 69, so outside the 0.10 threshold
        emitted = self._run_sequence(
            [
                (69.10, "2025-01-01T12:00:00+00:00"),
                (69.07, "2025-01-01T12:00:10+00:00"),
                (68.85, "2025-01-01T12:00:20+00:00"),  # 0.15 from 68 - outside threshold
            ]
        )
        
        signal_types = [row["signal_type"] for row in emitted]
        # Should NOT emit near_boundary_momentum_down since 68.85 is 0.15 from 68
        self.assertNotIn("near_boundary_momentum_down", signal_types)

    def test_microstructure_spike_momentum_down_tracker_state(self):
        """Test microstructure_spike_momentum_down tracker state and confidence scoring."""
        # Set up initial state with previous settlement bucket
        initial_state = {
            "last_settlement_bucket": 69,
            "last_observed_integer": 69,
            "running_daily_max": 71.5,
        }
        emitted = self._run_sequence(
            [
                (70.2, "2025-01-01T12:00:00+00:00"),  # floor=70, settlement_up
                (70.5, "2025-01-01T12:00:10+00:00"),  # Still above, max_temp_after_up = 70.5
                (70.0, "2025-01-01T12:00:20+00:00"),  # Still above
                (69.1, "2025-01-01T12:00:30+00:00"),  # floor=69, reversion_after_settlement
                (68.5, "2025-01-01T12:00:40+00:00"),  # floor=68, microstructure_spike_momentum_down
            ],
            initial_state=initial_state,
        )
        
        signal_types = [row["signal_type"] for row in emitted]
        print(f"Tracker test signals: {signal_types}")
        # microstructure_spike_momentum_down should emit when reversion happens with momentum
        self.assertIn("microstructure_spike_momentum_down", signal_types)
        
        # Also verify that confidence scoring data is included in the signal context
        # by checking the last emitted signal
        last_signal = emitted[-1]
        self.assertEqual(last_signal["signal_type"], "microstructure_spike_momentum_down")
        self.assertIn("confidence", last_signal)
        self.assertIn("confidence_factors", last_signal)


if __name__ == "__main__":
    unittest.main()
