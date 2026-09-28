#!/usr/bin/env python3
"""
Regression test — canonical 20-station filter (2026-09-26).

Locks the processing universe to the canonical 20 Kalshi weather stations and
proves that processing stages (settlement epochs, daily loop, intraday bridge)
never emit a non-canonical station (KDAL / TEST / non-Kalshi extras).

Run:  python3 -m pytest tests/test_canonical_20_station_filter.py
      python3 -m unittest tests.test_canonical_20_station_filter
"""

import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import station_registry as reg  # noqa: E402

# The canonical 20, derived from Kalshi live series endpoints
# (data/kalshi_official_mapping.json, 20 series) reconciled with the METAR /
# NWP / settlement_epochs ground truth. This literal is intentional: it is the
# independent oracle the registry is tested against.
EXPECTED_20 = {
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
}

FORBIDDEN = {"KDAL", "TEST"}

# Codes that were leaked by the pre-fix 30-station processing run.
LEAKED_EXTRAS = {
    "KDAL", "KBNA", "KCLT", "KDTW", "KJFK", "KORD",
    "KPDX", "KSLC", "KTPA", "PHNL",
}


def _load_script(name):
    path = os.path.join(REPO_ROOT, "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CanonicalRegistryTests(unittest.TestCase):
    def test_canonical_set_is_exactly_the_twenty(self):
        self.assertEqual(set(reg.CANONICAL_STATIONS), EXPECTED_20)
        self.assertEqual(len(reg.CANONICAL_STATIONS), 20)

    def test_get_all_stations_returns_canonical_twenty(self):
        self.assertEqual(set(reg.get_all_stations()), EXPECTED_20)

    def test_forbidden_stations_never_canonical(self):
        for s in FORBIDDEN:
            self.assertFalse(reg.is_canonical_station(s), f"{s} must be forbidden")
            self.assertNotIn(s, reg.get_all_stations())
            self.assertNotIn(s, reg.get_station_mapping())

    def test_leaked_extras_are_not_canonical(self):
        for s in LEAKED_EXTRAS:
            self.assertFalse(
                reg.is_canonical_station(s), f"{s} leaked into the old 30-station run"
            )

    def test_mapping_covers_canonical_twenty_only(self):
        mapping = reg.get_station_mapping()
        self.assertEqual(set(mapping.keys()), EXPECTED_20)

    def test_research_stations_alias_matches_canonical(self):
        self.assertEqual(set(reg.get_research_stations()), EXPECTED_20)


class CanonicalGuardTests(unittest.TestCase):
    def test_guard_passes_clean_input(self):
        self.assertEqual(
            reg.assert_canonical_stations(sorted(EXPECTED_20), stage="unit"),
            sorted(EXPECTED_20),
        )

    def test_guard_raises_on_non_canonical(self):
        with self.assertRaises(ValueError) as ctx:
            reg.assert_canonical_stations(["KNYC", "PHNL"], stage="unit")
        self.assertIn("PHNL", str(ctx.exception))

    def test_guard_raises_on_forbidden(self):
        for s in FORBIDDEN:
            with self.assertRaises(ValueError):
                reg.assert_canonical_stations(["KNYC", s], stage="unit")


class ProcessingStageFilterTests(unittest.TestCase):
    """Processing stages must be pinned to the canonical 20 at import/build."""

    def test_daily_loop_producer_stations_are_canonical(self):
        mod = _load_script("daily_loop_producer.py")
        self.assertEqual(set(mod.ALL_STATIONS), EXPECTED_20)

    def test_intraday_bridge_stations_are_canonical(self):
        mod = _load_script("intraday_bridge.py")
        self.assertEqual(set(mod.ALL_STATIONS), EXPECTED_20)

    def test_generate_settlement_epochs_filters_daily_stats(self):
        """The stated root cause: dynamic pull from daily_stats with no filter."""
        mod = _load_script("generate_settlement_epochs.py")
        # The script must expose the canonical filter helper.
        canonical = set(mod._get_canonical_stations())
        self.assertEqual(canonical, EXPECTED_20)

    def test_signal_pipeline_daily_metar_filter_source(self):
        """signal_pipeline._get_daily_metars filters settlement_epochs to canonical."""
        with open(os.path.join(REPO_ROOT, "core", "signal_pipeline.py")) as fh:
            src = fh.read()
        self.assertIn("_get_all_stations", src)
        # The old unguarded enumeration must be gone from the metar reader.
        self.assertNotIn(
            "stations = [row[0] for row in c.fetchall()]",
            src,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
