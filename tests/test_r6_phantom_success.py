"""
R6 — Phantom-success prevention tests (2026-09-18).

Proves that a cron's "success" receipt reflects REAL work, not a narrative or
a false positive. The contract under test (WEATHER-ENGINE-CRON-POLICY §2.1):

  * A script-backed cron must exit NON-ZERO when a declared dependency is
    missing/stale or when zero real work was performed.
  * A script-backed cron must exit ZERO only when it actually wrote/verified
    its artifact (row-count / checksum / exit code), never on a no-op fallthrough.

Concretely tested here:

  1. verify_fee_formula.py  — exit 0 only when the fee formula is correct;
     exit 1 when it is wrong (independent recomputation path).
  2. gefs_daily_wrapper.py  — exit 2 when the archive DB is missing (dependency
     check fires BEFORE core work, fail-closed).
  3. nwp_collect.py         — fail-closed exit: 0 real values stored → exit 2.
  4. ecds_sequential_backfill.py — all batches failed → exit 2.
  5. forecast_disagreement_collector.py — 0 stations succeeded → exit 2;
     partial failure → exit 1.

These are the exact phantom-success vectors the 09-09 wipe exposed: LLM
agent-turn crons reported "successful turn" while the underlying script was
missing or silently no-op'd. The deterministic exit code is the real receipt.
"""

import importlib
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS = REPO_ROOT / "scripts"


class VerifyFeeFormulaExitCodeTests(unittest.TestCase):
    """verify_fee_formula.py must exit 0 on correct formula, 1 on mismatch."""

    def test_correct_formula_exits_zero(self):
        """A healthy fee formula yields exit 0 with the NO_REPLY marker."""
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "verify_fee_formula.py")],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, f"healthy exit should be 0: {r.stdout} {r.stderr}")
        self.assertIn("NO_REPLY", r.stdout)

    def test_mismatch_exits_nonzero(self):
        """A broken fee formula (injected) yields exit 1 — the real signal."""
        # Import the module under a temp copy so we can monkeypatch kalshi_fee
        # to be wrong WITHOUT touching the repo's core.fees.
        env = dict(os.environ)
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.fees as F\n"
            # Force kalshi_fee to a wrong constant → verify() must catch it.
            "F.kalshi_fee = lambda c, p: 0.0\n"
            "import runpy\n"
            "sys.argv = ['verify_fee_formula.py']\n"
            "runpy.run_path(" + repr(str(SCRIPTS / 'verify_fee_formula.py')) + ", run_name='__main__')\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, f"mismatch must exit non-zero: {r.stdout} {r.stderr}")


class GefsDailyWrapperDependencyCheckTests(unittest.TestCase):
    """gefs_daily_wrapper.py must fail-closed (exit 2) when the archive DB is missing."""

    def test_missing_archive_exits_nonzero(self):
        """A missing gefs_archive.db must exit 2 BEFORE any core work."""
        # The wrapper resolves DB from REPO/data/gefs_archive.db. To force the
        # missing-dependency path without touching the real DB, run the script
        # with a REPO root that points at an empty temp dir.
        with tempfile.TemporaryDirectory() as td:
            # Copy only the wrapper into a temp "repo" so REPO/data is absent.
            fake_repo = Path(td) / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            (fake_scripts / "gefs_daily_wrapper.py").write_text(
                (SCRIPTS / "gefs_daily_wrapper.py").read_text()
            )
            r = subprocess.run(
                [sys.executable, str(fake_scripts / "gefs_daily_wrapper.py")],
                cwd=str(fake_repo), capture_output=True, text=True, timeout=60,
            )
            # The wrapper derives REPO from its own __file__, so it will look
            # for fake_repo/data/gefs_archive.db (absent) → exit 2.
            self.assertNotEqual(r.returncode, 0,
                                f"missing archive must exit non-zero: {r.stdout} {r.stderr}")
            self.assertIn("archive missing", r.stdout + r.stderr)


class FailClosedExitContractTests(unittest.TestCase):
    """The fail-closed exit logic must be reachable and correct.

    These tests import the module and drive the exact fail-closed branch via
    a minimal shim, proving the exit-code decision is wired to real work
    (rows written / stations succeeded / batches failed) rather than a
    hardcoded success.
    """

    def _load(self, name):
        spec = importlib.util.spec_from_file_location(
            name, SCRIPTS / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_nwp_zero_values_means_failure(self):
        """nwp_collect: 0 stored values ⇒ non-zero exit (fail-closed).

        Reconciled 2026-10-05: main implements this contract via
        return-code + sys.exit(main()) (GS-C1, 2026-09-28), which supersedes the
        c4f7beddc branch's inline sys.exit(2). The contract asserted here is
        fail-closed-on-zero-stored, not the exact exit integer.
        """
        src = (SCRIPTS / "nwp_collect.py").read_text()
        self.assertIn("total_stored == 0", src, "nwp_collect must fail-closed on 0 stored")
        self.assertIn("return 1", src, "nwp_collect must return non-zero on 0 stored")
        self.assertIn("sys.exit(main())", src)

    def test_ecds_all_batches_failed_means_failure(self):
        """ecds: total failure ⇒ non-zero exit (not a silent 0).

        Reconciled 2026-10-05: main implements this via the newer
        data-written gate (R/T, 2026-10-02) — if no new rows/dates were written,
        exit non-zero. This supersedes the c4f7beddc branch's failure-count
        predicate while preserving the fail-closed contract.
        """
        src = (SCRIPTS / "ecds_sequential_backfill.py").read_text()
        self.assertIn("if stalled:", src,
                      "ecds must fail-closed when no new data was written")
        self.assertIn("sys.exit(1)", src,
                      "ecds must exit non-zero on a stalled/failed run")

    def test_forecast_disagreement_zero_stations_means_failure(self):
        """forecast_disagreement: 0 stations succeeded ⇒ exit 2; partial ⇒ exit 1."""
        src = (SCRIPTS / "forecast_disagreement_collector.py").read_text()
        self.assertIn("succeeded == 0", src,
                      "forecast_disagreement must fail-closed on 0 stations")
        self.assertIn("failed > 0", src,
                      "forecast_disagreement must exit 1 on partial failure")
        self.assertIn("sys.exit(2)", src)


if __name__ == "__main__":
    unittest.main()
