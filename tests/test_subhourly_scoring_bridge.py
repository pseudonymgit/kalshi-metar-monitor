"""
Sub-hourly trajectory signal — scoring-harness wiring tests.

Covers the two wiring fixes Dan asked for (2026-10-05):

  1. Registry: `subhourly_trajectory` is present in `SignalRegistry.signals`
     (so `get_all_signals()` no longer silently drops it → NO_FIRES).
  2. Scoring path: the signal's nowcast output is converted to
     `AdditiveSignalResult` rows under `subhourly_trajectory`-prefixed keys and
     persisted to the same store the core/additive evaluators use.

These are DB-free unit tests: the bridge's aggregation logic is exercised
through `_rows_for_station` with synthetic nowcast events, and persistence
through a temp `signal_cache.db`. The DB-backed OOS reproduction lives in
`scripts/test_subhourly_trajectory_signal.py` (read-only).
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.signals import subhourly_scoring as ss  # noqa: E402


def _ev(prediction, slope, gap, confidence=None, outcome=None, strike=100.0, settle=101.0):
    return {
        "station": "KATL", "trading_date": "2026-09-18", "t_minute": 180,
        "strike": strike, "settle": settle, "cur_max": 98.0, "gap": gap,
        "slope": slope, "already_crossed": False, "prediction": prediction,
        "confidence": confidence if confidence is not None else (slope / 0.15),
        "outcome": outcome,
    }


class RegistryWiringTests(unittest.TestCase):
    def test_subhourly_trajectory_registered(self):
        from core.signals import create_signal_registry
        reg = create_signal_registry(str(REPO_ROOT / "data" / "metar_backfill.db"))
        self.assertIn("subhourly_trajectory", reg.signals)
        self.assertIn("subhourly_trajectory", reg.get_all_signals())
        # convenience attribute mirrors the key
        self.assertIs(reg.subhourly_trajectory, reg.signals["subhourly_trajectory"])


class ScoringBridgeTests(unittest.TestCase):
    def test_two_keys_and_prefix(self):
        events = [_ev("no-cross", 0.02, 4.0)]
        rows = ss._rows_for_station("KATL", "2026-09-18", events)
        keys = {r.signal for r in rows}
        self.assertEqual(keys, {ss.SIGNAL_KEY, ss.ANTICIPATORY_KEY})
        for r in rows:
            self.assertTrue(r.signal.startswith("subhourly_trajectory"))

    def test_main_row_fires_on_directional_output(self):
        # no-cross is a live directional prediction -> fires
        rows = ss._rows_for_station("KATL", "2026-09-18", [_ev("no-cross", 0.02, 4.0)])
        main = next(r for r in rows if r.signal == ss.SIGNAL_KEY)
        self.assertTrue(main.fired)
        self.assertIn("no-cross", main.reason)

    def test_anticipatory_key_fires_only_on_cell(self):
        # cross prediction in the cell -> anticipatory fires
        rows = ss._rows_for_station(
            "KATL", "2026-09-18", [_ev("cross", 0.08, 2.0, confidence=0.53)])
        cell = next(r for r in rows if r.signal == ss.ANTICIPATORY_KEY)
        self.assertTrue(cell.fired)
        # a day with no cross prediction -> anticipatory stays silent
        rows2 = ss._rows_for_station("KATL", "2026-09-18", [_ev("no-cross", 0.02, 4.0)])
        cell2 = next(r for r in rows2 if r.signal == ss.ANTICIPATORY_KEY)
        self.assertFalse(cell2.fired)

    def test_abstain_day_does_not_fire_main(self):
        rows = ss._rows_for_station("KATL", "2026-09-18", [_ev(None, 0.0, None)])
        main = next(r for r in rows if r.signal == ss.SIGNAL_KEY)
        self.assertFalse(main.fired)
        self.assertEqual(main.confidence, 0.0)

    def test_persists_to_signal_cache(self):
        from core.additive.persistence import persist_results_to_db
        by_day = {"KATL": ss._rows_for_station("KATL", "2026-09-18", [_ev("cross", 0.08, 2.0)])}
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signal_cache.db"
            n = persist_results_to_db(
                {(st, "2026-09-18"): rows for st, rows in by_day.items()}, db_path=db)
            self.assertGreater(n, 0)
            c = sqlite3.connect(db)
            got = c.execute(
                "SELECT COUNT(*) FROM additive_signal_results "
                "WHERE signal LIKE 'subhourly%'").fetchone()[0]
            c.close()
            self.assertGreaterEqual(got, 2)


if __name__ == "__main__":
    unittest.main()
