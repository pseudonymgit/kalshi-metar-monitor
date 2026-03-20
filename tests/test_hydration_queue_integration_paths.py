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

class ConfiguredHydrationMarketTypeTests(unittest.TestCase):
    @patch.dict("os.environ", {"KALSHI_TARGET_MARKET_TYPE": "HIGH,LOW"}, clear=False)
    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="production")
    @patch("core.kalshi_monitor.hydrate_station_ladder_snapshot", return_value={"status": "hydrated"})
    def test_hydration_queue_worker_uses_configured_market_types_by_default(self, mock_hydrate, _domain):
        from core import kalshi_monitor

        kalshi_monitor._hydration_queue[:] = ["KDEN"]
        kalshi_monitor._hydration_backoff_until.clear()
        kalshi_monitor._last_hydration_request_ts = 0.0

        try:
            result = kalshi_monitor.process_hydration_queue_worker()
        finally:
            kalshi_monitor._hydration_queue.clear()
            kalshi_monitor._hydration_backoff_until.clear()
            kalshi_monitor._last_hydration_request_ts = 0.0

        self.assertEqual(result["status"], "hydrated")
        mock_hydrate.assert_called_once_with(station="KDEN", market_types={"HIGH", "LOW"})

    @patch.dict("os.environ", {"KALSHI_TARGET_MARKET_TYPE": "HIGH"}, clear=False)
    @patch(
        "core.kalshi_monitor.get_cached_series_markets",
        side_effect=lambda ticker: {"markets": [], "station_local_day": "2026-01-01", "hydrated_at_utc": "2026-01-01T12:00:00+00:00"} if ticker == "KXHIGHDEN" else None,
    )
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": ["KXHIGHDEN", "KXLOWDEN"]})
    @patch("core.kalshi_monitor.station_local_day_key", return_value="2026-01-01")
    @patch("core.kalshi_monitor.datetime")
    def test_hydration_prerequisite_ignores_low_cache_when_low_disabled(self, datetime_mock, _day_key, _discovery, _cache):
        from core import kalshi_monitor

        datetime_mock.now.return_value = kalshi_monitor.parse_iso_utc("2026-01-01T12:00:00+00:00")
        result = kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")

        self.assertEqual(result["status"], "cache_valid")
        self.assertEqual(result["series_ticker"], "KXHIGHDEN")


class TransitionEvaluationWindowTests(unittest.TestCase):
    def setUp(self):
        metar_monitor._KALSHI_LAST_CALL_TS.clear()
        metar_monitor._TRANSITION_HISTORY.clear()

    def test_parse_iso_returns_none_for_invalid_timestamp(self):
        self.assertIsNone(metar_monitor._parse_iso("not-a-timestamp"))

    @patch("core.metar_monitor.datetime", wraps=metar_monitor.datetime)
    def test_parse_iso_does_not_fallback_to_wall_clock(self, mock_datetime):
        mock_datetime.utcnow.side_effect = AssertionError("utcnow must not be called")

        self.assertIsNone(metar_monitor._parse_iso("not-a-timestamp"))
        mock_datetime.utcnow.assert_not_called()

    def test_parse_iso_utc_optional_is_deterministic(self):
        self.assertEqual(
            metar_monitor._parse_iso_utc_optional("2026-01-01T00:00:00Z").isoformat(),
            "2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(metar_monitor._parse_iso_utc_optional("not-a-timestamp"))
        self.assertIsNone(metar_monitor._parse_iso_utc_optional(None))

    @patch("core.metar_monitor._annotate_transition_history_market_eval")
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"status": "cache_valid", "cache_valid": True}})
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value={"ok": True, "event_ticker": "KXHIGHDEN-26DEC31", "bucket_index": 0})
    @patch("core.kalshi_monitor.process_ladder_transition", return_value={"should_alert": False, "reason": "NO_TRANSITION", "terminal_state_blocked": False, "bucket_index": 0, "direction": "UP"})
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={
        "markets": [{"event_ticker": "KXHIGHDEN-26DEC31", "series_ticker": "KXHIGHDEN", "strike": 70}],
        "raw_market_count": 1,
        "filtered_market_count": 1,
        "rejection_counts": {},
        "cache_written": True,
    })
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    def test_alert_evaluation_proceeds_for_prior_cycle_transition_within_window(
        self,
        _active,
        _types,
        _snapshot,
        _transition,
        mock_send,
        _prereq,
        mock_annotate,
    ):
        payload = {
            "station": "KDEN",
            "timestamp_utc": "2026-01-01T00:00:30+00:00",
            "legacy": {
                "temp_f": 70.0,
                "prev_temp_f": 69.0,
                "delta_f": 1.0,
                "instant_bucket_changed": False,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2026-01-01T00:00:00+00:00", "transition_event_id": 7},
                "obs_time": "2026-01-01T00:00:30+00:00",
            },
            "summary": {"temp_f": 70.0},
        }

        with patch.dict("os.environ", {"ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS": "300"}, clear=False):
            metar_monitor._send_alert("https://example.invalid/webhook", payload)

        self.assertTrue(mock_send.called)
        self.assertEqual(mock_annotate.call_args.kwargs["evaluation_outcome"], "ALERT_SENT")

    @patch("core.metar_monitor._annotate_transition_history_market_eval")
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"status": "cache_valid", "cache_valid": True}})
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value={"ok": True})
    @patch("core.kalshi_monitor.process_ladder_transition", return_value={"should_alert": False, "reason": "NO_TRANSITION", "terminal_state_blocked": False, "bucket_index": 0, "direction": "UP"})
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={
        "markets": [{"event_ticker": "KXHIGHDEN-26DEC31", "series_ticker": "KXHIGHDEN", "strike": 70}],
        "raw_market_count": 1,
        "filtered_market_count": 1,
        "rejection_counts": {},
        "cache_written": True,
    })
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    def test_alert_suppressed_when_prior_transition_outside_window(
        self,
        _active,
        _types,
        _snapshot,
        _transition,
        mock_send,
        _prereq,
        mock_annotate,
    ):
        payload = {
            "station": "KDEN",
            "timestamp_utc": "2026-01-01T00:06:00+00:00",
            "legacy": {
                "temp_f": 70.0,
                "prev_temp_f": 69.0,
                "delta_f": 1.0,
                "instant_bucket_changed": False,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2026-01-01T00:00:00+00:00", "transition_event_id": 8},
                "obs_time": "2026-01-01T00:06:00+00:00",
            },
            "summary": {"temp_f": 70.0},
        }

        with patch.dict("os.environ", {"ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS": "300"}, clear=False):
            metar_monitor._send_alert("https://example.invalid/webhook", payload)

        self.assertFalse(mock_send.called)
        self.assertEqual(mock_annotate.call_args.kwargs["evaluation_outcome"], "SUPPRESSED_NO_TRANSITION")
        self.assertEqual(mock_annotate.call_args.kwargs["suppression_reason"], "NO_TRANSITION")

    @patch("core.metar_monitor._annotate_transition_history_market_eval")
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"status": "cache_valid", "cache_valid": True}})
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value={"ok": True, "event_ticker": "KXHIGHDEN-26DEC31", "bucket_index": 0})
    @patch("core.kalshi_monitor.process_ladder_transition", return_value={"should_alert": False, "reason": "NO_TRANSITION", "terminal_state_blocked": False, "bucket_index": 0, "direction": "UP"})
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={
        "markets": [{"event_ticker": "KXHIGHDEN-26DEC31", "series_ticker": "KXHIGHDEN", "strike": 70}],
        "raw_market_count": 1,
        "filtered_market_count": 1,
        "rejection_counts": {},
        "cache_written": True,
    })
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    def test_window_comparison_deterministic_for_identical_inputs(
        self,
        _active,
        _types,
        _snapshot,
        _transition,
        mock_send,
        _prereq,
        mock_annotate,
    ):
        payload = {
            "station": "KDEN",
            "timestamp_utc": "2026-01-01T00:00:30+00:00",
            "legacy": {
                "temp_f": 70.0,
                "prev_temp_f": 69.0,
                "delta_f": 1.0,
                "instant_bucket_changed": False,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2026-01-01T00:00:00+00:00", "transition_event_id": 9},
                "obs_time": "2026-01-01T00:00:30+00:00",
            },
            "summary": {"temp_f": 70.0},
        }

        with patch.dict("os.environ", {"ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS": "300"}, clear=False):
            metar_monitor._send_alert("https://example.invalid/webhook", payload)
            first_outcome = mock_annotate.call_args.kwargs["evaluation_outcome"]
            first_send_count = mock_send.call_count

            metar_monitor._KALSHI_LAST_CALL_TS.clear()
            metar_monitor._send_alert("https://example.invalid/webhook", payload)
            second_outcome = mock_annotate.call_args.kwargs["evaluation_outcome"]
            second_send_count = mock_send.call_count - first_send_count

        self.assertEqual(first_outcome, "ALERT_SENT")
        self.assertEqual(second_outcome, "ALERT_SENT")
        self.assertEqual(first_send_count, 1)
        self.assertEqual(second_send_count, 1)


if __name__ == "__main__":
    unittest.main()
