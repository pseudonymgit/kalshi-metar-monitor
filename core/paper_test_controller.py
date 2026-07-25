#!/usr/bin/env python3
"""
B1 + B10 + B11 + D3 — 30-Day Formal Paper Test Controller

Implements the phased 30-day paper test for the weather engine.

B1 — Three-Phase Rollout:
  Phase A (days 1-7): Directional forecasting lane only
  Phase B (days 8-14): Add spike reversion lane (shadow mode)
  Phase C (days 15-30): All lanes, full ensemble
  Each phase requires a 48h stability check before advancing.

B10 — Spike Reversion Shadow Mode:
  Spike reversion runs in shadow mode during paper test.
  Logs all trades but doesn't execute them.
  Compares shadow P&L against live directional P&L.
  Reports weekly.

B11 — Graduate Test Rollout:
  Monitors drawdown, trade count, and accuracy in real-time.
  If any metric breaches kill threshold:
    - Drawdown > 20%
    - Daily accuracy < 55%
  Halt trading and log the trigger.

D3 — Settlement-Confirmed Accuracy:
  Post-settlement accuracy comparing predicted direction against actual
  settlement outcome. Logs to a settlement_accuracy table.

Usage:
    from core.paper_test_controller import PaperTestController

    controller = PaperTestController()
    controller.start_phase("A")
    # ... run trading for 7 days ...
    controller.check_stability()  # Must pass for 48h
    controller.advance_to_next_phase()  # Or rollback
"""

import json
import logging
import os
import sqlite3
import time

from core.sqlite_utils import get_sqlite_connection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────

# Phase durations (days)
PHASE_A_DAYS = 7
PHASE_B_DAYS = 7
PHASE_C_DAYS = 16  # Remaining days to fill 30

# Stability check requirement
STABILITY_CHECK_HOURS = 48

# Kill thresholds (B11)
MAX_DRAWDOWN_PCT = 20.0
MIN_DAILY_ACCURACY = 0.55

# Default DB paths
PAPER_TEST_DB = "data/paper_test_results.db"
SETTLEMENT_DB = "data/paper_test_settlements.db"

# ─── Phase Enum ────────────────────────────────────────────────

PHASE_NAMES = {
    "A": {
        "name": "Phase A — Directional Forecasting",
        "days": PHASE_A_DAYS,
        "lanes": ["directional"],
    },
    "B": {
        "name": "Phase B — Add Spike Reversion (Shadow)",
        "days": PHASE_B_DAYS,
        "lanes": ["directional", "spike_reversion_shadow"],
    },
    "C": {
        "name": "Phase C — Full Ensemble",
        "days": PHASE_C_DAYS,
        "lanes": ["directional", "spike_reversion", "frontal_passage", "spatial_coherence"],
    },
}


# ─── PaperTestController ───────────────────────────────────────

class PaperTestController:
    """
    Controls the 30-day paper test with phased rollout, shadow mode,
    graduate monitoring, and settlement-confirmed accuracy tracking.
    """

    def __init__(
        self,
        db_path: str = PAPER_TEST_DB,
        settlement_db_path: str = SETTLEMENT_DB,
        state_file: str = "data/paper_test_state.json",
    ):
        self.db_path = Path(db_path)
        self.settlement_db_path = Path(settlement_db_path)
        self.state_file = Path(state_file)

        # Current state
        self.current_phase: Optional[str] = None  # 'A', 'B', 'C'
        self.phase_start_time: Optional[float] = None
        self.phase_stable_since: Optional[float] = None
        self.test_start_time: Optional[float] = None
        self.is_halted: bool = False
        self.halt_reason: Optional[str] = None

        # Metrics tracked during the test
        self._daily_metrics: List[Dict] = []
        self._shadow_trades: List[Dict] = []
        self._settlement_entries: List[Dict] = []

        # Initialize DBs
        self._init_dbs()

        # Load state
        self._load_state()

    # ── DB Initialization ─────────────────────────────────────

    def _init_dbs(self) -> None:
        """Initialize the paper test and settlement databases."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.settlement_db_path.parent.mkdir(parents=True, exist_ok=True)

        # Paper test DB: daily metrics and trades
        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_metrics (
                date TEXT PRIMARY KEY,
                phase TEXT,
                trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                accuracy REAL,
                drawdown_pct REAL,
                cumulative_pnl REAL,
                shadow_trades INTEGER,
                shadow_wins INTEGER,
                shadow_accuracy REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                station TEXT,
                market_type TEXT,
                direction TEXT,
                confidence REAL,
                signal_name TEXT,
                lane TEXT,
                is_shadow INTEGER DEFAULT 0,
                settled INTEGER DEFAULT 0,
                settlement_correct INTEGER,
                size REAL DEFAULT 0,
                pnl REAL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS halt_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                phase TEXT,
                trigger_metric TEXT,
                threshold REAL,
                actual_value REAL,
                reason TEXT
            )
        """)
        conn.commit()
        conn.close()

        # Settlement DB: confirmed accuracy tracking
        conn = get_sqlite_connection(str(self.settlement_db_path))
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settlement_accuracy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch_id TEXT,
                station TEXT,
                date TEXT,
                market_type TEXT,
                predicted_direction TEXT,
                predicted_confidence REAL,
                settlement_value REAL,
                prev_settlement_value REAL,
                actual_direction TEXT,
                was_correct INTEGER,
                signal_name TEXT,
                lane TEXT,
                settled_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    # ── Phase Management (B1) ─────────────────────────────────

    def start_phase(self, phase: str) -> bool:
        """
        Start a new phase of the paper test.

        Args:
            phase: 'A', 'B', or 'C'

        Returns:
            True if phase started, False if invalid transition
        """
        if phase not in PHASE_NAMES:
            logger.error(f"Invalid phase: {phase}")
            return False

        # Validate phase ordering
        if self.current_phase and phase <= self.current_phase:
            logger.error(f"Cannot go back to phase {phase} (current: {self.current_phase})")
            return False

        self.current_phase = phase
        self.phase_start_time = time.time()

        if self.test_start_time is None:
            self.test_start_time = time.time()

        self.phase_stable_since = None
        self._save_state()

        lanes = PHASE_NAMES[phase]["lanes"]
        logger.info(f"Started {PHASE_NAMES[phase]['name']} — lanes: {lanes}")
        return True

    def check_stability(self) -> Tuple[bool, str]:
        """
        Check if the current phase has been stable for 48 hours.

        Stability = no halt events in the last 48h.

        Returns:
            (is_stable, reason)
        """
        if self.is_halted:
            return False, "System is halted"

        if self.phase_start_time is None:
            return False, "No phase started"

        elapsed_hours = (time.time() - self.phase_start_time) / 3600
        if elapsed_hours < STABILITY_CHECK_HOURS:
            remaining = STABILITY_CHECK_HOURS - elapsed_hours
            return False, f"Need {remaining:.1f}h more for stability check ({elapsed_hours:.1f}/{STABILITY_CHECK_HOURS}h)"

        # Check for recent halt events
        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        cur.execute("SELECT COUNT(*) FROM halt_events WHERE timestamp >= ?", (cutoff,))
        halt_count = cur.fetchone()[0]
        conn.close()

        if halt_count > 0:
            return False, f"{halt_count} halt events in last 48h — stability check failed"

        self.phase_stable_since = time.time()
        self._save_state()
        return True, f"Phase {self.current_phase} stable for {STABILITY_CHECK_HOURS}h"

    def advance_to_next_phase(self) -> Optional[str]:
        """
        Attempt to advance to the next phase.

        Must pass stability check first.

        Returns:
            Next phase ('B', 'C') or None if cannot advance
        """
        if self.current_phase is None:
            self.start_phase("A")
            return "A"

        stable, reason = self.check_stability()
        if not stable:
            logger.warning(f"Cannot advance: {reason}")
            return None

        next_phase = {"A": "B", "B": "C"}.get(self.current_phase)
        if next_phase is None:
            logger.info("Already in final phase (C)")
            return None

        if self.start_phase(next_phase):
            return next_phase
        return None

    # ── Graduate Monitoring (B11) ─────────────────────────────

    def record_daily_metrics(
        self,
        date: str,
        trades: int,
        wins: int,
        losses: int,
        drawdown_pct: float,
        cumulative_pnl: float,
        shadow_trades: int = 0,
        shadow_wins: int = 0,
    ) -> Dict[str, Any]:
        """
        Record daily trading metrics and check kill thresholds.

        Args:
            date: ISO date string
            trades: Total trades executed
            wins: Winning trades
            losses: Losing trades
            drawdown_pct: Current drawdown as percentage
            cumulative_pnl: Cumulative P&L
            shadow_trades: Shadow mode trade count
            shadow_wins: Shadow mode winning trades

        Returns:
            Dict with 'halted' and 'reason' keys
        """
        accuracy = wins / trades if trades > 0 else 0.0
        shadow_accuracy = shadow_wins / shadow_trades if shadow_trades > 0 else 0.0

        entry = {
            "date": date,
            "phase": self.current_phase or "?",
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "accuracy": round(accuracy, 4),
            "drawdown_pct": round(drawdown_pct, 2),
            "cumulative_pnl": round(cumulative_pnl, 2),
            "shadow_trades": shadow_trades,
            "shadow_wins": shadow_wins,
            "shadow_accuracy": round(shadow_accuracy, 4),
        }
        self._daily_metrics.append(entry)

        # Persist
        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO daily_metrics
               (date, phase, trades, wins, losses, accuracy, drawdown_pct, cumulative_pnl,
                shadow_trades, shadow_wins, shadow_accuracy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, entry["phase"], trades, wins, losses, accuracy,
             drawdown_pct, cumulative_pnl, shadow_trades, shadow_wins, shadow_accuracy)
        )
        conn.commit()
        conn.close()

        # Check kill thresholds
        kill_result = self._check_kill_thresholds(
            drawdown_pct=drawdown_pct,
            daily_accuracy=accuracy,
            date=date,
        )

        return kill_result

    def _check_kill_thresholds(
        self,
        drawdown_pct: float,
        daily_accuracy: float,
        date: str,
    ) -> Dict[str, Any]:
        """
        Check if any kill thresholds are breached.

        Returns:
            Dict with 'halted' (bool), 'reason' (str), and 'trigger' (str)
        """
        result = {"halted": False, "reason": "", "trigger": ""}

        if self.is_halted:
            result["halted"] = True
            result["reason"] = self.halt_reason or "Previously halted"
            return result

        # Check drawdown
        if drawdown_pct > MAX_DRAWDOWN_PCT:
            self._trigger_halt(
                trigger="max_drawdown",
                threshold=MAX_DRAWDOWN_PCT,
                actual=drawdown_pct,
                reason=f"Drawdown {drawdown_pct:.1f}% exceeds {MAX_DRAWDOWN_PCT}%",
                date=date,
            )
            result["halted"] = True
            result["reason"] = self.halt_reason

        # Check daily accuracy
        if daily_accuracy < MIN_DAILY_ACCURACY and daily_accuracy > 0:
            # Only trigger if we have trades (accuracy > 0)
            self._trigger_halt(
                trigger="low_daily_accuracy",
                threshold=MIN_DAILY_ACCURACY,
                actual=daily_accuracy,
                reason=f"Daily accuracy {daily_accuracy:.3f} below {MIN_DAILY_ACCURACY}",
                date=date,
            )
            result["halted"] = True
            result["reason"] = self.halt_reason

        return result

    def _trigger_halt(
        self,
        trigger: str,
        threshold: float,
        actual: float,
        reason: str,
        date: str,
    ) -> None:
        """Trigger a trading halt and log the event."""
        self.is_halted = True
        self.halt_reason = reason

        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO halt_events
               (timestamp, phase, trigger_metric, threshold, actual_value, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), self.current_phase or "?",
             trigger, threshold, actual, reason)
        )
        conn.commit()
        conn.close()

        logger.critical(f"HALT: {reason}")

    # ── Shadow Mode (B10) ─────────────────────────────────────

    def record_shadow_trade(
        self,
        station: str,
        date: str,
        direction: str,
        confidence: float,
        signal_name: str = "spike_reversion",
        market_type: str = "HIGH",
    ) -> Dict:
        """
        Record a shadow-mode trade (logged but not executed).

        Shadow trades are tracked separately from live trades for
        comparison reporting.

        Args:
            station: Station code
            date: ISO date
            direction: 'up' or 'down'
            confidence: Signal confidence
            signal_name: Signal name
            market_type: 'HIGH' or 'LOW'

        Returns:
            Dict with trade details
        """
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "station": station,
            "date": date,
            "market_type": market_type,
            "direction": direction,
            "confidence": round(confidence, 4),
            "signal_name": signal_name,
            "lane": "spike_reversion_shadow",
            "is_shadow": True,
        }
        self._shadow_trades.append(trade)

        # Persist to DB
        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trades
               (timestamp, station, market_type, direction, confidence,
                signal_name, lane, is_shadow)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (trade["timestamp"], station, market_type, direction, confidence,
             signal_name, "spike_reversion_shadow")
        )
        conn.commit()
        conn.close()

        return trade

    def get_shadow_summary(
        self,
        since_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get a summary of shadow mode trades for reporting.

        Args:
            since_date: Only include trades since this date

        Returns:
            Dict with shadow trade statistics
        """
        conn = get_sqlite_connection(str(self.db_path))
        cur = conn.cursor()

        query = "SELECT COUNT(*), SUM(CASE WHEN settlement_correct = 1 THEN 1 ELSE 0 END), "
        query += "AVG(confidence) FROM trades WHERE is_shadow = 1"
        params = []

        if since_date:
            query += " AND timestamp >= ?"
            params.append(since_date + "T00:00:00")

        cur.execute(query, params)
        row = cur.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        avg_conf = row[2] or 0.0
        conn.close()

        live_trades = len(self._daily_metrics)
        live_acc = self._daily_metrics[-1]["accuracy"] if self._daily_metrics else 0.0

        return {
            "shadow_trades": total,
            "shadow_wins": wins,
            "shadow_accuracy": round(wins / total, 4) if total > 0 else 0.0,
            "shadow_avg_confidence": round(avg_conf, 4),
            "live_trades": live_trades,
            "live_accuracy": live_acc,
            "shadow_vs_live_accuracy": (
                round((wins / total) - live_acc, 4) if total > 0 and live_acc > 0 else 0.0
            ),
        }

    # ── Settlement-Confirmed Accuracy (D3) ────────────────────

    def record_settlement(
        self,
        epoch_id: str,
        station: str,
        date: str,
        market_type: str,
        predicted_direction: str,
        predicted_confidence: float,
        settlement_value: float,
        prev_settlement_value: float,
        signal_name: str,
        lane: str = "directional",
    ) -> Dict:
        """
        Record a post-settlement accuracy check.

        Compares predicted direction against actual settlement outcome.
        This is the ground truth — pre-settlement accuracy is the prediction,
        settlement accuracy is the result.

        Args:
            epoch_id: Epoch identifier
            station: Station code
            date: ISO date
            market_type: 'HIGH' or 'LOW'
            predicted_direction: 'up' or 'down'
            predicted_confidence: Confidence at prediction time
            settlement_value: Actual settlement temperature
            prev_settlement_value: Previous settlement temp for comparison
            signal_name: Signal that generated this trade
            lane: Trading lane

        Returns:
            Dict with settlement accuracy result
        """
        # Determine actual direction from settlement data
        if settlement_value > prev_settlement_value:
            actual_direction = "up"
        elif settlement_value < prev_settlement_value:
            actual_direction = "down"
        else:
            actual_direction = "flat"

        was_correct = 1 if predicted_direction == actual_direction else 0

        entry = {
            "epoch_id": epoch_id,
            "station": station,
            "date": date,
            "market_type": market_type,
            "predicted_direction": predicted_direction,
            "predicted_confidence": round(predicted_confidence, 4),
            "settlement_value": settlement_value,
            "prev_settlement_value": prev_settlement_value,
            "actual_direction": actual_direction,
            "was_correct": was_correct,
            "signal_name": signal_name,
            "lane": lane,
        }
        self._settlement_entries.append(entry)

        # Persist
        conn = get_sqlite_connection(str(self.settlement_db_path))
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO settlement_accuracy
               (epoch_id, station, date, market_type, predicted_direction,
                predicted_confidence, settlement_value, prev_settlement_value,
                actual_direction, was_correct, signal_name, lane, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (epoch_id, station, date, market_type, predicted_direction,
             predicted_confidence, settlement_value, prev_settlement_value,
             actual_direction, was_correct, signal_name, lane,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()

        return entry

    def get_settlement_accuracy(
        self,
        signal_name: Optional[str] = None,
        station: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get settlement-confirmed accuracy statistics.

        Args:
            signal_name: Optional filter by signal
            station: Optional filter by station

        Returns:
            Dict with accuracy stats
        """
        conn = get_sqlite_connection(str(self.settlement_db_path))
        cur = conn.cursor()

        query = "SELECT COUNT(*), SUM(was_correct) FROM settlement_accuracy WHERE 1=1"
        params = []

        if signal_name:
            query += " AND signal_name = ?"
            params.append(signal_name)
        if station:
            query += " AND station = ?"
            params.append(station)

        cur.execute(query, params)
        row = cur.fetchone()
        total = row[0] or 0
        correct = row[1] or 0

        # Per-station breakdown
        cur.execute(
            """SELECT station, COUNT(*), SUM(was_correct), AVG(was_correct)
               FROM settlement_accuracy
               WHERE 1=1""" +
               (" AND signal_name = ?" if signal_name else "") +
               (" AND station = ?" if station else "") +
               " GROUP BY station ORDER BY AVG(was_correct) DESC",
            params
        )
        per_station = []
        for r in cur.fetchall():
            per_station.append({
                "station": r[0],
                "trades": r[1],
                "correct": r[2],
                "accuracy": round(r[3], 4) if r[3] else 0.0,
            })

        conn.close()

        return {
            "total_trades": total,
            "correct_trades": correct,
            "settlement_accuracy": round(correct / total, 4) if total > 0 else 0.0,
            "per_station": per_station,
        }

    def get_phase_progress(self) -> Dict[str, Any]:
        """Get the current phase progress summary."""
        if self.test_start_time is None:
            return {"status": "not_started"}

        elapsed_days = (time.time() - self.test_start_time) / 86400
        phase_info = PHASE_NAMES.get(self.current_phase, {})
        phase_elapsed = 0
        if self.phase_start_time:
            phase_elapsed = (time.time() - self.phase_start_time) / 86400

        return {
            "status": "halted" if self.is_halted else "running",
            "current_phase": self.current_phase,
            "phase_name": phase_info.get("name", ""),
            "phase_elapsed_days": round(phase_elapsed, 1),
            "phase_max_days": phase_info.get("days", 0),
            "total_elapsed_days": round(elapsed_days, 1),
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "stable_for_hours": round(
                (time.time() - self.phase_stable_since) / 3600, 1
            ) if self.phase_stable_since else 0,
            "pending_trades": len(self._shadow_trades) + len(self._daily_metrics),
        }

    # ── State Persistence ─────────────────────────────────────

    def _save_state(self) -> None:
        """Save current test state to file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "current_phase": self.current_phase,
                "test_start_time": self.test_start_time,
                "phase_start_time": self.phase_start_time,
                "phase_stable_since": self.phase_stable_since,
                "is_halted": self.is_halted,
                "halt_reason": self.halt_reason,
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save paper test state: {e}")

    def _load_state(self) -> None:
        """Load test state from file."""
        try:
            if self.state_file.exists():
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                self.current_phase = state.get("current_phase")
                self.test_start_time = state.get("test_start_time")
                self.phase_start_time = state.get("phase_start_time")
                self.phase_stable_since = state.get("phase_stable_since")
                self.is_halted = state.get("is_halted", False)
                self.halt_reason = state.get("halt_reason")
        except Exception as e:
            logger.debug(f"Failed to load paper test state: {e}")


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        ctrl = PaperTestController(
            db_path=f"{tmpdir}/test_paper.db",
            settlement_db_path=f"{tmpdir}/test_settlement.db",
            state_file=f"{tmpdir}/test_state.json",
        )

        # Test 1: Start Phase A
        assert ctrl.start_phase("A"), "Failed to start Phase A"
        assert ctrl.current_phase == "A"
        print(f"Test 1 PASS: Phase A started")

        # Test 2: Check stability (should fail — not enough time)
        stable, reason = ctrl.check_stability()
        assert not stable, "Expected unstable"
        print(f"Test 2 PASS: not yet stable: {reason}")

        # Test 3: Advance should fail (not stable)
        next_p = ctrl.advance_to_next_phase()
        assert next_p is None, "Should not advance without stability"
        assert ctrl.current_phase == "A", "Still in Phase A"
        print(f"Test 3 PASS: cannot advance without stability")

        # Test 4: Record daily metrics
        result = ctrl.record_daily_metrics(
            date="2026-07-24",
            trades=10,
            wins=7,
            losses=3,
            drawdown_pct=5.0,
            cumulative_pnl=150.0,
        )
        assert not result["halted"]
        print(f"Test 4 PASS: daily metrics recorded")

        # Test 5: Record shadow trade
        shadow = ctrl.record_shadow_trade(
            station="KATL",
            date="2026-07-24",
            direction="down",
            confidence=0.65,
        )
        assert shadow["is_shadow"]
        assert shadow["direction"] == "down"
        print(f"Test 5 PASS: shadow trade recorded")

        # Test 6: Shadow summary
        summary = ctrl.get_shadow_summary()
        assert summary["shadow_trades"] == 1
        print(f"Test 6 PASS: shadow summary={summary['shadow_trades']} trades")

        # Test 7: Record settlement
        settlement = ctrl.record_settlement(
            epoch_id="2026-07-24_KATL_HIGH",
            station="KATL",
            date="2026-07-24",
            market_type="HIGH",
            predicted_direction="up",
            predicted_confidence=0.70,
            settlement_value=88.0,
            prev_settlement_value=85.0,
            signal_name="gaussian",
        )
        assert settlement["actual_direction"] == "up"
        assert settlement["was_correct"] == 1
        print(f"Test 7 PASS: settlement accuracy recorded")

        # Test 8: Settlement accuracy query
        acc = ctrl.get_settlement_accuracy()
        assert acc["total_trades"] == 1
        assert acc["settlement_accuracy"] == 1.0
        print(f"Test 8 PASS: settlement accuracy={acc['settlement_accuracy']}")

        # Test 9: Kill threshold — drawdown > 20%
        result = ctrl.record_daily_metrics(
            date="2026-07-25",
            trades=5,
            wins=2,
            losses=3,
            drawdown_pct=25.0,
            cumulative_pnl=-200.0,
        )
        assert result["halted"], f"Expected halt, got {result}"
        assert ctrl.is_halted
        print(f"Test 9 PASS: drawdown halt triggered: {ctrl.halt_reason}")

        # Test 10: Get phase progress
        progress = ctrl.get_phase_progress()
        assert progress["status"] == "halted"
        assert progress["current_phase"] == "A"
        print(f"Test 10 PASS: phase progress={progress['status']}")

        # Test 11: Kill threshold — low accuracy
        ctrl2 = PaperTestController(
            db_path=f"{tmpdir}/test_paper2.db",
            settlement_db_path=f"{tmpdir}/test_settlement2.db",
            state_file=f"{tmpdir}/test_state2.json",
        )
        ctrl2.start_phase("A")
        result = ctrl2.record_daily_metrics(
            date="2026-07-24",
            trades=10,
            wins=4,
            losses=6,
            drawdown_pct=10.0,
            cumulative_pnl=-50.0,
        )
        assert result["halted"], f"Expected halt for low accuracy"
        print(f"Test 11 PASS: low accuracy halt: {ctrl2.halt_reason}")

        # Test 12: Phase B — spike reversion only in shadow
        ctrl3 = PaperTestController(
            db_path=f"{tmpdir}/test_paper3.db",
            settlement_db_path=f"{tmpdir}/test_settlement3.db",
            state_file=f"{tmpdir}/test_state3.json",
        )
        ctrl3.start_phase("B")
        lanes = ctrl3._get_phase_lanes("B")
        assert "directional" in lanes
        assert "spike_reversion_shadow" in lanes
        print(f"Test 12 PASS: Phase B lanes={lanes}")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()