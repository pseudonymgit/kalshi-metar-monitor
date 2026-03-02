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
            }
        },
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
        self.assertEqual(payload["latest_transitions"]["bounded_limit"], 50)
        self.assertEqual(payload["latest_alerts"]["bounded_limit"], 50)
        self.assertEqual(payload["latest_alerts"]["count"], 1)
        self.assertEqual(payload["db"]["path"], "/var/data/alerts.db")
        self.assertTrue(payload["db"]["exists"])


if __name__ == "__main__":
    unittest.main()
