#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Alert State Machine v1.0 — Phase 3.4 Alert Lifecycle Tracking

Full state machine for alert lifecycle management.

States:
  PENDING → ISSUED → DELIVERED → ACKNOWLEDGED → EXECUTED → SETTLED
                                                              ↓
                                                          EXPIRED

Tracks:
- Delivery confirmation (webhook response)
- Acknowledgment timer (configurable timeout)
- Settlement/expiry of the associated trade
- Failure mode recording at each transition

All state transitions are recorded in a SQLite table for complete audit trail.

Rejects state transitions that don't follow the valid sequence.

Scripts only — no AI/ML in the state machine.
"""

import sqlite3
import json
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Set
from pathlib import Path
from enum import Enum, auto

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DB_PATH = str(REPO_ROOT / "data" / "alert_state_machine.db")


# ─── Alert States ────────────────────────────────────────────────────────

class AlertState(Enum):
    """Valid states in the alert lifecycle."""
    PENDING = "PENDING"           # Alert created, not yet sent
    ISSUED = "ISSUED"             # Alert dispatched to delivery channel
    DELIVERED = "DELIVERED"       # Confirmed delivered (webhook 200)
    ACKNOWLEDGED = "ACKNOWLEDGED" # User/system acknowledged (optional)
    EXECUTED = "EXECUTED"         # Trade executed based on this alert
    SETTLED = "SETTLED"           # Trade settled (won/lost)
    EXPIRED = "EXPIRED"           # Alert expired without execution
    FAILED = "FAILED"             # Irrecoverable delivery failure


# ─── Valid State Transitions ─────────────────────────────────────────────
# Maps each state to the set of valid next states.

VALID_TRANSITIONS: Dict[AlertState, Set[AlertState]] = {
    AlertState.PENDING:    {AlertState.ISSUED, AlertState.FAILED},
    AlertState.ISSUED:     {AlertState.DELIVERED, AlertState.FAILED, AlertState.EXPIRED},
    AlertState.DELIVERED:  {AlertState.ACKNOWLEDGED, AlertState.EXECUTED, AlertState.EXPIRED},
    AlertState.ACKNOWLEDGED: {AlertState.EXECUTED, AlertState.EXPIRED},
    AlertState.EXECUTED:   {AlertState.SETTLED, AlertState.EXPIRED},
    AlertState.SETTLED:    set(),   # Terminal state
    AlertState.EXPIRED:    set(),   # Terminal state
    AlertState.FAILED:     set(),   # Terminal state
}


# ─── Default Timeouts ───────────────────────────────────────────────────

DEFAULT_ACK_TIMEOUT_SECONDS = 3600      # 1 hour to acknowledge
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 86400  # 24 hours to execute
DEFAULT_SETTLEMENT_TIMEOUT_DAYS = 7     # 7 days for settlement


# ─── Alert State Machine ─────────────────────────────────────────────────

class AlertStateMachine:
    """
    Tracks and manages the lifecycle of alert states.

    Each alert gets a unique alert_id and transitions through the state
    machine. All transitions are persisted to SQLite for auditability.

    Usage:
        asm = AlertStateMachine()
        alert_id = asm.create_alert("KATL", "HIGH", "UP", "test_alert_001")
        asm.transition(alert_id, AlertState.ISSUED, metadata={"channel": "discord"})
        asm.transition(alert_id, AlertState.DELIVERED)
        # ... etc
        state = asm.get_state(alert_id)
    """

    def __init__(self, db_path: str = DEFAULT_STATE_DB_PATH):
        """
        Args:
            db_path: Path to SQLite database for state persistence.
        """
        self._db_path = db_path
        self._ensure_schema()
        self._logger = logging.getLogger(f"{__name__}.AlertStateMachine")

    def _ensure_schema(self):
        """Create the alert_state table and history table."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            # Main state table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_states (
                    alert_id TEXT PRIMARY KEY,
                    station TEXT NOT NULL,
                    market TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_id TEXT,
                    current_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state_entered_at TEXT NOT NULL,
                    ack_timeout_seconds INTEGER DEFAULT 3600,
                    exec_timeout_seconds INTEGER DEFAULT 86400,
                    failure_reason TEXT,
                    metadata_json TEXT
                )
            """)
            # State transition history (append-only)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    transitioned_at TEXT NOT NULL,
                    reason TEXT,
                    metadata_json TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_history_id
                ON alert_state_history(alert_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_states_current
                ON alert_states(current_state)
            """)
            conn.commit()
        finally:
            conn.close()

    def create_alert(
        self,
        station: str,
        market: str,
        direction: str,
        signal_id: Optional[str] = None,
        ack_timeout: Optional[int] = None,
        exec_timeout: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new alert in PENDING state.

        Args:
            station: Station ICAO code
            market: Market type (HIGH, LOW)
            direction: Signal direction (UP, DOWN)
            signal_id: Unique signal identifier
            ack_timeout: Acknowledgment timeout in seconds (default 3600)
            exec_timeout: Execution timeout in seconds (default 86400)
            metadata: Additional metadata dict

        Returns:
            alert_id string
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        alert_id = f"{station}_{market}_{direction}_{int(time.time())}"

        # Ensure uniqueness
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            existing = conn.execute(
                "SELECT alert_id FROM alert_states WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            if existing:
                # Append a counter to make it unique
                counter = 1
                while True:
                    alt_id = f"{alert_id}_{counter}"
                    existing = conn.execute(
                        "SELECT alert_id FROM alert_states WHERE alert_id = ?",
                        (alt_id,)
                    ).fetchone()
                    if not existing:
                        alert_id = alt_id
                        break
                    counter += 1

            conn.execute("""
                INSERT INTO alert_states
                    (alert_id, station, market, direction, signal_id,
                     current_state, created_at, updated_at, state_entered_at,
                     ack_timeout_seconds, exec_timeout_seconds, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert_id, station, market, direction, signal_id,
                AlertState.PENDING.value, now_iso, now_iso, now_iso,
                ack_timeout or DEFAULT_ACK_TIMEOUT_SECONDS,
                exec_timeout or DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                json.dumps(metadata, sort_keys=True) if metadata else None,
            ))

            # Record initial state in history
            conn.execute("""
                INSERT INTO alert_state_history
                    (alert_id, from_state, to_state, transitioned_at, reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (alert_id, None, AlertState.PENDING.value, now_iso, "alert_created",
                  json.dumps(metadata, sort_keys=True) if metadata else None))

            conn.commit()
        finally:
            conn.close()

        self._logger.info("Alert created: %s (%s %s %s)", alert_id, station, market, direction)
        return alert_id

    def transition(
        self,
        alert_id: str,
        target_state: AlertState,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        Transition an alert to a new state.

        Validates the transition against the state machine. If the transition
        is invalid, returns (False, reason).

        Args:
            alert_id: Alert identifier
            target_state: Target AlertState enum value
            reason: Optional reason for the transition
            metadata: Optional metadata dict for the transition

        Returns:
            (success: bool, message: str)
        """
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            # Get current state
            row = conn.execute(
                "SELECT current_state FROM alert_states WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()

            if row is None:
                return False, f"Alert {alert_id} not found"

            current_state_str = row[0]

            # Parse current state
            try:
                current_state = AlertState(current_state_str)
            except ValueError:
                return False, f"Unknown current state: {current_state_str}"

            # Validate transition
            valid_next = VALID_TRANSITIONS.get(current_state, set())
            if target_state not in valid_next:
                return False, (
                    f"Invalid transition: {current_state.value} -> {target_state.value}. "
                    f"Valid next states: {[s.value for s in valid_next]}"
                )

            # Perform transition
            now_iso = datetime.now(timezone.utc).isoformat()
            metadata_json = json.dumps(metadata, sort_keys=True) if metadata else None

            conn.execute("""
                UPDATE alert_states
                SET current_state = ?, updated_at = ?, state_entered_at = ?,
                    failure_reason = ?
                WHERE alert_id = ?
            """, (target_state.value, now_iso, now_iso,
                  reason if target_state in (AlertState.FAILED, AlertState.EXPIRED) else None,
                  alert_id))

            # Record in history
            conn.execute("""
                INSERT INTO alert_state_history
                    (alert_id, from_state, to_state, transitioned_at, reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (alert_id, current_state_str, target_state.value, now_iso, reason, metadata_json))

            conn.commit()
            self._logger.info("Transition: %s %s -> %s (%s)", alert_id, current_state_str, target_state.value, reason or "no reason")
            return True, f"Transitioned {current_state_str} -> {target_state.value}"

        finally:
            conn.close()

    def get_state(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current state and all fields for an alert.

        Args:
            alert_id: Alert identifier

        Returns:
            Dict with all alert state fields, or None if not found
        """
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM alert_states WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_history(self, alert_id: str) -> List[Dict[str, Any]]:
        """
        Get full transition history for an alert.

        Args:
            alert_id: Alert identifier

        Returns:
            List of transition records in chronological order
        """
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM alert_state_history
                WHERE alert_id = ?
                ORDER BY id ASC
            """, (alert_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_alerts_by_state(self, state: AlertState, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get all alerts currently in a given state.

        Args:
            state: AlertState to filter by
            limit: Max results

        Returns:
            List of alert state records
        """
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM alert_states
                WHERE current_state = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (state.value, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_pending_acknowledgments(self) -> List[Dict[str, Any]]:
        """
        Get alerts that are DELIVERED but past the acknowledgment timeout.

        Returns:
            List of alerts past due for acknowledgment
        """
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM alert_states
                WHERE current_state = 'DELIVERED'
                  AND datetime(state_entered_at, '+' || ack_timeout_seconds || ' seconds') < ?
                ORDER BY state_entered_at ASC
            """, (now.isoformat(),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_expired_alerts(self) -> List[Dict[str, Any]]:
        """
        Get alerts that are past their execution timeout.

        Returns:
            List of alerts that should be expired
        """
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.row_factory = sqlite3.Row
            # Alerts in DELIVERED or ACKNOWLEDGED past execution timeout
            rows = conn.execute("""
                SELECT * FROM alert_states
                WHERE current_state IN ('DELIVERED', 'ACKNOWLEDGED')
                  AND datetime(state_entered_at, '+' || exec_timeout_seconds || ' seconds') < ?
                ORDER BY state_entered_at ASC
            """, (now.isoformat(),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def expire_stale_alerts(self) -> List[str]:
        """
        Auto-expire alerts that have passed their execution timeout.

        Called periodically (e.g., every hour) to clean up stale alerts.

        Returns:
            List of alert_ids that were expired
        """
        expired = self.get_expired_alerts()
        expired_ids = []
        for alert in expired:
            success, msg = self.transition(
                alert["alert_id"], AlertState.EXPIRED,
                reason="Execution timeout exceeded",
                metadata={"auto_expired": True, "timeout_seconds": alert.get("exec_timeout_seconds")}
            )
            if success:
                expired_ids.append(alert["alert_id"])
                self._logger.info("Auto-expired alert %s", alert["alert_id"])
        return expired_ids

    def get_stats(self) -> Dict[str, Any]:
        """
        Get aggregate statistics for the alert state machine.

        Returns:
            Dict with counts by state, total alerts, etc.
        """
        conn = sqlite3.connect(self._db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
        try:
            total = conn.execute("SELECT COUNT(*) FROM alert_states").fetchone()[0]

            state_counts = dict(conn.execute("""
                SELECT current_state, COUNT(*) FROM alert_states GROUP BY current_state
            """).fetchall())

            # Total transitions
            total_transitions = conn.execute(
                "SELECT COUNT(*) FROM alert_state_history"
            ).fetchone()[0]

            # Most recent alerts
            recent = conn.execute("""
                SELECT alert_id, station, market, direction, current_state, created_at
                FROM alert_states
                ORDER BY created_at DESC LIMIT 10
            """).fetchall()

            return {
                "total_alerts": total,
                "by_state": state_counts,
                "total_transitions": total_transitions,
                "recent": [dict(r) if hasattr(r, 'keys') else {
                    "alert_id": r[0], "station": r[1], "market": r[2],
                    "direction": r[3], "state": r[4], "created_at": r[5]
                } for r in recent],
            }
        finally:
            conn.close()

    def confirm_delivery(self, alert_id: str, webhook_status: int = 200) -> Tuple[bool, str]:
        """
        Convenience: transition from ISSUED to DELIVERED on successful webhook.

        Args:
            alert_id: Alert identifier
            webhook_status: HTTP status code from webhook delivery

        Returns:
            (success, message)
        """
        return self.transition(
            alert_id, AlertState.DELIVERED,
            reason=f"Webhook delivery confirmed (HTTP {webhook_status})",
            metadata={"webhook_status_code": webhook_status}
        )

    def acknowledge(self, alert_id: str, ack_source: str = "auto") -> Tuple[bool, str]:
        """
        Convenience: transition from DELIVERED to ACKNOWLEDGED.

        Args:
            alert_id: Alert identifier
            ack_source: Source of acknowledgment (e.g., "system", "user")

        Returns:
            (success, message)
        """
        return self.transition(
            alert_id, AlertState.ACKNOWLEDGED,
            reason=f"Acknowledged by {ack_source}",
            metadata={"ack_source": ack_source}
        )

    def mark_executed(self, alert_id: str, trade_uuid: str = None) -> Tuple[bool, str]:
        """
        Convenience: transition from DELIVERED or ACKNOWLEDGED to EXECUTED.

        Args:
            alert_id: Alert identifier
            trade_uuid: Associated trade UUID

        Returns:
            (success, message)
        """
        state = self.get_state(alert_id)
        if state and state.get("current_state") in ("DELIVERED", "ACKNOWLEDGED"):
            metadata = {"trade_uuid": trade_uuid} if trade_uuid else None
            return self.transition(
                alert_id, AlertState.EXECUTED,
                reason=f"Trade executed: {trade_uuid}" if trade_uuid else "Trade executed",
                metadata=metadata,
            )
        return False, f"Alert {alert_id} is in state {state.get('current_state') if state else 'unknown'}, cannot execute"

    def mark_settled(self, alert_id: str, pnl: float = None) -> Tuple[bool, str]:
        """
        Convenience: transition from EXECUTED to SETTLED.

        Args:
            alert_id: Alert identifier
            pnl: Realized P&L for this trade

        Returns:
            (success, message)
        """
        return self.transition(
            alert_id, AlertState.SETTLED,
            reason=f"Trade settled. PnL: {pnl:+.2f}" if pnl is not None else "Trade settled",
            metadata={"realized_pnl": pnl} if pnl is not None else None,
        )


# ─── Multi-Channel Delivery ──────────────────────────────────────────────
# Phase 4.2: Delivery routing with Primary (Discord), Secondary (Console),
# and Tertiary (SMS — future stub) channels.


class DeliveryChannel(Enum):
    """Delivery channel tiers for alert routing."""
    PRIMARY_DISCORD = "discord"       # Discord webhook (configured via env)
    SECONDARY_CONSOLE = "console"     # Console/log output (always on)
    TERTIARY_SMS_STUB = "sms_stub"    # SMS/critical-only (future, stub now)


# Delivery channel configuration
DELIVERY_CHANNEL_CONFIG = {
    DeliveryChannel.PRIMARY_DISCORD: {
        "label": "Discord Webhook",
        "enabled": True,
        "always_on": False,
        "critical_only": False,
        "night_mode_allowed": True,
    },
    DeliveryChannel.SECONDARY_CONSOLE: {
        "label": "Console/Log",
        "enabled": True,
        "always_on": True,
        "critical_only": False,
        "night_mode_allowed": True,
    },
    DeliveryChannel.TERTIARY_SMS_STUB: {
        "label": "SMS (Future)",
        "enabled": False,
        "always_on": False,
        "critical_only": True,
        "night_mode_allowed": True,
    },
}


class DeliveryRouter:
    """
    Routes alerts to the appropriate delivery channel(s).

    Primary: Discord webhook (env var WEBHOOK_PROD / WEBHOOK_DEV / WEBHOOK_SBOX)
    Secondary: Console/log output (always on)
    Tertiary: SMS/critical-only (future stub)
    Night mode routing: queues to digest instead of real-time
    """

    def __init__(self, instance: Optional[str] = None):
        if instance is None:
            self._instance = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
        else:
            self._instance = instance.upper()
        self._webhook_urls = {
            "PROD": os.getenv("WEBHOOK_PROD", ""),
            "DEV": os.getenv("WEBHOOK_DEV", ""),
            "SBOX": os.getenv("WEBHOOK_SBOX", ""),
        }
        self._logger = logging.getLogger(f"{__name__}.DeliveryRouter")

    def get_webhook_url(self) -> str:
        return self._webhook_urls.get(self._instance, "")

    def is_primary_available(self) -> bool:
        return bool(self.get_webhook_url())

    def route_alert(self, alert_data: Dict[str, Any],
                     is_critical: bool = False,
                     is_night_mode: bool = False,
                     night_mode_digest=None) -> Dict[str, Any]:
        """Route an alert through the appropriate delivery channels."""
        result = {
            "alert_id": alert_data.get("alert_id", "unknown"),
            "channels": {},
            "overall_delivered": False,
        }

        # Always log to console (Secondary channel)
        result["channels"][DeliveryChannel.SECONDARY_CONSOLE.value] = self._deliver_console(alert_data)

        # Handle night mode
        if is_night_mode and not is_critical:
            if night_mode_digest is not None:
                station = alert_data.get("station", "")
                market = alert_data.get("market_type", "")
                direction = alert_data.get("direction", "")
                confidence = alert_data.get("trade_confidence", 0.0)
                edge = alert_data.get("edge", 0.0)
                lane = alert_data.get("lane", "regular")
                night_mode_digest.queue_signal(station, market, direction, confidence, edge, lane)

            result["channels"][DeliveryChannel.PRIMARY_DISCORD.value] = {
                "status": "night_mode_queued",
                "message": "Alert queued for morning digest (night mode)",
            }
            result["overall_delivered"] = True
            return result

        # Deliver via Discord webhook (Primary channel)
        if self.is_primary_available():
            discord_result = self._deliver_discord(alert_data)
            result["channels"][DeliveryChannel.PRIMARY_DISCORD.value] = discord_result
            if discord_result.get("status") == "queued":
                result["overall_delivered"] = True
        else:
            result["channels"][DeliveryChannel.PRIMARY_DISCORD.value] = {
                "status": "not_configured",
                "message": "Discord webhook URL not configured for this instance",
            }

        # SMS stub (Tertiary channel) — future
        result["channels"][DeliveryChannel.TERTIARY_SMS_STUB.value] = {
            "status": "stub",
            "message": "SMS delivery not yet implemented (future)",
        }

        if not result["overall_delivered"]:
            self._logger.warning(
                "Alert %s primary delivery failed. Console delivery only.",
                result.get("alert_id")
            )

        return result

    def _deliver_console(self, alert_data: Dict[str, Any]) -> Dict[str, Any]:
        station = alert_data.get("station", "?")
        market = alert_data.get("market_type", "?")
        direction = alert_data.get("direction", "?")
        grade = alert_data.get("grade", "?")
        lane = alert_data.get("lane_label", "?")
        edge = alert_data.get("edge_pct", "?")
        conf = alert_data.get("trade_confidence", 0.0)
        market_prob = alert_data.get("market_prob", 0.0)

        print(f"[CONSOLE] {station}:{market} {direction} | Grade: {grade} | Lane: {lane} | Edge: {edge} | Conf: {conf:.0%} | Market: {market_prob:.2%}")
        self._logger.info(
            "Console delivery: %s/%s/%s (grade=%s, lane=%s)",
            station, market, direction, grade, lane
        )
        return {
            "status": "delivered",
            "channel": "console",
            "message": f"Delivered to console: {station}/{market}/{direction}",
        }

    def _deliver_discord(self, alert_data: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver alert to Discord via webhook."""
        webhook_url = self.get_webhook_url()
        if not webhook_url:
            return {"status": "failed", "channel": "discord", "message": "No webhook URL configured"}

        try:
            from core.alert_builder import format_alert_for_discord
            discord_payload = format_alert_for_discord(alert_data)

            from core.alert_retry_queue import _queue_alert_for_delivery
            queue_result = _queue_alert_for_delivery(
                webhook_url=webhook_url,
                payload=discord_payload,
                station=alert_data.get("station"),
                metadata={
                    "alert_id": alert_data.get("alert_id", "unknown"),
                    "grade": alert_data.get("grade"),
                    "lane": alert_data.get("lane"),
                }
            )

            self._logger.info(
                "Discord delivery queued: %s (entry_id=%s)",
                alert_data.get("station", "?"),
                queue_result.get("entry_id", "?")
            )
            return {
                "status": "queued",
                "channel": "discord",
                "entry_id": queue_result.get("entry_id"),
                "message": f"Queued for Discord delivery (entry_id={queue_result.get('entry_id')})",
            }

        except Exception as e:
            self._logger.error("Discord delivery failed: %s", e)
            return {"status": "failed", "channel": "discord", "message": f"Delivery error: {e}"}

    def deliver_heartbeat(self, heartbeat_message: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver a heartbeat message via Discord."""
        webhook_url = self.get_webhook_url()
        if not webhook_url:
            return {"status": "failed", "message": "No webhook URL configured"}

        try:
            from core.alert_retry_queue import _queue_alert_for_delivery
            queue_result = _queue_alert_for_delivery(
                webhook_url=webhook_url,
                payload=heartbeat_message,
                metadata={
                    "type": "heartbeat",
                    "health_status": heartbeat_message.get("health_status", "ok"),
                }
            )
            return {
                "status": "queued",
                "entry_id": queue_result.get("entry_id"),
                "message": "Heartbeat queued for Discord delivery",
            }
        except Exception as e:
            self._logger.error("Heartbeat delivery failed: %s", e)
            return {"status": "failed", "message": f"Heartbeat delivery error: {e}"}

    def deliver_morning_digest(self, digest_message: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver the morning digest via Discord."""
        webhook_url = self.get_webhook_url()
        if not webhook_url:
            return {"status": "failed", "message": "No webhook URL configured"}

        try:
            from core.alert_retry_queue import _queue_alert_for_delivery
            queue_result = _queue_alert_for_delivery(
                webhook_url=webhook_url,
                payload=digest_message,
                metadata={
                    "type": "morning_digest",
                    "total_suppressed": digest_message.get("total_suppressed", 0),
                }
            )
            return {
                "status": "queued",
                "entry_id": queue_result.get("entry_id"),
                "message": "Morning digest queued for Discord delivery",
            }
        except Exception as e:
            self._logger.error("Digest delivery failed: %s", e)
            return {"status": "failed", "message": f"Digest delivery error: {e}"}


# ─── Module-level convenience functions ──────────────────────────────────

_ASM: Optional[AlertStateMachine] = None


def get_state_machine(db_path: Optional[str] = None) -> AlertStateMachine:
    """Get or create the module-level AlertStateMachine singleton."""
    global _ASM
    if _ASM is None:
        _ASM = AlertStateMachine(db_path or DEFAULT_STATE_DB_PATH)
    return _ASM


def create_alert(
    station: str,
    market: str,
    direction: str,
    signal_id: Optional[str] = None,
) -> str:
    """Convenience: create a new alert."""
    return get_state_machine().create_alert(station, market, direction, signal_id)


def transition_alert(alert_id: str, target_state: AlertState, reason: str = None) -> Tuple[bool, str]:
    """Convenience: transition an alert to a new state."""
    return get_state_machine().transition(alert_id, target_state, reason)


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_path = f.name

    try:
        asm = AlertStateMachine(test_path)
        alert_id = asm.create_alert("KATL", "HIGH", "UP", "test_001")
        print(f"Created alert: {alert_id}")

        # Full lifecycle
        asm.transition(alert_id, AlertState.ISSUED, "sending to discord")
        asm.transition(alert_id, AlertState.DELIVERED, "webhook 200")
        asm.transition(alert_id, AlertState.ACKNOWLEDGED, "acknowledged by system")
        asm.transition(alert_id, AlertState.EXECUTED, "trade placed")
        asm.transition(alert_id, AlertState.SETTLED, "settled +$50")

        state = asm.get_state(alert_id)
        print(f"Final state: {state['current_state'] if state else 'not found'}")

        history = asm.get_history(alert_id)
        print(f"Transitions: {len(history)}")
        for h in history:
            print(f"  {h['from_state']} -> {h['to_state']}: {h['reason']}")

        # Test invalid transition
        success, msg = asm.transition(alert_id, AlertState.PENDING)
        print(f"Invalid transition result: {success}, {msg}")

        stats = asm.get_stats()
        print(f"Stats: {stats}")

        print("Alert state machine OK")
    finally:
        os.unlink(test_path)