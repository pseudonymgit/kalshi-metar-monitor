import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class RuntimeAuthoritySnapshotEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.os.path.exists", return_value=True)
    @patch("app.get_recent_alerts", return_value=[
        {"id": 12, "station": "KDEN", "alert_type": "composed_alert_sent"},
        {"id": 11, "station": "KLAX", "alert_type": "composed_alert_sent"},
    ])
    @patch("app.get_transition_history", return_value=[
        {"id": 55, "station": "KDEN", "transition_type": "settlement_up"}
    ])
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={
        "snapshot_source": "in_memory_ladder_state",
        "total_ladder_state_keys": 1,
        "stations": {
            "KDEN": {
                "cache_present": True,
                "state_key_count": 1,
                "state_keys": ["KDEN_HIGH"],
                "hydration_prerequisite": {
                    "attempted": True,
                    "cache_valid": True,
                    "series_discovered": True,
                    "markets_cached": True,
                },
                "ingestion_admission": {
                    "hydration_passed": True,
                    "admitted_to_fetch": True,
                    "skip_reason": None,
                    "evaluated_at_utc": "2025-01-01T00:00:00+00:00",
                },
            }
        },
    })
    @patch("app.get_kalshi_connectivity_snapshot", return_value={
        "series_discovery_attempted": True,
        "last_series_discovery_success_utc": "2025-01-01T00:00:00+00:00",
        "last_series_discovery_error": "temporary_error",
        "markets_cache_population_count": 7,
    })
    @patch("app._build_ingestion_health_rows", return_value={
        "generated_utc": "2025-01-01T00:00:00+00:00",
        "stale_after_seconds": 180,
        "scheduler_running": True,
        "stations": [
            {"station": "KDEN", "status": "healthy"},
        ],
    })
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_runtime_authority_snapshot_returns_bounded_read_only_payload(self, *_mocks):
        response = self.client.get("/observability/runtime-authority-snapshot?station=KDEN")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["execution_mode"], "observability")
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["scheduler_health_snapshot"]["stations"][0]["station"], "KDEN")
        self.assertEqual(payload["hydration_snapshot"]["stations"]["KDEN"]["cache_present"], True)
        self.assertEqual(payload["hydration_snapshot"]["stations"]["KDEN"]["hydration_prerequisite"]["attempted"], True)
        self.assertEqual(payload["hydration_snapshot"]["stations"]["KDEN"]["hydration_prerequisite"]["cache_valid"], True)
        self.assertEqual(payload["hydration_snapshot"]["stations"]["KDEN"]["ingestion_admission"]["hydration_passed"], True)
        self.assertEqual(payload["kalshi_connectivity"]["series_discovery_attempted"], True)
        self.assertEqual(payload["kalshi_connectivity"]["last_series_discovery_success_utc"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(payload["kalshi_connectivity"]["last_series_discovery_error"], "temporary_error")
        self.assertEqual(payload["kalshi_connectivity"]["markets_cache_population_count"], 7)
        self.assertEqual(payload["latest_transitions"]["bounded_limit"], 50)
        self.assertEqual(payload["latest_alerts"]["bounded_limit"], 50)
        self.assertEqual(payload["latest_alerts"]["count"], 1)
        self.assertEqual(payload["db"]["path"], "/var/data/alerts.db")
        self.assertTrue(payload["db"]["exists"])

    @patch("app._kalshi_public_get", side_effect=AssertionError("observability endpoint must remain read-only"))
    @patch("app.os.path.exists", return_value=False)
    @patch("app.get_recent_alerts", return_value=[])
    @patch("app.get_transition_history", return_value=[])
    @patch("app.get_kalshi_connectivity_snapshot", return_value={
        "series_discovery_attempted": False,
        "last_series_discovery_success_utc": None,
        "last_series_discovery_error": None,
        "markets_cache_population_count": 0,
    })
    @patch("app._build_runtime_authority_hydration_snapshot", return_value={"stations": {}})
    @patch("app._build_ingestion_health_rows", return_value={"stations": []})
    @patch("app._canonical_live_station_universe", return_value={"stations": []})
    def test_runtime_authority_snapshot_does_not_trigger_live_kalshi_calls(self, *_mocks):
        response = self.client.get("/observability/runtime-authority-snapshot")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["execution_mode"], "observability")
        self.assertEqual(payload["kalshi_connectivity"]["markets_cache_population_count"], 0)


if __name__ == "__main__":
    unittest.main()
