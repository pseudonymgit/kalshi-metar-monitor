import unittest

from core.authoritative_state import commit_temperature_state, reset_station_daily_state, set_latest_observation
from core.replay_engine import execute_ordered_replay_stream
from core.security_boundaries import SecurityBoundaryViolation
from core.transition_emitter import emit_transition_if_changed


class SecurityBoundaryTests(unittest.TestCase):
    def test_authoritative_state_mutation_rejects_unauthorized_caller(self):
        with self.assertRaises(SecurityBoundaryViolation):
            set_latest_observation("KDEN", {"temp_f": 70.0}, "2025-01-01T00:00:00Z")

        with self.assertRaises(SecurityBoundaryViolation):
            commit_temperature_state("KDEN", 70, 70.1, 70, 70)

        with self.assertRaises(SecurityBoundaryViolation):
            reset_station_daily_state("KDEN", "2025-01-01")

    def test_transition_emitter_rejects_unauthorized_caller(self):
        with self.assertRaises(SecurityBoundaryViolation):
            emit_transition_if_changed(
                transition_type="instant_up",
                instant_changed=True,
                settlement_changed=False,
                station="KDEN",
                instant_bucket_before=69,
                instant_bucket_after=70,
                settlement_bucket=70,
                running_max=70.5,
                current_temp=70.5,
                metadata={},
                emit_fn=lambda **_: None,
            )

    def test_replay_engine_forces_isolated_flags(self):
        captured = {}

        def ingest_fn(station, ordered_observations, cfg, allow_alert_delivery, persist_cache):
            captured["station"] = station
            captured["allow_alert_delivery"] = allow_alert_delivery
            captured["persist_cache"] = persist_cache
            return (len(ordered_observations), 0)

        result = execute_ordered_replay_stream(
            station="KDEN",
            ordered_observations=[{"obs_time": "2025-01-01T00:00:00Z", "temp_f": 70.0}],
            cfg={},
            ingest_fn=ingest_fn,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured["station"], "KDEN")
        self.assertFalse(captured["allow_alert_delivery"])
        self.assertFalse(captured["persist_cache"])


if __name__ == "__main__":
    unittest.main()
