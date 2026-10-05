"""
R6 phantom-success prevention — Layer 4 divergence-alert tests.

Verifies the acceptance criteria from
docs/plans/G5-PHANTOM-SUCCESS-LAYER4-SPEC-2026-10-03.md:

  P1 baseline sidecar written and stable across runs
  P2 true negative  — healthy collector (exit 0, real rows) -> no alert
  P3 true positive  — exit 0 + frozen oracle -> PHANTOM-SUCCESS, exit 2
  P4 partial        — max_date advanced, rows flat -> WARN
  P5 no spam        — repeated phantom yields one alert until state changes
  +L5 kill gate     — N consecutive phantoms writes the halt file
  +recovery         — a recovered source clears its signature

The observer is exercised against a tiny frozen SQLite fixture so the rule is
tested deterministically, without touching any live DB or L1 script.
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "phantom_watch", REPO_ROOT / "scripts" / "phantom_watch.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)


SRC = dict(key="metar", db="fixture.db", table="metar_observations",
           date_col="date_utc", cadence_h=24, slack=1)

JOB_OK = [{"name": "METAR ingestion (30m)", "state": {"lastRunStatus": "ok"}}]
JOB_FAIL = [{"name": "METAR ingestion (30m)", "state": {"lastRunStatus": "error"}}]


def _make_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metar_observations (date_utc TEXT)")
    conn.executemany("INSERT INTO metar_observations VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


class PhantomWatchTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dd = Path(self.td.name)
        self.paths = dict(
            data_dir=self.dd,
            baseline_path=self.dd / "baseline.json",
            consec_path=self.dd / "consec.json",
            sig_path=self.dd / "sig",
            halt_path=self.dd / "halt",
        )

    def tearDown(self):
        self.td.cleanup()

    def _run(self, jobs=JOB_OK, n=3):
        return pw.run([SRC], jobs, n=n, **self.paths)

    # P1 — baseline sidecar written and stable across runs
    def test_baseline_written_and_stable(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01", "2026-10-02"])
        code1, _ = self._run()   # first run establishes the baseline
        b1 = json.loads(self.paths["baseline_path"].read_text())
        self._run()              # second run: no advance -> PHANTOM (correct per rule)
        b2 = json.loads(self.paths["baseline_path"].read_text())
        self.assertEqual(code1, 0)
        # Baseline values are stable across runs; checked_at is refreshed.
        self.assertEqual(b1["metar"]["max_date"], b2["metar"]["max_date"])
        self.assertEqual(b1["metar"]["row_count"], b2["metar"]["row_count"])
        self.assertEqual(b2["metar"]["row_count"], 2)

    # P2 — true negative: healthy collector, oracle advanced, rows grew
    def test_healthy_advance_is_ok(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # establish baseline
        # advance: add a newer row
        c = sqlite3.connect(self.dd / "fixture.db")
        c.execute("INSERT INTO metar_observations VALUES ('2026-10-02')")
        c.commit(); c.close()
        code, rep = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(rep["phantoms"], [])
        self.assertEqual(rep["results"][0]["status"], "OK")

    # P3 — true positive: exit 0 but oracle frozen -> PHANTOM, exit 2
    def test_frozen_oracle_after_exit_zero_is_phantom(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline
        code, rep = self._run()  # no advance, exit 0
        self.assertEqual(code, 2)
        self.assertEqual(rep["phantoms"], ["metar"])
        self.assertEqual(rep["results"][0]["status"], "PHANTOM")

    # Non-zero exit is L1's signal, not a phantom
    def test_nonzero_exit_is_not_phantom(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline
        code, rep = self._run(jobs=JOB_FAIL)
        self.assertEqual(rep["phantoms"], [])
        self.assertEqual(code, 0)

    # P4 — partial: max_date advanced but rows flat -> WARN not PHANTOM
    def test_partial_advance_rows_flat_is_warn(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline: 1 row, 10-01
        # rewrite the single row to a newer date: date advances, count stays 1
        c = sqlite3.connect(self.dd / "fixture.db")
        c.execute("UPDATE metar_observations SET date_utc='2026-10-02'")
        c.commit(); c.close()
        code, rep = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(rep["warns"], ["metar"])
        self.assertEqual(rep["phantoms"], [])

    # P5 — no spam: repeated phantom yields one alert until state changes
    def test_repeated_phantom_alerts_once(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline
        code1, _ = self._run()  # first phantom
        code2, _ = self._run()  # same state
        self.assertEqual(code1, 2)
        self.assertEqual(code2, 0)  # suppressed

    # L5 — N consecutive phantoms writes the halt file
    def test_kill_gate_writes_halt_at_n(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline
        self._run()  # phantom #1
        self._run()  # phantom #2
        self.assertFalse(self.paths["halt_path"].exists())
        code, rep = self._run()  # phantom #3 -> N
        self.assertTrue(self.paths["halt_path"].exists())
        self.assertEqual(rep["halted"], [("metar", 3)])
        self.assertIn("HALTED", self.paths["halt_path"].read_text())

    # Recovery clears the signature so a later phantom re-alerts
    def test_recovery_clears_signature(self):
        _make_db(self.dd / "fixture.db", ["2026-10-01"])
        self._run()  # baseline
        self._run()  # phantom -> sig written
        self.assertTrue(self.paths["sig_path"].exists())
        c = sqlite3.connect(self.dd / "fixture.db")
        c.execute("INSERT INTO metar_observations VALUES ('2026-10-02')")
        c.commit(); c.close()
        code, _ = self._run()  # recovered
        self.assertEqual(code, 0)
        self.assertFalse(self.paths["sig_path"].exists())

    # Mapping: a renamed job with the same stem still maps to the source
    def test_job_name_mapping(self):
        self.assertEqual(pw.map_job_to_source("METAR ingestion (30m)"), "metar")
        self.assertEqual(pw.map_job_to_source("z5-asos-1min-cron"), "iem_asos_1min")
        self.assertIsNone(pw.map_job_to_source("unrelated job"))


if __name__ == "__main__":
    unittest.main()
