"""
heartbeat_monitor.py — Synthetic Heartbeat Monitoring

Periodically tests the application's critical paths (webhook delivery, DB health,
scheduler status) and reports results. Designed to run as a background thread.

On failure, it logs to the structured logger and increments a counter.
On persistent failure (3+ consecutive), it emits a warning.
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.structured_logger import get_logger

log = get_logger("heartbeat_monitor")

# Defaults
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 10
HEARTBEAT_EVENT_PATH = "/api/heartbeat"

# State
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_stop = threading.Event()
_heartbeat_results: Dict[str, Any] = {
    "last_heartbeat_utc": None,
    "total_checks": 0,
    "successful_checks": 0,
    "failed_checks": 0,
    "consecutive_failures": 0,
    "last_error": None,
    "last_webhook_status": None,
}
_heartbeat_lock = threading.Lock()


def _send_heartbeat_webhook(webhook_url: str, payload: dict) -> bool:
    """Send a heartbeat event to the configured webhook URL.

    Args:
        webhook_url: The target webhook URL
        payload: JSON-serializable payload

    Returns:
        True if webhook responded with 2xx
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "OpenClaw-Weather-Engine/1.0 (heartbeat)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_WEBHOOK_TIMEOUT_SECONDS) as resp:
            status = resp.status
            if 200 <= status < 300:
                log.info(
                    "Heartbeat webhook delivered",
                    event_id="heartbeat_webhook_ok",
                    webhook_url=webhook_url[:40],
                    status=status,
                )
                return True
            else:
                log.warning(
                    "Heartbeat webhook returned non-2xx",
                    event_id="heartbeat_webhook_non_2xx",
                    webhook_url=webhook_url[:40],
                    status=status,
                )
                return False
    except urllib.error.HTTPError as e:
        log.warning(
            "Heartbeat webhook HTTP error",
            event_id="heartbeat_webhook_http_error",
            status=e.code,
        )
        return False
    except Exception as e:
        log.warning(
            "Heartbeat webhook connection error",
            event_id="heartbeat_webhook_error",
            error=str(e),
        )
        return False


def _run_heartbeat_loop():
    """Background heartbeat loop.

    Tests:
    1. Webhook delivery (if WEBHOOK_BASE_URL is configured)
    2. Application health (by fetching /healthz)
    """
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    app_base = os.environ.get("APP_BASE_URL", "http://localhost:10000")

    while not _heartbeat_stop.is_set():
        checks_start = time.time()
        now_utc = datetime.now(timezone.utc).isoformat()
        all_ok = True

        # 1. Health check via self-request
        health_url = f"{app_base}/healthz"
        self_health_ok = False
        try:
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8")
                    data = json.loads(body)
                    self_health_ok = data.get("healthy", False)
                else:
                    self_health_ok = False
        except Exception as e:
            log.warning(
                "Self-health check failed",
                event_id="heartbeat_self_health_fail",
                health_url=health_url,
                error=str(e) if app_base != "http://localhost:10000" else "(local dev — expected)",
            )
            # In local dev, self-health check may fail due to port binding
            # This is non-fatal
            if "localhost" in app_base:
                pass  # expected in dev
            else:
                all_ok = False

        # 2. Webhook delivery test (if configured)
        webhook_ok = True
        if webhook_base:
            heartbeat_payload = {
                "event": "heartbeat",
                "timestamp": now_utc,
                "healthy": self_health_ok,
                "version": "1.0",
            }
            webhook_url = f"{webhook_base.rstrip('/')}{HEARTBEAT_EVENT_PATH}"
            webhook_ok = _send_heartbeat_webhook(webhook_url, heartbeat_payload)
            if not webhook_ok:
                all_ok = False

        # 3. Update results
        with _heartbeat_lock:
            _heartbeat_results["last_heartbeat_utc"] = now_utc
            _heartbeat_results["total_checks"] += 1
            if all_ok:
                _heartbeat_results["successful_checks"] += 1
                _heartbeat_results["consecutive_failures"] = 0
            else:
                _heartbeat_results["failed_checks"] += 1
                _heartbeat_results["consecutive_failures"] += 1
                _heartbeat_results["last_error"] = (
                    "Self-health: " + ("OK" if self_health_ok else "FAIL") +
                    " | Webhook: " + ("OK" if webhook_ok else "FAIL")
                )

            _heartbeat_results["last_webhook_status"] = "OK" if webhook_ok else "FAIL"

        # Check for persistent failure
        with _heartbeat_lock:
            if _heartbeat_results["consecutive_failures"] >= 3:
                log.error(
                    "Heartbeat persistent failure",
                    event_id="heartbeat_persistent_fail",
                    consecutive_failures=_heartbeat_results["consecutive_failures"],
                    last_error=_heartbeat_results["last_error"],
                )

        # Wait for next interval
        wait_time = DEFAULT_HEARTBEAT_INTERVAL_SECONDS - (time.time() - checks_start)
        if wait_time > 0:
            _heartbeat_stop.wait(wait_time)


def start_heartbeat_monitor(daemon: bool = True) -> Optional[threading.Thread]:
    """Start the background heartbeat monitor.

    Args:
        daemon: If True, thread exits when main thread exits

    Returns:
        The monitor thread, or None if WEBHOOK_BASE_URL is not configured
    """
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        log.warning("Heartbeat monitor already running", event_id="heartbeat_already_running")
        return _heartbeat_thread

    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    if not webhook_base:
        log.info(
            "Heartbeat monitor not started — WEBHOOK_BASE_URL not configured",
            event_id="heartbeat_no_webhook",
        )
        return None

    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(target=_run_heartbeat_loop, daemon=daemon)
    _heartbeat_thread.start()

    log.info(
        "Heartbeat monitor started",
        event_id="heartbeat_start",
        interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    )
    return _heartbeat_thread


def stop_heartbeat_monitor():
    """Stop the background heartbeat monitor."""
    global _heartbeat_thread
    if _heartbeat_thread is None:
        return

    _heartbeat_stop.set()
    _heartbeat_thread.join(timeout=10)
    _heartbeat_thread = None

    log.info("Heartbeat monitor stopped", event_id="heartbeat_stop")


def get_heartbeat_status() -> Dict[str, Any]:
    """Get the current heartbeat monitor status."""
    with _heartbeat_lock:
        return dict(_heartbeat_results)