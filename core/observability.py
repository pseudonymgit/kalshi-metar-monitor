import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from core.authoritative_state import immutable_public_state_snapshot
from core.metar_monitor import _alert_db_path
from core.scoring_engine import score_settlement_epochs, serialize_epoch_scores


ReadOnlyRow = Tuple[Any, ...]


def _db_exists() -> bool:
    db_path = _alert_db_path()
    return os.path.exists(db_path)


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
