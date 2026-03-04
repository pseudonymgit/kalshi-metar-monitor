import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import metar_monitor


class HydrationQueueIntegrationPathTests(unittest.TestCase):
    def setUp(self):
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", side_effect=AssertionError("poll path must not hydrate inline"))
    @patch("core.kalshi_monitor.process_hydration_queue_worker", return_value={"status": "idle"})
    @patch("core.metar_monitor._save_cache")
    @patch("core.metar_monitor._ingest_obs", return_value=(0, 0))
    @patch("core.metar_monitor._fetch_range_strict", return_value=[])
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale", "reason": "cache_stale"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor.ensure_state_loaded")
    def test_poll_path_never_hydrates_inline(self, *_mocks):
        metar_monitor._poll_once()

    @patch("core.metar_monitor.requests.post")
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"status": "cache_missing", "cache_valid": False}})
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value={"ok": False})
    @patch("core.kalshi_monitor.process_ladder_transition", return_value={"should_alert": False, "reason": "NO_TRANSITION", "terminal_state_blocked": False})
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={"markets": [], "raw_market_count": 0, "filtered_market_count": 0, "rejection_counts": {}, "cache_written": False})
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", side_effect=AssertionError("alert path must not hydrate inline"))
    def test_alert_path_enqueues_hydration_without_inline_execution(self, _hydrate, _active, _types, _snapshot, _transition, _send, _prereq, mock_enqueue, _post):
        payload = {
            "station": "KDEN",
            "legacy": {
                "temp_f": 70.0,
                "prev_temp_f": 69.0,
                "delta_f": 1.0,
                "instant_bucket_changed": True,
                "settlement_bucket_changed": False,
                "transition_correlation": "abc123",
                "obs_time": datetime.now(timezone.utc).isoformat(),
            },
            "summary": {"temp_f": 70.0},
        }

        metar_monitor._send_alert("https://example.invalid/webhook", payload)

        self.assertTrue(mock_enqueue.called)


if __name__ == "__main__":
    unittest.main()
