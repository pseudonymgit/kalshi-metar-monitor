import os
import sqlite3
from typing import Any, Dict, Optional, Tuple

from core.station_time import parse_iso_utc, station_local_day_key


_SETTLEMENT_UP = "settlement_up"
_OPEN = "open"
_CLOSED = "closed"


def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")


def _event_day_key(station: str, obs_timestamp_utc: Optional[str], event_timestamp_utc: Optional[str]) -> str:
    source_timestamp = obs_timestamp_utc if parse_iso_utc(obs_timestamp_utc) is not None else event_timestamp_utc
    return station_local_day_key(station, source_timestamp)


def _close_epoch(
    conn: sqlite3.Connection,
    *,
    epoch_id: int,
    close_reason: str,
    close_timestamp_utc: Optional[str],
    terminal_state_reached: bool,
) -> None:
    conn.execute(
        """
        UPDATE settlement_epochs
        SET epoch_status = ?,
            epoch_close_reason = ?,
            epoch_close_timestamp_utc = ?,
            terminal_state_reached = ?
        WHERE id = ?
        """,
        (
            _CLOSED,
            close_reason,
            close_timestamp_utc,
            1 if terminal_state_reached else 0,
            epoch_id,
        ),
    )


def _maybe_add_duration(
    *,
    start_iso: Optional[str],
    end_iso: Optional[str],
    previous_temp: Optional[float],
    settlement_bucket: int,
    at_or_above_seconds: float,
    strictly_above_seconds: float,
) -> Tuple[float, float]:
    if previous_temp is None:
        return at_or_above_seconds, strictly_above_seconds

    start_dt = parse_iso_utc(start_iso)
    end_dt = parse_iso_utc(end_iso)
    if start_dt is None or end_dt is None:
        return at_or_above_seconds, strictly_above_seconds

    delta_seconds = max(0.0, (end_dt - start_dt).total_seconds())
    if delta_seconds <= 0:
        return at_or_above_seconds, strictly_above_seconds

    if previous_temp >= settlement_bucket:
        at_or_above_seconds += delta_seconds
    if previous_temp > settlement_bucket:
        strictly_above_seconds += delta_seconds

    return at_or_above_seconds, strictly_above_seconds


def log_transition_for_settlement_epoch(
    *,
    station: str,
    transition_type: Optional[str],
    settlement_bucket: int,
    current_temp: float,
    metadata: Optional[Dict[str, Any]],
    transition_event_id: Optional[int],
    event_timestamp_utc: Optional[str],
) -> None:
    transition_name = str(transition_type or "").strip()
    if not transition_name:
        return

    metadata_dict = metadata or {}
    market_type = metadata_dict.get("market_type")
    obs_timestamp_utc = metadata_dict.get("obs_time")
    terminal_state_reached = bool(metadata_dict.get("terminal_state_reached"))

    station_normalized = (station or "").strip().upper()
    day_key = _event_day_key(station_normalized, obs_timestamp_utc, event_timestamp_utc)

    db_path = _alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=1)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settlement_epochs (
                id INTEGER PRIMARY KEY,
                station TEXT NOT NULL,
                market_type TEXT,
                local_trading_date TEXT NOT NULL,
                settlement_bucket INTEGER NOT NULL,
                prior_settlement_bucket INTEGER,
                settlement_timestamp_utc TEXT NOT NULL,
                settlement_jump_magnitude INTEGER,
                epoch_status TEXT NOT NULL,
                epoch_close_reason TEXT,
                epoch_close_timestamp_utc TEXT,
                reversion_occurred INTEGER NOT NULL DEFAULT 0,
                first_reversion_timestamp_utc TEXT,
                max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
                duration_at_or_above_settlement_seconds REAL NOT NULL DEFAULT 0,
                duration_strictly_above_settlement_seconds REAL NOT NULL DEFAULT 0,
                terminal_state_reached INTEGER NOT NULL DEFAULT 0,
                settlement_transition_event_id INTEGER,
                last_transition_event_id INTEGER,
                last_transition_timestamp_utc TEXT,
                last_transition_temp_f REAL
            )
            """
        )

        existing = conn.execute(
            """
            SELECT id,
                   local_trading_date,
                   settlement_bucket,
                   duration_at_or_above_settlement_seconds,
                   duration_strictly_above_settlement_seconds,
                   last_transition_timestamp_utc,
                   last_transition_temp_f,
                   max_excursion_above_settlement,
                   terminal_state_reached
            FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND epoch_status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (station_normalized, market_type, market_type, _OPEN),
        ).fetchone()

        if existing:
            (
                epoch_id,
                open_day_key,
                open_settlement_bucket,
                duration_at_or_above,
                duration_strictly_above,
                last_transition_timestamp,
                last_transition_temp,
                max_excursion,
                existing_terminal,
            ) = existing

            if str(open_day_key) != day_key:
                _close_epoch(
                    conn,
                    epoch_id=int(epoch_id),
                    close_reason="day_reset",
                    close_timestamp_utc=event_timestamp_utc,
                    terminal_state_reached=bool(existing_terminal),
                )
                existing = None
            elif transition_name == _SETTLEMENT_UP:
                _close_epoch(
                    conn,
                    epoch_id=int(epoch_id),
                    close_reason="next_settlement_up",
                    close_timestamp_utc=event_timestamp_utc,
                    terminal_state_reached=bool(existing_terminal) or terminal_state_reached,
                )
                existing = None
            else:
                new_at_or_above, new_strictly_above = _maybe_add_duration(
                    start_iso=last_transition_timestamp,
                    end_iso=event_timestamp_utc,
                    previous_temp=last_transition_temp,
                    settlement_bucket=int(open_settlement_bucket),
                    at_or_above_seconds=float(duration_at_or_above or 0.0),
                    strictly_above_seconds=float(duration_strictly_above or 0.0),
                )
                new_max_excursion = max(float(max_excursion or 0.0), float(current_temp) - int(open_settlement_bucket), 0.0)

                reversion_occurred = 1 if transition_name == "reversion_after_settlement" else 0
                first_reversion_ts = event_timestamp_utc if reversion_occurred else None

                conn.execute(
                    """
                    UPDATE settlement_epochs
                    SET duration_at_or_above_settlement_seconds = ?,
                        duration_strictly_above_settlement_seconds = ?,
                        max_excursion_above_settlement = ?,
                        reversion_occurred = CASE
                            WHEN reversion_occurred = 1 THEN 1
                            ELSE ?
                        END,
                        first_reversion_timestamp_utc = CASE
                            WHEN first_reversion_timestamp_utc IS NOT NULL THEN first_reversion_timestamp_utc
                            ELSE ?
                        END,
                        terminal_state_reached = CASE
                            WHEN terminal_state_reached = 1 THEN 1
                            ELSE ?
                        END,
                        last_transition_event_id = ?,
                        last_transition_timestamp_utc = ?,
                        last_transition_temp_f = ?
                    WHERE id = ?
                    """,
                    (
                        new_at_or_above,
                        new_strictly_above,
                        new_max_excursion,
                        reversion_occurred,
                        first_reversion_ts,
                        1 if terminal_state_reached else 0,
                        transition_event_id,
                        event_timestamp_utc,
                        float(current_temp),
                        int(epoch_id),
                    ),
                )

        if transition_name == _SETTLEMENT_UP:
            prior_settlement_bucket = metadata_dict.get("previous_settlement_bucket")
            jump = None
            if isinstance(prior_settlement_bucket, int):
                jump = int(settlement_bucket) - int(prior_settlement_bucket)

            initial_excursion = max(float(current_temp) - int(settlement_bucket), 0.0)
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station,
                    market_type,
                    local_trading_date,
                    settlement_bucket,
                    prior_settlement_bucket,
                    settlement_timestamp_utc,
                    settlement_jump_magnitude,
                    epoch_status,
                    epoch_close_reason,
                    reversion_occurred,
                    first_reversion_timestamp_utc,
                    max_excursion_above_settlement,
                    duration_at_or_above_settlement_seconds,
                    duration_strictly_above_settlement_seconds,
                    terminal_state_reached,
                    settlement_transition_event_id,
                    last_transition_event_id,
                    last_transition_timestamp_utc,
                    last_transition_temp_f
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, 0, 0, ?, ?, ?, ?, ?)
                """,
                (
                    station_normalized,
                    market_type,
                    day_key,
                    int(settlement_bucket),
                    prior_settlement_bucket if isinstance(prior_settlement_bucket, int) else None,
                    event_timestamp_utc,
                    jump,
                    _OPEN,
                    initial_excursion,
                    1 if terminal_state_reached else 0,
                    transition_event_id,
                    transition_event_id,
                    event_timestamp_utc,
                    float(current_temp),
                ),
            )

            if terminal_state_reached:
                new_epoch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                _close_epoch(
                    conn,
                    epoch_id=new_epoch_id,
                    close_reason="terminal_state",
                    close_timestamp_utc=event_timestamp_utc,
                    terminal_state_reached=True,
                )

        conn.commit()
    finally:
        conn.close()
