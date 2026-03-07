import unittest

import app as app_module


class HydrationStallSignalTests(unittest.TestCase):
    def test_signal_is_derived_deterministically_from_inputs(self):
        first = app_module._build_hydration_stall_signal(
            station="KDEN",
            hydration_reason="hydration_cache_not_written",
            transitions_seen_today=1,
            alerts_sent_today=0,
        )
        second = app_module._build_hydration_stall_signal(
            station="KDEN",
            hydration_reason="hydration_cache_not_written",
            transitions_seen_today=1,
            alerts_sent_today=0,
        )

        self.assertEqual(first, second)
        self.assertTrue(first["hydration_stall_condition"])

    def test_signal_requires_exact_condition_tuple(self):
        self.assertFalse(
            app_module._build_hydration_stall_signal(
                station="KDEN",
                hydration_reason="hydration_ready",
                transitions_seen_today=1,
                alerts_sent_today=0,
            )["hydration_stall_condition"]
        )
        self.assertFalse(
            app_module._build_hydration_stall_signal(
                station="KDEN",
                hydration_reason="hydration_cache_not_written",
                transitions_seen_today=0,
                alerts_sent_today=0,
            )["hydration_stall_condition"]
        )
        self.assertFalse(
            app_module._build_hydration_stall_signal(
                station="KDEN",
                hydration_reason="hydration_cache_not_written",
                transitions_seen_today=1,
                alerts_sent_today=1,
            )["hydration_stall_condition"]
        )


if __name__ == "__main__":
    unittest.main()
