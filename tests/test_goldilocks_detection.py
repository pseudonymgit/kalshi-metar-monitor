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


if __name__ == "__main__":
    unittest.main()
