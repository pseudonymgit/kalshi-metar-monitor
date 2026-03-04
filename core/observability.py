"""
Observability

This module provides read-only runtime introspection over authoritative state
and persisted event streams.

Responsibilities
- Expose transition, epoch, and scheduler visibility for diagnostics.
- Compute deterministic read models from persisted data.
- Enforce observability read-only execution boundaries.

This module MUST NOT
- Mutate authoritative runtime state.
- Trigger ingestion, evaluation, or alert side effects.
- Issue live Kalshi execution calls.
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from core.authoritative_state import immutable_public_state_snapshot
from core.scoring_engine import score_settlement_epochs, serialize_epoch_scores
from core.security_boundaries import (
    detect_illegal_cross_layer_imports,
    verify_observability_read_only,
)


# Architectural boundary: observability imports are validated so this
# module cannot silently become a runtime execution authority.
detect_illegal_cross_layer_imports(module_name=__name__, module_globals=globals())

ReadOnlyRow = Tuple[Any, ...]


def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")


def _db_exists() -> bool:
    db_path = _alert_db_path()
    return os.path.exists(db_path)


# Observability invariant: all database reads use SQLite read-only mode
# so diagnostics can never mutate causal runtime history.
def _query_rows_readonly(query: str, params: Tuple[Any, ...]) -> List[ReadOnlyRow]:
    db_path = _alert_db_path()
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(query, params)
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_emitted_transition_stream(*, station: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """
    Read-only transition stream exposure.
    Consumes only persisted transition events.
    """
    normalized_station = (station or "").strip().upper()
    bounded_limit = max(1, min(int(limit), 1000))

    params: Tuple[Any, ...]
    if normalized_station:
        query = """
            SELECT id, created_utc, station, transition_type,
                   instant_bucket_before, instant_bucket_after,
                   settlement_bucket, running_max, current_temp,
                   metadata_json
            FROM transition_events
            WHERE station = ?
            ORDER BY id DESC
            LIMIT ?
        """
        params = (normalized_station, bounded_limit)
    else:
        query = """
            SELECT id, created_utc, station, transition_type,
                   instant_bucket_before, instant_bucket_after,
                   settlement_bucket, running_max, current_temp,
                   metadata_json
            FROM transition_events
            ORDER BY id DESC
            LIMIT ?
        """
        params = (bounded_limit,)

    rows = _query_rows_readonly(query, params)
    events = [
        {
            "id": row[0],
            "created_utc": row[1],
            "station": row[2],
            "transition_type": row[3],
            "instant_bucket_before": row[4],
            "instant_bucket_after": row[5],
            "settlement_bucket": row[6],
            "running_max": row[7],
            "current_temp": row[8],
            "metadata_json": row[9],
        }
        for row in rows
    ]

    return {
        "stream": "transition_events",
        "station": normalized_station or None,
        "count": len(events),
        "events": events,
    }


# Causal flow reconstruction: persisted transitions -> deterministic
# scoring projection. This is analysis-only and does not feed runtime logic.
def get_settlement_epoch_scores(*, station: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """
    Read-only deterministic scoring derived from emitted transition history.
    """
    transition_stream = get_emitted_transition_stream(station=station, limit=limit)
    ordered_events = list(reversed(transition_stream["events"]))
    scores = score_settlement_epochs(ordered_events)

    return {
        "source": "transition_events",
        "station": transition_stream["station"],
        "transition_count": transition_stream["count"],
        "epoch_count": len(scores),
        "scores": serialize_epoch_scores(scores),
    }


def get_input_acceptance_visibility(*, station: Optional[str] = None) -> Dict[str, Any]:
    """
    Read-only visibility into accepted observations currently held in
    authoritative immutable snapshot.
    """
    normalized_station = (station or "").strip().upper()
    snapshot = immutable_public_state_snapshot()
    verify_observability_read_only(snapshot)

    last_seen_iso = snapshot["last_seen_iso"]
    last_obs = snapshot["last_obs"]

    if normalized_station:
        visibility = {
            normalized_station: {
                "last_seen_iso": last_seen_iso.get(normalized_station),
                "last_observation": last_obs.get(normalized_station),
            }
        }
    else:
        stations = sorted(set(last_seen_iso.keys()) | set(last_obs.keys()))
        visibility = {
            code: {
                "last_seen_iso": last_seen_iso.get(code),
                "last_observation": last_obs.get(code),
            }
            for code in stations
        }

    return {
        "source": "authoritative_state_snapshot",
        "station": normalized_station or None,
        "accepted_inputs": visibility,
    }


def get_execution_boundary_markers() -> Dict[str, Any]:
    """
    Read-only scheduler boundary markers from immutable authoritative snapshot.
    """
    snapshot = immutable_public_state_snapshot()
    verify_observability_read_only(snapshot)
    return {
        "source": "authoritative_state_snapshot",
        "poll_count": snapshot["poll_count"],
        "last_poll_utc": snapshot["last_poll_utc"],
        "last_loop_utc": snapshot["last_loop_utc"],
    }


def get_persistence_confirmation_events(*, station: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """
    Read-only persistence confirmation exposure for authoritative audit tables.
    """
    normalized_station = (station or "").strip().upper()
    bounded_limit = max(1, min(int(limit), 1000))

    params: Tuple[Any, ...]
    if normalized_station:
        transition_query = """
            SELECT id, created_utc, station, 'transition_events' AS table_name
            FROM transition_events
            WHERE station = ?
            ORDER BY id DESC
            LIMIT ?
        """
        alert_query = """
            SELECT id, created_utc, station, 'alerts' AS table_name
            FROM alerts
            WHERE station = ?
            ORDER BY id DESC
            LIMIT ?
        """
        params = (normalized_station, bounded_limit)
    else:
        transition_query = """
            SELECT id, created_utc, station, 'transition_events' AS table_name
            FROM transition_events
            ORDER BY id DESC
            LIMIT ?
        """
        alert_query = """
            SELECT id, created_utc, station, 'alerts' AS table_name
            FROM alerts
            ORDER BY id DESC
            LIMIT ?
        """
        params = (bounded_limit,)

    transition_rows = _query_rows_readonly(transition_query, params)
    alert_rows = _query_rows_readonly(alert_query, params)

    confirmations = [
        {
            "id": row[0],
            "created_utc": row[1],
            "station": row[2],
            "table": row[3],
        }
        for row in (transition_rows + alert_rows)
    ]
    confirmations.sort(key=lambda item: (item["created_utc"] or "", item["id"] or 0), reverse=True)

    return {
        "database_present": _db_exists(),
        "station": normalized_station or None,
        "count": len(confirmations),
        "events": confirmations[:bounded_limit],
    }


def get_current_settlement_epoch_summaries(*, station: Optional[str] = None) -> Dict[str, Any]:
    """
    Deterministic per-station (and market_type) current epoch visibility.

    Selection rule per station+market_type key:
      1) Prefer currently open epoch (epoch_status='open').
      2) If no open epoch exists, fall back to latest epoch by id.
    """
    normalized_station = (station or "").strip().upper()

    open_params: Tuple[Any, ...]
    latest_params: Tuple[Any, ...]

    if normalized_station:
        open_query = """
            SELECT se.station,
                   se.market_type,
                   se.id,
                   se.local_trading_date,
                   se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.epoch_close_reason,
                   se.epoch_close_timestamp_utc,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.settlement_transition_event_id,
                   se.last_transition_event_id,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'open_epoch' AS selection_source
            FROM settlement_epochs se
            WHERE se.station = ?
              AND se.epoch_status = 'open'
              AND se.id = (
                  SELECT MAX(inner_open.id)
                  FROM settlement_epochs inner_open
                  WHERE inner_open.station = se.station
                    AND ((inner_open.market_type IS NULL AND se.market_type IS NULL) OR inner_open.market_type = se.market_type)
                    AND inner_open.epoch_status = 'open'
              )
        """
        latest_query = """
            SELECT se.station,
                   se.market_type,
                   se.id,
                   se.local_trading_date,
                   se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.epoch_close_reason,
                   se.epoch_close_timestamp_utc,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.settlement_transition_event_id,
                   se.last_transition_event_id,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'latest_epoch_fallback' AS selection_source
            FROM settlement_epochs se
            WHERE se.station = ?
              AND se.id = (
                  SELECT MAX(inner_latest.id)
                  FROM settlement_epochs inner_latest
                  WHERE inner_latest.station = se.station
                    AND ((inner_latest.market_type IS NULL AND se.market_type IS NULL) OR inner_latest.market_type = se.market_type)
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM settlement_epochs open_exists
                  WHERE open_exists.station = se.station
                    AND ((open_exists.market_type IS NULL AND se.market_type IS NULL) OR open_exists.market_type = se.market_type)
                    AND open_exists.epoch_status = 'open'
              )
        """
        open_params = (normalized_station,)
        latest_params = (normalized_station,)
    else:
        open_query = """
            SELECT se.station,
                   se.market_type,
                   se.id,
                   se.local_trading_date,
                   se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.epoch_close_reason,
                   se.epoch_close_timestamp_utc,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.settlement_transition_event_id,
                   se.last_transition_event_id,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'open_epoch' AS selection_source
            FROM settlement_epochs se
            WHERE se.epoch_status = 'open'
              AND se.id = (
                  SELECT MAX(inner_open.id)
                  FROM settlement_epochs inner_open
                  WHERE inner_open.station = se.station
                    AND ((inner_open.market_type IS NULL AND se.market_type IS NULL) OR inner_open.market_type = se.market_type)
                    AND inner_open.epoch_status = 'open'
              )
        """
        latest_query = """
            SELECT se.station,
                   se.market_type,
                   se.id,
                   se.local_trading_date,
                   se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.epoch_close_reason,
                   se.epoch_close_timestamp_utc,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.settlement_transition_event_id,
                   se.last_transition_event_id,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'latest_epoch_fallback' AS selection_source
            FROM settlement_epochs se
            WHERE se.id = (
                  SELECT MAX(inner_latest.id)
                  FROM settlement_epochs inner_latest
                  WHERE inner_latest.station = se.station
                    AND ((inner_latest.market_type IS NULL AND se.market_type IS NULL) OR inner_latest.market_type = se.market_type)
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM settlement_epochs open_exists
                  WHERE open_exists.station = se.station
                    AND ((open_exists.market_type IS NULL AND se.market_type IS NULL) OR open_exists.market_type = se.market_type)
                    AND open_exists.epoch_status = 'open'
              )
        """
        open_params = ()
        latest_params = ()

    open_rows = _query_rows_readonly(open_query, open_params)
    fallback_rows = _query_rows_readonly(latest_query, latest_params)
    rows = sorted(open_rows + fallback_rows, key=lambda row: ((row[0] or ""), (row[1] or "")))

    summaries = [
        {
            "station": row[0],
            "market_type": row[1],
            "epoch_id": row[2],
            "local_trading_date": row[3],
            "settlement_bucket": row[4],
            "prior_settlement_bucket": row[5],
            "settlement_timestamp_utc": row[6],
            "settlement_jump_magnitude": row[7],
            "epoch_status": row[8],
            "epoch_close_reason": row[9],
            "epoch_close_timestamp_utc": row[10],
            "reversion_occurred": bool(row[11]),
            "first_reversion_timestamp_utc": row[12],
            "max_excursion_above_settlement": row[13],
            "duration_at_or_above_settlement_seconds": row[14],
            "duration_strictly_above_settlement_seconds": row[15],
            "terminal_state_reached": bool(row[16]),
            "settlement_transition_event_id": row[17],
            "last_transition_event_id": row[18],
            "last_transition_timestamp_utc": row[19],
            "last_transition_temp_f": row[20],
            "selection_source": row[21],
            "is_open_epoch": row[21] == "open_epoch",
        }
        for row in rows
    ]

    return {
        "database_present": _db_exists(),
        "station": normalized_station or None,
        "count": len(summaries),
        "epochs": summaries,
    }


def get_current_day_structure_summaries(
    *,
    station_day_keys: Dict[str, str],
    station: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Deterministic per-station current-day structural settlement epoch summary.

    Selection rule per station for current/latest fields:
      1) Prefer latest open epoch for station and local_trading_date.
      2) If none exists, fall back to latest epoch by id for that day.
    """
    normalized_station = (station or "").strip().upper()

    if normalized_station:
        stations = [normalized_station] if normalized_station in station_day_keys else []
    else:
        stations = sorted(station_day_keys.keys())

    rows: List[Dict[str, Any]] = []
    for station_code in stations:
        local_day_key = station_day_keys.get(station_code)
        if not local_day_key:
            continue

        aggregate_row = _query_rows_readonly(
            """
            SELECT COUNT(*) AS epoch_count_today,
                   MAX(CASE WHEN epoch_status = 'open' THEN 1 ELSE 0 END) AS open_epoch_present,
                   SUM(CASE WHEN epoch_status = 'closed' THEN 1 ELSE 0 END) AS closed_epoch_count_today,
                   SUM(CASE WHEN reversion_occurred = 1 THEN 1 ELSE 0 END) AS reverted_epoch_count_today,
                   SUM(CASE WHEN epoch_close_reason = 'terminal_state' THEN 1 ELSE 0 END) AS terminal_epoch_count_today
            FROM settlement_epochs
            WHERE station = ?
              AND local_trading_date = ?
            """,
            (station_code, local_day_key),
        )

        epoch_count_today = int((aggregate_row[0][0] or 0) if aggregate_row else 0)
        open_epoch_present = bool((aggregate_row[0][1] or 0) if aggregate_row else 0)
        closed_epoch_count_today = int((aggregate_row[0][2] or 0) if aggregate_row else 0)
        reverted_epoch_count_today = int((aggregate_row[0][3] or 0) if aggregate_row else 0)
        terminal_epoch_count_today = int((aggregate_row[0][4] or 0) if aggregate_row else 0)

        selected_rows = _query_rows_readonly(
            """
            SELECT se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'open_epoch' AS selection_source
            FROM settlement_epochs se
            WHERE se.station = ?
              AND se.local_trading_date = ?
              AND se.epoch_status = 'open'
              AND se.id = (
                  SELECT MAX(inner_open.id)
                  FROM settlement_epochs inner_open
                  WHERE inner_open.station = se.station
                    AND inner_open.local_trading_date = se.local_trading_date
                    AND inner_open.epoch_status = 'open'
              )
            UNION ALL
            SELECT se.settlement_bucket,
                   se.prior_settlement_bucket,
                   se.settlement_timestamp_utc,
                   se.settlement_jump_magnitude,
                   se.epoch_status,
                   se.reversion_occurred,
                   se.first_reversion_timestamp_utc,
                   se.max_excursion_above_settlement,
                   se.duration_at_or_above_settlement_seconds,
                   se.duration_strictly_above_settlement_seconds,
                   se.terminal_state_reached,
                   se.last_transition_timestamp_utc,
                   se.last_transition_temp_f,
                   'latest_epoch_fallback' AS selection_source
            FROM settlement_epochs se
            WHERE se.station = ?
              AND se.local_trading_date = ?
              AND se.id = (
                  SELECT MAX(inner_latest.id)
                  FROM settlement_epochs inner_latest
                  WHERE inner_latest.station = se.station
                    AND inner_latest.local_trading_date = se.local_trading_date
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM settlement_epochs open_exists
                  WHERE open_exists.station = se.station
                    AND open_exists.local_trading_date = se.local_trading_date
                    AND open_exists.epoch_status = 'open'
              )
            """,
            (station_code, local_day_key, station_code, local_day_key),
        )
        selected = selected_rows[0] if selected_rows else None

        rows.append(
            {
                "station": station_code,
                "local_trading_date": local_day_key,
                "epoch_count_today": epoch_count_today,
                "open_epoch_present": open_epoch_present,
                "current_or_latest_settlement_bucket": selected[0] if selected else None,
                "current_or_latest_prior_settlement_bucket": selected[1] if selected else None,
                "current_or_latest_settlement_timestamp_utc": selected[2] if selected else None,
                "current_or_latest_settlement_jump_magnitude": selected[3] if selected else None,
                "current_or_latest_epoch_status": selected[4] if selected else None,
                "current_or_latest_reversion_occurred": bool(selected[5]) if selected else None,
                "current_or_latest_first_reversion_timestamp_utc": selected[6] if selected else None,
                "current_or_latest_max_excursion_above_settlement": selected[7] if selected else None,
                "current_or_latest_duration_at_or_above_settlement_seconds": selected[8] if selected else None,
                "current_or_latest_duration_strictly_above_settlement_seconds": selected[9] if selected else None,
                "current_or_latest_terminal_state_reached": bool(selected[10]) if selected else None,
                "latest_transition_timestamp_utc": selected[11] if selected else None,
                "latest_transition_temp_f": selected[12] if selected else None,
                "closed_epoch_count_today": closed_epoch_count_today,
                "reverted_epoch_count_today": reverted_epoch_count_today,
                "terminal_epoch_count_today": terminal_epoch_count_today,
                "latest_selection_source": selected[13] if selected else None,
            }
        )

    return {
        "database_present": _db_exists(),
        "station": normalized_station or None,
        "count": len(rows),
        "rows": rows,
    }
