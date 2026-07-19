#!/usr/bin/env python3
"""
Heartbeat v1.0 — Phase 4.2 System Health Heartbeat

Sends periodic status heartbeats to monitor system health.

Tiers:
  🟢 OK — System running normally
  🟡 WARNING — METAR data stale
  🔴 ERROR — Errors detected in last hour

Heartbeat interval: 60 minutes (configurable)
Tracks last heartbeat time to avoid duplicates.

Scripts only — no AI/ML in the heartbeat loop.
"""

import os
import logging
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Default heartbeat interval: 60 minutes
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 3600

# Staleness thresholds
METAR_STALE_HOURS = 2  # Warn if last METAR collection was > 2 hours ago


# ─── Heartbeat State Tracking ───────────────────────────────────────────

class HeartbeatSender:
    """
    Periodic health heartbeat sender.

    Tracks last heartbeat time to avoid sending duplicates within the interval.
    Builds status messages based on current system health.
    Supports primary (Discord webhook), secondary (console/log), and
    tertiary (SMS — future stub) delivery tiers.

    Usage:
        hb = HeartbeatSender()
        hb.update_metrics(signals_evaluated=42, alerts_pending=3, errors=[])
        hb.maybe_send_heartbeat()  # Sends only if interval has elapsed
    """

    def __init__(self, interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS):
        """
        Args:
            interval_seconds: Minimum interval between heartbeats in seconds.
        """
        self._interval_seconds = interval_seconds
        self._last_heartbeat_time: Optional[float] = None
        self._logger = logging.getLogger(f"{__name__}.HeartbeatSender")

        # Metrics state
        self._signals_evaluated: int = 0
        self._alerts_pending: int = 0
        self._alerts_issued: int = 0
        self._errors: List[str] = []
        self._last_metar_collection_time: Optional[float] = None
        self._last_metar_collection_str: Optional[str] = None

        # Active health state
        self._health_status: str = "ok"  # "ok", "warning", "error"

    def update_metrics(self, signals_evaluated: int = 0, alerts_pending: int = 0,
                       alerts_issued: int = 0, errors: Optional[List[str]] = None,
                       last_metar_collection: Optional[str] = None):
        """
        Update heartbeat metrics for the next heartbeat.

        Args:
            signals_evaluated: Number of signals evaluated in the current cycle
            alerts_pending: Number of alerts pending delivery
            alerts_issued: Number of alerts issued in the current cycle
            errors: List of error messages from the current cycle
            last_metar_collection: ISO timestamp of last METAR data collection
        """
        self._signals_evaluated = signals_evaluated
        self._alerts_pending = alerts_pending
        self._alerts_issued = alerts_issued
        self._errors = errors or []

        if last_metar_collection:
            self._last_metar_collection_str = last_metar_collection
            try:
                self._last_metar_collection_time = datetime.fromisoformat(
                    last_metar_collection
                ).timestamp()
            except (ValueError, TypeError):
                self._last_metar_collection_time = None

        # Update health status
        if self._errors:
            self._health_status = "error"
        elif self._is_metar_stale():
            self._health_status = "warning"
        else:
            self._health_status = "ok"

    def _is_metar_stale(self) -> bool:
        """Check if METAR data is stale."""
        if self._last_metar_collection_time is None:
            return False  # Unknown, don't warn
        elapsed_hours = (time.time() - self._last_metar_collection_time) / 3600
        return elapsed_hours > METAR_STALE_HOURS

    def _get_metar_stale_hours(self) -> float:
        """Get hours since last METAR collection."""
        if self._last_metar_collection_time is None:
            return 0.0
        return (time.time() - self._last_metar_collection_time) / 3600

    def create_heartbeat_message(self) -> Dict[str, Any]:
        """
        Build the heartbeat status message.

        Produces three tiers based on health:
          🟢 OK — "System OK — N signals evaluated — M alerts pending — 0 errors"
          🟡 WARNING — "System OK — METAR data stale (N hours) — N signals — M alerts"
          🔴 ERROR — "System ERROR — {error summary} — check logs"

        Returns:
            Dict with Discord embed structure for the heartbeat
        """
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

        if self._health_status == "error":
            # 🔴 ERROR
            error_summary = "; ".join(self._errors[:3])  # Show top 3 errors
            if len(self._errors) > 3:
                error_summary += f" (+{len(self._errors) - 3} more)"

            embed = {
                "title": "🔴 System ERROR",
                "description": f"Errors detected in the last hour.",
                "color": 0xFF0000,  # Red
                "fields": [
                    {"name": "Errors", "value": error_summary, "inline": False},
                    {"name": "Signals Evaluated", "value": str(self._signals_evaluated), "inline": True},
                    {"name": "Alerts Pending", "value": str(self._alerts_pending), "inline": True},
                    {"name": "Alerts Issued", "value": str(self._alerts_issued), "inline": True},
                ],
                "footer": {"text": f"Weather Engine Heartbeat • {now_utc}"},
            }

            return {
                "content": None,
                "embeds": [embed],
                "health_status": "error",
                "summary": f"System ERROR — {error_summary} — check logs",
            }

        elif self._health_status == "warning":
            # 🟡 WARNING: stale METAR data
            stale_hours = self._get_metar_stale_hours()
            embed = {
                "title": "🟡 System OK — Data Stale",
                "description": f"METAR data is stale ({stale_hours:.1f} hours).",
                "color": 0xFFA500,  # Orange
                "fields": [
                    {"name": "METAR Staleness", "value": f"{stale_hours:.1f} hours", "inline": True},
                    {"name": "Signals Evaluated", "value": str(self._signals_evaluated), "inline": True},
                    {"name": "Alerts Pending", "value": str(self._alerts_pending), "inline": True},
                    {"name": "Alerts Issued", "value": str(self._alerts_issued), "inline": True},
                    {"name": "Errors", "value": "0", "inline": True},
                ],
                "footer": {"text": f"Weather Engine Heartbeat • {now_utc}"},
            }

            return {
                "content": None,
                "embeds": [embed],
                "health_status": "warning",
                "summary": f"🟡 System OK — METAR data stale ({stale_hours:.1f}h) — {self._signals_evaluated} signals — {self._alerts_pending} alerts",
            }

        else:
            # 🟢 OK
            embed = {
                "title": "🟢 System OK",
                "description": "All systems nominal.",
                "color": 0x00FF00,  # Green
                "fields": [
                    {"name": "Signals Evaluated", "value": str(self._signals_evaluated), "inline": True},
                    {"name": "Alerts Pending", "value": str(self._alerts_pending), "inline": True},
                    {"name": "Alerts Issued", "value": str(self._alerts_issued), "inline": True},
                    {"name": "Errors", "value": "0", "inline": True},
                ],
                "footer": {"text": f"Weather Engine Heartbeat • {now_utc}"},
            }

            return {
                "content": None,
                "embeds": [embed],
                "health_status": "ok",
                "summary": f"🟢 System OK — {self._signals_evaluated} signals evaluated — {self._alerts_pending} alerts pending — 0 errors",
            }

    def maybe_send_heartbeat(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Send heartbeat if the interval has elapsed since last send.

        Returns:
            Tuple of (sent: bool, message: dict or None)
        """
        now = time.time()

        if self._last_heartbeat_time is not None:
            elapsed = now - self._last_heartbeat_time
            if elapsed < self._interval_seconds:
                remaining = self._interval_seconds - elapsed
                self._logger.debug(
                    "Heartbeat skipped — last was %.0fs ago (interval %ds, %.0fs remaining)",
                    elapsed, self._interval_seconds, remaining
                )
                return False, None

        # Build and record heartbeat
        message = self.create_heartbeat_message()
        self._last_heartbeat_time = now

        self._logger.info(
            "Heartbeat sent: %s",
            message.get("summary", "unknown")
        )
        return True, message

    def force_send_heartbeat(self) -> Dict[str, Any]:
        """
        Force send a heartbeat regardless of interval.

        Useful for manual status checks or initial startup.

        Returns:
            Heartbeat message dict
        """
        message = self.create_heartbeat_message()
        self._last_heartbeat_time = time.time()
        self._logger.info(
            "Forced heartbeat sent: %s",
            message.get("summary", "unknown")
        )
        return message

    def get_status(self) -> Dict[str, Any]:
        """
        Get current heartbeat status without sending.

        Returns:
            Dict with current metrics and health status
        """
        return {
            "health_status": self._health_status,
            "signals_evaluated": self._signals_evaluated,
            "alerts_pending": self._alerts_pending,
            "alerts_issued": self._alerts_issued,
            "errors": self._errors,
            "last_heartbeat_time": self._last_heartbeat_time,
            "interval_seconds": self._interval_seconds,
            "metar_stale": self._is_metar_stale(),
            "metar_stale_hours": self._get_metar_stale_hours() if self._is_metar_stale() else 0,
        }

    def reset_interval(self):
        """Reset the heartbeat timer (force next send to be immediate)."""
        self._last_heartbeat_time = None
        self._logger.debug("Heartbeat interval reset")


# ─── Module-level singleton ─────────────────────────────────────────────

_HEARTBEAT: Optional[HeartbeatSender] = None


def get_heartbeat_sender(interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS) -> HeartbeatSender:
    """Get or create the module-level HeartbeatSender singleton."""
    global _HEARTBEAT
    if _HEARTBEAT is None:
        _HEARTBEAT = HeartbeatSender(interval_seconds)
    return _HEARTBEAT


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test heartbeat sender
    hb = HeartbeatSender(interval_seconds=10)  # Short interval for testing

    # Test 🟢 OK
    hb.update_metrics(
        signals_evaluated=42,
        alerts_pending=3,
        alerts_issued=2,
        errors=[],
        last_metar_collection=datetime.now(timezone.utc).isoformat(),
    )
    sent, msg = hb.maybe_send_heartbeat()
    print(f"Sent: {sent}")
    if sent:
        print(f"  Title: {msg['embeds'][0]['title']}")
        print(f"  Summary: {msg['summary']}")

    # Test duplicate skip
    sent, msg = hb.maybe_send_heartbeat()
    print(f"Duplicate skip: {not sent}")

    # Test 🟡 WARNING — stale METAR
    hb.reset_interval()
    hb.update_metrics(
        signals_evaluated=10,
        alerts_pending=1,
        alerts_issued=0,
        errors=[],
        last_metar_collection=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
    )
    sent, msg = hb.maybe_send_heartbeat()
    print(f"\nWARNING sent: {sent}")
    if sent:
        print(f"  Title: {msg['embeds'][0]['title']}")
        print(f"  Summary: {msg['summary']}")

    # Test 🔴 ERROR
    hb.reset_interval()
    hb.update_metrics(
        signals_evaluated=5,
        alerts_pending=0,
        alerts_issued=0,
        errors=["Webhook timeout", "Kalshi API rate limit exceeded"],
        last_metar_collection=datetime.now(timezone.utc).isoformat(),
    )
    sent, msg = hb.maybe_send_heartbeat()
    print(f"\nERROR sent: {sent}")
    if sent:
        print(f"  Title: {msg['embeds'][0]['title']}")
        print(f"  Summary: {msg['summary']}")

    print("\nHeartbeat module OK")