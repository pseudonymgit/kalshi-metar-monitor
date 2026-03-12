import unittest
from unittest.mock import patch
from types import SimpleNamespace

from core import metar_monitor


class SignalLayerAlertTests(unittest.TestCase):
    def setUp(self):
        metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
        metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
        metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
        metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()
        metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
        metar_monitor._LAST_SETTLEMENT_UP_TS.clear()
        metar_monitor._MISSING_LADDER_DEDUPE.clear()
        metar_monitor._KALSHI_LAST_CALL_TS.clear()

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
            return {
                "station": kwargs.get("station"),
                "timestamp_utc": kwargs.get("metadata", {}).get("obs_time"),
                "transition_event_id": 1,
            }

        def fake_emit_signal_alert(*, station, obs_time, temp_f, signal_context, cfg):
            del cfg
            emitted.append(
                {
                    "station": station,
                    "obs_time": obs_time,
                    "temp_f": temp_f,
                    "signal_type": signal_context.get("signal_type"),
                }
            )

        with patch("core.metar_monitor._maybe_daily_reset_local", return_value=None), \
            patch("core.metar_monitor.read_temperature_state", side_effect=fake_read_temperature_state), \
            patch("core.metar_monitor.commit_temperature_state", side_effect=fake_commit_temperature_state), \
            patch("core.metar_monitor.emit_transition_if_changed", side_effect=fake_emit_transition_if_changed), \
            patch("core.metar_monitor.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}}), \
            patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}}), \
            patch("core.metar_monitor._emit_signal_alert", side_effect=fake_emit_signal_alert):
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

    def test_near_boundary_momentum_up_emits_once(self):
        emitted = self._run_sequence(
            [
                (69.92, "2025-01-01T12:00:00+00:00"),
                (69.95, "2025-01-01T12:00:10+00:00"),
                (69.99, "2025-01-01T12:00:20+00:00"),
                (69.98, "2025-01-01T12:00:40+00:00"),
            ]
        )

        self.assertEqual([row["signal_type"] for row in emitted], ["near_boundary_momentum_up"])
        runtime = metar_monitor.get_latest_station_signal_runtime("KDEN").get("KDEN", {})
        self.assertEqual(runtime.get("suppression_reason"), "STATION_COOLDOWN_ACTIVE")

    def test_goldilocks_reversion_alert_emits_in_same_epoch(self):
        metar_monitor._SIGNAL_EPOCH_COUNTER["KDEN"] = 1
        metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER[("KDEN", 1)] = {
            "settlement_bucket_at_up": 70,
            "max_temp_after_up": 71.3,
            "exceeded_by_one_or_more": True,
            "reverted_below_settlement": False,
            "alert_emitted": False,
            "settlement_up_obs_time": "2025-01-01T12:00:00+00:00",
        }
        emitted = self._run_sequence(
            [
                (69.7, "2025-01-01T12:02:00+00:00"),
            ]
        )

        self.assertIn("goldilocks_reversion_alert", [row["signal_type"] for row in emitted])

    def test_signal_sequence_is_replay_deterministic(self):
        observations = [
            (69.92, "2025-01-01T12:00:00+00:00"),
            (69.95, "2025-01-01T12:00:10+00:00"),
            (69.99, "2025-01-01T12:00:20+00:00"),
        ]

        first = self._run_sequence(observations)
        self.setUp()
        second = self._run_sequence(observations)

        self.assertEqual(first, second)

    def test_snapshot_restore_preserves_last_settlement_up_timestamp(self):
        metar_monitor._LAST_SETTLEMENT_UP_TS["KDEN"] = "2025-01-01T12:00:00+00:00"

        snapshot = metar_monitor._snapshot_station_state("KDEN")
        metar_monitor._LAST_SETTLEMENT_UP_TS.pop("KDEN", None)

        metar_monitor._restore_station_state("KDEN", snapshot)

        self.assertEqual(
            metar_monitor._LAST_SETTLEMENT_UP_TS.get("KDEN"),
            "2025-01-01T12:00:00+00:00",
        )

    def test_daily_reset_clears_last_settlement_up_timestamp(self):
        metar_monitor._LAST_SETTLEMENT_UP_TS["KDEN"] = "2025-01-01T12:00:00+00:00"

        metar_monitor.reset_station_daily_state("KDEN", "2025-01-02")

        self.assertNotIn("KDEN", metar_monitor._LAST_SETTLEMENT_UP_TS)

    @patch.dict("os.environ", {"ALERT_ON_MISSING_LADDER": "true", "KALSHI_TARGET_MARKET_TYPE": "HIGH"}, clear=False)
    @patch("core.metar_monitor.requests.post", return_value=SimpleNamespace(status_code=200))
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value=None)
    @patch("core.kalshi_monitor.process_ladder_transition", return_value=(False, "NO_TRANSITION", None))
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True, "status": "cache_valid"}})
    @patch("core.kalshi_monitor.enqueue_station_hydration", return_value=None)
    @patch("core.kalshi_monitor.classify_proximity", return_value="NEAR")
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={"markets": [], "raw_market_count": 0, "filtered_market_count": 0, "rejection_counts": {}})
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    def test_send_alert_missing_ladder_dedupe_uses_observation_time(self, *_mocks):
        captured_local_conversion = {"ts": None}

        def fake_to_local(station, dt_utc):
            del station
            captured_local_conversion["ts"] = dt_utc.isoformat()
            return dt_utc

        payload = {
            "station": "KDEN",
            "summary": {"temp_f": 69.5},
            "legacy": {
                "temp_f": 69.5,
                "prev_temp_f": 69.4,
                "delta_f": 0.1,
                "obs_time": "2025-01-01T12:00:00+00:00",
                "instant_bucket_changed": True,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2025-01-01T12:00:00+00:00", "transition_event_id": 1},
            },
        }

        with patch("core.metar_monitor._to_local", side_effect=fake_to_local):
            metar_monitor._send_alert("https://example.com/webhook", payload)

        self.assertIn("KDEN_HIGH_2025-01-01", metar_monitor._MISSING_LADDER_DEDUPE)
        self.assertEqual(captured_local_conversion["ts"], "2025-01-01T12:00:00+00:00")

    @patch.dict("os.environ", {"ALERT_ON_MISSING_LADDER": "true", "KALSHI_TARGET_MARKET_TYPE": "HIGH", "HYDRATION_MISSING_LADDER_WARMUP_SECONDS": "900"}, clear=False)
    @patch("core.metar_monitor.is_scheduler_running", return_value=True)
    @patch("core.metar_monitor.requests.post", return_value=SimpleNamespace(status_code=200))
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value=None)
    @patch("core.kalshi_monitor.process_ladder_transition", return_value=(False, "NO_TRANSITION", None))
    @patch("core.kalshi_monitor.hydration_queue_snapshot", return_value={"queued_stations": ["KDEN"], "backoff_until": {}})
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"attempted": True, "series_discovered": True, "markets_cached": False, "cache_valid": False, "status": "cache_missing", "evaluated_at_utc": "2025-01-01T11:59:50+00:00"}})
    @patch("core.kalshi_monitor.enqueue_station_hydration", return_value=None)
    @patch("core.kalshi_monitor.classify_proximity", return_value="NEAR")
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={"markets": [], "raw_market_count": 0, "filtered_market_count": 0, "rejection_counts": {}})
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    @patch("core.metar_monitor._annotate_transition_history_market_eval", return_value=None)
    def test_send_alert_missing_ladder_suppressed_during_hydration_warmup(self, mock_annotate, _active, _types, _snapshot, _proximity, _enqueue, _hydration_state, _queue_snapshot, _transition, _send_composed, mock_post, _scheduler):
        payload = {
            "station": "KDEN",
            "summary": {"temp_f": 69.5},
            "legacy": {
                "temp_f": 69.5,
                "prev_temp_f": 69.4,
                "delta_f": 0.1,
                "obs_time": "2025-01-01T12:00:00+00:00",
                "instant_bucket_changed": True,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2025-01-01T12:00:00+00:00", "transition_event_id": 1},
            },
        }

        metar_monitor._send_alert("https://example.com/webhook", payload)

        mock_post.assert_not_called()
        self.assertEqual(mock_annotate.call_args.kwargs.get("suppression_reason"), "ladder_hydration_warmup")

    @patch.dict("os.environ", {"ALERT_ON_MISSING_LADDER": "true", "KALSHI_TARGET_MARKET_TYPE": "HIGH", "HYDRATION_MISSING_LADDER_WARMUP_SECONDS": "900"}, clear=False)
    @patch("core.metar_monitor.is_scheduler_running", return_value=True)
    @patch("core.metar_monitor.requests.post", return_value=SimpleNamespace(status_code=200))
    @patch("core.kalshi_monitor.send_composed_weather_market_alert", return_value=None)
    @patch("core.kalshi_monitor.process_ladder_transition", return_value=(False, "NO_TRANSITION", None))
    @patch("core.kalshi_monitor.hydration_queue_snapshot", return_value={"queued_stations": [], "backoff_until": {}})
    @patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"attempted": True, "series_discovered": True, "markets_cached": False, "cache_valid": False, "status": "cache_missing", "evaluated_at_utc": "2025-01-01T11:40:00+00:00"}})
    @patch("core.kalshi_monitor.enqueue_station_hydration", return_value=None)
    @patch("core.kalshi_monitor.classify_proximity", return_value="NEAR")
    @patch("core.kalshi_monitor.build_structured_snapshot_from_cache", return_value={"markets": [], "raw_market_count": 0, "filtered_market_count": 0, "rejection_counts": {}})
    @patch("core.kalshi_monitor._parse_target_market_types", return_value={"HIGH"})
    @patch("core.kalshi_monitor._get_active_stations", return_value={"KDEN"})
    def test_send_alert_missing_ladder_resumes_after_warmup(self, _active, _types, _snapshot, _proximity, _enqueue, _hydration_state, _queue_snapshot, _transition, _send_composed, mock_post, _scheduler):
        payload = {
            "station": "KDEN",
            "summary": {"temp_f": 69.5},
            "legacy": {
                "temp_f": 69.5,
                "prev_temp_f": 69.4,
                "delta_f": 0.1,
                "obs_time": "2025-01-01T12:00:00+00:00",
                "instant_bucket_changed": True,
                "settlement_bucket_changed": False,
                "transition_correlation": {"timestamp_utc": "2025-01-01T12:00:00+00:00", "transition_event_id": 1},
            },
        }

        metar_monitor._send_alert("https://example.com/webhook", payload)

        mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
