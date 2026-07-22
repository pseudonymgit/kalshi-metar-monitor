"""
Health monitor Module

Extracted from metar_monitor.py during Phase 20.1 monolith decomposition.
"""



# core/metar_monitor.py

import os
import copy
import json
import csv
import math
import logging
import sqlite3
import threading
import requests
import time
import re
from collections import deque
from io import StringIO
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from core.authoritative_state import (
    clear_latest_observation,
    commit_temperature_state,
    immutable_public_state_snapshot,
    read_temperature_state,
    reset_station_daily_state as _reset_station_daily_state_authoritative,
    set_latest_observation,
    state_lock,
    state_ref,
)
from core.transition_emitter import emit_transition_if_changed
from core.replay_engine import execute_ordered_replay_stream
from core.security_boundaries import enforce_execution_domain_guard
from core.station_time import station_local_day_key, station_timezone_name, to_station_local
from core.alert_schema import (
    ALERT_SCHEMA_VERSION,
    ALERT_TYPE_DIRECTION,
    TIER_1_PROTECTED_TYPES,
    OUTCOME_ELIGIBLE_NOT_ALERTABLE,
    OUTCOME_NO_ELIGIBLE_MARKET,
    OUTCOME_ALERT_SENT,
    OUTCOME_HYDRATION_BLOCKED,
    OUTCOME_NO_SIGNAL_CONDITION_MATCH,
)

# Near-Miss Audit Integration (2026-07-10)
try:
    from core.near_miss_audit import (
        log_near_miss,
        log_near_miss_if_cooldown,
        log_near_miss_if_distance_to_boundary,
        log_near_miss_if_no_eligible_market,
        log_near_miss_if_epoch_alert_emitted,
    )
except ImportError:
    # Graceful degradation if near_miss_audit module not available
    def log_near_miss(*args, **kwargs):
        pass
    def log_near_miss_if_cooldown(*args, **kwargs):
        pass
    def log_near_miss_if_distance_to_boundary(*args, **kwargs):
        pass
    def log_near_miss_if_no_eligible_market(*args, **kwargs):
        pass
    def log_near_miss_if_epoch_alert_emitted(*args, **kwargs):
        pass

# zoneinfo (Python 3.9+). If unavailable, we'll no-op ET/local conversions.
try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None

# Layer 3: Execution domain guard and market state tracking
# Note: Import is handled inside functions to avoid circular dependency
# with kalshi_monitor.py

# Layer 4: Webhook signature verification and alert categorization
import hmac
import hashlib
__all__ = ['get_station_ingestion_runtime', 'get_station_ingestion_window_runtime', 'get_last_nws_fetch_diagnostic', 'get_alert_review_diagnostics', 'get_latest_station_market_evaluation_context', 'get_recent_alerts', 'get_retention_metrics', 'prune_old_alerts', 'run_replay_for_station_day', 'get_transition_history', 'get_persisted_transition_history', 'log_near_miss', 'log_near_miss_if_cooldown', 'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market', 'log_near_miss_if_epoch_alert_emitted']


# Layer 4: Webhook signature verification
def get_station_ingestion_runtime(station: str) -> Dict[str, Any]:
    normalized_station = (station or "").strip().upper()
    if not normalized_station:
        return {}

    state = get_state()
    runtime = (state.get("ingestion_runtime") or {}).get(normalized_station) or {}
    latest_raw = (state.get("last_obs") or {}).get(normalized_station) or {}
    latest_accepted = (state.get("last_seen_iso") or {}).get(normalized_station)

    return {
        "station": normalized_station,
        "last_poll_attempt_utc": runtime.get("last_poll_attempt_utc"),
        "last_fetch_status": runtime.get("last_fetch_status") or "not_attempted",
        "fetched_observation_count": int(runtime.get("fetched_observation_count") or 0),
        "ingested_observation_count": int(runtime.get("ingested_observation_count") or 0),
        "rejected_observation_count": int(runtime.get("rejected_observation_count") or 0),
        "rejection_reasons": runtime.get("rejection_reasons") or [],
        "latest_raw_observation_timestamp": runtime.get("latest_raw_observation_timestamp") or latest_raw.get("obs_time"),
        "latest_accepted_observation_timestamp": latest_accepted,
    }
def get_station_ingestion_window_runtime(station: str) -> Dict[str, Any]:
    normalized_station = (station or "").strip().upper()
    if not normalized_station:
        return {}

    state = get_state()
    runtime = (state.get("ingestion_runtime") or {}).get(normalized_station) or {}

    return {
        "station": normalized_station,
        "window_start_utc": runtime.get("window_start_utc"),
        "window_end_utc": runtime.get("window_end_utc"),
        "last_seen_iso": (state.get("last_seen_iso") or {}).get(normalized_station),
        "latest_raw_observation_timestamp": runtime.get("latest_raw_observation_timestamp"),
        "latest_accepted_observation_timestamp": runtime.get("latest_accepted_observation_timestamp"),
        "sample_rejected_observations": runtime.get("sample_rejected_observations") or [],
    }
def get_last_nws_fetch_diagnostic(station: str) -> Dict[str, Any]:
    normalized_station = (station or "").strip().upper()
    if not normalized_station:
        return {}

    diagnostic = _LAST_NWS_FETCH_DIAGNOSTIC.get(normalized_station) or {}
    return {
        "station": normalized_station,
        "timestamp_utc": diagnostic.get("timestamp_utc"),
        "request_url": diagnostic.get("request_url"),
        "start_iso": diagnostic.get("start_iso"),
        "end_iso": diagnostic.get("end_iso"),
        "http_status": diagnostic.get("http_status"),
        "feature_count": diagnostic.get("feature_count"),
        "response_timestamp": diagnostic.get("response_timestamp"),
    }
def get_alert_review_diagnostics(
    limit: int = 50,
    transitions: Optional[List[Dict[str, Any]]] = None,
    latest_evaluations: Optional[Dict[str, Dict[str, Any]]] = None,
    hydration_snapshot: Optional[Dict[str, Any]] = None,
    recent_alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    try:
        bounded_limit = max(1, min(int(limit), 200))
    except Exception:
        bounded_limit = 50

    transitions = list(transitions) if isinstance(transitions, list) else get_transition_history(limit=bounded_limit)
    latest_evaluations = latest_evaluations if isinstance(latest_evaluations, dict) else {}
    hydration_stations = (
        hydration_snapshot.get("stations")
        if isinstance(hydration_snapshot, dict) and isinstance(hydration_snapshot.get("stations"), dict)
        else {}
    )
    recent_alerts = recent_alerts if isinstance(recent_alerts, list) else []

    rows: List[Dict[str, Any]] = []
    summary = {
        "total_transitions": 0,
        "transitions_with_markets": 0,
        "transitions_without_markets": 0,
        "suppressed_directional_strike": 0,
        "suppressed_market_rules": 0,
        "suppressed_hydration": 0,
        "alerts_emitted": 0,
    }
    market_totals = {
        "markets_considered_count": 0,
        "eligible_markets_count": 0,
        "rejected_markets_count": 0,
        "directional_strike_rejections": 0,
        "settlement_mismatch_rejections": 0,
        "wrong_series_rejections": 0,
        "expired_market_rejections": 0,
    }

    for row in transitions:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        runtime = (
            row.get("market_eligibility_runtime")
            if isinstance(row.get("market_eligibility_runtime"), dict)
            else metadata.get("market_eligibility_runtime")
            if isinstance(metadata.get("market_eligibility_runtime"), dict)
            else {}
        )
        station = (row.get("station") or "").strip().upper()
        latest_eval = latest_evaluations.get(station) if station else {}
        latest_runtime = (
            latest_eval.get("market_eligibility_runtime")
            if isinstance(latest_eval, dict) and isinstance(latest_eval.get("market_eligibility_runtime"), dict)
            else {}
        )
        if not runtime and latest_runtime:
            runtime = latest_runtime

        breakdown = runtime.get("rejection_breakdown") if isinstance(runtime.get("rejection_breakdown"), dict) else {}

        markets_considered_count = int(runtime.get("markets_considered_count") or 0)
        eligible_markets_count = int(runtime.get("eligible_markets_count") or 0)
        directional_strike_rejected = int(breakdown.get("directional_strike_rejected") or 0)
        settlement_mismatch = int(breakdown.get("settlement_mismatch") or 0)
        wrong_series = int(breakdown.get("wrong_series") or 0)
        expired_market = int(breakdown.get("expired_market") or 0)
        unknown_reason = int(breakdown.get("unknown_reason") or 0)
        rejected_markets_count = max(markets_considered_count - eligible_markets_count, 0)

        markets_after_directional_filter = max(markets_considered_count - directional_strike_rejected, 0)
        markets_after_settlement_filter = max(markets_after_directional_filter - settlement_mismatch, 0)
        markets_after_all_rules = max(eligible_markets_count, 0)

        alerts_sent = int(row.get("alerts_sent") or metadata.get("alerts_sent") or 0)
        evaluation_outcome = (
            str(row.get("evaluation_outcome") or metadata.get("evaluation_outcome") or "").strip().upper() or "UNKNOWN"
        )
        runtime_suppression_reason = str(
            row.get("suppression_reason") or metadata.get("suppression_reason") or ""
        ).strip().upper()

        hydration_runtime = hydration_stations.get(station) if station else {}
        hydration_prerequisite = (
            hydration_runtime.get("hydration_prerequisite")
            if isinstance(hydration_runtime, dict) and isinstance(hydration_runtime.get("hydration_prerequisite"), dict)
            else {}
        )
        hydration_cache_written = bool(hydration_prerequisite.get("cache_valid"))

        if runtime_suppression_reason:
            diagnostic_suppression_reason = runtime_suppression_reason
        elif alerts_sent > 0:
            diagnostic_suppression_reason = ""
        elif markets_considered_count == 0:
            diagnostic_suppression_reason = "NO_MARKETS_DISCOVERED"
        elif directional_strike_rejected > 0 and eligible_markets_count == 0:
            diagnostic_suppression_reason = "DIRECTIONAL_STRIKE_REJECTED"
        elif rejected_markets_count > 0:
            diagnostic_suppression_reason = "MARKET_RULES"
        elif isinstance(hydration_runtime, dict) and not hydration_cache_written:
            diagnostic_suppression_reason = "HYDRATION_NOT_READY"
        else:
            diagnostic_suppression_reason = "UNKNOWN"

        transition_diag = {
            "station": station,
            "timestamp": row.get("timestamp_utc"),
            "transition_type": row.get("transition_type"),
            "instant_bucket_before": row.get("instant_bucket_before"),
            "instant_bucket_after": row.get("instant_bucket_after"),
            "settlement_bucket": row.get("settlement_bucket"),
            "running_max": row.get("running_max"),
            "markets_considered_count": markets_considered_count,
            "eligible_markets_count": eligible_markets_count,
            "rejected_markets_count": rejected_markets_count,
            "market_evaluation_context": {
                "markets_considered_count": markets_considered_count,
                "eligible_markets_count": eligible_markets_count,
                "rejected_markets_count": rejected_markets_count,
            },
            "rejection_breakdown": {
                "directional_strike_rejected": directional_strike_rejected,
                "settlement_mismatch": settlement_mismatch,
                "wrong_series": wrong_series,
                "expired_market": expired_market,
                "unknown_reason": unknown_reason,
            },
            "decision_outcome": {
                "alerts_sent": alerts_sent,
                "evaluation_outcome": evaluation_outcome,
                "runtime_suppression_reason": runtime_suppression_reason,
                "diagnostic_suppression_reason": diagnostic_suppression_reason,
            },
            "hydration_cache_written": hydration_cache_written if isinstance(hydration_runtime, dict) else None,
        }
        rows.append(transition_diag)

        market_totals["markets_considered_count"] += markets_considered_count
        market_totals["eligible_markets_count"] += eligible_markets_count
        market_totals["rejected_markets_count"] += rejected_markets_count
        market_totals["directional_strike_rejections"] += directional_strike_rejected
        market_totals["settlement_mismatch_rejections"] += settlement_mismatch
        market_totals["wrong_series_rejections"] += wrong_series
        market_totals["expired_market_rejections"] += expired_market

        summary["total_transitions"] += 1
        if markets_considered_count > 0:
            summary["transitions_with_markets"] += 1
        else:
            summary["transitions_without_markets"] += 1

        emitted_alert = alerts_sent > 0 or evaluation_outcome == "ALERT_SENT"
        if emitted_alert:
            summary["alerts_emitted"] += 1
            continue

        directional_strike_suppressed = markets_considered_count > 0 and markets_after_all_rules == 0 and directional_strike_rejected > 0
        if directional_strike_suppressed:
            summary["suppressed_directional_strike"] += 1
        elif evaluation_outcome != "UNKNOWN":
            summary["suppressed_market_rules"] += 1

    transition_count = len(rows)
    denominator = float(transition_count) if transition_count else 1.0

    market_statistics = {
        "avg_markets_considered": market_totals["markets_considered_count"] / denominator,
        "avg_eligible_markets": market_totals["eligible_markets_count"] / denominator,
        "avg_rejected_markets": market_totals["rejected_markets_count"] / denominator,
        "directional_strike_rejections": market_totals["directional_strike_rejections"],
        "settlement_mismatch_rejections": market_totals["settlement_mismatch_rejections"],
        "wrong_series_rejections": market_totals["wrong_series_rejections"],
        "expired_market_rejections": market_totals["expired_market_rejections"],
    }

    return {
        "summary": summary,
        "market_statistics": market_statistics,
        "transition_samples": rows,
    }
def get_latest_station_market_evaluation_context(station: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    normalized_station = (station or "").strip().upper()
    latest_by_station: Dict[str, Dict[str, Any]] = {}

    try:
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return latest_by_station

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                query = """
                    SELECT
                        te.station,
                        te.created_utc,
                        te.transition_type,
                        te.id,
                        te.metadata_json
                    FROM transition_events te
                    WHERE te.station != ''
                """
                params: List[Any] = []
                if normalized_station:
                    query += " AND te.station = ?"
                    params.append(normalized_station)

                query += " ORDER BY te.id DESC"
                rows = conn.execute(query, tuple(params)).fetchall()
            finally:
                conn.close()

        for row in rows:
            station_code = (row[0] or "").strip().upper()
            if not station_code or station_code in latest_by_station:
                continue

            metadata_json = row[4]
            metadata: Dict[str, Any] = {}
            if metadata_json:
                try:
                    metadata = json.loads(metadata_json)
                except Exception:
                    metadata = {}

            if metadata.get("market_evaluated") is not True:
                continue

            latest_by_station[station_code] = {
                "latest_evaluation_timestamp_utc": row[1],
                "latest_market_evaluated": metadata.get("market_evaluated"),
                "latest_alerts_sent": metadata.get("alerts_sent"),
                "latest_evaluation_outcome": metadata.get("evaluation_outcome"),
                "latest_suppression_reason": metadata.get("suppression_reason"),
                "latest_transition_type": row[2],
                "latest_transition_event_id": row[3],
                "market_eligibility_runtime": metadata.get("market_eligibility_runtime") if isinstance(metadata.get("market_eligibility_runtime"), dict) else None,
            }
    except Exception as e:
        _ALERT_LOGGER.warning("latest_market_eval_context_query_failed station=%s error=%s", normalized_station, e)

    return latest_by_station
def _audit_alert(
    station: str,
    market_type: str,
    event_ticker: str,
    alert_type: str,
    direction: Optional[str],
    temp_f: Optional[float],
    bucket_index: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        market_type TEXT,
                        event_ticker TEXT,
                        alert_type TEXT,
                        direction TEXT,
                        temp_f REAL,
                        bucket_index INTEGER,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        created_utc,
                        station,
                        market_type,
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now_utc_iso(),
                        (station or "").upper(),
                        (market_type or "").upper(),
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        json.dumps(metadata or {}, sort_keys=True),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("audit_log_write_failed station=%s error=%s", station, e)
def get_recent_alerts(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        limit = min(max(int(limit), 1), 500)
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return []

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                rows = conn.execute(
                    """
                    SELECT id, created_utc, station, market_type,
                           event_ticker, alert_type, direction,
                           temp_f, bucket_index, metadata_json
                    FROM alerts
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()

        alerts: List[Dict[str, Any]] = []
        for row in rows:
            metadata = {}
            if row[9]:
                metadata = json.loads(row[9])
            alerts.append(
                {
                    "id": row[0],
                    "created_utc": row[1],
                    "station": row[2],
                    "market_type": row[3],
                    "event_ticker": row[4],
                    "alert_type": row[5],
                    "direction": row[6],
                    "temp_f": row[7],
                    "bucket_index": row[8],
                    "metadata": metadata,
                }
            )
        return alerts
    except Exception:
        return []
def get_retention_metrics() -> Dict[str, Any]:
    db_path = _alert_db_path()
    file_exists = os.path.exists(db_path)
    file_size_bytes = os.path.getsize(db_path) if file_exists else 0

    if not file_exists:
        return {
            "db_path": db_path,
            "file_exists": file_exists,
            "file_size_bytes": file_size_bytes,
            "total_rows": 0,
            "oldest_created_utc": None,
            "newest_created_utc": None,
            "rows_last_24h": 0,
        }

    total_rows = 0
    oldest_created_utc = None
    newest_created_utc = None
    rows_last_24h = 0

    conn = sqlite3.connect(db_path, timeout=1)
    try:
        total_rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        oldest_created_utc, newest_created_utc = conn.execute(
            "SELECT MIN(created_utc), MAX(created_utc) FROM alerts"
        ).fetchone()
        cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
        rows_last_24h = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE created_utc >= ?",
            (cutoff,)
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "db_path": db_path,
        "file_exists": file_exists,
        "file_size_bytes": file_size_bytes,
        "total_rows": total_rows,
        "oldest_created_utc": oldest_created_utc,
        "newest_created_utc": newest_created_utc,
        "rows_last_24h": rows_last_24h,
    }
def prune_old_alerts() -> Dict[str, Any]:
    db_path = _alert_db_path()
    env_value = os.getenv("ALERT_RETENTION_DAYS")

    retention_days: Optional[int] = None
    if env_value is not None:
        try:
            retention_days = int(env_value)
        except (TypeError, ValueError):
            retention_days = None

    if not os.path.exists(db_path):
        return {
            "retention_days": retention_days,
            "rows_deleted": 0,
            "remaining_rows": 0,
        }

    with _AUDIT_LOCK:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            if retention_days is None:
                remaining_rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
                return {
                    "retention_days": None,
                    "rows_deleted": 0,
                    "remaining_rows": remaining_rows,
                }

            cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat() + "Z"
            cursor = conn.execute(
                "DELETE FROM alerts WHERE created_utc < ?",
                (cutoff,),
            )
            deleted_count = cursor.rowcount if cursor.rowcount != -1 else 0
            conn.commit()
            remaining_rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            return {
                "retention_days": retention_days,
                "rows_deleted": deleted_count,
                "remaining_rows": remaining_rows,
            }
        except sqlite3.Error:
            return {
                "retention_days": retention_days,
                "rows_deleted": 0,
                "remaining_rows": 0,
            }
        finally:
            conn.close()
def _run_alert_retention() -> None:
    try:
        days = int(os.getenv("ALERT_RETENTION_DAYS", "180"))
        max_rows = int(os.getenv("ALERT_RETENTION_MAX_ROWS", "200000"))
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    """
                    DELETE FROM alerts
                    WHERE created_utc < datetime('now', ?)
                    """,
                    (f"-{days} days",),
                )
                conn.execute(
                    """
                    DELETE FROM alerts
                    WHERE id NOT IN (
                        SELECT id FROM alerts
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    """,
                    (max_rows,),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("alert_retention_failed error=%s", e)
def _snapshot_alert_queue_stats() -> Dict[str, Any]:
    """Get snapshot of alert queue statistics."""
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return {
            "pending": 0,
            "delivered": 0,
            "dead_letter": 0,
        }
    
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) as count FROM alert_delivery_queue GROUP BY status"
            ).fetchall()
            
            counts = {row[0]: row[1] for row in rows}
            
            return {
                "pending": counts.get("pending", 0),
                "delivered": counts.get("delivered", 0),
                "dead_letter": counts.get("dead_letter", 0),
                "total": sum(counts.values()),
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}
def _snapshot_station_state(station: str) -> Dict[str, Any]:
    station = (station or "").strip().upper()
    with _STATE_LOCK:
        last_obs = _STATE["last_obs"].get(station)
        snapshot = {
            "last_observed_integer": _STATE["last_observed_integer"].get(station),
            "running_daily_max": _STATE["running_daily_max"].get(station),
            "last_settlement_bucket": _STATE["last_settlement_bucket"].get(station),
            "last_instant_bucket": _STATE["last_instant_bucket"].get(station),
            "last_seen_iso": _STATE["last_seen_iso"].get(station),
            "last_obs": copy.deepcopy(last_obs),
            "last_reset_date_local": _STATE["last_reset_date_local"].get(station),
            "last_settlement_up_ts": _LAST_SETTLEMENT_UP_TS.get(station),
        }
    with _SIGNAL_LOCK:
        snapshot.update(
            {
                "signal_observation_window": list(_SIGNAL_OBSERVATION_WINDOWS.get(station) or []),
                "signal_station_last_emit": _SIGNAL_STATION_LAST_EMIT.get(station),
                "signal_epoch_counter": _SIGNAL_EPOCH_COUNTER.get(station),
                "signal_goldilocks_epoch_tracker": {
                    key[1]: copy.deepcopy(value)
                    for key, value in _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.items()
                    if key[0] == station
                },
                "signal_boundary_last_emit": {
                    (key[1], key[2]): value
                    for key, value in _SIGNAL_BOUNDARY_LAST_EMIT.items()
                    if key[0] == station
                },
                "latest_signal_runtime": copy.deepcopy(_LATEST_SIGNAL_RUNTIME.get(station)),
            }
        )
    return snapshot
def _restore_station_state(station: str, snapshot: Dict[str, Any]) -> None:
    station = (station or "").strip().upper()
    _LAST_SETTLEMENT_UP_TS.pop(station, None)
    with _SIGNAL_LOCK:
        _SIGNAL_OBSERVATION_WINDOWS.pop(station, None)
        _SIGNAL_STATION_LAST_EMIT.pop(station, None)
        _SIGNAL_EPOCH_COUNTER.pop(station, None)
        _LATEST_SIGNAL_RUNTIME.pop(station, None)
        for key in [k for k in _SIGNAL_GOLDILOCKS_EPOCH_TRACKER if k[0] == station]:
            _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.pop(key, None)
        for key in [k for k in _SIGNAL_BOUNDARY_LAST_EMIT if k[0] == station]:
            _SIGNAL_BOUNDARY_LAST_EMIT.pop(key, None)
    reset_station_daily_state(station, snapshot.get("last_reset_date_local") or "")
    if snapshot.get("last_observed_integer") is not None:
        commit_temperature_state(
            icao=station,
            curr_floor=int(snapshot["last_observed_integer"]),
            running_daily_max=float(snapshot["running_daily_max"]),
            settlement_bucket=int(snapshot["last_settlement_bucket"]),
            instant_bucket=int(snapshot["last_instant_bucket"]),
        )
    if snapshot.get("last_obs") is not None and snapshot.get("last_seen_iso"):
        set_latest_observation(station, snapshot["last_obs"], snapshot["last_seen_iso"])
    else:
        clear_latest_observation(station)
    prior_window = snapshot.get("signal_observation_window") or []
    if snapshot.get("last_settlement_up_ts"):
        _LAST_SETTLEMENT_UP_TS[station] = str(snapshot.get("last_settlement_up_ts"))
    with _SIGNAL_LOCK:
        if prior_window:
            restored_window = deque(maxlen=_SIGNAL_MOMENTUM_WINDOW_SIZE)
            for row in prior_window:
                if isinstance(row, dict):
                    restored_window.append(copy.deepcopy(row))
            if restored_window:
                _SIGNAL_OBSERVATION_WINDOWS[station] = restored_window
        if snapshot.get("signal_station_last_emit") is not None:
            _SIGNAL_STATION_LAST_EMIT[station] = float(snapshot["signal_station_last_emit"])
        if snapshot.get("signal_epoch_counter") is not None:
            _SIGNAL_EPOCH_COUNTER[station] = int(snapshot["signal_epoch_counter"])
        for epoch_id, tracker in (snapshot.get("signal_goldilocks_epoch_tracker") or {}).items():
            _SIGNAL_GOLDILOCKS_EPOCH_TRACKER[(station, int(epoch_id))] = copy.deepcopy(tracker)
        for boundary_key, ts in (snapshot.get("signal_boundary_last_emit") or {}).items():
            boundary, epoch_id = boundary_key
            _SIGNAL_BOUNDARY_LAST_EMIT[(station, int(boundary), int(epoch_id))] = float(ts)
        if isinstance(snapshot.get("latest_signal_runtime"), dict):
            _LATEST_SIGNAL_RUNTIME[station] = copy.deepcopy(snapshot["latest_signal_runtime"])
def _reset_replay_runtime_state_for_station(
    station: str,
    replay_local_day: str,
) -> None:
    station = (station or "").strip().upper()
    _LAST_SETTLEMENT_UP_TS.pop(station, None)
    with _SIGNAL_LOCK:
        _SIGNAL_OBSERVATION_WINDOWS.pop(station, None)
        _SIGNAL_STATION_LAST_EMIT.pop(station, None)
        _SIGNAL_EPOCH_COUNTER.pop(station, None)
        _LATEST_SIGNAL_RUNTIME.pop(station, None)
        for key in [k for k in _SIGNAL_GOLDILOCKS_EPOCH_TRACKER if k[0] == station]:
            _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.pop(key, None)
        for key in [k for k in _SIGNAL_BOUNDARY_LAST_EMIT if k[0] == station]:
            _SIGNAL_BOUNDARY_LAST_EMIT.pop(key, None)
    reset_station_daily_state(station, replay_local_day)
    clear_latest_observation(station)
def run_replay_for_station_day(station: str, date_local: str) -> Dict[str, Any]:
    """
    Deterministic replay executor for one station-local date.
    Replays persisted observations strictly in ingest_sequence_id order.
    """
    station = (station or "").strip().upper()
    scheduler_was_running = is_scheduler_running()
    if scheduler_was_running:
        stop_scheduler()
    snapshot = _snapshot_station_state(station)

    try:
        ensure_state_loaded()
        date_local = (date_local or "").strip()
        datetime.strptime(date_local, "%Y-%m-%d")

        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            _reset_replay_runtime_state_for_station(
                station,
                date_local,
            )
            return {
                "station": station,
                "date": date_local,
                "observations_processed": 0,
                "status": "completed",
            }

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                try:
                    rows = conn.execute(
                        """
                        SELECT ingest_sequence_id, obs_time, temp_f, source, raw_json
                        FROM metar_observations
                        WHERE station = ?
                        ORDER BY ingest_sequence_id ASC
                        """,
                        (station,),
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
            finally:
                conn.close()

        replay_rows = []
        for row in rows:
            obs_time = row[1]
            obs_local_date = _to_local(station, _parse_iso(obs_time)).date().isoformat()
            if obs_local_date == date_local:
                replay_rows.append(row)

        _reset_replay_runtime_state_for_station(
            station,
            date_local,
        )

        cfg = get_default_config()
        replay_observations: List[Dict[str, Any]] = []
        for row in replay_rows:
            obs_time = row[1]
            temp_f = float(row[2])
            raw_json = row[4]
            raw = {}
            if raw_json:
                try:
                    raw = json.loads(raw_json)
                except Exception:
                    raw = {"raw_json": raw_json}

            replay_observations.append(
                _obs_tuple(temp_f, obs_time, raw, row[3] or "replay")
            )

        # Replay causal flow: ordered historical observations -> deterministic
        # transition/state reconstruction through the same ingestion semantics.
        result = execute_ordered_replay_stream(
            station=station,
            ordered_observations=replay_observations,
            cfg=cfg,
            ingest_fn=_ingest_obs,
        )
        result["date"] = date_local
        return result
    finally:
        _restore_station_state(station, snapshot)
        if scheduler_was_running:
            start_scheduler(_ALERT_LOGGER)
def _log_transition_event(
    station: str,
    transition_type: Optional[str],
    instant_bucket_before: Optional[int],
    instant_bucket_after: int,
    settlement_bucket: int,
    running_max: float,
    current_temp: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    execution_domain = "production"
    try:
        from core.kalshi_monitor import _current_kalshi_execution_domain

        execution_domain = _current_kalshi_execution_domain()
    except Exception:
        execution_domain = "production"

    if execution_domain != "production":
        return None

    now_iso = _now_utc_iso()
    event_metadata = dict(metadata or {})
    event_metadata.setdefault("alert_schema_version", 2)
    event_metadata.setdefault("alert_classification", "STRUCTURAL")
    try:
        transition_event_id = None
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transition_events (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        transition_type TEXT,
                        instant_bucket_before INTEGER,
                        instant_bucket_after INTEGER,
                        settlement_bucket INTEGER,
                        running_max REAL,
                        current_temp REAL,
                        metadata_json TEXT
                    )
                    """
                )
                cur = conn.execute(
                    """
                    INSERT INTO transition_events (
                        created_utc,
                        station,
                        transition_type,
                        instant_bucket_before,
                        instant_bucket_after,
                        settlement_bucket,
                        running_max,
                        current_temp,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso,
                        (station or "").upper(),
                        transition_type,
                        instant_bucket_before,
                        instant_bucket_after,
                        settlement_bucket,
                        running_max,
                        current_temp,
                        json.dumps(event_metadata, sort_keys=True),
                    ),
                )
                transition_event_id = int(cur.lastrowid or 0) or None
                conn.commit()
            finally:
                conn.close()

        with _TRANSITION_LOCK:
            _TRANSITION_HISTORY.append(
                {
                    "station": (station or "").upper(),
                    "transition_type": transition_type,
                    "instant_bucket_before": instant_bucket_before,
                    "instant_bucket_after": instant_bucket_after,
                    "settlement_bucket": settlement_bucket,
                    "running_max": running_max,
                    "current_temp": current_temp,
                    "timestamp_utc": now_iso,
                    "transition_event_id": transition_event_id,
                    "metadata": copy.deepcopy(event_metadata),
                }
                )
        return {
            "station": (station or "").upper(),
            "timestamp_utc": now_iso,
            "transition_event_id": transition_event_id,
        }
    except Exception as e:
        _ALERT_LOGGER.warning("transition_event_log_failed station=%s error=%s", station, e)
    return None
def _find_correlated_transition_entry(
    station: str,
    transition_correlation: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    normalized_station = (station or "").strip().upper()
    transition_id = None
    transition_timestamp = None
    if isinstance(transition_correlation, dict):
        raw_id = transition_correlation.get("transition_event_id")
        if raw_id is not None:
            try:
                transition_id = int(raw_id)
            except Exception:
                transition_id = None
        raw_timestamp = transition_correlation.get("timestamp_utc")
        if raw_timestamp is not None:
            transition_timestamp = str(raw_timestamp)
    if transition_id is None and not transition_timestamp:
        return None

    with _TRANSITION_LOCK:
        for entry in reversed(_TRANSITION_HISTORY):
            if entry.get("station") != normalized_station:
                continue
            if transition_id is not None and int(entry.get("transition_event_id") or 0) != transition_id:
                continue
            if transition_id is None and transition_timestamp and entry.get("timestamp_utc") != transition_timestamp:
                continue
            return entry
    return None
def _annotate_transition_history_market_eval(
    station: str,
    transition_correlation: Optional[Dict[str, Any]],
    alerts_sent: int,
    evaluation_outcome: str,
    suppression_reason: Optional[str] = None,
    market_eligibility_runtime: Optional[Dict[str, Any]] = None,
    evaluated_market_types: Optional[list] = None,
) -> None:
    normalized_station = (station or "").strip().upper()
    correlated_transition = _find_correlated_transition_entry(
        station=station,
        transition_correlation=transition_correlation,
    )
    transition_id = None
    transition_timestamp = None
    if correlated_transition is not None:
        raw_transition_id = correlated_transition.get("transition_event_id")
        if raw_transition_id is not None:
            try:
                transition_id = int(raw_transition_id)
            except Exception:
                transition_id = None
        raw_transition_timestamp = correlated_transition.get("timestamp_utc")
        if raw_transition_timestamp is not None:
            transition_timestamp = str(raw_transition_timestamp)
    if transition_id is None and not transition_timestamp:
        _ALERT_LOGGER.warning(
            "transition_market_eval_annotation_skipped station=%s reason=missing_correlation",
            normalized_station,
        )
        return
    safe_outcome = (evaluation_outcome or "").strip().upper() or "SUPPRESSED_UNKNOWN"
    safe_suppression_reason = (suppression_reason or "").strip().upper() or None
    eligibility_runtime = market_eligibility_runtime if isinstance(market_eligibility_runtime, dict) else None
    if safe_outcome == "ALERT_SENT":
        alert_classification = "MARKET_ELIGIBLE"
    elif safe_outcome.startswith("HYDRATION_BLOCKED"):
        alert_classification = "HYDRATION_BLOCKED"
    elif safe_outcome == "NO_ELIGIBLE_MARKET":
        alert_classification = "MARKET_SUPPRESSED"
    else:
        alert_classification = "MARKET_SUPPRESSED"

    with _TRANSITION_LOCK:
        for entry in reversed(_TRANSITION_HISTORY):
            if entry.get("station") != normalized_station:
                continue
            if transition_id is not None and int(entry.get("transition_event_id") or 0) != transition_id:
                continue
            if transition_id is None and transition_timestamp and entry.get("timestamp_utc") != transition_timestamp:
                continue
            entry["market_evaluated"] = True
            entry["alerts_sent"] = int(alerts_sent)
            entry["evaluation_outcome"] = safe_outcome
            entry["alert_schema_version"] = 2
            entry["alert_classification"] = alert_classification
            if safe_suppression_reason:
                entry["suppression_reason"] = safe_suppression_reason
            if eligibility_runtime is not None:
                entry["market_eligibility_runtime"] = copy.deepcopy(eligibility_runtime)
            # Add market_type from evaluated market types
            if evaluated_market_types and isinstance(evaluated_market_types, list):
                if len(evaluated_market_types) == 1:
                    entry["market_type"] = evaluated_market_types[0]
                else:
                    entry["market_type"] = evaluated_market_types[0] if evaluated_market_types else None
            break

    try:
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                cur = conn.execute(
                    """
                    SELECT id, metadata_json
                    FROM transition_events
                    WHERE station = ?
                    AND (
                        (? IS NOT NULL AND id = ?)
                        OR (? IS NULL AND ? IS NOT NULL AND created_utc = ?)
                    )
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        normalized_station,
                        transition_id,
                        transition_id,
                        transition_id,
                        transition_timestamp,
                        transition_timestamp,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    return

                metadata: Dict[str, Any] = {}
                raw_metadata = row[1]
                if raw_metadata:
                    try:
                        metadata = json.loads(raw_metadata)
                    except Exception:
                        metadata = {"raw_metadata_json": raw_metadata}

                metadata["market_evaluated"] = True
                metadata["alerts_sent"] = int(alerts_sent)
                metadata["evaluation_outcome"] = safe_outcome
                metadata["alert_schema_version"] = 2
                metadata["alert_classification"] = alert_classification
                if safe_suppression_reason:
                    metadata["suppression_reason"] = safe_suppression_reason
                if eligibility_runtime is not None:
                    metadata["market_eligibility_runtime"] = copy.deepcopy(eligibility_runtime)
                # Add market_type from the evaluated market types
                if evaluated_market_types and isinstance(evaluated_market_types, list):
                    # Store the first market type that had eligible markets
                    # If multiple market types were evaluated, store them all
                    if len(evaluated_market_types) == 1:
                        metadata["market_type"] = evaluated_market_types[0]
                    else:
                        metadata["market_type"] = evaluated_market_types[0] if evaluated_market_types else None

                conn.execute(
                    "UPDATE transition_events SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, sort_keys=True), row[0]),
                )
                conn.commit()

                # Also update the settlement_epochs table with market_type
                # The settlement epoch was created by log_transition_for_settlement_epoch
                # with NULL market_type because the metadata didn't have it yet.
                # Now that we know the market_type, backfill it.
                if evaluated_market_types and isinstance(evaluated_market_types, list) and len(evaluated_market_types) > 0:
                    market_type_value = evaluated_market_types[0]
                    conn.execute(
                        """
                        UPDATE settlement_epochs
                        SET market_type = ?
                        WHERE settlement_transition_event_id = ?
                        AND market_type IS NULL
                        """,
                        (market_type_value, row[0]),
                    )
                    conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning(
            "transition_market_eval_annotation_failed station=%s error=%s",
            normalized_station,
            e,
        )
def _annotate_transition_history_alert_path_truth(
    station: str,
    transition_correlation: Optional[Dict[str, Any]],
    alert_path_truth: Optional[Dict[str, Any]],
) -> None:
    normalized_station = (station or "").strip().upper()
    if not isinstance(alert_path_truth, dict):
        return

    transition_id = None
    transition_timestamp = None
    if isinstance(transition_correlation, dict):
        raw_id = transition_correlation.get("transition_event_id")
        if raw_id is not None:
            try:
                transition_id = int(raw_id)
            except Exception:
                transition_id = None
        raw_timestamp = transition_correlation.get("timestamp_utc")
        if raw_timestamp is not None:
            transition_timestamp = str(raw_timestamp)

    if transition_id is None and not transition_timestamp:
        return

    safe_truth = copy.deepcopy(alert_path_truth)

    with _TRANSITION_LOCK:
        for entry in reversed(_TRANSITION_HISTORY):
            if entry.get("station") != normalized_station:
                continue
            if transition_id is not None and int(entry.get("transition_event_id") or 0) != transition_id:
                continue
            if transition_id is None and transition_timestamp and entry.get("timestamp_utc") != transition_timestamp:
                continue
            existing_truth = (
                copy.deepcopy(entry.get("alert_path_truth"))
                if isinstance(entry.get("alert_path_truth"), dict)
                else {}
            )
            entry["alert_path_truth"] = {
                **existing_truth,
                **safe_truth,
            }
            break

    try:
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                cur = conn.execute(
                    """
                    SELECT id, metadata_json
                    FROM transition_events
                    WHERE station = ?
                    AND (
                        (? IS NOT NULL AND id = ?)
                        OR (? IS NULL AND ? IS NOT NULL AND created_utc = ?)
                    )
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        normalized_station,
                        transition_id,
                        transition_id,
                        transition_id,
                        transition_timestamp,
                        transition_timestamp,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    return

                metadata: Dict[str, Any] = {}
                raw_metadata = row[1]
                if raw_metadata:
                    try:
                        metadata = json.loads(raw_metadata)
                    except Exception:
                        metadata = {"raw_metadata_json": raw_metadata}

                existing_truth = (
                    copy.deepcopy(metadata.get("alert_path_truth"))
                    if isinstance(metadata.get("alert_path_truth"), dict)
                    else {}
                )
                metadata["alert_path_truth"] = {
                    **existing_truth,
                    **safe_truth,
                }

                conn.execute(
                    "UPDATE transition_events SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, sort_keys=True), row[0]),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning(
            "transition_alert_path_truth_annotation_failed station=%s error=%s",
            normalized_station,
            e,
        )
def _normalize_transition_history_query(
    station=None,
    day: Optional[str] = None,
    limit=50,
) -> Tuple[str, str, int]:
    normalized_station = (station or "").strip().upper()
    normalized_day = (day or "").strip()
    try:
        bounded_limit = max(1, min(int(limit), 200))
    except Exception:
        bounded_limit = 50

    if normalized_day:
        datetime.strptime(normalized_day, "%Y-%m-%d")

    return normalized_station, normalized_day, bounded_limit
def _load_transition_event_metadata(raw_metadata: Any) -> Dict[str, Any]:
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except Exception:
        return {"raw_metadata_json": raw_metadata}
    return parsed if isinstance(parsed, dict) else {"raw_metadata_json": raw_metadata}
def _build_transition_history_row_from_persisted(row: Tuple[Any, ...]) -> Dict[str, Any]:
    metadata = _load_transition_event_metadata(row[9])
    transition_row: Dict[str, Any] = {
        "station": (row[2] or "").strip().upper(),
        "transition_type": row[3],
        "instant_bucket_before": row[4],
        "instant_bucket_after": row[5],
        "settlement_bucket": row[6],
        "running_max": row[7],
        "current_temp": row[8],
        "timestamp_utc": row[1],
        "transition_event_id": row[0],
        "metadata": metadata,
    }

    promoted_metadata_fields = (
        "market_evaluated",
        "alerts_sent",
        "evaluation_outcome",
        "alert_schema_version",
        "alert_classification",
        "suppression_reason",
        "market_eligibility_runtime",
        "alert_path_truth",
    )
    for field in promoted_metadata_fields:
        if field in metadata:
            transition_row[field] = copy.deepcopy(metadata[field])

    return transition_row
def get_transition_history(station=None, day: Optional[str] = None, limit=50):
    normalized_station, normalized_day, bounded_limit = _normalize_transition_history_query(
        station=station,
        day=day,
        limit=limit,
    )

    with _TRANSITION_LOCK:
        history = list(_TRANSITION_HISTORY)

    if normalized_station:
        history = [entry for entry in history if entry.get("station") == normalized_station]

    if normalized_day:
        filtered_history = []
        for entry in history:
            obs_time = (entry.get("metadata") or {}).get("obs_time")
            if not obs_time:
                continue
            try:
                if station_local_day_key(entry.get("station") or normalized_station, obs_time) == normalized_day:
                    filtered_history.append(entry)
            except Exception:
                continue
        history = filtered_history

    history = list(reversed(history))
    return history[:bounded_limit]
def get_persisted_transition_history(station=None, day: Optional[str] = None, limit=50):
    normalized_station, normalized_day, bounded_limit = _normalize_transition_history_query(
        station=station,
        day=day,
        limit=limit,
    )

    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return []

    query = """
        SELECT id, created_utc, station, transition_type,
               instant_bucket_before, instant_bucket_after,
               settlement_bucket, running_max, current_temp,
               metadata_json
        FROM transition_events
    """
    params: List[Any] = []
    conditions = []
    if normalized_station:
        conditions.append("station = ?")
        params.append(normalized_station)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC"
    if not normalized_day:
        query += " LIMIT ?"
        params.append(bounded_limit)

    try:
        with _AUDIT_LOCK:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            try:
                rows = conn.execute(query, tuple(params)).fetchall()
            finally:
                conn.close()
    except Exception:
        return []

    history = []
    for row in rows:
        transition_row = _build_transition_history_row_from_persisted(row)
        if normalized_day:
            obs_time = (transition_row.get("metadata") or {}).get("obs_time")
            if not obs_time:
                continue
            try:
                if station_local_day_key(transition_row.get("station") or normalized_station, obs_time) != normalized_day:
                    continue
            except Exception:
                continue
        history.append(transition_row)
        if len(history) >= bounded_limit:
            break

    return history
def _prune_transition_events() -> None:
    try:
        retention_days = int(os.getenv("TRANSITION_RETENTION_DAYS", "3"))
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return

        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat() + "Z"
        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    "DELETE FROM transition_events WHERE created_utc < ?",
                    (cutoff,),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("transition_event_prune_failed error=%s", e)
def _queue_alert_for_delivery(alert_id: str, webhook_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue an alert for delivery with retry logic (L1-T1).
    
    This replaces direct _send_alert() calls with queued delivery that
    handles failures with exponential backoff.
    """
    # Extract station info from payload
    station = payload.get("station") or payload.get("legacy", {}).get("station", None)
    temp_f = payload.get("temp_f") if isinstance(payload.get("temp_f"), (int, float)) else None
    obs_time = payload.get("obs_time") or payload.get("legacy", {}).get("obs_time", None)
    
    # Create metadata for tracking
    metadata = {
        "alert_id": alert_id,
        "queued_at": _now_utc_iso(),
    }
    
    # Delegate to alert_retry_queue module
    from core.alert_retry_queue import _queue_alert_for_delivery as _ar_queue
    result = _ar_queue(
        webhook_url=webhook_url,
        payload=payload,
        station=station,
        temp_f=temp_f,
        obs_time=obs_time,
        metadata=metadata,
    )
    
    return {
        "queued": result.get("status") == "queued",
        "alert_id": alert_id,
        "estimated_retry_time": result.get("estimated_retry_time"),
    }
def _retry_delivery_batch() -> Dict[str, Any]:
    """Process pending deliveries with exponential backoff (L1-T3)."""
    from core.alert_retry_queue import _retry_delivery_batch as _ar_retry
    result = _ar_retry(batch_size=10, immediate=True)  # immediate=True for testing
    return result
def _get_pending_deliveries() -> List[Dict[str, Any]]:
    """Get pending alert deliveries (L1-T2)."""
    return _get_alert_delivery_queue_entries(status="pending")
def _get_failed_alerts() -> List[Dict[str, Any]]:
    """Get failed/dead-lettered alerts (L1-T2)."""
    return _get_alert_delivery_queue_entries(status="dead_letter")
def _get_alert_delivery_queue_entries(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get alert delivery queue entries filtered by status."""
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return []
    
    _ensure_alert_schema()
    
    entries = []
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            if status:
                rows = conn.execute(
                    """SELECT id, alert_id, created_at, updated_at, webhook_url, alert_payload_json,
                               attempt_count, next_retry_at, last_error, original_station,
                               original_temp_f, original_obs_time
                        FROM alert_delivery_queue WHERE status = ?
                        ORDER BY created_at DESC LIMIT 100""",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, alert_id, created_at, updated_at, webhook_url, alert_payload_json,
                               attempt_count, next_retry_at, last_error, original_station,
                               original_temp_f, original_obs_time
                        FROM alert_delivery_queue ORDER BY created_at DESC LIMIT 100"""
                ).fetchall()
            
            for row in rows:
                entry_id, alert_id_val, created_at, updated_at, webhook_url, payload_json, \
                    attempt_count, next_retry_at, last_error, station, temp_f, obs_time = row
                
                entries.append({
                    "entry_id": entry_id,
                    "alert_id": alert_id_val,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "webhook_url": webhook_url,
                    "alert_payload": json.loads(payload_json),
                    "attempt_count": attempt_count,
                    "next_retry_at": next_retry_at,
                    "last_error": last_error,
                    "station": station,
                    "temp_f": temp_f,
                    "obs_time": obs_time,
                })
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("alert_queue_query_failed status=%s error=%s", status, e)
    
    return entries
def _mark_alert_delivery_queue_dead_letter(alert_id: str, reason: str) -> None:
    """Mark an alert as dead-lettered for manual inspection (L1-T2)."""
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return
    
    _ensure_alert_schema()
    
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # First, get the entry_id from alert_id metadata
            now_iso = _now_utc_iso()
            conn.execute(
                """UPDATE alert_delivery_queue
                    SET status = 'dead_letter', updated_at = ?, last_error = ?
                    WHERE alert_id = ?""",
                (now_iso, reason, alert_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("mark_dead_letter_failed alert_id=%s error=%s", alert_id, e)
def _update_alert_delivery_queue_attempt(alert_id: str, error: str) -> None:
    """Update attempt count and error for an alert (L1-T3)."""
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return
    
    _ensure_alert_schema()
    
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Find the entry by alert_id in metadata and update
            now_iso = _now_utc_iso()
            rows = conn.execute(
                "SELECT id, attempt_count FROM alert_delivery_queue WHERE alert_id = ?",
                (alert_id,),
            ).fetchall()
            
            for row in rows:
                entry_id, attempt_count = row
                new_attempt = attempt_count + 1
                # Calculate exponential backoff: 60 * 2^attempt seconds (min 1m, max 1h)
                delay_seconds = 60 * (2 ** attempt_count)
                delay_seconds = min(delay_seconds, 3600)  # Cap at 1 hour
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
                
                conn.execute(
                    """UPDATE alert_delivery_queue
                        SET attempt_count = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                        WHERE id = ?""",
                    (new_attempt, next_retry, error, now_iso, entry_id),
                )
            
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("update_alert_attempt_failed alert_id=%s error=%s", alert_id, e)
def _delete_alert_delivery_queue(alert_id: str) -> None:
    """Delete an alert from the queue after successful delivery (L1-T3)."""
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return
    
    _ensure_alert_schema()
    
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            conn.execute(
                "DELETE FROM alert_delivery_queue WHERE alert_id = ?",
                (alert_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("delete_alert_failed alert_id=%s error=%s", alert_id, e)
def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")
def _ensure_alert_schema() -> None:
    """Ensure Layer 1 alert delivery queue and Layer 0 persistence schemas exist."""
    from core.alert_retry_queue import _ensure_alert_delivery_queue_schema as _ensure_schema
    _ensure_schema()
    
    db_path = _alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    with _SIGNAL_LOCK:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Signal layer state persistence (L0-T1)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_layer_state (
                    signal_name TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Market cache persistence (L0-T2)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_cache (
                    market_id TEXT PRIMARY KEY,
                    station TEXT NOT NULL,
                    cache_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_hydrated_utc TEXT
                )
                """
            )
            conn.commit()
            
            # Upgrade: Add last_hydrated_utc column if missing (existing databases)
            try:
                cursor = conn.execute("PRAGMA table_info(market_cache)")
                columns = {row[1] for row in cursor.fetchall()}
                if "last_hydrated_utc" not in columns:
                    conn.execute("ALTER TABLE market_cache ADD COLUMN last_hydrated_utc TEXT")
                    conn.commit()
                    _ALERT_LOGGER.info("market_cache_schema_upgraded added_last_hydrated_utc")
            except Exception as e:
                _ALERT_LOGGER.info("market_cache_schema_upgrade_skipped error=%s", str(e))
        finally:
            conn.close()
def _persist_signal_state(signal_name: str, state_dict: Dict[str, Any]) -> None:
    """Persist signal state to SQLite (L0-T1).
    
    Stores cooldown timestamps and epoch tracking state for restart survival.
    """
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Ensure schema exists
            _ensure_alert_schema()
            state_json = json.dumps(state_dict, sort_keys=True)
            now_iso = _now_utc_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO signal_layer_state (
                    signal_name, state_json, updated_at
                ) VALUES (?, ?, ?)
                """,
                (signal_name, state_json, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("signal_state_persist_failed name=%s error=%s", signal_name, e)
def _load_signal_state(signal_name: str) -> Optional[Dict[str, Any]]:
    """Load signal state from SQLite."""
    try:
        db_path = _alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            row = conn.execute(
                "SELECT state_json FROM signal_layer_state WHERE signal_name = ?",
                (signal_name,),
            ).fetchone()
            if not row:
                return None
            return json.loads(row[0])
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("signal_state_load_failed name=%s error=%s", signal_name, e)
        return None
def _load_all_signal_state() -> Dict[str, Dict[str, Any]]:
    """Load all signal state entries from SQLite for startup hydration."""
    result = {}
    try:
        db_path = _alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            rows = conn.execute(
                "SELECT signal_name, state_json FROM signal_layer_state"
            ).fetchall()

            for row in rows:
                signal_name, state_json = row
                try:
                    result[signal_name] = json.loads(state_json)
                except Exception:
                    result[signal_name] = {}
        finally:
            conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("signal_state_load_all_failed error=%s", e)
    return result
def _hydrate_all_signal_state() -> int:
    """Load all signal state from SQLite and hydrate in-memory state.
    
    Returns:
        Number of signal state entries hydrated
    """
    loaded = _load_all_signal_state()
    hydrate_count = 0
    
    with _SIGNAL_LOCK:
        for signal_name, state in loaded.items():
            # Parse signal name and restore state
            if signal_name.startswith("station_cooldown:"):
                # Format: station_cooldown:STATION
                parts = signal_name.split(":")
                if len(parts) >= 2:
                    station = parts[-1]
                    last_emit = state.get("last_emit")
                    if last_emit:
                        _SIGNAL_STATION_LAST_EMIT[station] = last_emit
                        hydrate_count += 1
            elif signal_name.startswith("boundary_cooldown:"):
                # Format: boundary_cooldown:STATION:BOUNDARY:EPOCH
                parts = signal_name.split(":")
                if len(parts) >= 4:
                    station = parts[1]
                    boundary = int(parts[2])
                    epoch = int(parts[3])
                    last_emit = state.get("last_emit")
                    if last_emit:
                        _SIGNAL_BOUNDARY_LAST_EMIT[(station, boundary, epoch)] = last_emit
                        hydrate_count += 1
            elif signal_name.startswith("goldilocks_tracker:"):
                # Format: goldilocks_tracker:STATION:EPOCH
                parts = signal_name.split(":")
                if len(parts) >= 3:
                    station = parts[1]
                    epoch = int(parts[2])
                    _SIGNAL_GOLDILOCKS_EPOCH_TRACKER[(station, epoch)] = copy.deepcopy(state)
                    hydrate_count += 1
    
    if hydrate_count > 0:
        _ALERT_LOGGER.info("signal_state_hydrated_entries=%d", hydrate_count)
    
    return hydrate_count
