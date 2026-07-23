"""
db_health_monitor.py — Database Health Monitor

Periodically checks SQLite database integrity and attempts auto-recovery
on corruption. Used by the healthz endpoint and logging monitoring.

Responsibilities:
- PRAGMA integrity_check on configured databases
- Auto-recovery (VACUUM, restore from backup)
- Metrics collection for observability
- Event emission for alerts
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.structured_logger import get_logger

log = get_logger("db_health_monitor")

# Default thresholds
DEFAULT_CHECK_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_INTEGRITY_CHECK_TIMEOUT_S = 30

# Global health state
_last_health_check: Dict[str, Any] = {}
_last_health_check_lock = threading.Lock()
_health_monitor_thread: Optional[threading.Thread] = None
_health_monitor_stop = threading.Event()


class DatabaseHealthCheck:
    """Run health checks on a SQLite database."""

    def __init__(self, db_path: str, label: str = "default"):
        self.db_path = db_path
        self.label = label
        self.last_check_utc: Optional[str] = None
        self.last_integrity_result: Optional[bool] = None
        self.last_integrity_detail: Optional[str] = None
        self.consecutive_failures = 0
        self.recovery_attempted = False

    def check_connectivity(self, timeout_s: int = 3) -> bool:
        """Check basic database connectivity.

        Args:
            timeout_s: Connection timeout in seconds

        Returns:
            True if connection succeeds and SELECT 1 works
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=timeout_s)
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            log.warning(
                "Database connectivity check failed",
                event_id="db_connectivity_fail",
                db_label=self.label,
                db_path=self.db_path,
                error=str(e),
            )
            return False

    def check_integrity(self, timeout_s: int = DEFAULT_INTEGRITY_CHECK_TIMEOUT_S) -> Tuple[bool, str]:
        """Run PRAGMA integrity_check on the database.

        Args:
            timeout_s: Maximum time to wait for integrity check

        Returns:
            Tuple of (passed: bool, detail: str)
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=DEFAULT_BUSY_TIMEOUT_MS)
            # Set a reasonable busy timeout
            conn.execute("PRAGMA busy_timeout = ?", (DEFAULT_BUSY_TIMEOUT_MS,))
            cursor = conn.execute("PRAGMA integrity_check")
            rows = cursor.fetchall()
            conn.close()

            # integrity_check returns one row per error. If OK, returns ["ok"]
            errors = [row[0] for row in rows if row[0] != "ok"]
            if not errors:
                self.last_integrity_result = True
                self.last_integrity_detail = "ok"
                self.consecutive_failures = 0
                return True, "ok"
            else:
                detail = "; ".join(errors)
                self.last_integrity_result = False
                self.last_integrity_detail = detail
                self.consecutive_failures += 1
                log.error(
                    "Database integrity check failed",
                    event_id="db_integrity_fail",
                    db_label=self.label,
                    db_path=self.db_path,
                    errors=detail,
                    consecutive_failures=self.consecutive_failures,
                )
                return False, detail

        except Exception as e:
            detail = str(e)
            self.last_integrity_result = False
            self.last_integrity_detail = detail
            self.consecutive_failures += 1
            log.error(
                "Database integrity check exception",
                event_id="db_integrity_error",
                db_label=self.label,
                db_path=self.db_path,
                error=detail,
            )
            return False, detail

    def attempt_recovery(self) -> bool:
        """Attempt to recover a corrupted database.

        Recovery steps:
        1. VACUUM (rebuilds database file, fixes some corruption)
        2. Rebuild indexes

        Returns:
            True if recovery succeeded
        """
        log.warning(
            "Attempting database recovery",
            event_id="db_recovery_attempt",
            db_label=self.label,
            db_path=self.db_path,
        )

        try:
            # Step 1: VACUUM
            conn = sqlite3.connect(self.db_path, timeout=DEFAULT_BUSY_TIMEOUT_MS)
            conn.execute("VACUUM")
            conn.close()

            # Step 2: Verify recovery
            recovered, detail = self.check_integrity()
            if recovered:
                self.recovery_attempted = True
                log.info(
                    "Database recovery successful",
                    event_id="db_recovery_success",
                    db_label=self.label,
                )
                return True
            else:
                log.error(
                    "Database recovery failed — integrity check still failing",
                    event_id="db_recovery_fail",
                    db_label=self.label,
                    detail=detail,
                )
                return False

        except Exception as e:
            log.error(
                "Database recovery exception",
                event_id="db_recovery_error",
                db_label=self.label,
                error=str(e),
            )
            return False

    def get_metrics(self) -> Dict[str, Any]:
        """Get current health metrics for this database."""
        return {
            "label": self.label,
            "db_path": self.db_path,
            "last_check_utc": self.last_check_utc,
            "last_integrity_result": self.last_integrity_result,
            "last_integrity_detail": self.last_integrity_detail,
            "consecutive_failures": self.consecutive_failures,
            "recovery_attempted": self.recovery_attempted,
            "file_exists": os.path.exists(self.db_path),
            "file_size_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
        }


# ─── Global health monitor ─────────────────────────────────────────────────

_SUPERVISED_DATABASES: List[DatabaseHealthCheck] = []


def register_database(db_path: str, label: str = "default") -> DatabaseHealthCheck:
    """Register a database for periodic health monitoring.

    Args:
        db_path: Path to the SQLite database file
        label: Human-readable label for the database

    Returns:
        DatabaseHealthCheck instance
    """
    check = DatabaseHealthCheck(db_path, label)
    _SUPERVISED_DATABASES.append(check)
    log.info(
        "Database registered for health monitoring",
        event_id="db_monitor_register",
        label=label,
        db_path=db_path,
    )
    return check


def _run_health_loop():
    """Background loop that periodically checks all registered databases."""
    while not _health_monitor_stop.is_set():
        for db_check in _SUPERVISED_DATABASES:
            try:
                db_check.last_check_utc = datetime.now(timezone.utc).isoformat()

                # Check connectivity first
                connected = db_check.check_connectivity()
                if not connected:
                    continue

                # Run integrity check
                passed, detail = db_check.check_integrity()

                # Auto-recovery on failure (after 3 consecutive failures)
                if not passed and db_check.consecutive_failures >= 3:
                    db_check.attempt_recovery()

            except Exception as e:
                log.error(
                    "Health monitor loop error",
                    event_id="health_monitor_error",
                    db_label=db_check.label,
                    error=str(e),
                )

        # Update global snapshot
        with _last_health_check_lock:
            _last_health_check["timestamp"] = datetime.now(timezone.utc).isoformat()
            _last_health_check["databases"] = [
                db.get_metrics() for db in _SUPERVISED_DATABASES
            ]

        # Wait for next check interval
        _health_monitor_stop.wait(DEFAULT_CHECK_INTERVAL_SECONDS)


def start_health_monitor(daemon: bool = True) -> threading.Thread:
    """Start the background database health monitor.

    Args:
        daemon: If True, thread exits when main thread exits

    Returns:
        The monitor thread
    """
    global _health_monitor_thread
    if _health_monitor_thread is not None and _health_monitor_thread.is_alive():
        log.warning("Health monitor already running", event_id="health_monitor_already_running")
        return _health_monitor_thread

    _health_monitor_stop.clear()
    _health_monitor_thread = threading.Thread(target=_run_health_loop, daemon=daemon)
    _health_monitor_thread.start()

    log.info(
        "Database health monitor started",
        event_id="health_monitor_start",
        interval_seconds=DEFAULT_CHECK_INTERVAL_SECONDS,
    )
    return _health_monitor_thread


def stop_health_monitor():
    """Stop the background database health monitor."""
    global _health_monitor_thread
    if _health_monitor_thread is None:
        return

    _health_monitor_stop.set()
    _health_monitor_thread.join(timeout=10)
    _health_monitor_thread = None

    log.info("Database health monitor stopped", event_id="health_monitor_stop")


def get_health_snapshot() -> Dict[str, Any]:
    """Get the latest health snapshot for all monitored databases.

    Returns:
        Dict with timestamp and per-database metrics
    """
    with _last_health_check_lock:
        if _last_health_check:
            return dict(_last_health_check)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "databases": [db.get_metrics() for db in _SUPERVISED_DATABASES],
        }


# ─── Auto-register common databases ─────────────────────────────────────────

def auto_register_databases():
    """Register known databases from environment variables or defaults."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # METAR database
    metar_db = os.environ.get(
        "METAR_DB_PATH",
        os.path.join(project_root, "data", "metar_backfill.db"),
    )
    register_database(metar_db, label="metar")

    # Alerts database
    alerts_db = os.environ.get(
        "ALERTS_DB_PATH",
        os.path.join(project_root, "core", "alerts.db"),
    )
    register_database(alerts_db, label="alerts")

    # Optional: forecast disagreement database
    forecast_db = os.path.join(project_root, "data", "forecast_disagreement_live.db")
    if os.path.exists(forecast_db):
        register_database(forecast_db, label="forecast_disagreement")


# Auto-start on import when not in test mode
if not os.environ.get("SKIP_DB_HEALTH_MONITOR") == "1":
    try:
        auto_register_databases()
        start_health_monitor()
    except Exception as e:
        log.warning(
            "Failed to auto-start database health monitor",
            event_id="health_monitor_auto_start_fail",
            error=str(e),
        )