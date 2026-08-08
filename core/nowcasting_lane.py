#!/usr/bin/env python3
"""
nowcasting_lane.py — Wraps the fixed nowcasting signal with persistence,
window-weighting, and probability-mass shift output.

Architecture per Gray Room spec (2026-08-08):
  Nowcasting is a lane (modulator), not a signal. It restricts analog pools,
  shifts probability mass, and modulates confidence — it does not compete
  as a co-eval signal.

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from core.signals.frontal_passage_nowcast_signal import (
    FrontalPassageNowcastSignal,
    NowcastShift,
    _is_in_trading_window,
    _get_window_weight,
    _compute_nowcast_shift,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NOWCAST_DB = os.path.join(REPO_ROOT, "data", "nowcasting_state.db")

NOWCAST_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS nowcasting_state (
    station TEXT NOT NULL,
    last_detection_ts TEXT,
    direction TEXT,
    confidence REAL DEFAULT 0,
    gradient_C REAL DEFAULT 0,
    n_upstream INTEGER DEFAULT 0,
    prob_shift_json TEXT,
    window_weight REAL DEFAULT 1.0,
    in_trading_window INTEGER DEFAULT 0,
    expiry_ts TEXT,
    PRIMARY KEY (station)
);
CREATE TABLE IF NOT EXISTS nowcasting_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    detected_ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    gradient_C REAL NOT NULL,
    n_upstream INTEGER DEFAULT 0,
    prob_shift_json TEXT,
    window_weight REAL DEFAULT 1.0,
    in_trading_window INTEGER DEFAULT 0,
    signal_fired INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_nowcast_station ON nowcasting_state(station);
CREATE INDEX IF NOT EXISTS idx_nowcast_events_ts ON nowcasting_events(detected_ts);
"""


def get_db(db_path: str = DEFAULT_NOWCAST_DB) -> sqlite3.Connection:
    """Get connection with WAL mode."""
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize schema."""
    conn.executescript(NOWCAST_DB_SCHEMA)
    conn.commit()


class NowcastingLane:
    """
    Wraps the FrontalPassageNowcastSignal with persistence, window-weighting,
    and probability-mass shift output.

    Responsibilities:
    - Runs nowcasting detection on current METAR data
    - Enforces time-of-day and obs-age filters (delegated to signal)
    - Persists state to DB to survive restarts
    - Outputs probability-mass shift vector (NowcastShift)
    - Tracks expiry: detections older than 90 min are stale
    """

    def __init__(self, metar_db: str = None, nowcast_db: str = DEFAULT_NOWCAST_DB,
                 expiry_minutes: int = 90):
        self._signal = FrontalPassageNowcastSignal(
            metar_db_path=metar_db or self._default_metar_db()
        )
        self._nowcast_db = nowcast_db
        self._expiry_minutes = expiry_minutes
        self._conn = None

    @staticmethod
    def _default_metar_db() -> str:
        return os.path.join(REPO_ROOT, "data", "metar_backfill.db")

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_db(self._nowcast_db)
            init_db(self._conn)
        return self._conn

    def evaluate(self, station: str, market_type: str = "HIGH",
                 force: bool = False) -> Optional[NowcastShift]:
        """
        Evaluate nowcasting for a station.

        Returns NowcastShift dict if a front is detected, None otherwise.
        If force=True, re-evaluate even if cached state exists.
        """
        conn = self._get_conn()
        now_ts = datetime.now(timezone.utc).isoformat()

        # Check cached state (non-expired)
        if not force:
            cached = self._get_cached_state(conn, station)
            if cached and self._is_valid(cached, now_ts):
                logger.debug("NowcastingLane: using cached state for %s", station)
                return cached

        # Run detection
        result = self._signal.evaluate_for_station(station, None, market_type=market_type)
        if result:
            # Persist to DB
            self._persist_detection(conn, result)
            self._log_event(conn, result, signal_fired=True)
            return result

        # No detection — log negative event and clear state
        self._clear_state(conn, station)
        return None

    def get_prob_shift(self, station: str,
                       prior_distribution: Optional[Dict[str, float]] = None
                       ) -> Dict[str, float]:
        """
        Get the probability mass shift for a station.

        If a nowcast event is active (non-expired), returns the prob_shift.
        Otherwise returns identity shift (all zeros).
        """
        conn = self._get_conn()
        now_ts = datetime.now(timezone.utc).isoformat()

        cached = self._get_cached_state(conn, station)
        if not cached or not self._is_valid(cached, now_ts):
            # No active nowcasting — identity shift
            if prior_distribution:
                return {k: 0.0 for k in prior_distribution}
            return {}

        prob_shift = cached.get("prob_shift", {})
        if not prob_shift:
            return {}

        # Apply window weighting
        weight = cached.get("window_weight", 1.0)
        if weight < 1.0:
            prob_shift = {k: v * weight for k, v in prob_shift.items()}

        return prob_shift

    def get_all_active(self) -> List[NowcastShift]:
        """Get all stations with active (non-expired) nowcast detections."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=self._expiry_minutes)).isoformat()

        rows = conn.execute("""
            SELECT * FROM nowcasting_state
            WHERE expiry_ts IS NOT NULL AND expiry_ts > ?
            ORDER BY confidence DESC
        """, (cutoff,)).fetchall()

        results = []
        for r in rows:
            prob_shift = {}
            if r["prob_shift_json"]:
                try:
                    prob_shift = json.loads(r["prob_shift_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append({
                "type": "nowcast_shift",
                "station": r["station"],
                "direction": r["direction"],
                "confidence": r["confidence"],
                "gradient_C": r["gradient_C"],
                "n_upstream": r["n_upstream"],
                "timestamp_utc": r["last_detection_ts"],
                "in_trading_window": bool(r["in_trading_window"]),
                "window_weight": r["window_weight"],
                "prob_shift": prob_shift,
            })

        return results

    def clear_expired(self) -> int:
        """Remove expired states and return count."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=self._expiry_minutes)).isoformat()
        cur = conn.execute("DELETE FROM nowcasting_state WHERE expiry_ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount

    # ── Internal persistence ──

    def _get_cached_state(self, conn: sqlite3.Connection, station: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM nowcasting_state WHERE station=?", (station,)
        ).fetchone()
        return dict(row) if row else None

    def _is_valid(self, state: dict, now_ts: str) -> bool:
        """Check if cached state is still within expiry."""
        expiry = state.get("expiry_ts")
        if not expiry:
            return False
        return expiry > now_ts

    def _persist_detection(self, conn: sqlite3.Connection, shift: NowcastShift) -> None:
        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=self._expiry_minutes)
        ).isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO nowcasting_state
            (station, last_detection_ts, direction, confidence, gradient_C,
             n_upstream, prob_shift_json, window_weight, in_trading_window, expiry_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            shift.get("station", ""),
            shift.get("timestamp_utc", ""),
            shift.get("direction", ""),
            shift.get("confidence", 0.0),
            shift.get("gradient_C", 0.0),
            shift.get("n_upstream", 0),
            json.dumps(shift.get("prob_shift", {})),
            shift.get("window_weight", 1.0),
            1 if shift.get("in_trading_window", False) else 0,
            expiry,
        ))
        conn.commit()

    def _clear_state(self, conn: sqlite3.Connection, station: str) -> None:
        conn.execute("DELETE FROM nowcasting_state WHERE station=?", (station,))
        conn.commit()

    def _log_event(self, conn: sqlite3.Connection, shift: NowcastShift,
                    signal_fired: bool = True) -> None:
        conn.execute("""
            INSERT INTO nowcasting_events
            (station, detected_ts, direction, confidence, gradient_C,
             n_upstream, prob_shift_json, window_weight, in_trading_window, signal_fired)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            shift.get("station", ""),
            shift.get("timestamp_utc", ""),
            shift.get("direction", ""),
            shift.get("confidence", 0.0),
            shift.get("gradient_C", 0.0),
            shift.get("n_upstream", 0),
            json.dumps(shift.get("prob_shift", {})),
            shift.get("window_weight", 1.0),
            1 if shift.get("in_trading_window", False) else 0,
            1 if signal_fired else 0,
        ))
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Test ──

def test_nowcasting_lane():
    print("=" * 72)
    print("  NowcastingLane — Phase 2 Self-Test")
    print("=" * 72)

    import tempfile
    import os
    lane = NowcastingLane(nowcast_db="/tmp/test_nowcasting.db")

    print(f"  Expiry: {lane._expiry_minutes} min")
    print(f"  Nowcast DB: {lane._nowcast_db}")

    # Test evaluate (will likely return None without live METAR data)
    result = lane.evaluate("KNYC", "HIGH")
    print(f"  KNYC nowcast: {result}")

    # Test get_all_active (should be empty)
    active = lane.get_all_active()
    print(f"  Active nowcasts: {len(active)}")

    # Test clear_expired
    cleared = lane.clear_expired()
    print(f"  Cleared expired: {cleared}")

    # Test prob_shift with prior
    prior = {"below_30": 0.1, "30-35": 0.3, "35-40": 0.4, "above_40": 0.2}
    ps = lane.get_prob_shift("KNYC", prior)
    print(f"  Prob shift for KNYC: {ps}")

    # Cleanup
    lane.close()
    try:
        os.remove("/tmp/test_nowcasting.db*")
    except:
        pass

    print("\n  ✅ Phase 2 self-test complete")


if __name__ == "__main__":
    test_nowcasting_lane()