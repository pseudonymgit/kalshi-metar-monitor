import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import kalshi_monitor, metar_monitor


class StructuralHardeningTests(unittest.TestCase):
    @patch("core.kalshi_monitor._kalshi_public_get", side_effect=AssertionError("should not be called"))
    @patch("core.kalshi_monitor.build_structured_snapshot", side_effect=AssertionError("should not be called"))
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    def test_hydration_prerequisite_is_cache_only(self, *_mocks):
        with patch.dict("core.kalshi_monitor._SERIES_MARKETS_CACHE", {}, clear=True):
            result = kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")
        self.assertEqual(result["status"], "cache_missing")

    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._station_local_previous_day", return_value="2025-03-09")
    @patch("core.kalshi_monitor.station_local_day_key", return_value="2025-03-10")
    @patch(
        "core.kalshi_monitor.get_cached_series_markets",
        return_value={
            "markets": [],
            "hydrated_at_utc": "2025-01-01T10:00:00+00:00",
            "station_local_day": "2025-03-09",
        },
    )
    def test_hydration_prerequisite_accepts_station_local_yesterday_cache(self, *_mocks):
        result = kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")
        self.assertEqual(result["status"], "cache_valid")
        self.assertTrue(result["rollover_grace"])


    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor._station_local_previous_day", return_value="2025-03-09")
    @patch("core.kalshi_monitor.station_local_day_key", return_value="2025-03-10")
    @patch(
        "core.kalshi_monitor.get_cached_series_markets",
        return_value={
            "markets": [],
            "hydrated_at_utc": "2025-01-01T10:00:00+00:00",
            "station_local_day": "2025-03-10",
        },
    )
    def test_hydration_prerequisite_marks_markets_cached_false_when_markets_empty(self, *_mocks):
        kalshi_monitor.ensure_ladder_hydration_prerequisite("KDEN")
        snapshot = kalshi_monitor.get_hydration_prerequisite_state_snapshot()
        self.assertFalse(snapshot["KDEN"]["markets_cached"])

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3})
    @patch("core.metar_monitor._compute_window")
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_missing"})
    def test_fetch_window_skips_ingest_when_ladder_not_hydrated(
        self,
        _hydration,
        mock_ingest,
        mock_fetch,
        mock_compute,
        *_mocks,
    ):
        now = datetime.now(timezone.utc)
        mock_compute.return_value = ("s", "e", now, now)

        result = metar_monitor.fetch_window("KDEN", 3, source="nws")

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["poll_skipped_reason"], "ladder_not_hydrated")
        mock_fetch.assert_not_called()
        mock_ingest.assert_not_called()

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale"})
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.process_hydration_queue_worker")
    @patch("core.metar_monitor._save_cache")
    def test_poll_once_enqueues_station_when_ladder_not_hydrated(self, _save, _worker, mock_ingest, mock_fetch, mock_enqueue, *_mocks):
        metar_monitor._poll_once()

        mock_enqueue.assert_called_once_with("KDEN", reason="cache_stale")
        mock_fetch.assert_called_once()
        mock_ingest.assert_called_once()

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale"})
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.process_hydration_queue_worker")
    @patch("core.metar_monitor._save_cache")
    def test_poll_once_records_ingestion_admission_for_non_hydrated_station(self, _save, _worker, _ingest, _fetch, _enqueue, *_mocks):
        metar_monitor._poll_once()

        state = metar_monitor.get_state()
        admission = state["ingestion_admission"]["KDEN"]
        self.assertFalse(admission["hydration_passed"])
        self.assertTrue(admission["admitted_to_fetch"])
        self.assertIsNone(admission["skip_reason"])
        self.assertIsNotNone(admission["evaluated_at_utc"])

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={"default_source": "nws", "lookback_min": 3, "stations": ["KDEN"], "cache_file": "/tmp/cache.json"})
    @patch("core.metar_monitor._resolve_live_polling_stations", return_value=["KDEN"])
    @patch("core.kalshi_monitor.ensure_ladder_hydration_prerequisite", return_value={"status": "cache_stale"})
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("core.metar_monitor._fetch_range_strict")
    @patch("core.metar_monitor._ingest_obs")
    @patch("core.kalshi_monitor.process_hydration_queue_worker")
    @patch("core.metar_monitor._save_cache")
    def test_poll_once_enqueues_hydrator_when_prerequisite_missing(self, _save, mock_worker, _ingest, _fetch, mock_enqueue, *_mocks):
        metar_monitor._poll_once()

        mock_enqueue.assert_called_once_with("KDEN", reason="cache_stale")
        mock_worker.assert_called_once_with(market_types={"HIGH"})

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={})
    @patch("core.metar_monitor._ingest_obs", return_value=(1, 0))
    def test_simulation_uses_ingest_path(self, mock_ingest, *_mocks):
        metar_monitor._simulate_temperature_for_testing("KDEN", 71.1)

        mock_ingest.assert_called_once()

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={})
    def test_simulation_no_alert_generated_reports_null_delivery_fields(self, *_mocks):
        with patch("core.metar_monitor._ingest_obs", return_value=(1, 0)):
            result = metar_monitor._simulate_temperature_for_testing("KDEN", 71.1, allow_alert_delivery=True)

        self.assertEqual(result["alerts_generated"], 0)
        self.assertFalse(result["delivery_attempted"])
        self.assertFalse(result["delivery_succeeded"])
        self.assertIsNone(result["webhook_status_code"])
        self.assertIsNone(result["webhook_exception"])
        self.assertIsNone(result["webhook_response_text"])

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={})
    def test_simulation_alert_generated_uses_own_success_delivery_result(self, *_mocks):
        def _ingest_side_effect(*args, **kwargs):
            kwargs["delivery_results"].append(
                {
                    "delivery_succeeded": True,
                    "webhook_status_code": 204,
                    "webhook_exception": None,
                    "webhook_response_text": "ok",
                }
            )
            return (1, 1)

        with patch("core.metar_monitor._ingest_obs", side_effect=_ingest_side_effect):
            result = metar_monitor._simulate_temperature_for_testing("KDEN", 71.1, allow_alert_delivery=True)

        self.assertTrue(result["delivery_attempted"])
        self.assertTrue(result["delivery_succeeded"])
        self.assertEqual(result["webhook_status_code"], 204)
        self.assertIsNone(result["webhook_exception"])
        self.assertEqual(result["webhook_response_text"], "ok")

    @patch("core.metar_monitor.ensure_state_loaded")
    @patch("core.metar_monitor.get_default_config", return_value={})
    def test_simulation_alert_generated_uses_own_failed_delivery_result(self, *_mocks):
        captured_delivery_lists = []

        def _ingest_side_effect(*args, **kwargs):
            delivery_results = kwargs["delivery_results"]
            captured_delivery_lists.append(delivery_results)
            delivery_results.append(
                {
                    "delivery_succeeded": False,
                    "webhook_status_code": 500,
                    "webhook_exception": "boom",
                    "webhook_response_text": "failed",
                }
            )
            return (1, 1)

        with patch("core.metar_monitor._ingest_obs", side_effect=_ingest_side_effect):
            first = metar_monitor._simulate_temperature_for_testing("KDEN", 71.1, allow_alert_delivery=True)
            second = metar_monitor._simulate_temperature_for_testing("KDEN", 72.1, allow_alert_delivery=True)

        self.assertEqual(len(captured_delivery_lists), 2)
        self.assertIsNot(captured_delivery_lists[0], captured_delivery_lists[1])
        self.assertTrue(first["delivery_attempted"])
        self.assertFalse(first["delivery_succeeded"])
        self.assertEqual(first["webhook_status_code"], 500)
        self.assertEqual(first["webhook_exception"], "boom")
        self.assertEqual(first["webhook_response_text"], "failed")
        self.assertTrue(second["delivery_attempted"])
        self.assertFalse(second["delivery_succeeded"])
        self.assertEqual(second["webhook_status_code"], 500)

    def test_observability_domain_blocks_live_kalshi_calls(self):
        with kalshi_monitor.kalshi_execution_domain("observability"):
            with self.assertRaises(RuntimeError):
                kalshi_monitor._kalshi_public_get("/markets?limit=1")

    @patch("core.kalshi_monitor.build_structured_snapshot", return_value={"markets": []})
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch("core.kalshi_monitor.get_cached_series_markets", return_value={"markets": [], "hydrated_at_utc": "2025-01-01T00:00:00+00:00", "station_local_day": "2025-01-01"})
    def test_explicit_hydrator_requires_production_domain(self, *_mocks):
        with kalshi_monitor.kalshi_execution_domain("observability"):
            with self.assertRaises(RuntimeError):
                kalshi_monitor.hydrate_station_ladder_snapshot("KDEN", {"HIGH", "LOW"})

    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch(
        "core.kalshi_monitor._kalshi_public_get",
        side_effect=[
            {"events": [{"event_ticker": "KXHIGHDEN-26MAR12"}]},
            {"markets": [{"ticker": "KXHIGHDEN-26MAR12-T80"}]},
        ],
    )
    def test_cache_metadata_present(self, mock_get, *_mocks):
        kalshi_monitor.build_structured_snapshot("KDEN", {"HIGH"})

        self.assertEqual(mock_get.call_args_list[0].args[0], "/events?series_ticker=KXHIGHDEN")
        self.assertEqual(mock_get.call_args_list[1].args[0], "/markets?event_ticker=KXHIGHDEN-26MAR12&limit=100")

        cached = kalshi_monitor.get_cached_series_markets("KXHIGHDEN")
        self.assertIsNotNone(cached)
        self.assertIn("markets", cached)
        self.assertIn("hydrated_at_utc", cached)
        self.assertIn("station_local_day", cached)

    @patch("core.kalshi_monitor.station_local_day_key", return_value="2026-03-12")
    @patch("core.kalshi_monitor._station_local_kalshi_date_token", return_value="26MAR12")
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch(
        "core.kalshi_monitor._kalshi_public_get",
        side_effect=[
            {
                "events": [
                    {"event_ticker": "KXHIGHDEN-26MAR11", "status": "open"},
                    {"event_ticker": "KXHIGHDEN-26MAR12", "status": "open"},
                    {"event_ticker": "KXHIGHDEN-26MAR13", "status": "closed"},
                ]
            },
            {"markets": []},
        ],
    )
    def test_cache_metadata_prefers_station_day_event_ticker(self, mock_get, *_mocks):
        kalshi_monitor.build_structured_snapshot("KDEN", {"HIGH"})

        self.assertEqual(mock_get.call_args_list[1].args[0], "/markets?event_ticker=KXHIGHDEN-26MAR12&limit=100")

    @patch("core.kalshi_monitor.station_local_day_key", return_value="2026-03-12")
    @patch("core.kalshi_monitor.ensure_series_discovery_loaded", return_value={"KDEN": "KXHIGHDEN"})
    @patch(
        "core.kalshi_monitor._kalshi_public_get",
        side_effect=[
            {"events": [{"event_ticker": "KXHIGHDEN-26MAR12", "status": "active"}]},
            {"markets": []},
        ],
    )
    def test_empty_market_fetch_does_not_write_cache(self, mock_get, *_mocks):
        with kalshi_monitor._SERIES_LOCK:
            kalshi_monitor._SERIES_MARKETS_CACHE.clear()

        snapshot = kalshi_monitor.build_structured_snapshot("KDEN", {"HIGH"})

        self.assertFalse(snapshot.get("cache_written"))
        self.assertIsNone(kalshi_monitor.get_cached_series_markets("KXHIGHDEN"))
        self.assertEqual(mock_get.call_args_list[1].args[0], "/markets?event_ticker=KXHIGHDEN-26MAR12&limit=100")

    def test_no_direct_state_mutation_for_temperature_domains(self):
        source = open("core/metar_monitor.py", "r", encoding="utf-8").read()
        forbidden_fragments = [
            '_STATE["last_obs"][',
            '_STATE["last_seen_iso"][',
            '_STATE["last_observed_integer"][',
            '_STATE["running_daily_max"][',
            '_STATE["last_settlement_bucket"][',
            '_STATE["last_instant_bucket"][',
            '_STATE["last_obs"].update(',
            '_STATE["last_seen_iso"].update(',
            '_STATE["last_observed_integer"].update(',
            '_STATE["running_daily_max"].update(',
            '_STATE["last_settlement_bucket"].update(',
            '_STATE["last_instant_bucket"].update(',
            '_STATE["last_obs"].pop(',
            '_STATE["last_seen_iso"].pop(',
            '_STATE["last_observed_integer"].pop(',
            '_STATE["running_daily_max"].pop(',
            '_STATE["last_settlement_bucket"].pop(',
            '_STATE["last_instant_bucket"].pop(',
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, source)


class SimulationEndpointHydrationTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    @patch("app.ensure_scheduler_started", return_value=True)
    @patch("app.ensure_series_discovery_loaded", return_value={})
    @patch("core.kalshi_monitor.process_hydration_queue_worker", return_value={"status": "hydrated", "station": "KDEN"})
    @patch("core.kalshi_monitor.enqueue_station_hydration")
    @patch("app._simulate_temperature_for_testing", return_value={"ok": True})
    def test_simulate_ladder_hydration_flows_through_queue_worker(self, _simulate, mock_enqueue, mock_worker, *_mocks):
        response = self.client.post("/metar/simulate-ladder", json={"icao": "KDEN", "temp_f": 70.0})
        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once_with("KDEN", reason="simulate_ladder")
        mock_worker.assert_called_once_with(market_types={"HIGH", "LOW"})


if __name__ == "__main__":
    unittest.main()


class HydrationRecoverySafetyGateTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        app_module._autostart_fallback_done = True
        app_module.app.config["TESTING"] = False
        self.client = app_module.app.test_client()

    def _runtime_side_effect_snapshot(self):
        return {
            "hydration_queue": kalshi_monitor.hydration_queue_snapshot(),
            "hydration_execution": kalshi_monitor.get_last_hydration_execution_snapshot(),
            "ladder_state": kalshi_monitor.get_ladder_state_snapshot(),
            "recent_alerts": metar_monitor.get_recent_alerts(limit=5),
            "transition_history": metar_monitor.get_transition_history(limit=5),
        }

    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="replay")
    def test_recovery_noop_in_replay_domain(self, _domain, _env, mock_enqueue, mock_worker):
        before = self._runtime_side_effect_snapshot()

        response = self.client.post("/ops/hydration-recovery", json={"station": "KDEN"})
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "no_op")
        self.assertEqual(payload["reason"], "non_production_execution_domain")
        self.assertEqual(payload["execution_domain"], "replay")
        self.assertEqual(payload["queue_before"], payload["queue_after"])
        mock_enqueue.assert_not_called()
        mock_worker.assert_not_called()
        self.assertEqual(before, self._runtime_side_effect_snapshot())

    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="observability")
    def test_recovery_noop_in_observability_domain(self, _domain, _env, mock_enqueue, mock_worker):
        before = self._runtime_side_effect_snapshot()

        response = self.client.post("/ops/hydration-recovery", json={"station": "KDEN"})
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "no_op")
        self.assertEqual(payload["reason"], "non_production_execution_domain")
        self.assertEqual(payload["execution_domain"], "observability")
        self.assertEqual(payload["queue_before"], payload["queue_after"])
        mock_enqueue.assert_not_called()
        mock_worker.assert_not_called()
        self.assertEqual(before, self._runtime_side_effect_snapshot())

    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch("app._is_non_production_environment", return_value=True)
    @patch("app._current_kalshi_execution_domain", return_value="production")
    def test_recovery_noop_in_non_production_environment(self, _domain, _env, mock_enqueue, mock_worker):
        before = kalshi_monitor.hydration_queue_snapshot()

        response = self.client.post("/ops/hydration-recovery", json={"station": "KDEN"})
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "no_op")
        self.assertEqual(payload["reason"], "non_production_environment")
        self.assertEqual(payload["queue_before"], payload["queue_after"])
        mock_enqueue.assert_not_called()
        mock_worker.assert_not_called()
        self.assertEqual(before, kalshi_monitor.hydration_queue_snapshot())

    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="production")
    def test_recovery_refuses_when_allowlist_missing(self, _domain, _env):
        with patch.dict("os.environ", {"HYDRATION_RECOVERY_STATION_ALLOWLIST": ""}, clear=False):
            response = self.client.post("/ops/hydration-recovery", json={"station": "KDEN"})

        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["reason"], "allowlist_required")

    @patch.dict("os.environ", {"HYDRATION_RECOVERY_STATION_ALLOWLIST": "KDEN"}, clear=False)
    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="production")
    def test_recovery_refuses_station_not_in_allowlist(self, _domain, _env):
        response = self.client.post("/ops/hydration-recovery", json={"station": "KPHL"})
        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["reason"], "station_not_allowlisted")


    @patch.dict("os.environ", {"HYDRATION_RECOVERY_STATION_ALLOWLIST": "KDEN,KPHL"}, clear=False)
    @patch("app.hydration_queue_snapshot")
    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="production")
    def test_bounded_recovery_executes_only_in_production_domain(self, _domain, _env, mock_enqueue, mock_worker, mock_queue):
        mock_queue.side_effect = [
            {
                "queue": [],
                "queue_depth": 0,
                "queued_stations": [],
                "backoff_until": {},
                "backoff_stations": [],
                "last_hydration_request_ts": 0.0,
            },
            {
                "queue": ["KDEN"],
                "queue_depth": 1,
                "queued_stations": ["KDEN"],
                "backoff_until": {},
                "backoff_stations": [],
                "last_hydration_request_ts": 8.0,
            },
        ]
        mock_worker.side_effect = [
            {"status": "hydrated", "station": "KDEN"},
            {"status": "rate_limited", "reason": "interval_guard"},
        ]

        response = self.client.post(
            "/ops/hydration-recovery",
            json={"station": "KDEN", "bounded_limit": 2},
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["execution_domain"], "production")
        self.assertEqual(payload["bounded_limit"], 2)
        mock_enqueue.assert_called_once_with("KDEN", reason="ops_hydration_recovery")
        self.assertEqual(mock_worker.call_count, 2)
        self.assertEqual(payload["worker_runs"][0]["status"], "hydrated")

    @patch.dict("os.environ", {"HYDRATION_RECOVERY_STATION_ALLOWLIST": "KDEN"}, clear=False)
    @patch("app.hydration_queue_snapshot")
    @patch("app.process_hydration_queue_worker")
    @patch("app.enqueue_station_hydration")
    @patch("app._is_non_production_environment", return_value=False)
    @patch("app._current_kalshi_execution_domain", return_value="production")
    def test_recovery_bounded_limit_is_clamped(self, _domain, _env, mock_enqueue, mock_worker, mock_queue):
        mock_queue.side_effect = [
            {
                "queue": [],
                "queue_depth": 0,
                "queued_stations": [],
                "backoff_until": {},
                "backoff_stations": [],
                "last_hydration_request_ts": 0.0,
            },
            {
                "queue": ["KDEN"],
                "queue_depth": 1,
                "queued_stations": ["KDEN"],
                "backoff_until": {},
                "backoff_stations": [],
                "last_hydration_request_ts": 3.0,
            },
        ]
        mock_worker.side_effect = [
            {"status": "hydrated", "station": "KDEN"},
            {"status": "rate_limited", "reason": "interval_guard"},
            {"status": "rate_limited", "reason": "interval_guard"},
        ]

        response = self.client.post(
            "/ops/hydration-recovery",
            json={"station": "KDEN", "bounded_limit": 999},
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["bounded_limit"], 3)
        mock_enqueue.assert_called_once_with("KDEN", reason="ops_hydration_recovery")
        self.assertEqual(mock_worker.call_count, 3)
