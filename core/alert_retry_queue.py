# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 2. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
#


"""Alert Retry Queue - Infrastructure Hardening (Layer 1, CRITICAL-1).

This module implements alert delivery retry with exponential backoff and a dead-letter queue
for failed alerts that require manual intervention.
"""

import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_ALERT_LOGGER = logging.getLogger(__name__)
_RETRY_LOCK = threading.Lock()


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def _alert_db_path() -> str:
    default_path = str(_REPO_ROOT / "data" / "alert_retry_queue.db")
    return os.getenv("ALERT_DB_PATH", default_path)

# Exponential backoff constants
MIN_RETRY_DELAY_SECONDS = 60  # 1 minute
MAX_RETRY_DELAY_SECONDS = 3600  # 1 hour
MAX_RETRIES = 5
BACKOFF_MULTIPLIER = 2.0

# Alert delivery timeout (webhook response time)
DELIVERY_TIMEOUT_SECONDS = 30


def _ensure_alert_delivery_queue_schema() -> None:
    """Create alert_delivery_queue table if it doesn't exist."""
    db_path = _alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            # Create alert delivery queue table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_delivery_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    webhook_url TEXT NOT NULL,
                    alert_payload_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_error TEXT,
                    original_station TEXT,
                    original_temp_f REAL,
                    original_obs_time TEXT,
                    metadata_json TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _queue_alert_for_delivery(
    webhook_url: str,
    payload: Dict[str, Any],
    station: Optional[str] = None,
    temp_f: Optional[float] = None,
    obs_time: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Queue an alert for delivery with retry logic.
    
    Args:
        webhook_url: Target webhook URL
        payload: Full alert payload
        station: Optional station identifier for tracking
        temp_f: Optional temperature for tracking
        obs_time: Optional observation time for tracking
        metadata: Optional additional metadata
        
    Returns:
        Queue entry details
    """
    db_path = _alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    _ensure_alert_delivery_queue_schema()
    
    now_iso = _now_utc_iso()
    initial_delay = MIN_RETRY_DELAY_SECONDS
    next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=initial_delay)).isoformat()
    
    entry_metadata = {
        "queued_at": now_iso,
        "first_attempt": True,
    }
    if metadata:
        entry_metadata.update(metadata)
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            # Extract alert_id from metadata for the UNIQUE column
            alert_id_value = (metadata or {}).get("alert_id") or f"auto:{now_iso}"
            cur = conn.execute(
                """
                INSERT INTO alert_delivery_queue (
                    alert_id, created_at, updated_at, status, webhook_url, alert_payload_json,
                    attempt_count, next_retry_at, original_station, original_temp_f,
                    original_obs_time, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id_value,
                    now_iso,
                    now_iso,
                    "pending",
                    webhook_url,
                    json.dumps(payload, sort_keys=True),
                    0,
                    next_retry_at,
                    station,
                    temp_f,
                    obs_time,
                    json.dumps(entry_metadata, sort_keys=True),
                ),
            )
            conn.commit()
            
            entry_id = cur.lastrowid or 0
            return {
                "status": "queued",
                "entry_id": entry_id,
                "webhook_url": webhook_url,
                "estimated_retry_time": next_retry_at,
            }
        finally:
            conn.close()


def _retry_delivery_batch(batch_size: int = 10, immediate: bool = False) -> Dict[str, Any]:
    """Process pending deliveries in batch with exponential backoff.
    
    Args:
        batch_size: Maximum entries to process in this batch
        immediate: If True, process all pending entries regardless of next_retry_at
        
    Returns:
        Processing results summary
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return {
            "status": "no_queue",
            "processed": 0,
            "success": 0,
            "failed": 0,
            "backed_off": 0,
        }
    
    _ensure_alert_delivery_queue_schema()
    
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    
    success_count = 0
    failed_count = 0
    backed_off_count = 0
    processed_entries = []
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            # Get pending entries that need retry
            # If immediate=True, process all pending entries regardless of next_retry_at
            if immediate:
                rows = conn.execute(
                    """
                    SELECT id, webhook_url, alert_payload_json, attempt_count, 
                           next_retry_at, last_error, original_station, original_temp_f,
                           original_obs_time, metadata_json, created_at
                    FROM alert_delivery_queue
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (batch_size,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, webhook_url, alert_payload_json, attempt_count, 
                           next_retry_at, last_error, original_station, original_temp_f,
                           original_obs_time, metadata_json, created_at
                    FROM alert_delivery_queue
                    WHERE status = 'pending'
                      AND next_retry_at <= ?
                    ORDER BY next_retry_at ASC
                    LIMIT ?
                    """,
                    (now_iso, batch_size),
                ).fetchall()
            
            for row in rows:
                entry_id, webhook_url, payload_json, attempt_count, next_retry_at, \
                    last_error, station, temp_f, obs_time, metadata_json, created_at = row
                
                payload = json.loads(payload_json)
                metadata = json.loads(metadata_json) if metadata_json else {}
                
                # Check if we've exceeded max retries
                if attempt_count >= MAX_RETRIES:
                    # Move to dead-letter status
                    conn.execute(
                        """
                        UPDATE alert_delivery_queue
                        SET status = 'dead_letter', updated_at = ?, last_error = ?
                        WHERE id = ?
                        """,
                        (now_iso, f"Max retries ({MAX_RETRIES}) exceeded", entry_id),
                    )
                    processed_entries.append({
                        "entry_id": entry_id,
                        "status": "moved_to_dead_letter",
                        "reason": f"Max retries exceeded",
                    })
                    failed_count += 1
                    continue
                
                # Attempt delivery
                try:
                    from core.metar_monitor import _send_alert
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
                    result = _send_alert(webhook_url, payload)
                    
                    if result.get("delivery_succeeded"):
                        # Mark as delivered
                        conn.execute(
                            """
                            UPDATE alert_delivery_queue
                            SET status = 'delivered', updated_at = ?, attempt_count = ?
                            WHERE id = ?
                            """,
                            (now_iso, attempt_count + 1, entry_id),
                        )
                        processed_entries.append({
                            "entry_id": entry_id,
                            "status": "delivered",
                            "webhook_status": result.get("webhook_status_code"),
                        })
                        success_count += 1
                    else:
                        # Schedule retry with exponential backoff
                        new_attempt = attempt_count + 1
                        delay_seconds = int(MIN_RETRY_DELAY_SECONDS * (BACKOFF_MULTIPLIER ** attempt_count))
                        delay_seconds = min(delay_seconds, MAX_RETRY_DELAY_SECONDS)
                        next_retry = (now + timedelta(seconds=delay_seconds)).isoformat()
                        
                        error_msg = result.get("webhook_exception", "unknown_error")
                        
                        conn.execute(
                            """
                            UPDATE alert_delivery_queue
                            SET updated_at = ?, attempt_count = ?, next_retry_at = ?,
                                last_error = ?
                            WHERE id = ?
                            """,
                            (now_iso, new_attempt, next_retry, error_msg, entry_id),
                        )
                        processed_entries.append({
                            "entry_id": entry_id,
                            "status": "scheduled_retry",
                            "attempt": new_attempt,
                            "next_retry_at": next_retry,
                            "error": error_msg,
                        })
                        backed_off_count += 1
                        
                except Exception as e:
                    # Schedule retry on exception
                    new_attempt = attempt_count + 1
                    delay_seconds = int(MIN_RETRY_DELAY_SECONDS * (BACKOFF_MULTIPLIER ** attempt_count))
                    delay_seconds = min(delay_seconds, MAX_RETRY_DELAY_SECONDS)
                    next_retry = (now + timedelta(seconds=delay_seconds)).isoformat()
                    
                    conn.execute(
                        """
                        UPDATE alert_delivery_queue
                        SET updated_at = ?, attempt_count = ?, next_retry_at = ?,
                            last_error = ?
                        WHERE id = ?
                        """,
                        (now_iso, new_attempt, next_retry, str(e), entry_id),
                    )
                    processed_entries.append({
                        "entry_id": entry_id,
                        "status": "scheduled_retry",
                        "attempt": new_attempt,
                        "next_retry_at": next_retry,
                        "error": str(e),
                    })
                    backed_off_count += 1
            
            conn.commit()
            
        finally:
            conn.close()
    
    return {
        "status": "processed",
        "processed": len(processed_entries),
        "success": success_count,
        "failed": failed_count,
        "backed_off": backed_off_count,
        "entries": processed_entries,
    }


def _process_dead_letter_queue() -> Dict[str, List[Dict[str, Any]]]:
    """Retrieve dead-letter queue entries for manual inspection.
    
    Returns:
        Dictionary with 'entries' key containing dead-letter entries
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return {"entries": []}
    
    _ensure_alert_delivery_queue_schema()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            rows = conn.execute(
                """
                SELECT id, created_at, webhook_url, alert_payload_json, 
                       attempt_count, last_error, original_station, original_temp_f,
                       original_obs_time, metadata_json
                FROM alert_delivery_queue
                WHERE status = 'dead_letter'
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
            
            entries = []
            for row in rows:
                entry_id, created_at, webhook_url, payload_json, attempt_count, \
                    last_error, station, temp_f, obs_time, metadata_json = row
                
                entries.append({
                    "entry_id": entry_id,
                    "created_at": created_at,
                    "webhook_url": webhook_url,
                    "alert_payload": json.loads(payload_json),
                    "attempt_count": attempt_count,
                    "last_error": last_error,
                    "station": station,
                    "temp_f": temp_f,
                    "obs_time": obs_time,
                    "metadata": json.loads(metadata_json) if metadata_json else None,
                })
            
            return {"entries": entries}
            
        finally:
            conn.close()


def _purge_completed_entries(days_old: int = 7) -> int:
    """Purge completed (delivered) entries older than specified days.
    
    Args:
        days_old: Entries older than this many days will be purged
        
    Returns:
        Number of entries purged
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return 0
    
    _ensure_alert_delivery_queue_schema()
    
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            cursor = conn.execute(
                """
                DELETE FROM alert_delivery_queue
                WHERE status = 'delivered'
                  AND created_at < ?
                """,
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount or 0
        finally:
            conn.close()


def _get_queue_stats() -> Dict[str, Any]:
    """Get current queue statistics.
    
    Returns:
        Dictionary with queue statistics
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return {
            "pending": 0,
            "delivered": 0,
            "dead_letter": 0,
            "oldest_pending": None,
            "newest_pending": None,
        }
    
    _ensure_alert_delivery_queue_schema()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            # Count by status
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM alert_delivery_queue
                GROUP BY status
                """
            ).fetchall()
            
            counts = {row[0]: row[1] for row in rows}
            
            # Get timestamps for pending
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM alert_delivery_queue WHERE status = 'pending'"
            ).fetchone()[0]
            newest = conn.execute(
                "SELECT MAX(created_at) FROM alert_delivery_queue WHERE status = 'pending'"
            ).fetchone()[0]
            
            return {
                "pending": counts.get("pending", 0),
                "delivered": counts.get("delivered", 0),
                "dead_letter": counts.get("dead_letter", 0),
                "oldest_pending": oldest,
                "newest_pending": newest,
            }
        finally:
            conn.close()


def _get_entry_details(entry_id: int) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific queue entry.
    
    Args:
        entry_id: Queue entry ID
        
    Returns:
        Entry details or None if not found
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return None
    
    _ensure_alert_delivery_queue_schema()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            row = conn.execute(
                """
                SELECT id, created_at, updated_at, status, webhook_url, alert_payload_json,
                       attempt_count, next_retry_at, last_error, original_station, 
                       original_temp_f, original_obs_time, metadata_json
                FROM alert_delivery_queue
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
            
            if not row:
                return None
            
            return {
                "id": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "status": row[3],
                "webhook_url": row[4],
                "alert_payload": json.loads(row[5]),
                "attempt_count": row[6],
                "next_retry_at": row[7],
                "last_error": row[8],
                "station": row[9],
                "temp_f": row[10],
                "obs_time": row[11],
                "metadata": json.loads(row[12]) if row[12] else None,
            }
        finally:
            conn.close()


def _retry_entry(entry_id: int) -> Dict[str, Any]:
    """Manually trigger a retry for a specific entry.
    
    Args:
        entry_id: Queue entry ID
        
    Returns:
        Result of retry operation
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return {"status": "error", "reason": "queue_file_missing"}
    
    _ensure_alert_delivery_queue_schema()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            row = conn.execute(
                "SELECT webhook_url, alert_payload_json, attempt_count, last_error FROM alert_delivery_queue WHERE id = ?",
                (entry_id,),
            ).fetchone()
            
            if not row:
                return {"status": "error", "reason": "entry_not_found"}
            
            webhook_url, payload_json, attempt_count, last_error = row
            
            # Clear the last error and reset next_retry_at to now
            now_iso = _now_utc_iso()
            conn.execute(
                """
                UPDATE alert_delivery_queue
                SET next_retry_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (now_iso, entry_id),
            )
            conn.commit()
            
            return {
                "status": "requeued",
                "entry_id": entry_id,
                "attempt": attempt_count + 1,
                "next_retry_at": now_iso,
            }
        finally:
            conn.close()


def _clear_queue(status_filter: Optional[str] = None) -> int:
    """Clear queue entries, optionally filtered by status.
    
    Args:
        status_filter: Filter by status (pending, delivered, dead_letter) or None for all
        
    Returns:
        Number of entries cleared
    """
    db_path = _alert_db_path()
    
    if not os.path.exists(db_path):
        return 0
    
    _ensure_alert_delivery_queue_schema()
    
    with _RETRY_LOCK:
        conn = get_sqlite_connection(db_path, timeout=1)
        try:
            if status_filter:
                cursor = conn.execute(
                    "DELETE FROM alert_delivery_queue WHERE status = ?",
                    (status_filter,),
                )
            else:
                cursor = conn.execute("DELETE FROM alert_delivery_queue")
            
            conn.commit()
            return cursor.rowcount or 0
        finally:
            conn.close()
