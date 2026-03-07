import unittest

import app as app_module


class HydrationStallSignalTests(unittest.TestCase):
    def test_signal_is_derived_deterministically_from_inputs(self):
        first = app_module._build_hydration_stall_signal(
            station="KDEN",
            hydration_execution_snapshot={"KDEN": {"cache_written": False}},
            transition_runtime_summary={"transitions_seen_today": 1},
            alert_fire_audit_rows={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]},
        )
        second = app_module._build_hydration_stall_signal(
            station="KDEN",
            hydration_execution_snapshot={"KDEN": {"cache_written": False}},
            transition_runtime_summary={"transitions_seen_today": 1},
            alert_fire_audit_rows={"stations": [{"station": "KDEN", "alerts_sent_today": 0}]},
        )

        self.assertEqual(first, second)
        self.assertTrue(first["hydration_stall_condition"])


if __name__ == "__main__":
    unittest.main()
