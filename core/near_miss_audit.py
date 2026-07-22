# CHANGELOG (last 10 broad changes):
# 1. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
#


"""
Near-Miss Audit Log - Lightweight signal suppression tracking

This module implements structured audit logging for signal conditions that are
CLOSE to triggering alerts but do not meet all gating criteria. This enables
the team to monitor how close the system is to actual alerts during dry periods.

Features:
- Log structured near-miss records (suppressed conditions, close calls)
- Query audit log to assess alert likelihood during dry periods
- Integrate with p3_scheduler without changing live alerting behavior
- No impact on existing alert delivery pipeline

Usage:
    from core.near_miss_audit import (
        log_near_miss,
        query_near_miss_log,
        get_near_miss_summary
    )

    # Log a near miss when signal conditions are almost met
    log_near_miss(
        station="KDEN",
        near_miss_type="NEAR_BOUNDARY_LOW_MOMENTUM",
        details={
            "distance_to_boundary": 0.03,
            "momentum": 0.0015,  # below threshold of 0.002
            "window_size": 3,
        }
    )

    # Query recent near misses
    recent = query_near_miss_log(station="KDEN", limit=10)

    # Get summary for dry periods
    summary = get_near_miss_summary(station="KDEN", hours=24)

Author: Donna Paulsen (via Gilfoyle)
Date: 2026-07-10
"""

from __future__ import annotations

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from core.metar_monitor import _now_utc_iso

# ─── Audit Lock ─────────────────────────────────────────────────────────────
_AUDIT_LOCK = threading.Lock()

# ─── Near-Miss Types ────────────────────────────────────────────────────────
# Categorization of signal conditions that are close to triggering alerts

NEAR_MISS_TYPES = {
    # Signal conditions close to thresholds
    "NEAR_BOUNDARY_LOW_MOMENTUM": {
        "description": "Temperature near boundary but momentum below threshold",
        "fields": ["distance_to_boundary", "momentum", "momentum_threshold", "window_size"],
    },
    "STATION_COOLDOWN": {
        "description": "Signal eligible but station in cooldown period",
        "fields": ["remaining_cooldown_seconds", "total_cooldown_seconds"],
    },
    "BOUNDARY_COOLDOWN": {
        "description": "Signal eligible but boundary in cooldown period",
        "fields": ["boundary_level", "remaining_cooldown_seconds", "total_cooldown_seconds"],
    },
    "EPOCH_ALERT_ALREADY_EMITTED": {
        "description": "Goldilocks epoch alert already fired this epoch",
        "fields": ["epoch_id", "signal_type"],
    },
    "NO_ELIGIBLE_MARKET": {
        "description": "Signal conditions met but no Kalshi market discovered",
        "fields": ["discovered_markets_count", "hydration_status"],
    },
    "HYDRATION_CACHE_INVALID": {
        "description": "Signal conditions met but hydration cache invalid",
        "fields": ["hydration_cache_valid", "cache_status"],
    },
    "NO_SIGNAL_CONDITION_MATCH": {
        "description": "No signal condition met after observation window review",
        "fields": ["observation_window_size", "required_window_size"],
    },
    "MARKET_ELIGIBILITY_FILTER": {
        "description": "Market exists but failed eligibility filter",
        "fields": ["filter_type", "market_ticker", "rejection_reason"],
    },
    "DIRECTIONAL_FILTER": {
        "description": "Market exists but directional filter rejected",
        "fields": ["market_direction", "expected_direction", "strike_distance"],
    },
    # Confidence/criteria close to threshold
    "LOW_CONFIDENCE_GOLDILOCKS": {
        "description": "Goldilocks pattern detected but confidence below threshold",
        "fields": ["confidence", "confidence_threshold", "reversion_direction"],
    },
    "MOMENTUM_BELOW_THRESHOLD": {
        "description": "Momentum detected but below minimum threshold",
        "fields": ["momentum", "momentum_threshold", "time_window_seconds"],
    },
    "TOO_FAR_FROM_BOUNDARY": {
        "description": "Temperature too far from integer boundary for signal",
        "fields": ["distance_to_boundary", "max_allowed_distance"],
    },
    "SETTLEMENT_NOT_CHANGE": {
        "description": "Settlement not changing but signal conditions considered",
        "fields": ["current_settlement", "previous_settlement", "delta_threshold"],
    },
}

# ─── Alert DB Path Helper (fallback to writable location) ────────────────────


def _get_alert_db_path() -> str:
    """Get alert DB path, falling back to workspace if /var/data is not accessible."""
    # Use workspace data directory
    workspace_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..",
        "data",
    )
    db_path = os.path.join(workspace_dir, "near_miss_audit.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def _alert_db_path() -> str:
    """Alias for _get_alert_db_path for API consistency."""
    return _get_alert_db_path()


# ─── Schema ─────────────────────────────────────────────────────────────────


def _ensure_near_miss_audit_schema() -> None:
    """Create near_miss_audit table if it doesn't exist."""
    db_path = _get_alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with _AUDIT_LOCK:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS near_miss_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_utc TEXT NOT NULL,
                    station TEXT NOT NULL,
                    near_miss_type TEXT NOT NULL,
                    severity TEXT,
                    details_json TEXT,
                    metadata_json TEXT,
                    suppressed_alert_type TEXT
                )
                """
            )
            # Add indexes for common queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_near_miss_station 
                ON near_miss_audit(station, created_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_near_miss_type 
                ON near_miss_audit(near_miss_type, created_utc DESC)
                """
            )
            conn.commit()
        finally:
            conn.close()


# ─── Core Functions ─────────────────────────────────────────────────────────


def log_near_miss(
    *,
    station: str,
    near_miss_type: str,
    severity: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    suppressed_alert_type: Optional[str] = None,
) -> int:
    """
    Log a near-miss event to the audit table.
    
    Args:
        station: ICAO station code (will be normalized to uppercase)
        near_miss_type: One of NEAR_MISS_TYPES keys
        severity: Optional severity level (LOW/MEDIUM/HIGH)
        details: Dictionary of condition-specific details
        metadata: Optional additional metadata (e.g., observation window state)
        suppressed_alert_type: The alert type that would have fired if conditions met
        
    Returns:
        The database row ID of the newly inserted record
    """
    # Validate near_miss_type
    if near_miss_type not in NEAR_MISS_TYPES:
        # Accept it anyway but log warning
        print(f"[WARNING] Unknown near_miss_type: {near_miss_type}")
    
    # Normalize station
    station = (station or "").strip().upper()
    
    # Validate severity if provided
    if severity and severity.upper() not in ("LOW", "MEDIUM", "HIGH"):
        print(f"[WARNING] Invalid severity: {severity}, using None")
        severity = None
    else:
        severity = severity.upper() if severity else None
    
    details_json = json.dumps(details or {}, sort_keys=True)
    metadata_json = json.dumps(metadata or {}, sort_keys=True)
    
    with _AUDIT_LOCK:
        _ensure_near_miss_audit_schema()
        
        conn = sqlite3.connect(_get_alert_db_path(), timeout=1)
        try:
            cursor = conn.execute(
                """
                INSERT INTO near_miss_audit (
                    created_utc, station, near_miss_type, severity,
                    details_json, metadata_json, suppressed_alert_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_utc_iso(),
                    station,
                    near_miss_type,
                    severity,
                    details_json,
                    metadata_json,
                    suppressed_alert_type,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0
        finally:
            conn.close()


def query_near_miss_log(
    *,
    station: Optional[str] = None,
    near_miss_type: Optional[str] = None,
    hours: Optional[int] = None,
    limit: int = 100,
    order_by: str = "created_utc DESC",
) -> List[Dict[str, Any]]:
    """
    Query the near-miss audit log.
    
    Args:
        station: Filter by station code (optional, case-insensitive)
        near_miss_type: Filter by type (optional)
        hours: Filter to last N hours (optional, overrides limit)
        limit: Maximum rows to return (default 100)
        order_by: SQL ORDER BY clause (default: created_utc DESC)
        
    Returns:
        List of near-miss records with parsed JSON fields
    """
    # Normalize station if provided
    if station:
        station = station.strip().upper()
    
    # Build query
    where_clauses = []
    params = []
    
    if station:
        where_clauses.append("station = ?")
        params.append(station)
    
    if near_miss_type:
        where_clauses.append("near_miss_type = ?")
        params.append(near_miss_type)
    
    if hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        where_clauses.append("created_utc >= ?")
        params.append(cutoff)
    
    where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    limit_clause = f"LIMIT {min(int(limit), 500)}"
    
    query = f"""
        SELECT id, created_utc, station, near_miss_type, severity,
               details_json, metadata_json, suppressed_alert_type
        FROM near_miss_audit
        {where_clause}
        ORDER BY {order_by}
        {limit_clause}
    """
    
    with _AUDIT_LOCK:
        _ensure_near_miss_audit_schema()
        
        conn = sqlite3.connect(_get_alert_db_path(), timeout=1)
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
    
    results = []
    for row in rows:
        details = {}
        if row[5]:
            try:
                details = json.loads(row[5])
            except json.JSONDecodeError:
                pass
        
        metadata = {}
        if row[6]:
            try:
                metadata = json.loads(row[6])
            except json.JSONDecodeError:
                pass
        
        results.append({
            "id": row[0],
            "created_utc": row[1],
            "station": row[2],
            "near_miss_type": row[3],
            "severity": row[4],
            "details": details,
            "metadata": metadata,
            "suppressed_alert_type": row[7],
        })
    
    return results


def get_near_miss_summary(
    *,
    station: Optional[str] = None,
    hours: int = 24,
    by_type: bool = False,
) -> Dict[str, Any]:
    """
    Get summary statistics for near-miss events.
    
    Args:
        station: Filter by station (optional)
        hours: Lookback window in hours (default: 24)
        by_type: Group counts by near_miss_type (default: False)
        
    Returns:
        Summary dictionary with counts and recent records
    """
    summary: Dict[str, Any] = {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_count": 0,
        "by_type": {},
        "recent_records": [],
    }
    
    # Get all records in window
    records = query_near_miss_log(
        station=station,
        hours=hours,
        limit=5000,
    )
    
    summary["total_count"] = len(records)
    
    if by_type:
        type_counts: Dict[str, int] = {}
        for record in records:
            mt = record["near_miss_type"]
            type_counts[mt] = type_counts.get(mt, 0) + 1
        summary["by_type"] = type_counts
    
    # Add recent records (last 10)
    summary["recent_records"] = records[:10]
    
    # Add severity breakdown
    severity_counts: Dict[str, int] = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "None": 0}
    for record in records:
        sev = record.get("severity") or "None"
        if sev in severity_counts:
            severity_counts[sev] += 1
        else:
            severity_counts["None"] += 1
    summary["severity_breakdown"] = severity_counts
    
    # Add suppressed_alert_type breakdown
    alert_counts: Dict[str, int] = {}
    for record in records:
        at = record.get("suppressed_alert_type")
        if at:
            alert_counts[at] = alert_counts.get(at, 0) + 1
    summary["suppressed_alert_types"] = alert_counts
    
    return summary


# ─── Integration Helper Functions ────────────────────────────────────────────


def log_near_miss_if_cooldown(
    *,
    station: str,
    cooldown_type: str,  # "STATION" or "BOUNDARY"
    remaining_seconds: float,
    total_seconds: float,
    boundary_level: Optional[int] = None,
    epoch_id: Optional[int] = None,
) -> bool:
    """
    Log near-miss if cooldown prevents alert emission.
    
    Returns True if near-miss was logged, False otherwise.
    """
    details = {
        "remaining_seconds": remaining_seconds,
        "total_seconds": total_seconds,
    }
    
    if cooldown_type == "BOUNDARY" and boundary_level is not None:
        details["boundary_level"] = boundary_level
    
    if epoch_id is not None:
        details["epoch_id"] = epoch_id
    
    log_near_miss(
        station=station,
        near_miss_type="STATION_COOLDOWN" if cooldown_type == "STATION" else "BOUNDARY_COOLDOWN",
        severity="MEDIUM" if remaining_seconds < total_seconds * 0.5 else "LOW",
        details=details,
    )
    
    return True


def log_near_miss_if_distance_to_boundary(
    *,
    station: str,
    distance: float,
    momentum: Optional[float],
    threshold_distance: float = 0.10,
    threshold_momentum: float = 0.002,
) -> bool:
    """
    Log near-miss for distance-to-boundary conditions.
    
    Returns True if near-miss was logged (distance < threshold but conditions not fully met).
    """
    if distance > threshold_distance:
        return False  # Too far to be considered a near miss
    
    details = {
        "distance_to_boundary": distance,
        "threshold_distance": threshold_distance,
    }
    
    if momentum is not None:
        details["momentum"] = momentum
        details["momentum_threshold"] = threshold_momentum
        details["momentum_deficit"] = threshold_momentum - momentum if momentum < threshold_momentum else 0
    
    severity = "HIGH" if distance < 0.02 else "MEDIUM" if distance < 0.05 else "LOW"
    
    if momentum is None or momentum < threshold_momentum:
        near_miss_type = "NEAR_BOUNDARY_LOW_MOMENTUM"
        suppressed = "near_boundary_momentum_up" if momentum is not None and momentum > 0 else None
    else:
        near_miss_type = "TOO_FAR_FROM_BOUNDARY"
        suppressed = None
    
    log_near_miss(
        station=station,
        near_miss_type=near_miss_type,
        severity=severity,
        details=details,
        suppressed_alert_type=suppressed,
    )
    
    return True


def log_near_miss_if_no_eligible_market(
    *,
    station: str,
    signal_detected: bool,
    discovered_markets_count: int,
    hydration_valid: bool,
) -> bool:
    """
    Log near-miss when signal detected but no eligible market exists.
    
    Returns True if near-miss was logged.
    """
    if not signal_detected:
        return False  # No signal means no near-miss
    
    details = {
        "discovered_markets_count": discovered_markets_count,
        "hydration_cache_valid": hydration_valid,
        "signal_detected": True,
    }
    
    # Determine the specific suppression reason
    if not hydration_valid:
        near_miss_type = "HYDRATION_CACHE_INVALID"
        suppressed = "goldilocks_reversion_alert"
    elif discovered_markets_count <= 0:
        near_miss_type = "NO_ELIGIBLE_MARKET"
        suppressed = "near_boundary_momentum_up"
    else:
        near_miss_type = "NO_ELIGIBLE_MARKET"
        suppressed = "near_boundary_momentum_up"
    
    log_near_miss(
        station=station,
        near_miss_type=near_miss_type,
        severity="MEDIUM",
        details=details,
        suppressed_alert_type=suppressed,
    )
    
    return True


def log_near_miss_if_epoch_alert_emitted(
    *,
    station: str,
    epoch_id: int,
    signal_type: str,
) -> bool:
    """
    Log near-miss when goldilocks epoch alert already emitted.
    
    Returns True if near-miss was logged.
    """
    details = {
        "epoch_id": epoch_id,
        "signal_type": signal_type,
    }
    
    log_near_miss(
        station=station,
        near_miss_type="EPOCH_ALERT_ALREADY_EMITTED",
        severity="LOW",
        details=details,
        suppressed_alert_type="goldilocks_reversion_alert",
    )
    
    return True
