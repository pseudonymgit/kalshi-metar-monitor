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
from collections import deque
from io import StringIO
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from core.authoritative_state import (
    commit_temperature_state,
    immutable_public_state_snapshot,
    read_temperature_state,
    reset_station_daily_state,
    set_latest_observation,
    state_lock,
    state_ref,
)
from core.transition_emitter import emit_transition_if_changed
from core.replay_engine import execute_ordered_replay_stream
from core.security_boundaries import enforce_execution_domain_guard
from core.station_time import station_local_day_key, station_timezone_name, to_station_local

# zoneinfo (Python 3.9+). If unavailable, we'll no-op ET/local conversions.
try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None

# -------- Constants --------
ET_TZ_NAME = "America/New_York"
OVERLAP_SECONDS = 120               # small overlap to avoid missing late arrivals
FIRST_RUN_CUSHION_SEC = 300         # first contact: add 5 min cushion
METAR_ACCEPTANCE_GRACE_SECONDS = min(900, OVERLAP_SECONDS * 5)  # 15 minutes max, bounded by overlap safety

def _icao_tz_name(icao: str) -> str:
    return station_timezone_name(icao)



# =========================
# In-memory state (authoritative owner lives in core.authoritative_state)
# =========================
_STATE_LOCK = state_lock()
_STATE = state_ref()

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_LOCK = threading.Lock()
_LIVE_STATION_UNIVERSE_RESOLVER = None
_AUDIT_LOCK = threading.Lock()
_MISSING_LADDER_DEDUPE = {}
_MISSING_LADDER_LOCK = threading.Lock()
_KALSHI_RATE_LIMIT_LOCK = threading.Lock()
_KALSHI_LAST_CALL_TS = {}
_KALSHI_CALL_THROTTLE_SECONDS = 5
_ALERT_LOGGER = logging.getLogger(__name__)
_TRANSITION_HISTORY = deque(maxlen=500)
_TRANSITION_LOCK = threading.Lock()


# =========================
# Config
# =========================
def get_default_config() -> Dict[str, Any]:
    """
    Builds runtime config from env (keeps your AWC_* names; HTTP_* works too).
    """
    http_from = (
        os.getenv("AWC_FROM_EMAIL")
        or os.getenv("HTTP_FROM_EMAIL")
        or "you@example.com"
    )
    http_agent = (
        os.getenv("AWC_USER_AGENT")
        or os.getenv("HTTP_USER_AGENT")
        or "KalshiMetarMonitor/1.1 (+you@example.com)"
    )

    return {
        "stations": json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]')),
        "poll_seconds": int(os.getenv("METAR_POLL_SECONDS", "60")),
        # delta_f retained for compatibility but no longer used for integer-cross alerts
        "delta_f": float(os.getenv("TEMP_ALERT_DELTA_F", "1.0")),
        "webhook": os.getenv("ALERT_WEBHOOK_URL", ""),
        "cache_file": os.getenv("METAR_CACHE_FILE", "/opt/render/project/src/data/metar_state.json"),

        # Source control
        "default_source": (os.getenv("METAR_DEFAULT_SOURCE") or "nws").lower(),
        "strict": os.getenv("METAR_STRICT", "true").lower() in ("1", "true", "yes", "y"),

        # Etiquette for api.weather.gov
        "http_from": http_from,
        "http_agent": http_agent,

        # Windows
        "iem_hours": int(os.getenv("IEM_LOOKBACK_HOURS", "1")),
        "lookback_min": int(os.getenv("METAR_LOOKBACK_MIN", "3")),
    }


# =========================
# Time helpers
# =========================
def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _parse_iso(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def _iso_to_tz(iso_str: Optional[str], tz_name: str) -> Optional[str]:
    if not iso_str:
        return None
    try:
        dt = _parse_iso(iso_str)
        if ZoneInfo is None:
            return dt.isoformat()  # fallback: keep UTC
        return dt.astimezone(ZoneInfo(tz_name)).isoformat()
    except Exception:
        return iso_str


def _to_local(icao: str, dt_utc: datetime) -> datetime:
    """Convert a UTC datetime to the station's local timezone."""
    return to_station_local(icao, dt_utc)


def _maybe_daily_reset_local(icao: str, dt_iso: str) -> None:
    """Reset integer-cross memory once per *local* day for this station."""
    dt_utc = _parse_iso(dt_iso)
    dt_local = _to_local(icao, dt_utc)
    local_day = dt_local.date().isoformat()
    with _STATE_LOCK:
        last = _STATE["last_reset_date_local"].get(icao)
    if last != local_day:
        reset_station_daily_state(icao, local_day)
        _prune_transition_events()


# =========================
# Cache helpers
# =========================
def _load_cache(path: str) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[ERROR] station=UNK function=_load_cache: {e}")
    return {}


def _save_cache(path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass  # best-effort caching


# =========================
# State boot
# =========================
def ensure_state_loaded():
    cfg = get_default_config()
    with _STATE_LOCK:
        if not _STATE["cfg"]:
            _STATE["cfg"] = cfg
        if not _STATE["stations"]:
            _STATE["stations"] = cfg["stations"]

        cache = _load_cache(cfg["cache_file"])
        if "last_obs" in cache:
            _STATE["last_obs"].update(cache["last_obs"])
        if "last_seen_iso" in cache:
            _STATE["last_seen_iso"].update(cache["last_seen_iso"])

        # Persist daily-reset + last observed integer across restarts
        if "last_reset_date_local" in cache:
            _STATE["last_reset_date_local"].update(cache["last_reset_date_local"])
        if "last_observed_integer" in cache:
            _STATE["last_observed_integer"].update(cache["last_observed_integer"])
        if "running_daily_max" in cache:
            _STATE["running_daily_max"].update(cache["running_daily_max"])
        if "last_settlement_bucket" in cache:
            _STATE["last_settlement_bucket"].update(cache["last_settlement_bucket"])
        if "last_instant_bucket" in cache:
            _STATE["last_instant_bucket"].update(cache["last_instant_bucket"])


def get_state() -> Dict[str, Any]:
    snapshot = immutable_public_state_snapshot()
    return {
        "stations": list(snapshot["stations"]),
        "last_obs": dict(snapshot["last_obs"]),
        "last_alert": dict(snapshot["last_alert"]),
        "last_seen_iso": dict(snapshot["last_seen_iso"]),
        "last_reset_date_local": dict(snapshot["last_reset_date_local"]),
        "last_observed_integer": dict(snapshot["last_observed_integer"]),
        "running_daily_max": dict(snapshot["running_daily_max"]),
        "last_settlement_bucket": dict(snapshot["last_settlement_bucket"]),
        "last_instant_bucket": dict(snapshot["last_instant_bucket"]),
        "cfg": dict(snapshot["cfg"]),
        "poll_count": snapshot["poll_count"],
        "last_poll_utc": snapshot["last_poll_utc"],
        "last_loop_utc": snapshot["last_loop_utc"],
    }


# =========================
# Source helpers
# =========================
def _headers_for_nws(cfg) -> Dict[str, str]:
    return {
        "User-Agent": cfg["http_agent"],
        "From": cfg["http_from"],
        "Accept": "application/geo+json",
    }


def _iso_seconds_z(dt: datetime) -> str:
    # api.weather.gov prefers 'Z' (UTC) with seconds precision
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nws_range_url(icao: str, start_iso_z: str, end_iso_z: str, limit: int = 200) -> str:
    return (
        f"https://api.weather.gov/stations/{icao}/observations"
        f"?start={start_iso_z}&end={end_iso_z}&limit={min(limit, 200)}"
    )


def _iem_range_url(icao: str, hours: int) -> str:
    # Use CSV; we'll filter client-side to [start,end] UTC.
    return (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={icao}&data=tmpf&tz=UTC&format=comma&hours={hours}"
    )


def _tgftp_latest_url(icao: str) -> str:
    return f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"


# =========================
# Parse helpers
# =========================
def _obs_tuple(temp_f: float, ts_iso: str, raw: Any, source: str) -> Dict[str, Any]:
    return {"temp_f": round(float(temp_f), 1), "obs_time": ts_iso, "raw": raw, "source": source}


def _record_timeout(icao: str):
    from datetime import datetime, timezone

    with _STATE_LOCK:
        _STATE["timeout_count"] += 1
        _STATE["last_timeout_station"] = icao
        _STATE["last_timeout_utc"] = datetime.now(timezone.utc).isoformat()


def _parse_nws_collection(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for f in j.get("features", []):
        props = f.get("properties", {})
        val_c = props.get("temperature", {}).get("value")
        ts = props.get("timestamp")
        if val_c is None or not ts:
            continue
        out.append(_obs_tuple(_c_to_f(float(val_c)), ts, props, "nws"))
    out.sort(key=lambda x: _parse_iso(x["obs_time"]))
    return out


def _parse_iem_csv(text: str, start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    # If IEM returns HTML (maintenance) or ERROR, avoid exceptions.
    if text.lstrip().startswith("<") or text.strip().upper().startswith("ERROR"):
        return []
    out: List[Dict[str, Any]] = []
    rdr = csv.DictReader(StringIO(text))
    for row in rdr:
        valid = row.get("valid")
        tmpf = row.get("tmpf")
        if not valid or tmpf in (None, "", "M"):
            continue
        try:
            ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < start_dt or ts > end_dt:
            continue
        try:
            tf = float(tmpf)
        except Exception:
            continue
        out.append(_obs_tuple(tf, ts.isoformat(), row, "iem"))
    out.sort(key=lambda x: _parse_iso(x["obs_time"]))
    return out


def _parse_tgftp_text(text: str) -> Optional[Dict[str, Any]]:
    lines = text.strip().splitlines()
    if not lines:
        return None
    ts_line = lines[0].strip()
    metar_line = lines[-1].strip()

    # Find temp from token like 12/01 or M02/M05
    temp_f: Optional[float] = None
    for token in metar_line.split():
        if "/" in token and len(token) <= 6:
            left, _, _right = token.partition("/")
            try:
                c = -float(left[1:]) if left.startswith("M") else float(left)
                temp_f = _c_to_f(c)
                break
            except Exception:
                continue
    if temp_f is None:
        return None

    try:
        ts = datetime.strptime(ts_line, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        ts = _now_utc_iso()
    return _obs_tuple(temp_f, ts, metar_line, "tgftp")


# =========================
# Range fetchers (strict by source)
# =========================
def _fetch_range_nws(icao: str, start_iso_z: str, end_iso_z: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = _nws_range_url(icao, start_iso_z, end_iso_z)
    try:
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    except requests.exceptions.Timeout:
        _record_timeout(icao)
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    r.raise_for_status()
    return _parse_nws_collection(r.json())


def _fetch_nws_latest_single(icao: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"https://api.weather.gov/stations/{icao}/observations/latest"
    try:
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    except requests.exceptions.Timeout:
        _record_timeout(icao)
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    r.raise_for_status()
    j = r.json()
    props = j.get("properties", {}) if isinstance(j, dict) else {}
    val_c = props.get("temperature", {}).get("value")
    ts = props.get("timestamp")
    if val_c is None or not ts:
        return None
    return _obs_tuple(_c_to_f(float(val_c)), ts, props, "nws")


def _fetch_range_iem(icao: str, start_dt: datetime, end_dt: datetime, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    minutes = max(1, int((end_dt - start_dt).total_seconds() // 60))
    hours = max(1, min(3, (minutes // 60) + 1, int(cfg.get("iem_hours", 1))))
    url = _iem_range_url(icao, hours)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return _parse_iem_csv(r.text, start_dt, end_dt)


def _fetch_latest_tgftp(icao: str) -> List[Dict[str, Any]]:
    r = requests.get(_tgftp_latest_url(icao), timeout=15)
    r.raise_for_status()
    parsed = _parse_tgftp_text(r.text)
    return [parsed] if parsed else []


# =========================
# Ingestion (dedupe & alerts)
# =========================
def _ingest_obs(
    icao: str,
    new_obs: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    allow_alert_delivery: bool = True,
    persist_cache: bool = True,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Tuple[int, int]:
    """
    Ingests observations in chronological order.
    Returns (ingested_count, alerts_count).
    """
    if not new_obs:
        return (0, 0)

    enforce_execution_domain_guard(
        allow_alert_delivery=allow_alert_delivery,
        persist_cache=persist_cache,
    )

    with _STATE_LOCK:
        last_seen_iso = _STATE["last_seen_iso"].get(icao)
        last_temp = _STATE["last_obs"].get(icao, {}).get("temp_f")

    ingested = 0
    alerts = 0
    window_end_day_key = station_local_day_key(icao, window_end.isoformat()) if window_end else None

    for obs in new_obs:
        ts = obs["obs_time"]
        obs_dt = _parse_iso(ts)

        if window_end and obs_dt > window_end:
            continue

        if window_start:
            grace_start = window_start - timedelta(seconds=METAR_ACCEPTANCE_GRACE_SECONDS)
            if obs_dt < grace_start:
                continue

            obs_day_key = station_local_day_key(icao, obs_dt.isoformat())
            if window_end_day_key and obs_day_key != window_end_day_key:
                continue

            if obs_dt < window_start:
                lag_seconds = int((window_start - obs_dt).total_seconds())
                _ALERT_LOGGER.debug(f"accepted_with_grace station={icao} lag_seconds={lag_seconds}")

        if last_seen_iso and obs_dt <= _parse_iso(last_seen_iso):
            continue

        # store through authoritative state owner
        set_latest_observation(icao, obs, ts)

        ingested += 1

        alerts += _process_temperature_event(
            icao=icao,
            temp_f=float(obs["temp_f"]),
            obs_time=ts,
            cfg=cfg,
            last_temp_f=last_temp,
            allow_alert_delivery=allow_alert_delivery,
        )

        last_temp = obs["temp_f"]

    if persist_cache:
        with _STATE_LOCK:
            _save_cache(cfg["cache_file"], {
                "last_obs": _STATE["last_obs"],
                "last_seen_iso": _STATE["last_seen_iso"],
                "last_reset_date_local": _STATE["last_reset_date_local"],
                "last_observed_integer": _STATE["last_observed_integer"],
                "running_daily_max": _STATE["running_daily_max"],
                "last_settlement_bucket": _STATE["last_settlement_bucket"],
                "last_instant_bucket": _STATE["last_instant_bucket"],
            })

    return (ingested, alerts)


def _emit_alert(
    icao: str,
    prev_f: float,
    now_f: float,
    delta_f: float,
    obs_time: str,
    cfg: Dict[str, Any],
    instant_bucket_changed: bool = False,
    settlement_bucket_changed: bool = False,
    transition_correlation: Optional[Dict[str, Any]] = None,
) -> None:
    payload = {
        "type": "temp_change",
        "station": icao,
        "prev_temp_f": prev_f,
        "temp_f": now_f,
        "delta_f": delta_f,
        "obs_time": obs_time,
        "at_utc": _now_utc_iso(),
        "instant_bucket_changed": bool(instant_bucket_changed),
        "settlement_bucket_changed": bool(settlement_bucket_changed),
        "transition_correlation": transition_correlation,
    }
    _send_alert(cfg.get("webhook", ""), payload)


def _process_temperature_event(
    icao: str,
    temp_f: float,
    obs_time: str,
    cfg: Dict[str, Any],
    last_temp_f: Optional[float] = None,
    allow_alert_delivery: bool = True,
) -> int:
    prev_f = float(last_temp_f) if last_temp_f is not None else float(temp_f)
    now_f = float(temp_f)

    _maybe_daily_reset_local(icao, obs_time)

    curr_floor = int(math.floor(now_f))
    instant_bucket = curr_floor
    temperature_state = read_temperature_state(icao)
    last_observed_integer = temperature_state["last_observed_integer"]
    prev_running_max = temperature_state["running_daily_max"]
    previous_settlement_bucket = temperature_state["last_settlement_bucket"]
    previous_instant_bucket = temperature_state["last_instant_bucket"]

    new_running_max = max(prev_running_max, now_f) if prev_running_max is not None else now_f
    settlement_bucket = int(math.floor(new_running_max))

    instant_changed = previous_instant_bucket is not None and instant_bucket != previous_instant_bucket
    settlement_changed = (
        previous_settlement_bucket is not None and settlement_bucket > previous_settlement_bucket
    )
    transition_type = None
    if previous_instant_bucket is not None:
        if instant_bucket > previous_instant_bucket:
            transition_type = "instant_up"
        elif instant_bucket < previous_instant_bucket:
            transition_type = "instant_down"
    if previous_settlement_bucket is not None and settlement_bucket > previous_settlement_bucket:
        transition_type = "settlement_up"
    if (
        transition_type == "instant_down"
        and previous_settlement_bucket is not None
        and settlement_bucket == previous_settlement_bucket
    ):
        transition_type = "reversion_after_settlement"

    transition_correlation = emit_transition_if_changed(
        transition_type=transition_type,
        instant_changed=instant_changed,
        settlement_changed=settlement_changed,
        station=icao,
        instant_bucket_before=previous_instant_bucket,
        instant_bucket_after=instant_bucket,
        settlement_bucket=settlement_bucket,
        running_max=new_running_max,
        current_temp=now_f,
        metadata={
            "obs_time": obs_time,
            "prev_temp_f": prev_f,
            "prev_running_max": prev_running_max,
            "previous_settlement_bucket": previous_settlement_bucket,
        },
        emit_fn=_log_transition_event,
    )

    alerts = 0
    if (
        last_observed_integer is not None
        and curr_floor != last_observed_integer
    ):
        _ALERT_LOGGER.info(
            "EVENT integer_cross station=%s market_type=ALL prev_int=%s curr_int=%s",
            icao,
            last_observed_integer,
            curr_floor,
        )
        if allow_alert_delivery:
            d = round(now_f - prev_f, 1)
            _emit_alert(
                icao,
                prev_f=prev_f,
                now_f=now_f,
                delta_f=d,
                obs_time=obs_time,
                cfg=cfg,
                instant_bucket_changed=instant_changed,
                settlement_bucket_changed=settlement_changed,
                transition_correlation=transition_correlation,
            )
        alerts = 1

    commit_temperature_state(
        icao=icao,
        curr_floor=curr_floor,
        running_daily_max=new_running_max,
        settlement_bucket=settlement_bucket,
        instant_bucket=instant_bucket,
    )

    return alerts


def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")


def _snapshot_station_state(station: str) -> Dict[str, Any]:
    station = (station or "").strip().upper()
    with _STATE_LOCK:
        last_obs = _STATE["last_obs"].get(station)
        return {
            "last_observed_integer": _STATE["last_observed_integer"].get(station),
            "running_daily_max": _STATE["running_daily_max"].get(station),
            "last_settlement_bucket": _STATE["last_settlement_bucket"].get(station),
            "last_instant_bucket": _STATE["last_instant_bucket"].get(station),
            "last_seen_iso": _STATE["last_seen_iso"].get(station),
            "last_obs": copy.deepcopy(last_obs),
            "last_reset_date_local": _STATE["last_reset_date_local"].get(station),
        }


def _restore_station_state(station: str, snapshot: Dict[str, Any]) -> None:
    station = (station or "").strip().upper()
    with _STATE_LOCK:
        for state_key, snapshot_key in (
            ("last_observed_integer", "last_observed_integer"),
            ("running_daily_max", "running_daily_max"),
            ("last_settlement_bucket", "last_settlement_bucket"),
            ("last_instant_bucket", "last_instant_bucket"),
            ("last_seen_iso", "last_seen_iso"),
            ("last_obs", "last_obs"),
            ("last_reset_date_local", "last_reset_date_local"),
        ):
            value = snapshot.get(snapshot_key)
            if value is None:
                _STATE[state_key].pop(station, None)
            else:
                _STATE[state_key][station] = value


def _reset_replay_runtime_state_for_station(station: str) -> None:
    station = (station or "").strip().upper()
    with _STATE_LOCK:
        _STATE["last_observed_integer"].pop(station, None)
        _STATE["running_daily_max"].pop(station, None)
        _STATE["last_settlement_bucket"].pop(station, None)
        _STATE["last_instant_bucket"].pop(station, None)
        _STATE["last_seen_iso"].pop(station, None)
        _STATE["last_obs"].pop(station, None)
        _STATE["last_reset_date_local"].pop(station, None)


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
            _reset_replay_runtime_state_for_station(station)
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

        _reset_replay_runtime_state_for_station(station)

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
    now_iso = _now_utc_iso()
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
                        json.dumps(metadata or {}, sort_keys=True),
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


def _annotate_transition_history_market_eval(
    station: str,
    transition_correlation: Optional[Dict[str, Any]],
    alerts_sent: int,
    evaluation_outcome: str,
    suppression_reason: Optional[str] = None,
) -> None:
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
        _ALERT_LOGGER.warning(
            "transition_market_eval_annotation_skipped station=%s reason=missing_correlation",
            normalized_station,
        )
        return
    safe_outcome = (evaluation_outcome or "").strip().upper() or "SUPPRESSED_UNKNOWN"
    safe_suppression_reason = (suppression_reason or "").strip().upper() or None

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
            if safe_suppression_reason:
                entry["suppression_reason"] = safe_suppression_reason
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
                if safe_suppression_reason:
                    metadata["suppression_reason"] = safe_suppression_reason

                conn.execute(
                    "UPDATE transition_events SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, sort_keys=True), row[0]),
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


def get_transition_history(station=None, limit=50):
    normalized_station = (station or "").strip().upper()
    try:
        bounded_limit = max(1, min(int(limit), 200))
    except Exception:
        bounded_limit = 50

    with _TRANSITION_LOCK:
        history = list(_TRANSITION_HISTORY)

    if normalized_station:
        history = [entry for entry in history if entry.get("station") == normalized_station]

    history = list(reversed(history))
    return history[:bounded_limit]


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
            }
    except Exception as e:
        _ALERT_LOGGER.warning("latest_market_eval_context_query_failed station=%s error=%s", normalized_station, e)

    return latest_by_station


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


def _simulate_temperature_for_testing(
    icao: str,
    temp_f: float,
    logger=None,
    allow_alert_delivery: bool = False,
) -> Dict[str, Any]:
    ensure_state_loaded()
    cfg = get_default_config()
    ts = _now_utc_iso()
    icao = (icao or "").strip().upper()

    _maybe_daily_reset_local(icao, ts)

    with _STATE_LOCK:
        last_temp = _STATE["last_obs"].get(icao, {}).get("temp_f")
        previous_integer = _STATE["last_observed_integer"].get(icao)
        _STATE["last_obs"][icao] = _obs_tuple(float(temp_f), ts, {"simulated": True}, "simulated")
        _STATE["last_seen_iso"][icao] = ts

    alerts = _process_temperature_event(
        icao=icao,
        temp_f=float(temp_f),
        obs_time=ts,
        cfg=cfg,
        last_temp_f=last_temp,
        allow_alert_delivery=allow_alert_delivery,
    )

    with _STATE_LOCK:
        current_integer = _STATE["last_observed_integer"].get(icao)

    delivery_attempted = allow_alert_delivery and alerts > 0

    if logger:
        logger.info(f"Simulated ladder event for {icao} at {temp_f}F (alerts={alerts})")

    return {
        "ok": True,
        "icao": icao,
        "temp_f": float(temp_f),
        "alerts_generated": alerts,
        "delivery_requested": allow_alert_delivery,
        "delivery_attempted": delivery_attempted,
        "previous_integer": previous_integer,
        "current_integer": current_integer,
        "crossed_integer": previous_integer is not None and previous_integer != current_integer,
    }


def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    if not webhook:
        return
    try:
        station = (payload.get("station") or "UNK").upper()
        tf = payload.get("temp_f")
        pf = payload.get("prev_temp_f")
        df = payload.get("delta_f")
        transition_correlation = payload.get("transition_correlation")

        if tf is None:
            return

        instant_bucket_changed = bool(payload.get("instant_bucket_changed"))
        settlement_bucket_changed = bool(payload.get("settlement_bucket_changed"))
        if not instant_bucket_changed and not settlement_bucket_changed:
            return

        now_ts = time.time()
        with _KALSHI_RATE_LIMIT_LOCK:
            last_call_ts = _KALSHI_LAST_CALL_TS.get(station)
            if last_call_ts is not None and (now_ts - last_call_ts) < _KALSHI_CALL_THROTTLE_SECONDS:
                return
            _KALSHI_LAST_CALL_TS[station] = now_ts

        try:
            from core.kalshi_monitor import (
                _get_active_stations,
                _parse_target_market_types,
                build_structured_snapshot,
                process_ladder_transition,
                send_composed_weather_market_alert,
            )

            active = _get_active_stations()
            station_is_active = active is None or station in active
            should_alert_on_missing = os.getenv("ALERT_ON_MISSING_LADDER", "false").lower() in ("1", "true", "yes", "y")
            target_market_types = _parse_target_market_types(
                os.getenv("KALSHI_TARGET_MARKET_TYPE")
            )
            if not target_market_types:
                target_market_types = {"HIGH"}

            evaluated_market_attempts = 0
            no_eligible_market_count = 0
            market_alerts_sent = 0
            suppression_reason = None
            saw_terminal_state = False

            if station_is_active:
                for market_type_token in sorted(target_market_types):
                    evaluated_market_attempts += 1
                    snapshot = build_structured_snapshot(station, {market_type_token})
                    markets = snapshot.get("markets") or []
                    _ALERT_LOGGER.info(
                        "EVAL ladder_check station=%s type=%s market_type=%s markets_found=%s",
                        station,
                        market_type_token,
                        market_type_token,
                        len(markets),
                    )

                    if not markets:
                        no_eligible_market_count += 1
                        if should_alert_on_missing:
                            try:
                                if _to_local:
                                    local_date = _to_local(station, datetime.now(timezone.utc)).date().isoformat()
                                else:
                                    local_date = datetime.now(timezone.utc).date().isoformat()
                            except Exception:
                                local_date = datetime.now(timezone.utc).date().isoformat()

                            dedupe_key = f"{station}_{market_type_token}_{local_date}"

                            with _MISSING_LADDER_LOCK:
                                if dedupe_key in _MISSING_LADDER_DEDUPE:
                                    continue
                            _ALERT_LOGGER.info(
                                "WARN ladder_missing station=%s type=%s market_type=%s",
                                station,
                                market_type_token,
                                market_type_token,
                            )
                            response = requests.post(
                                webhook,
                                json={"content": f"⚠️ Ladder missing — station={station} type={market_type_token} temp={tf}°F"},
                                timeout=10,
                            )
                            if 200 <= response.status_code < 300:
                                with _MISSING_LADDER_LOCK:
                                    _MISSING_LADDER_DEDUPE[dedupe_key] = True
                                _audit_alert(
                                    station=station,
                                    market_type=market_type_token,
                                    event_ticker="",
                                    alert_type="ladder_missing",
                                    direction=None,
                                    temp_f=float(tf),
                                    bucket_index=None,
                                    metadata={"status_code": response.status_code},
                                )
                        continue

                    transition = process_ladder_transition(
                        station=station,
                        market_type=market_type_token,
                        snapshot=snapshot,
                        current_temp=tf,
                    )

                    if transition.get("should_alert"):
                        direction = transition.get("direction") or "UP"
                        bucket_index = transition.get("bucket_index")
                        event_ticker = (markets[0] or {}).get("event_ticker") or ""
                        _ALERT_LOGGER.info(
                            "EVENT ladder_transition station=%s type=%s market_type=%s direction=%s bucket=%s",
                            station,
                            market_type_token,
                            market_type_token,
                            direction,
                            bucket_index,
                        )
                        _audit_alert(
                            station=station,
                            market_type=market_type_token,
                            event_ticker=event_ticker,
                            alert_type="ladder_transition",
                            direction=direction,
                            temp_f=float(tf),
                            bucket_index=bucket_index,
                            metadata={"reason": transition.get("reason")},
                        )
                        result = send_composed_weather_market_alert(
                            station=station,
                            market_types={market_type_token},
                            transition_reason=transition.get("reason"),
                            prev_temp_f=pf,
                            now_temp_f=tf,
                            delta_f=df,
                            obs_time_utc=payload.get("obs_time"),
                        )
                        if result and result.get("ok"):
                            market_alerts_sent += 1
                            send_event_ticker = result.get("event_ticker") or event_ticker
                            _ALERT_LOGGER.info(
                                "SEND composed_alert station=%s type=%s market_type=%s event=%s",
                                station,
                                market_type_token,
                                market_type_token,
                                send_event_ticker,
                            )
                            _audit_alert(
                                station=station,
                                market_type=market_type_token,
                                event_ticker=send_event_ticker,
                                alert_type="composed_alert_sent",
                                direction=direction,
                                temp_f=float(tf),
                                bucket_index=result.get("bucket_index"),
                                metadata={
                                    "reason": transition.get("reason"),
                                    "attention_phrase": result.get("attention_phrase"),
                                    "alert_context": result.get("alert_context"),
                                },
                            )
                    else:
                        if transition.get("terminal_state_blocked"):
                            saw_terminal_state = True
                        raw_reason = (transition.get("reason") or "").strip().upper()
                        if raw_reason:
                            suppression_reason = raw_reason

            if evaluated_market_attempts > 0:
                if market_alerts_sent > 0:
                    evaluation_outcome = "ALERT_SENT"
                elif saw_terminal_state:
                    evaluation_outcome = "TERMINAL_STATE"
                elif no_eligible_market_count == evaluated_market_attempts:
                    evaluation_outcome = "NO_ELIGIBLE_MARKET"
                else:
                    reason_token = suppression_reason or "NO_TRANSITION"
                    evaluation_outcome = f"SUPPRESSED_{reason_token}"

                _annotate_transition_history_market_eval(
                    station=station,
                    transition_correlation=transition_correlation,
                    alerts_sent=market_alerts_sent,
                    evaluation_outcome=evaluation_outcome,
                    suppression_reason=suppression_reason,
                )
        except Exception as e:
            print(f"[ERROR] station={station} function=_send_alert: {e}")
    except Exception as e:
        print(f"[ERROR] station={payload.get('station') or 'UNK'} function=_send_alert: {e}")


# =========================
# Window + fetch routers (STRICT: no auto-fallback)
# =========================
def _compute_window(icao: str, minutes: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None):
    """
    Compute a rolling start/end window in UTC.
    - If we've seen this ICAO, start from (last_seen - OVERLAP_SECONDS).
    - If first run, use now - lookback_min - FIRST_RUN_CUSHION_SEC.
    Returns: (start_iso_z, end_iso_z, start_dt, end_dt)
    """
    if cfg is None:
        cfg = get_default_config()
    lookback = int(minutes if minutes is not None else cfg.get("lookback_min", 3))

    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    with _STATE_LOCK:
        last_seen = _STATE["last_seen_iso"].get(icao)

    if last_seen:
        start_dt = _parse_iso(last_seen) - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start_dt = now - timedelta(minutes=lookback) - timedelta(seconds=FIRST_RUN_CUSHION_SEC)

    end_dt = now
    return (_iso_seconds_z(start_dt), _iso_seconds_z(end_dt), start_dt, end_dt)


def _fetch_range_strict(icao: str, chosen: str,
                        start_iso: str, end_iso: str,
                        start_dt: datetime, end_dt: datetime,
                        cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    s = (chosen or cfg["default_source"] or "nws").lower()
    if s == "nws":
        return _fetch_range_nws(icao, _iso_seconds_z(start_dt), _iso_seconds_z(end_dt), cfg)
    if s == "iem":
        return _fetch_range_iem(icao, start_dt, end_dt, cfg)
    if s == "tgftp":
        return _fetch_latest_tgftp(icao)
    return []


def fetch_window(icao: str, minutes: int, source: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute a small window, fetch strictly from chosen source (no fallback),
    ingest in-order, and return the latest-known obs plus window diagnostics.
    """
    ensure_state_loaded()
    cfg = get_default_config()
    chosen = (source or cfg["default_source"] or "nws").lower()

    start_iso, end_iso, start_dt, end_dt = _compute_window(icao, minutes, cfg)

    try:
        obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
        ing, al = _ingest_obs(icao, obs_list, cfg, window_start=start_dt, window_end=end_dt)
        with _STATE_LOCK:
            latest = _STATE["last_obs"].get(icao)
        return {
            "status": "ok",
            "icao": icao,
            "source": chosen,
            "ingested": ing,
            "alerts": al,
            "latest": latest,
            "window_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "window_et": {
                "start": _iso_to_tz(start_dt.isoformat(), ET_TZ_NAME),
                "end": _iso_to_tz(end_dt.isoformat(), ET_TZ_NAME),
            },
        }
    except Exception as e:
        return {
            "status": "error",
            "icao": icao,
            "source": chosen,
            "error": str(e),
        }


def fetch_latest(icao: str, source: Optional[str] = None) -> dict:
    cfg = get_default_config()
    minutes = int(cfg.get("lookback_min", 3))
    chosen = (source or cfg["default_source"] or "nws").lower()

    # First try the windowed read
    res = fetch_window(icao, minutes, source=chosen)
    latest = res.get("latest")
    if latest:
        return {"icao": icao, "source": res.get("source"), **latest}

    # If window was empty and we're staying strict-NWS, hit the NWS 'latest' doc
    if chosen == "nws":
        try:
            single = _fetch_nws_latest_single(icao, cfg)
            if single:
                _ingest_obs(icao, [single], cfg)
                return {"icao": icao, "source": "nws", **single}
        except Exception as e:
            return {
                "icao": icao,
                "source": "nws",
                "error": str(e),
                "status": "error",
            }

    # Nothing available
    return {
        "icao": icao,
        "source": res.get("source"),
        "error": res.get("error") or "no observation",
        "status": res.get("status", "error"),
    }


def fetch_now(stations: List[str], source: Optional[str] = None) -> Dict[str, Any]:
    """
    Batch latest across many ICAOs. Internally calls fetch_window for each,
    then returns a {icao: latest|None} map plus totals.
    """
    cfg = get_default_config()
    minutes = int(cfg.get("lookback_min", 3))
    chosen = (source or cfg["default_source"] or "nws").lower()

    observations: Dict[str, Optional[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    total_ing = 0
    total_alerts = 0

    for icao in stations:
        res = fetch_window(icao, minutes, source=chosen)
        if res.get("status") != "ok" and res.get("error"):
            errors[icao] = res["error"]
            observations[icao] = None
        else:
            observations[icao] = res.get("latest")
            total_ing += int(res.get("ingested", 0))
            total_alerts += int(res.get("alerts", 0))

    out = {
        "status": "ok",
        "stations": stations,
        "observations": observations,
        "source": chosen,
        "ingested": total_ing,
        "alerts": total_alerts,
    }
    if errors:
        out["errors"] = errors
    return out


def get_latest_metar(icao: str, source: Optional[str] = None) -> Dict[str, Any]:
    """Back-compat alias used by app.py."""
    return fetch_latest(icao, source=source)


# =========================
# Watchlist + metrics
# =========================
def set_watchlist(icaos: Optional[List[str]]) -> Dict[str, Any]:
    if not icaos or not isinstance(icaos, list):
        return {"error": "POST JSON must include non-empty 'icaos' list"}
    cleaned = [x.strip().upper() for x in icaos if isinstance(x, str) and x.strip()]
    with _STATE_LOCK:
        _STATE["stations"] = cleaned
    return {"ok": True, "watchlist": cleaned, "count": len(cleaned)}


def get_watchlist() -> Dict[str, Any]:
    with _STATE_LOCK:
        wl = list(_STATE["stations"]) or get_default_config()["stations"]
    return {"watchlist": wl, "count": len(wl)}


def get_metrics() -> Dict[str, Any]:
    with _STATE_LOCK:
        last_poll_utc = _STATE["last_poll_utc"]
        last_loop_utc = _STATE["last_loop_utc"]
        poll_count = _STATE["poll_count"]
        watch_ct = len(_STATE["stations"] or get_default_config()["stations"])
        timeout_count = _STATE["timeout_count"]
        last_timeout_station = _STATE["last_timeout_station"]
        last_timeout_utc = _STATE["last_timeout_utc"]
    return {
        "last_poll_utc": last_poll_utc,
        "last_loop_utc": last_loop_utc,
        "last_poll_et": _iso_to_tz(last_poll_utc, ET_TZ_NAME),
        "poll_count": poll_count,
        "watchlist_size": watch_ct,
        "timeout_count": timeout_count,
        "last_timeout_station": last_timeout_station,
        "last_timeout_utc": last_timeout_utc,
    }


def set_live_station_universe_resolver(resolver) -> None:
    """
    Register a callable that returns the canonical live station universe.
    Resolver may return either a list of station codes or a dict containing
    a "stations" list.
    """
    global _LIVE_STATION_UNIVERSE_RESOLVER
    _LIVE_STATION_UNIVERSE_RESOLVER = resolver


def _resolve_live_polling_stations(cfg: Dict[str, Any]) -> List[str]:
    stations = _STATE["stations"] or cfg["stations"]

    resolver = _LIVE_STATION_UNIVERSE_RESOLVER
    if callable(resolver):
        try:
            resolved = resolver()
            if isinstance(resolved, dict):
                resolved = resolved.get("stations")
            if isinstance(resolved, list):
                canonical = [
                    station.strip().upper()
                    for station in resolved
                    if isinstance(station, str) and station.strip()
                ]
                if canonical:
                    stations = sorted(set(canonical))
                    with _STATE_LOCK:
                        _STATE["stations"] = stations
        except Exception:
            pass

    return stations


# =========================
# Scheduler
# =========================
def _poll_once(logger=None):
    ensure_state_loaded()
    cfg = get_default_config()
    stations = _resolve_live_polling_stations(cfg)

    from core.kalshi_monitor import build_structured_snapshot

    for icao in stations:
        try:
            build_structured_snapshot(
                station=icao,
                market_types={"HIGH", "LOW"}
            )
        except Exception:
            pass

    chosen = cfg["default_source"] or "nws"

    total_ing = 0
    total_alerts = 0
    for icao in stations:
        try:
            start_iso, end_iso, start_dt, end_dt = _compute_window(icao, cfg.get("lookback_min", 3), cfg)
            obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
            ing, al = _ingest_obs(icao, obs_list, cfg, window_start=start_dt, window_end=end_dt)
            total_ing += ing
            total_alerts += al
        except Exception as e:
            if logger:
                logger.error(f"poll failed for {icao} ({chosen}): {e}")

    with _STATE_LOCK:
        _STATE["poll_count"] += 1
        _STATE["last_poll_utc"] = _now_utc_iso()
        _save_cache(cfg["cache_file"], {
            "last_obs": _STATE["last_obs"],
            "last_seen_iso": _STATE["last_seen_iso"],
            "last_reset_date_local": _STATE["last_reset_date_local"],
            "last_observed_integer": _STATE["last_observed_integer"],
        })

def _scheduler_loop(logger, interval_sec: int):
    loop_count = 0
    while not _SCHEDULER_STOP.is_set():
        try:
            _poll_once(logger)
            with _STATE_LOCK:
                _STATE["last_loop_utc"] = _now_utc_iso()
        except Exception as e:
            if logger:
                logger.exception(f"METAR scheduler loop error: {e}")
        loop_count += 1
        if loop_count % 100 == 0:
            _run_alert_retention()
        _SCHEDULER_STOP.wait(interval_sec)


def start_scheduler(logger, cfg=None) -> bool:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return True
        if cfg is None:
            cfg = get_default_config()
        ensure_state_loaded()
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop, args=(logger, int(cfg["poll_seconds"])), daemon=True
        )
        _SCHEDULER_THREAD.start()
    return True


def ensure_scheduler_started(logger, cfg=None) -> bool:
    return start_scheduler(logger, cfg=cfg)


def stop_scheduler() -> bool:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if not _SCHEDULER_THREAD:
            return True
        _SCHEDULER_STOP.set()
        _SCHEDULER_THREAD.join(timeout=5)
        _SCHEDULER_THREAD = None
    return True


def is_scheduler_running() -> bool:
    return _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive()
