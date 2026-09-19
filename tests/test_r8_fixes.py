"""
Tests for the R8 runtime fixes (2026-09-16).

1. Settlement JOIN fix — `find_overlapping_markets` must join on (station, target_date),
   not event_ticker, so finalized vs historical_api cross-validation finds real overlap.

2. Bridge fail-closed — `intraday_bridge.run_bridge` must halt (non-zero, no write)
   when daily LOOP output is missing or stale, instead of silently writing a
   NEUTRAL/0.5 `default_fallback` anchor.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_kalshi_settlement_sources import find_overlapping_markets
from scripts import intraday_bridge


class SettlementJoinFixTests(unittest.TestCase):
    """The JOIN must key on (station, target_date, series), never event_ticker,
    and must not count series-mismatched (HIGH vs LOW) pairs as overlap."""

    def _build_db(self):
        """Build an in-memory settlements DB with the production schema."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE kalshi_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                series TEXT NOT NULL,
                event_ticker TEXT NOT NULL UNIQUE,
                target_date TEXT NOT NULL,
                kalshi_temp REAL,
                markets_found INTEGER,
                finalized_count INTEGER,
                source TEXT,
                source_type TEXT NOT NULL DEFAULT 'historical_api',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT
            )
            """
        )
        return conn

    def test_series_mismatch_is_not_counted_as_overlap(self):
        """HIGH finalized vs LOW historical on same station+date must NOT match."""
        conn = self._build_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXHIGHLAX', 'KXHIGHLAX-26JUL27', '2026-07-27', 82.5, 'finalized')"
        )
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXLOWTLAX', 'KXLOWTLAX-26JUL27', '2026-07-27', 69.0, 'historical_api')"
        )
        conn.commit()

        overlaps = find_overlapping_markets(cur)
        self.assertEqual(len(overlaps), 0)

    def test_same_series_overlap_is_found(self):
        """HIGH finalized vs HIGH historical on same station+date MUST match."""
        conn = self._build_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXHIGHLAX', 'KXHIGHLAX-26JUL27', '2026-07-27', 82.5, 'finalized')"
        )
        # Same series + station + date, but a different ticker (tickers are
        # UNIQUE and differ by source in production). The series match is what
        # matters for a valid cross-validation pair.
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXHIGHLAX', 'KXHIGHLAX-26JUL27-HIST', '2026-07-27', 82.1, 'historical_api')"
        )
        conn.commit()

        overlaps = find_overlapping_markets(cur)
        self.assertEqual(len(overlaps), 1)
        o = overlaps[0]
        self.assertEqual(o["station"], "KLAX")
        self.assertAlmostEqual(o["abs_error"], abs(82.5 - 82.1), places=6)

    def test_join_returns_zero_when_no_station_date_overlap(self):
        conn = self._build_db()
        cur = conn.cursor()
        # Different dates → no overlap.
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXHIGHLAX', 'KXHIGHLAX-26JUL27', '2026-07-27', 82.5, 'finalized')"
        )
        cur.execute(
            "INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, source_type) "
            "VALUES ('KLAX', 'KXHIGHLAX', 'KXHIGHLAX-26JUL28', '2026-07-28', 82.1, 'historical_api')"
        )
        conn.commit()

        overlaps = find_overlapping_markets(cur)
        self.assertEqual(len(overlaps), 0)


class BridgeFailClosedTests(unittest.TestCase):
    """The bridge must halt loudly on missing/stale daily data, never write neutral."""

    def setUp(self):
        # Point the bridge at a temp daily output + anchor file.
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()
        self.anchor_dir = self.data_dir / "intraday"
        self.anchor_dir.mkdir()

        self._orig_daily = intraday_bridge.DAILY_LOOP_OUTPUT
        self._orig_anchor = intraday_bridge.ANCHOR_H24_FILE
        intraday_bridge.DAILY_LOOP_OUTPUT = self.data_dir / "daily_loop_output.json"
        intraday_bridge.ANCHOR_H24_FILE = self.anchor_dir / "anchor_h24.json"

    def tearDown(self):
        intraday_bridge.DAILY_LOOP_OUTPUT = self._orig_daily
        intraday_bridge.ANCHOR_H24_FILE = self._orig_anchor
        self._tmp.cleanup()

    def _write_daily(self, age_hours=0.0):
        ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        payload = {
            "timestamp_utc": ts,
            "consensus": {"direction": "UP", "confidence": 0.7, "mu": 0.6, "sigma": 1.5},
            "stations_data": {},
        }
        intraday_bridge.DAILY_LOOP_OUTPUT.write_text(
            __import__("json").dumps(payload)
        )

    def test_missing_daily_output_halts_nonzero_no_write(self):
        # No daily_loop_output.json exists.
        rc = intraday_bridge.run_bridge(dry_run=False, force=False)
        self.assertEqual(rc, 2)
        # Must NOT write any anchor.
        self.assertFalse(intraday_bridge.ANCHOR_H24_FILE.exists())

    def test_stale_daily_output_halts_nonzero_no_write(self):
        # Write daily output that is 100h old (beyond MAX_DAILY_AGE_HOURS=36).
        self._write_daily(age_hours=100.0)
        rc = intraday_bridge.run_bridge(dry_run=False, force=False)
        self.assertEqual(rc, 2)
        self.assertFalse(intraday_bridge.ANCHOR_H24_FILE.exists())

    def test_fresh_daily_output_writes_daily_loop_anchor(self):
        self._write_daily(age_hours=0.0)
        rc = intraday_bridge.run_bridge(dry_run=False, force=False)
        self.assertEqual(rc, 0)
        self.assertTrue(intraday_bridge.ANCHOR_H24_FILE.exists())
        anchor = __import__("json").loads(intraday_bridge.ANCHOR_H24_FILE.read_text())
        self.assertEqual(anchor["source"], "daily_loop")
        self.assertEqual(anchor["direction"], "UP")
        self.assertAlmostEqual(anchor["confidence"], 0.7, places=6)

    def test_force_overrides_stale_and_writes(self):
        self._write_daily(age_hours=100.0)
        rc = intraday_bridge.run_bridge(dry_run=False, force=True)
        self.assertEqual(rc, 0)
        self.assertTrue(intraday_bridge.ANCHOR_H24_FILE.exists())


if __name__ == "__main__":
    unittest.main()
