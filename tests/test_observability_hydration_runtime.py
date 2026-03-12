import unittest
from unittest.mock import patch
from contextlib import ExitStack

import app as app_module

app = app_module.app


class HydrationPrerequisiteRuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_requires_station_query_param(self):
        response = self.client.get("/observability/hydration-prerequisite-runtime")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"ok": False, "error": "station query param required"},
        )

    @patch("app._current_kalshi_execution_domain", return_value="production")
    @patch("app.is_scheduler_running", return_value=True)
    @patch(
        "app.get_hydration_prerequisite_state_snapshot",
        return_value={"KDEN": {"attempted": True, "cache_valid": True}},
    )
    @patch(
        "app.get_station_hydration_cache_probe",
        return_value={
            "station": "KDEN",
            "series_ticker": "KXHIGHKDEN",
            "raw_market_count": 0,
            "cache_present": False,
            "markets_cached": False,
            "station_local_day": None,
            "hydrated_at_utc": None,
        },
    )
    def test_returns_runtime_shape(self, *_mocks):
        response = self.client.get("/observability/hydration-prerequisite-runtime?station=kden")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["execution_domain"], "production")
        self.assertTrue(payload["scheduler_running"])
        self.assertEqual(payload["status"], "cache_missing")
        self.assertEqual(payload["reason"], "cache_missing")
        self.assertFalse(payload["cache_valid"])
        self.assertFalse(payload["markets_cached"])
        self.assertEqual(payload["hydration_state"], {"attempted": True, "cache_valid": True})
        self.assertEqual(payload["persisted_hydration_state"], {"attempted": True, "cache_valid": True})
        self.assertEqual(payload["current_cache_probe"]["raw_market_count"], 0)
        self.assertEqual(
            set(payload["current_cache_probe"].keys()),
            {
                "station",
                "series_ticker",
                "raw_market_count",
                "cache_present",
                "markets_cached",
                "station_local_day",
                "hydrated_at_utc",
            },
        )
        self.assertIn("hydration_queue", payload)
        self.assertTrue(payload["ok"])

    def test_no_cache_entry_maps_to_cache_missing(self):
        with ExitStack() as stack:
            stack.enter_context(patch("app._current_kalshi_execution_domain", return_value="production"))
            stack.enter_context(patch("app.is_scheduler_running", return_value=True))
            stack.enter_context(
                patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"attempted": True, "cache_valid": True}})
            )
            stack.enter_context(
                patch(
                    "app.get_station_hydration_cache_probe",
                    return_value={
                        "station": "KDEN",
                        "series_ticker": "KXHIGHKDEN",
                        "raw_market_count": 0,
                        "cache_present": False,
                        "markets_cached": False,
                        "station_local_day": None,
                        "hydrated_at_utc": None,
                    },
                )
            )
            response = self.client.get("/observability/hydration-prerequisite-runtime?station=KDEN")

        payload = response.get_json()
        self.assertEqual(payload["status"], "cache_missing")
        self.assertEqual(payload["reason"], "cache_missing")
        self.assertFalse(payload["cache_valid"])
        self.assertFalse(payload["markets_cached"])
        self.assertEqual(payload["persisted_hydration_state"], {"attempted": True, "cache_valid": True})

    def test_empty_cache_entry_maps_to_cache_empty(self):
        with ExitStack() as stack:
            stack.enter_context(patch("app._current_kalshi_execution_domain", return_value="production"))
            stack.enter_context(patch("app.is_scheduler_running", return_value=True))
            stack.enter_context(
                patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"attempted": True, "cache_valid": True, "status": "cache_valid"}})
            )
            stack.enter_context(
                patch(
                    "app.get_station_hydration_cache_probe",
                    return_value={
                        "station": "KDEN",
                        "series_ticker": "KXHIGHKDEN",
                        "raw_market_count": 0,
                        "cache_present": True,
                        "markets_cached": False,
                        "station_local_day": "2026-01-01",
                        "hydrated_at_utc": "2026-01-01T00:00:00+00:00",
                    },
                )
            )
            response = self.client.get("/observability/hydration-prerequisite-runtime?station=KDEN")

        payload = response.get_json()
        self.assertEqual(payload["status"], "cache_missing")
        self.assertEqual(payload["reason"], "cache_empty")
        self.assertFalse(payload["cache_valid"])
        self.assertFalse(payload["markets_cached"])
        self.assertEqual(
            payload["persisted_hydration_state"],
            {"attempted": True, "cache_valid": True, "status": "cache_valid"},
        )

    def test_non_empty_cache_entry_maps_to_cache_valid(self):
        with ExitStack() as stack:
            stack.enter_context(patch("app._current_kalshi_execution_domain", return_value="production"))
            stack.enter_context(patch("app.is_scheduler_running", return_value=True))
            persisted = {"attempted": True, "cache_valid": False, "status": "cache_missing"}
            stack.enter_context(
                patch("app.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": persisted})
            )
            stack.enter_context(
                patch(
                    "app.get_station_hydration_cache_probe",
                    return_value={
                        "station": "KDEN",
                        "series_ticker": "KXHIGHKDEN",
                        "raw_market_count": 5,
                        "cache_present": True,
                        "markets_cached": True,
                        "station_local_day": "2026-01-01",
                        "hydrated_at_utc": "2026-01-01T00:00:00+00:00",
                    },
                )
            )
            response = self.client.get("/observability/hydration-prerequisite-runtime?station=KDEN")

        payload = response.get_json()
        self.assertEqual(payload["status"], "cache_valid")
        self.assertIsNone(payload["reason"])
        self.assertTrue(payload["cache_valid"])
        self.assertTrue(payload["markets_cached"])
        self.assertEqual(payload["persisted_hydration_state"], persisted)


if __name__ == "__main__":
    unittest.main()
