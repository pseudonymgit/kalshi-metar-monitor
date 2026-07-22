#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
#

"""
Alert Frequency Throttle v1.0 — Phase 5.1

SQLite-backed alert throttling system to enforce per-station cooldown
timer:
- 4h Regular
- 8h Sure Thing
- 12h Goldilocks

Cooldown resets only on signal state transition (e.g., direction change,
confidence crossing threshold). Tracks last_alerted_at, last_state,
alert_count_24h.

Scripts only — no AI/ML in the throttle loop.
"""

import sqlite3
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, Any
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THROTTLE_DB_PATH = str(REPO_ROOT / "data" / "alert_throttle.db")


class AlertThrottle:
    """
    Per-station alert throttling with varying cooldown periods by alert type.

    Cooldown periods:
    - Regular: 4 hours
    - Sure Thing: 8 hours  
    - Goldilocks: 12 hours

    Throttling resets only on signal state transition.
    """

    def __init__(self, db_path: str = DEFAULT_THROTTLE_DB_PATH):
        """
        Initialize the alert throttle.

        Args:
            db_path: Path to SQLite database file. Defaults to data/alert_throttle.db.
        """
        self._db_path = db_path
        self._cooldows_periods = {
            'regular': 4 * 3600,      # 4 hours
            'sure_thing': 8 * 3600,   # 8 hours
            'goldilocks': 12 * 3600,  # 12 hours
        }
        self._ensure_schema()
        self._logger = logging.getLogger(f"{__name__}.AlertThrottle")

    def _ensure_schema(self):
        """Create the alert_throttle table if it doesn't exist."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = get_sqlite_connection(self._db_path)
        try:
            # Main throttle tracking table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_throttle (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    last_alerted_at TEXT,
                    last_state TEXT,
                    alert_count_24h INTEGER DEFAULT 0,
                    last_count_reset TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_station_alert_type
                ON alert_throttle(station, alert_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_last_alerted
                ON alert_throttle(last_alerted_at)
            """)
            
            # Cleanup trigger: prune counts daily
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS cleanup_old_counts
                AFTER UPDATE ON alert_throttle
                FOR EACH ROW
                WHEN strftime('%Y-%m-%d', 'now') > strftime('%Y-%m-%d', OLD.last_count_reset)
                BEGIN
                    UPDATE alert_throttle
                    SET alert_count_24h = 0,
                        last_count_reset = datetime('now')
                    WHERE id = NEW.id;
                END
            """)
            conn.commit()
        finally:
            conn.close()

    def _normalize_alert_type(self, alert_type: str) -> str:
        """Normalize alert types for consistent lookup."""
        alert_map = {
            'regular': 'regular',
            'Regular': 'regular',
            'REGULAR': 'regular',
            'sure_thing': 'sure_thing',
            'Sure Thing': 'sure_thing',
            'SURE_THING': 'sure_thing',
            'goldilocks': 'goldilocks',
            'Goldilocks': 'goldilocks',
            'GOLDILOCKS': 'goldilocks',
        }
        return alert_map.get(alert_type, 'regular')

    def should_send_alert(self, station: str, alert_type: str, current_state: Any) -> bool:
        """
        Check if an alert should be sent for this station, type, and state.

        Args:
            station: Station ICAO code
            alert_type: 'regular', 'sure_thing', or 'goldilocks'
            current_state: Any object representing the current signal state (converted to string)

        Returns:
            True if alert should be sent, False if throttled
        """
        alert_type_norm = self._normalize_alert_type(alert_type)
        
        # Get the current entry for this station/type
        current_entry = self._get_throttle_entry(station, alert_type_norm)
        
        if current_entry is None:
            # No existing entry - allow alert and create initial record
            self._set_throttle_entry(station, alert_type_norm, str(current_state))
            self._log_throttle_action(station, alert_type_norm, "first_alert", "Allowing first alert for station.")
            return True

        last_alerted_at_str = current_entry.get('last_alerted_at')
        last_state_str = current_entry.get('last_state')
        current_time = datetime.now(timezone.utc)
        
        # Convert input to string for comparison
        current_state_str = str(current_state)
        
        # Check for state transition
        state_changed = last_state_str != current_state_str
        
        if state_changed:
            # State transition - allow and update state
            self._update_throttle_entry(station, alert_type_norm, current_state_str, reset_count=True)
            self._log_throttle_action(
                station, alert_type_norm, "state_change_allow", 
                f"State transition from '{last_state_str}' to '{current_state_str}' - allowing alert." 
            )
            return True

        # Same state - check timeout period
        if last_alerted_at_str:
            try:
                last_alerted = datetime.fromisoformat(last_alerted_at_str.replace('Z', '+00:00'))
                
                # Calculate cooldown period in seconds
                cooldown_seconds = self._cooldows_periods[alert_type_norm]
              
                # Check if cooldown has passed  
                if (current_time - last_alerted).total_seconds() >= cooldown_seconds:
                    # Cooldown expired - allow and update
                    self._update_throttle_entry(station, alert_type_norm, current_state_str)
                    self._log_throttle_action(
                        station, alert_type_norm, "cooldown_expired",
                        f"Cooldown expired ({cooldown_seconds}s) - allowing alert."
                    )
                    return True
                else:
                    # Still in cooldown
                    self._record_alert_attempt(station, alert_type_norm)
                    self._log_throttle_action(
                        station, alert_type_norm, "throttled",
                        f"Still in cooldown (need {cooldown_seconds}s from last alert)."
                    )
                    return False
            except ValueError:
                # Invalid datetime format - treat as expired
                self._update_throttle_entry(station, alert_type_norm, current_state_str)
                return True
        else:
            # Invalid last_alerted_at - treat as cooldown expired
            self._update_throttle_entry(station, alert_type_norm, current_state_str)
            return True

    def _get_throttle_entry(self, station: str, alert_type: str) -> Optional[Dict[str, Any]]:
        """Retrieve the current throttle entry for a station and alert type."""
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM alert_throttle
                WHERE station = ? AND alert_type = ?
            """, (station, alert_type)).fetchone()
            
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def _set_throttle_entry(self, station: str, alert_type: str, current_state: str):
        """Set initial throttle entry."""
        current_time = datetime.now(timezone.utc).isoformat()
        
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.execute("""
                INSERT INTO alert_throttle
                (station, alert_type, last_alerted_at, last_state, alert_count_24h, 
                 last_count_reset, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                station, alert_type, current_time, current_state, 1,
                current_time, current_time, current_time
            ))
            conn.commit()
        finally:
            conn.close()

    def _update_throttle_entry(self, station: str, alert_type: str, current_state: str, reset_count: bool = False):
        """Update an existing throttle entry."""
        current_time = datetime.now(timezone.utc).isoformat()
        
        conn = get_sqlite_connection(self._db_path)
        try:
            if not reset_count:
                # Just update time and state, keep count
                conn.execute("""
                    UPDATE alert_throttle
                    SET last_alerted_at = ?, last_state = ?, 
                        updated_at = ?
                    WHERE station = ? AND alert_type = ?
                """, (current_time, current_state, current_time, station, alert_type))
            else:
                # Update and increment count
                conn.execute("""
                    UPDATE alert_throttle
                    SET last_alerted_at = ?, last_state = ?, alert_count_24h = ?, 
                        last_count_reset = ?, updated_at = ?
                    WHERE station = ? AND alert_type = ?
                """, (
                    current_time, current_state, 1, 
                    current_time, current_time, station, alert_type
                ))
            conn.commit()
        finally:
            conn.close()

    def _record_alert_attempt(self, station: str, alert_type: str):
        """Record that an alert was attempted but throttled."""
        current_time = datetime.now(timezone.utc).isoformat()
        
        # Check if we need to reset the daily counter
        current_entry = self._get_throttle_entry(station, alert_type)
        reset_daily_count = False
        
        if current_entry and current_entry.get('last_count_reset'):
            # Check if it's a new day
            last_reset_date = current_entry['last_count_reset'][:10]
            today_date = current_time[:10]
            reset_daily_count = last_reset_date != today_date
            
        conn = get_sqlite_connection(self._db_path)
        try:
            if reset_daily_count:
                conn.execute("""
                    UPDATE alert_throttle
                    SET alert_count_24h = 1, last_count_reset = ?, updated_at = ?
                    WHERE station = ? AND alert_type = ?
                """, (current_time, current_time, station, alert_type))
            else:
                conn.execute("""
                    UPDATE alert_throttle
                    SET alert_count_24h = alert_count_24h + 1, updated_at = ?
                    WHERE station = ? AND alert_type = ?
                """, (current_time, station, alert_type))
            conn.commit()
        finally:
            conn.close()

    def _log_throttle_action(self, station: str, alert_type: str, action: str, message: str):
        """Log throttle actions."""
        log_level = logging.WARNING if action == "throttled" else logging.DEBUG
        self._logger.log(log_level, f"[THROTTLE] {station}|{alert_type}|{action}: {message}")

    def get_throttle_status(self, station: str, alert_type: str) -> Dict[str, Any]:
        """
        Get current throttle status for a station/alert type.

        Returns status information about the current throttle state.
        """
        alert_type_norm = self._normalize_alert_type(alert_type)
        entry = self._get_throttle_entry(station, alert_type_norm)
        
        if not entry:
            return {
                'station': station,
                'alert_type': alert_type_norm,
                'is_active': False,
                'next_window_seconds': 0,
                'status': 'not_tracked',
                'count_24h': 0,
            }

        current_time = datetime.now(timezone.utc)
        cooldown_seconds = self._cooldows_periods[alert_type_norm]
        
        if entry.get('last_alerted_at'):
            try:
                last_alerted = datetime.fromisoformat(entry['last_alerted_at'].replace('Z', '+00:00'))
                elapsed = (current_time - last_alerted).total_seconds()
                remaining = max(0, cooldown_seconds - elapsed)
            except ValueError:
                remaining = 0
        else:
            remaining = 0

        is_active = remaining > 0 and entry.get('last_state') is not None
        status = 'cooling_down' if is_active else 'ready'
        
        return {
            'station': station,
            'alert_type': alert_type_norm,
            'is_active': is_active,
            'next_window_seconds': remaining,
            'status': status,
            'last_alerted_at': entry.get('last_alerted_at'),
            'last_state': entry.get('last_state'),
            'count_24h': entry.get('alert_count_24h', 0),
        }


# Module-level convenience function
_THROTTLE: Optional[AlertThrottle] = None


def get_alert_throttle(db_path: Optional[str] = None) -> AlertThrottle:
    """Get or create the module-level AlertThrottle singleton."""
    global _THROTTLE
    if _THROTTLE is None:
        _THROTTLE = AlertThrottle(db_path or DEFAULT_THROTTLE_DB_PATH)
    return _THROTTLE


def should_send_alert(station: str, alert_type: str, current_state: Any) -> bool:
    """Convenience: check if an alert should be sent."""
    throttle = get_alert_throttle()
    return throttle.should_send_alert(station, alert_type, current_state)


# Self-test
if __name__ == "__main__":
    import tempfile
    import time

    # Create temporary database for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_path = f.name

    try:
        print("Testing AlertThrottle...")
        
        # Adjust for testing with shorter timeouts
        throttle = AlertThrottle(test_path)
        # Mock shorter cooldown times for testing
        throttle._cooldows_periods = {
            'regular': 5,  # 5 seconds
            'sure_thing': 10,  # 10 seconds
            'goldilocks': 15,  # 15 seconds
        }
        
        # Test basic functionality
        print("Allowing first alert for KATL regular...")
        allowed = should_send_alert("KATL", "regular", {"signal": "high_temp", "confidence": 0.75})
        print(f"First alert allowed: {allowed}")
        
        print("Trying second alert within cooldown...")
        allowed = should_send_alert("KATL", "regular", {"signal": "high_temp", "confidence": 0.75})
        print(f"Second alert allowed: {allowed}")
        
        print("Trying third alert with different state...")
        allowed = should_send_alert("KATL", "regular", {"signal": "high_temp", "confidence": 0.90})  # Different state
        print(f"Different state alert allowed: {allowed}")
        
        # Wait for cooldown to test expiration
        print("Waiting for cooldown to expire...")
        time.sleep(6)  # Sleep longer than our test timeout (5 seconds)
        
        print("Trying alert after cooldown...")
        allowed = should_send_alert("KATL", "regular", {"signal": "high_temp", "confidence": 0.75})
        print(f"After cooldown alert allowed: {allowed}")
        
        # Test status
        status = throttle.get_throttle_status("KATL", "regular")
        print(f"Status after tests: {status}")

        print("AlertThrottle tests complete.")
    finally:
        import os
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
        os.unlink(test_path)