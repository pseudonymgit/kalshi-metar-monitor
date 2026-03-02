import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import metar_monitor


class ComputeWindowPublicationLagTests(unittest.TestCase):
    def setUp(self):
        with metar_monitor._STATE_LOCK:
            metar_monitor._STATE["last_seen_iso"].pop("KDEN", None)

    @patch("core.metar_monitor.datetime")
    def test_first_run_window_end_is_buffered(self, mock_datetime):
        now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        mock_datetime.utcnow.return_value = now.replace(tzinfo=None)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        _start_iso, _end_iso, start_dt, end_dt = metar_monitor._compute_window("KDEN", minutes=3, cfg={"lookback_min": 3})

        self.assertEqual(end_dt, datetime(2026, 3, 2, 11, 58, 30, tzinfo=timezone.utc))
        self.assertEqual(start_dt, datetime(2026, 3, 2, 11, 50, 30, tzinfo=timezone.utc))

    @patch("core.metar_monitor.datetime")
    def test_last_seen_overlap_remains_anchored_to_last_seen(self, mock_datetime):
        now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        mock_datetime.utcnow.return_value = now.replace(tzinfo=None)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        with metar_monitor._STATE_LOCK:
            metar_monitor._STATE["last_seen_iso"]["KDEN"] = "2026-03-02T11:57:00+00:00"

        _start_iso, _end_iso, start_dt, end_dt = metar_monitor._compute_window("KDEN", minutes=3, cfg={"lookback_min": 3})

        self.assertEqual(start_dt, datetime(2026, 3, 2, 11, 55, 0, tzinfo=timezone.utc))
        self.assertEqual(end_dt, datetime(2026, 3, 2, 11, 58, 30, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
