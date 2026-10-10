import unittest
from unittest.mock import patch

import app as app_module
from core import kalshi_monitor
import core.market_monitor as market_monitor

app = app_module.app


class ExecutionDomainEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_execution_domain_defaults_to_production(self):
        response = self.client.get("/execution-domain")
        self.assertEqual(response.status_code, 200)
        # core normalizes the production execution domain to its canonical short
        # form "prod" (core/kalshi_monitor.py::_current_kalshi_execution_domain).
        self.assertEqual(response.get_json(), {"execution_domain": "prod"})


class ExecutionDomainGuardTests(unittest.TestCase):
    """H5: the production-only guards must match the canonical domain value.

    ``_current_kalshi_execution_domain()`` normalizes ``production`` -> ``prod``.
    Callers that compared the accessor result against the literal ``"production"``
    were therefore *never* satisfied: the hydration worker always short-circuited
    to ``non_production_domain`` and the ladder snapshot always raised. The
    guards now compare against the canonical ``"prod"``.
    """

    def test_production_domain_satisfies_hydration_worker_guard(self):
        with patch("core.kalshi_monitor.hydrate_station_ladder_snapshot",
                   return_value={"status": "hydrated"}), \
             patch("core.kalshi_monitor.time.time", return_value=1000.0):
            kalshi_monitor._hydration_queue[:] = ["KDEN"]
            kalshi_monitor._hydration_backoff_until.clear()
            kalshi_monitor._last_hydration_request_ts = 0.0
            token = kalshi_monitor.set_kalshi_execution_domain("production")
            try:
                result = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})
            finally:
                kalshi_monitor.reset_kalshi_execution_domain(token)
                kalshi_monitor._hydration_queue.clear()
                kalshi_monitor._hydration_backoff_until.clear()
                kalshi_monitor._last_hydration_request_ts = 0.0

        self.assertEqual(result["status"], "hydrated")

    def test_production_domain_satisfies_ladder_snapshot_guard(self):
        with patch("core.kalshi_monitor.build_structured_snapshot",
                   return_value={"markets": []}), \
             patch("core.kalshi_monitor.ensure_series_discovery_loaded",
                   return_value={"KDEN": ["KXHIGHDEN"]}), \
             patch("core.kalshi_monitor.get_cached_series_markets",
                   return_value={"markets": [], "hydrated_at_utc": "2025-01-01T00:00:00+00:00",
                                 "station_local_day": "2025-01-01"}):
            token = kalshi_monitor.set_kalshi_execution_domain("production")
            try:
                result = kalshi_monitor.hydrate_station_ladder_snapshot("KDEN", {"HIGH"})
            finally:
                kalshi_monitor.reset_kalshi_execution_domain(token)

        self.assertIn("station", result)

    def test_non_production_domain_still_blocks_hydration_worker(self):
        with patch("core.kalshi_monitor.hydrate_station_ladder_snapshot",
                   side_effect=AssertionError("must not hydrate in observability")):
            token = kalshi_monitor.set_kalshi_execution_domain("observability")
            try:
                result = kalshi_monitor.process_hydration_queue_worker(market_types={"HIGH"})
            finally:
                kalshi_monitor.reset_kalshi_execution_domain(token)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "non_production_domain")

    def test_market_and_metar_guards_use_canonical_domain(self):
        """The market_monitor / metar_monitor copies of the production-only guard
        must compare against the canonical ``"prod"``. Their domain is read from
        a different contextvar than kalshi_monitor's, so pin the literal in
        source — a comparison to the non-canonical ``"production"`` can never be
        satisfied and would silently disable the gated path (H5)."""
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        mm = (repo / "core" / "market_monitor.py").read_text()
        metar = (repo / "core" / "metar_monitor.py").read_text()

        self.assertNotIn('_current_kalshi_execution_domain() != "production"', mm,
                         "market_monitor still compares to non-canonical 'production'")
        self.assertIn('_current_kalshi_execution_domain() != "prod"', mm)
        self.assertNotIn('execution_domain != "production"', metar,
                         "metar_monitor still compares to non-canonical 'production'")
        self.assertIn('execution_domain != "prod"', metar)


if __name__ == "__main__":
    unittest.main()
