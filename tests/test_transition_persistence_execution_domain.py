import unittest
from unittest.mock import patch

from core import metar_monitor


class TransitionPersistenceExecutionDomainTests(unittest.TestCase):
    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="replay")
    @patch("core.metar_monitor.sqlite3.connect")
    def test_log_transition_event_skips_persistence_outside_production(self, mock_connect, _mock_domain):
        result = metar_monitor._log_transition_event(
            station="KDEN",
            transition_type="instant_up",
            instant_bucket_before=69,
            instant_bucket_after=70,
            settlement_bucket=70,
            running_max=70.2,
            current_temp=70.2,
            metadata={"source": "replay"},
        )

        self.assertIsNone(result)
        mock_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
