"""
METAR Monitor

This module owns ingestion of METAR observations and deterministic generation
of station temperature transitions used by downstream market evaluation.

Responsibilities
- Poll METAR providers and normalize accepted observations.
- Maintain authoritative station-local temperature state.
- Detect instant/settlement ladder transitions.
- Route transition emissions through transition_emitter.

This module MUST NOT
- Bypass transition_emitter for transition persistence.
- Let observability paths influence runtime execution state.
- Perform non-deterministic replay behavior.
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
from core.alert_schema import ALERT_SCHEMA_VERSION

# zoneinfo (Python 3.9+). If unavailable, we'll no-op ET/local conversions.
try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None

# -------- Constants --------
ET_TZ_NAME = "America/New_York"
OVERLAP_SECONDS = 120               # small overlap to avoid missing late arrivals
FIRST_RUN_CUSHION_SEC = 300         # first contact: add 5 min cushion
BOOTSTRAP_LOOKBACK_MINUTES = 60     # first contact: deterministic widened bootstrap window
PUBLICATION_LAG_BUFFER_SECONDS = 90
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
# Rate-limit invariant: this window throttles repeated market evaluation
# calls; transition events may still be persisted even when evaluation skips.
_KALSHI_CALL_THROTTLE_SECONDS = 5
_ALERT_LOGGER = logging.getLogger(__name__)
_TRANSITION_HISTORY = deque(maxlen=500)
_TRANSITION_LOCK = threading.Lock()
_LAST_SETTLEMENT_UP_TS = {}
_LAST_NWS_FETCH_DIAGNOSTIC = {}
_SIGNAL_OBSERVATION_WINDOWS: Dict[str, deque] = {}
_SIGNAL_STATION_LAST_EMIT: Dict[str, float] = {}
_SIGNAL_BOUNDARY_LAST_EMIT: Dict[Tuple[str, int, int], float] = {}
_SIGNAL_EPOCH_COUNTER: Dict[str, int] = {}
_SIGNAL_GOLDILOCKS_EPOCH_TRACKER: Dict[Tuple[str, int], Dict[str, Any]] = {}
_LATEST_SIGNAL_RUNTIME: Dict[str, Dict[str, Any]] = {}

_SIGNAL_STATION_COOLDOWN_SECONDS = 300
_SIGNAL_BOUNDARY_COOLDOWN_SECONDS = 900
_SIGNAL_MOMENTUM_WINDOW_SIZE = 3


def reset_station_daily_state(icao: str, local_day: str) -> None:
    _reset_station_daily_state_authoritative(icao, local_day)
    _LAST_SETTLEMENT_UP_TS.pop((icao or "").strip().upper(), None)


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


def _parse_iso_utc_optional(raw):
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _alert_integrity_evaluation_window_seconds() -> int:
    try:
        return max(30, int(os.getenv("ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300


def _resolve_missing_ladder_cause(empty_reason: str) -> Tuple[str, str]:
    reason_key = str(empty_reason or "").strip()
    cause_map = {
        "no_directional_ladder_match": (
            "directional_ladder_mismatch",
            "markets exist but none match the requested directional ladder",
        ),
        "filtered_to_zero": (
            "market_filters_removed_all_candidates",
            "markets exist but eligibility filters removed all candidates",
        ),
        "cache_missing_or_empty": (
            "market_cache_empty",
            "no ladder markets are currently cached for this station",
        ),
    }
    return cause_map.get(
        reason_key,
        (
            "unknown_missing_ladder_cause",
            "missing ladder condition detected but no human-readable explanation is mapped",
        ),
    )


def _is_recent_transition_active(
    *,
    reference_timestamp_utc: Optional[str],
    transition_correlation: Optional[Dict[str, Any]],
) -> bool:
    reference_dt = _parse_iso_utc_optional(reference_timestamp_utc)
    if reference_dt is None:
        return False

    if not isinstance(transition_correlation, dict):
        return False

    last_transition_timestamp = _parse_iso_utc_optional(transition_correlation.get("timestamp_utc"))
    if last_transition_timestamp is None:
        return False

    elapsed_seconds = (reference_dt - last_transition_timestamp).total_seconds()
    return 0 <= elapsed_seconds <= _alert_integrity_evaluation_window_seconds()


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
        if "last_reset_date_local" in cache:
            _STATE["last_reset_date_local"].update(cache["last_reset_date_local"])

    last_obs_cache = cache.get("last_obs") or {}
    last_seen_cache = cache.get("last_seen_iso") or {}
    for station in set(last_obs_cache.keys()) | set(last_seen_cache.keys()):
        obs = last_obs_cache.get(station)
        obs_time = last_seen_cache.get(station)
        if obs is not None and obs_time:
            set_latest_observation(station, obs, obs_time)

    observed_integer_cache = cache.get("last_observed_integer") or {}
    running_daily_max_cache = cache.get("running_daily_max") or {}
    last_settlement_cache = cache.get("last_settlement_bucket") or {}
    last_instant_cache = cache.get("last_instant_bucket") or {}
    for station in (
        set(observed_integer_cache.keys())
        | set(running_daily_max_cache.keys())
        | set(last_settlement_cache.keys())
        | set(last_instant_cache.keys())
    ):
        curr_floor = observed_integer_cache.get(station)
        running_daily_max = running_daily_max_cache.get(station)
        settlement_bucket = last_settlement_cache.get(station)
        instant_bucket = last_instant_cache.get(station)
        if (
            curr_floor is not None
            and running_daily_max is not None
            and settlement_bucket is not None
            and instant_bucket is not None
        ):
            commit_temperature_state(
                icao=station,
                curr_floor=int(curr_floor),
                running_daily_max=float(running_daily_max),
                settlement_bucket=int(settlement_bucket),
                instant_bucket=int(instant_bucket),
            )


def get_state() -> Dict[str, Any]:
    snapshot = immutable_public_state_snapshot()
    return {
        "stations": list(snapshot["stations"]),
        "last_obs": dict(snapshot["last_obs"]),
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
        "ingestion_admission": dict(snapshot["ingestion_admission"]),
        "ingestion_runtime": dict(snapshot["ingestion_runtime"]),
    }


def _compute_rejection_reasons(
    icao: str,
    obs_list: List[Dict[str, Any]],
    *,
    last_seen_iso: Optional[str],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> Dict[str, Any]:
    reasons: Dict[str, int] = {}
    sample_rejected_observations: List[Dict[str, Any]] = []
    cursor_last_seen_iso = last_seen_iso
    window_end_day_key = station_local_day_key(icao, window_end.isoformat()) if window_end else None

    for obs in obs_list:
        ts = obs.get("obs_time")
        if not ts:
            reasons["missing_observation_timestamp"] = reasons.get("missing_observation_timestamp", 0) + 1
            continue

        obs_dt = _parse_iso(ts)
        rejection_reason = None

        if window_end and obs_dt > window_end:
            rejection_reason = "outside_window_after_end"
        elif window_start:
            grace_start = window_start - timedelta(seconds=METAR_ACCEPTANCE_GRACE_SECONDS)
            if obs_dt < grace_start:
                rejection_reason = "outside_window_before_grace_start"
            else:
                obs_day_key = station_local_day_key(icao, obs_dt.isoformat())
                if window_end_day_key and obs_day_key != window_end_day_key:
                    rejection_reason = "outside_station_local_trading_day"

        if not rejection_reason and cursor_last_seen_iso and obs_dt <= _parse_iso(cursor_last_seen_iso):
            rejection_reason = "dedup_older_or_equal_timestamp"

        if rejection_reason:
            reasons[rejection_reason] = reasons.get(rejection_reason, 0) + 1
            if len(sample_rejected_observations) < 5:
                sample_rejected_observations.append(
                    {
                        "obs_time": ts,
                        "rejection_reason": rejection_reason,
                        "delta_seconds_from_window_start": (
                            (obs_dt - window_start).total_seconds() if window_start else None
                        ),
                        "delta_seconds_from_window_end": (
                            (obs_dt - window_end).total_seconds() if window_end else None
                        ),
                    }
                )
            continue

        cursor_last_seen_iso = ts

    return {
        "reasons": reasons,
        "sample_rejected_observations": sample_rejected_observations,
    }


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


def _nws_collection_url(icao: str, limit: int = 200) -> str:
    return f"https://api.weather.gov/stations/{icao}/observations?limit={min(limit, 200)}"


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
    url = _nws_collection_url(icao)
    normalized_station = (icao or "").strip().upper()
    execution_domain = "production"
    try:
        from core.kalshi_monitor import _current_kalshi_execution_domain

        execution_domain = _current_kalshi_execution_domain()
    except Exception:
        execution_domain = "production"

    headers = _headers_for_nws(cfg)
    _ALERT_LOGGER.info(
        "NWS_FETCH_DIAGNOSTIC\n"
        "station=%s\n"
        "url=%s\n"
        "start=%s\n"
        "end=%s\n"
        "execution_domain=%s\n"
        "user_agent=%s\n"
        "timestamp_utc=%s",
        icao,
        url,
        start_iso_z,
        end_iso_z,
        execution_domain,
        headers.get("User-Agent", ""),
        _now_utc_iso(),
    )
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.Timeout:
        _record_timeout(icao)
        r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    payload = r.json()
    features = payload.get("features", []) if isinstance(payload, dict) else []
    _ALERT_LOGGER.info(
        "NWS_FETCH_RESULT\n"
        "station=%s\n"
        "http_status=%s\n"
        "feature_count=%s\n"
        "response_timestamp=%s",
        icao,
        r.status_code,
        len(features) if isinstance(features, list) else 0,
        r.headers.get("Date") if getattr(r, "headers", None) else None,
    )
    _LAST_NWS_FETCH_DIAGNOSTIC[normalized_station] = {
        "timestamp_utc": _now_utc_iso(),
        "request_url": url,
        "start_iso": start_iso_z,
        "end_iso": end_iso_z,
        "http_status": r.status_code,
        "feature_count": len(features) if isinstance(features, list) else 0,
        "response_timestamp": r.headers.get("Date") if getattr(r, "headers", None) else None,
    }
    parsed_obs = _parse_nws_collection(payload)

    def _parse_utc(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)

    try:
        start_dt = _parse_utc(start_iso_z)
        end_dt = _parse_utc(end_iso_z)
    except Exception:
        return parsed_obs
    filtered_obs: List[Dict[str, Any]] = []
    for obs in parsed_obs:
        try:
            obs_dt = _parse_utc(obs.get("obs_time", ""))
        except Exception:
            continue
        if start_dt <= obs_dt <= end_dt:
            filtered_obs.append(obs)
    return filtered_obs


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
    delivery_results: Optional[List[Dict[str, Any]]] = None,
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
            delivery_results=delivery_results,
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
) -> Dict[str, Any]:
    transition_type = None
    if settlement_bucket_changed:
        transition_type = "settlement_up"
    elif instant_bucket_changed:
        if now_f > prev_f:
            transition_type = "instant_up"
        elif now_f < prev_f:
            transition_type = "instant_down"

    instant_before = int(math.floor(prev_f))
    instant_after = int(math.floor(now_f))
    settlement_bucket = None
    running_max = now_f
    if isinstance(transition_correlation, dict):
        metadata = transition_correlation.get("metadata") or {}
        settlement_bucket = metadata.get("previous_settlement_bucket")
        if settlement_bucket_changed:
            settlement_bucket = int(math.floor(now_f))
        running_max = metadata.get("prev_running_max", running_max)

    headline = f"{icao} transition detected"
    summary_transition = transition_type or "transition"
    payload = {
        "schema_version": ALERT_SCHEMA_VERSION,
        "timestamp_utc": _now_utc_iso(),
        "station": icao,
        "classification": "STRUCTURAL",
        "summary": {
            "headline": headline,
            "transition": summary_transition,
            "temp_f": now_f,
            "instant_bucket": instant_after,
            "settlement_bucket": settlement_bucket,
        },
        "transition_context": {
            "transition_type": transition_type,
            "instant_before": instant_before,
            "instant_after": instant_after,
            "settlement_bucket": settlement_bucket,
            "running_max": running_max,
            "obs_time": obs_time,
        },
        "market_context": {
            "series_ticker": None,
            "event_ticker": None,
            "market_type": None,
            "strike": None,
            "proximity_regime": None,
            "hydrated": False,
        },
        "eligibility_evaluation": {
            "markets_considered": 0,
            "eligible_markets": 0,
            "rejected_markets": 0,
            "rejection_breakdown": {},
        },
        "suppression": {
            "suppressed": False,
            "reason": "",
            "reason_category": "NO_TRANSITION",
        },
        "execution_context": {
            "execution_domain": "production",
            "hydration_state": {},
            "scheduler_poll_count": get_metrics().get("poll_count"),
        },
        "legacy": {
            "type": "temp_change",
            "prev_temp_f": prev_f,
            "temp_f": now_f,
            "delta_f": delta_f,
            "obs_time": obs_time,
            "at_utc": _now_utc_iso(),
            "instant_bucket_changed": bool(instant_bucket_changed),
            "settlement_bucket_changed": bool(settlement_bucket_changed),
            "transition_correlation": transition_correlation,
        },
    }
    return _send_alert(cfg.get("webhook", ""), payload)


def _observation_seconds(obs_time: str) -> Optional[float]:
    obs_dt = _parse_iso_utc_optional(obs_time)
    if obs_dt is None:
        return None
    return obs_dt.timestamp()


def _current_signal_epoch(station: str) -> int:
    return int(_SIGNAL_EPOCH_COUNTER.get(station, 0) or 0)


def _record_signal_runtime(station: str, runtime: Dict[str, Any]) -> None:
    _LATEST_SIGNAL_RUNTIME[(station or "").strip().upper()] = copy.deepcopy(runtime)


def _get_signal_runtime(station: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    normalized_station = (station or "").strip().upper()
    if normalized_station:
        runtime = _LATEST_SIGNAL_RUNTIME.get(normalized_station)
        return {normalized_station: copy.deepcopy(runtime)} if isinstance(runtime, dict) else {}
    return copy.deepcopy(_LATEST_SIGNAL_RUNTIME)


def _emit_signal_alert(*, station: str, obs_time: str, temp_f: float, signal_context: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    payload = {
        "schema_version": ALERT_SCHEMA_VERSION,
        "timestamp_utc": _now_utc_iso(),
        "station": station,
        "classification": "SIGNAL",
        "summary": {
            "headline": f"{station} signal alert",
            "transition": signal_context.get("signal_type"),
            "temp_f": float(temp_f),
            "instant_bucket": int(math.floor(temp_f)),
            "settlement_bucket": signal_context.get("settlement_bucket_at_up"),
        },
        "signal_context": copy.deepcopy(signal_context),
        "legacy": {
            "type": "signal_alert",
            "obs_time": obs_time,
            "temp_f": float(temp_f),
            "signal_type": signal_context.get("signal_type"),
        },
    }
    _audit_alert(
        station=station,
        market_type="SIGNAL",
        event_ticker=str(signal_context.get("dedupe_key") or ""),
        alert_type=str(signal_context.get("signal_type") or "signal"),
        direction="UP" if str(signal_context.get("signal_type") or "").endswith("_up") else "REVERSAL",
        temp_f=float(temp_f),
        bucket_index=int(math.floor(temp_f)),
        metadata={"signal_context": copy.deepcopy(signal_context)},
    )
    _send_alert(cfg.get("webhook", ""), payload)


def get_latest_station_signal_runtime(station: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    return _get_signal_runtime(station)


def _evaluate_deterministic_signal_layer(
    *,
    station: str,
    now_f: float,
    obs_time: str,
    transition_type: Optional[str],
    settlement_bucket: int,
    hydration_cache_valid: bool,
    eligible_markets_count: int,
    cfg: Dict[str, Any],
) -> None:
    station = (station or "").strip().upper()
    obs_seconds = _observation_seconds(obs_time)
    if obs_seconds is None:
        return

    window = _SIGNAL_OBSERVATION_WINDOWS.setdefault(station, deque(maxlen=_SIGNAL_MOMENTUM_WINDOW_SIZE))
    window.append({"temp_f": float(now_f), "obs_time": obs_time, "seconds": obs_seconds})

    if transition_type == "settlement_up":
        _SIGNAL_EPOCH_COUNTER[station] = _current_signal_epoch(station) + 1
    epoch_id = _current_signal_epoch(station)

    for key in list(_SIGNAL_BOUNDARY_LAST_EMIT.keys()):
        key_station, key_boundary, _key_epoch = key
        if key_station == station and now_f >= float(key_boundary):
            _SIGNAL_BOUNDARY_LAST_EMIT.pop(key, None)

    station_last_emit = _SIGNAL_STATION_LAST_EMIT.get(station)
    station_cooldown_active = station_last_emit is not None and (obs_seconds - station_last_emit) < _SIGNAL_STATION_COOLDOWN_SECONDS

    runtime = {
        "station": station,
        "obs_time": obs_time,
        "signal_type": None,
        "signal_emitted": False,
        "suppression_reason": None,
        "cooldown_state": {
            "station_active": station_cooldown_active,
            "station_cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,
            "boundary_active": False,
            "boundary_cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS,
        },
    }

    if not hydration_cache_valid:
        runtime["suppression_reason"] = "HYDRATION_CACHE_INVALID"
        _record_signal_runtime(station, runtime)
        return
    if int(eligible_markets_count) <= 0:
        runtime["suppression_reason"] = "NO_ELIGIBLE_MARKETS"
        _record_signal_runtime(station, runtime)
        return
    if station_cooldown_active:
        runtime["suppression_reason"] = "STATION_COOLDOWN_ACTIVE"

    next_integer = int(math.floor(now_f)) + 1
    distance_to_integer = float(next_integer) - float(now_f)
    momentum = None
    pressure_to_boundary_seconds = None
    boundary_cooldown_active = False
    boundary_key = (station, int(next_integer), int(epoch_id))
    boundary_last_emit = _SIGNAL_BOUNDARY_LAST_EMIT.get(boundary_key)
    if boundary_last_emit is not None and (obs_seconds - boundary_last_emit) < _SIGNAL_BOUNDARY_COOLDOWN_SECONDS:
        boundary_cooldown_active = True
    runtime["cooldown_state"]["boundary_active"] = boundary_cooldown_active

    near_boundary_all_conditions = False
    if 0.0 < distance_to_integer <= 0.10 and len(window) == _SIGNAL_MOMENTUM_WINDOW_SIZE:
        x1, x2, x3 = window[0], window[1], window[2]
        monotonic = x1["temp_f"] <= x2["temp_f"] <= x3["temp_f"]
        increasing_time = x1["seconds"] < x2["seconds"] < x3["seconds"]
        movement_ok = (x3["temp_f"] - x1["temp_f"]) >= 0.05
        total_seconds = x3["seconds"] - x1["seconds"]
        if increasing_time and total_seconds > 0:
            momentum = (x3["temp_f"] - x1["temp_f"]) / total_seconds
            if momentum > 0:
                pressure_to_boundary_seconds = distance_to_integer / momentum
        near_boundary_all_conditions = bool(
            monotonic
            and increasing_time
            and movement_ok
            and momentum is not None
            and momentum >= 0.002
        )

    if near_boundary_all_conditions and not station_cooldown_active and not boundary_cooldown_active:
        signal_context = {
            "signal_type": "near_boundary_momentum_up",
            "signal_version": 1,
            "station": station,
            "obs_time": obs_time,
            "dedupe_key": f"near_boundary_momentum_up:{station}:{epoch_id}:{next_integer}",
            "cooldown_applied": True,
            "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS,
            "distance_to_next_integer": distance_to_integer,
            "momentum_f_per_sec": momentum,
            "momentum_window_size": _SIGNAL_MOMENTUM_WINDOW_SIZE,
            "next_integer_boundary": next_integer,
            "pressure_to_boundary_seconds": pressure_to_boundary_seconds,
        }
        _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
        _SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds
        _emit_signal_alert(station=station, obs_time=obs_time, temp_f=now_f, signal_context=signal_context, cfg=cfg)
        runtime.update({"signal_type": "near_boundary_momentum_up", "signal_emitted": True, "suppression_reason": None})
        runtime["cooldown_state"]["station_active"] = True
        runtime["cooldown_state"]["boundary_active"] = True
        _record_signal_runtime(station, runtime)
        return

    epoch_key = (station, int(epoch_id))
    tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)
    if transition_type == "settlement_up":
        tracker = {
            "settlement_bucket_at_up": int(settlement_bucket),
            "max_temp_after_up": float(now_f),
            "exceeded_by_one_or_more": float(now_f) >= float(settlement_bucket) + 1.2,
            "reverted_below_settlement": float(now_f) <= float(settlement_bucket) - 0.2,
            "alert_emitted": False,
            "settlement_up_obs_time": obs_time,
        }
        _SIGNAL_GOLDILOCKS_EPOCH_TRACKER[epoch_key] = tracker
    elif isinstance(tracker, dict):
        tracker["max_temp_after_up"] = max(float(tracker.get("max_temp_after_up") or now_f), float(now_f))
        tracker["exceeded_by_one_or_more"] = bool(tracker.get("exceeded_by_one_or_more")) or (
            tracker["max_temp_after_up"] >= float(tracker.get("settlement_bucket_at_up") or settlement_bucket) + 1.2
        )
        tracker["reverted_below_settlement"] = bool(tracker.get("reverted_below_settlement")) or (
            float(now_f) <= float(tracker.get("settlement_bucket_at_up") or settlement_bucket) - 0.2
        )

    if isinstance(tracker, dict):
        if bool(tracker.get("alert_emitted")):
            runtime["suppression_reason"] = "EPOCH_ALERT_ALREADY_EMITTED"
        elif station_cooldown_active:
            runtime["suppression_reason"] = "STATION_COOLDOWN_ACTIVE"
        elif bool(tracker.get("exceeded_by_one_or_more")) and bool(tracker.get("reverted_below_settlement")):
            signal_context = {
                "signal_type": "goldilocks_reversion_alert",
                "signal_version": 1,
                "station": station,
                "obs_time": obs_time,
                "dedupe_key": f"goldilocks_reversion_alert:{station}:{epoch_id}",
                "cooldown_applied": True,
                "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,
                "settlement_bucket_at_up": int(tracker.get("settlement_bucket_at_up") or settlement_bucket),
                "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),
                "reverted_temp": float(now_f),
                "epoch_id": int(epoch_id),
            }
            tracker["alert_emitted"] = True
            _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
            _emit_signal_alert(station=station, obs_time=obs_time, temp_f=now_f, signal_context=signal_context, cfg=cfg)
            runtime.update({"signal_type": "goldilocks_reversion_alert", "signal_emitted": True, "suppression_reason": None})
            runtime["cooldown_state"]["station_active"] = True

    if runtime["signal_type"] is None and runtime["suppression_reason"] is None:
        runtime["suppression_reason"] = "NO_SIGNAL_CONDITION_MATCH"
    _record_signal_runtime(station, runtime)


def _process_temperature_event(
    icao: str,
    temp_f: float,
    obs_time: str,
    cfg: Dict[str, Any],
    last_temp_f: Optional[float] = None,
    allow_alert_delivery: bool = True,
    delivery_results: Optional[List[Dict[str, Any]]] = None,
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

    # Causal flow: observation -> bucket transition detection -> authoritative
    # transition emission (persisted before any alert/evaluation side effects).
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

    normalized_station = (icao or "").strip().upper()
    if transition_type == "settlement_up":
        _LAST_SETTLEMENT_UP_TS[normalized_station] = obs_time
    elif transition_type == "reversion_after_settlement":
        settlement_up_obs_time = _LAST_SETTLEMENT_UP_TS.get(normalized_station)
        if settlement_up_obs_time:
            delta_seconds = (
                _parse_iso(obs_time) - _parse_iso(settlement_up_obs_time)
            ).total_seconds()
            if delta_seconds <= 300:
                emit_transition_if_changed(
                    transition_type="goldilocks_reversion",
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
                        "settlement_up_obs_time": settlement_up_obs_time,
                        "delta_seconds": delta_seconds,
                    },
                    emit_fn=_log_transition_event,
                )

    hydration_cache_valid = False
    eligible_markets_count = 0
    try:
        from core.kalshi_monitor import get_hydration_prerequisite_state_snapshot

        hydration_state = get_hydration_prerequisite_state_snapshot().get(normalized_station) or {}
        hydration_cache_valid = bool(hydration_state.get("cache_valid"))
    except Exception:
        hydration_cache_valid = False

    latest_market_eval = get_latest_station_market_evaluation_context(station=normalized_station).get(normalized_station, {})
    eligibility_runtime = latest_market_eval.get("market_eligibility_runtime") or {}
    eligible_markets_count = int(eligibility_runtime.get("eligible_markets_count") or 0)

    _evaluate_deterministic_signal_layer(
        station=normalized_station,
        now_f=now_f,
        obs_time=obs_time,
        transition_type=transition_type,
        settlement_bucket=settlement_bucket,
        hydration_cache_valid=hydration_cache_valid,
        eligible_markets_count=eligible_markets_count,
        cfg=cfg,
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
        # Alert eligibility boundary: delivery is attempted only when deterministic
        # integer-cross criteria were met and delivery is explicitly enabled.
        if allow_alert_delivery:
            d = round(now_f - prev_f, 1)
            send_result = _emit_alert(
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
            if delivery_results is not None:
                delivery_results.append(send_result)
        elif delivery_results is not None:
            delivery_results.append(
                {
                    "delivery_attempted": False,
                    "delivery_succeeded": False,
                    "webhook_status_code": None,
                    "webhook_exception": None,
                    "webhook_response_text": None,
                    "delivery_blocking_stage": "delivery_gate",
                    "delivery_blocking_reason": "ALERT_DELIVERY_DISABLED",
                }
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
            "last_settlement_up_ts": _LAST_SETTLEMENT_UP_TS.get(station),
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


def _restore_station_state(station: str, snapshot: Dict[str, Any]) -> None:
    station = (station or "").strip().upper()
    _LAST_SETTLEMENT_UP_TS.pop(station, None)
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
    if prior_window:
        restored_window = deque(maxlen=_SIGNAL_MOMENTUM_WINDOW_SIZE)
        for row in prior_window:
            if isinstance(row, dict):
                restored_window.append(copy.deepcopy(row))
        if restored_window:
            _SIGNAL_OBSERVATION_WINDOWS[station] = restored_window
    if snapshot.get("signal_station_last_emit") is not None:
        _SIGNAL_STATION_LAST_EMIT[station] = float(snapshot["signal_station_last_emit"])
    if snapshot.get("last_settlement_up_ts"):
        _LAST_SETTLEMENT_UP_TS[station] = str(snapshot.get("last_settlement_up_ts"))
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


# Replay mode reconstructs deterministic station state from historical
# observations and must not perform live alert delivery side effects.
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


def _annotate_transition_history_market_eval(
    station: str,
    transition_correlation: Optional[Dict[str, Any]],
    alerts_sent: int,
    evaluation_outcome: str,
    suppression_reason: Optional[str] = None,
    market_eligibility_runtime: Optional[Dict[str, Any]] = None,
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


def get_transition_history(station=None, day: Optional[str] = None, limit=50):
    normalized_station = (station or "").strip().upper()
    normalized_day = (day or "").strip()
    try:
        bounded_limit = max(1, min(int(limit), 200))
    except Exception:
        bounded_limit = 50

    if normalized_day:
        datetime.strptime(normalized_day, "%Y-%m-%d")

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

    with _STATE_LOCK:
        previous_integer = _STATE["last_observed_integer"].get(icao)

    simulated_obs = _obs_tuple(float(temp_f), ts, {"simulated": True}, "simulated")
    delivery_results: List[Dict[str, Any]] = []
    _, alerts = _ingest_obs(
        icao,
        [simulated_obs],
        cfg,
        allow_alert_delivery=allow_alert_delivery,
        persist_cache=allow_alert_delivery,
        delivery_results=delivery_results,
    )

    with _STATE_LOCK:
        current_integer = _STATE["last_observed_integer"].get(icao)

    delivery_result = delivery_results[-1] if delivery_results else {
        "delivery_attempted": False,
        "delivery_succeeded": False,
        "webhook_status_code": None,
        "webhook_exception": None,
        "webhook_response_text": None,
        "delivery_blocking_stage": None,
        "delivery_blocking_reason": None,
    }
    delivery_attempted = bool(
        delivery_result.get("delivery_attempted")
        or delivery_result.get("webhook_status_code") is not None
        or delivery_result.get("webhook_exception") is not None
        or delivery_result.get("webhook_response_text") is not None
        or bool(delivery_result.get("delivery_succeeded", False))
    )
    if (
        alerts > 0
        and allow_alert_delivery
        and not delivery_attempted
        and delivery_result.get("delivery_blocking_stage") is None
        and delivery_result.get("delivery_blocking_reason") is None
    ):
        delivery_result = {
            **delivery_result,
            "delivery_blocking_stage": "invariant_violation",
            "delivery_blocking_reason": "MISSING_DELIVERY_RESULT_FOR_GENERATED_ALERT",
        }
    if logger:
        logger.info(f"Simulated ladder event for {icao} at {temp_f}F (alerts={alerts})")

    return {
        "ok": True,
        "icao": icao,
        "temp_f": float(temp_f),
        "alerts_generated": alerts,
        "delivery_requested": allow_alert_delivery,
        "delivery_attempted": delivery_attempted,
        "delivery_succeeded": bool(delivery_result.get("delivery_succeeded", False)),
        "webhook_status_code": delivery_result.get("webhook_status_code"),
        "webhook_exception": delivery_result.get("webhook_exception"),
        "webhook_response_text": delivery_result.get("webhook_response_text"),
        "delivery_blocking_stage": delivery_result.get("delivery_blocking_stage"),
        "delivery_blocking_reason": delivery_result.get("delivery_blocking_reason"),
        "previous_integer": previous_integer,
        "current_integer": current_integer,
        "crossed_integer": previous_integer is not None and previous_integer != current_integer,
    }


def _send_alert(webhook: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "delivery_attempted": False,
        "delivery_succeeded": False,
        "webhook_status_code": None,
        "webhook_exception": None,
        "webhook_response_text": None,
        "delivery_blocking_stage": None,
        "delivery_blocking_reason": None,
    }

    if not webhook:
        result["delivery_blocking_stage"] = "config"
        result["delivery_blocking_reason"] = "WEBHOOK_MISSING"
        return result
    try:
        station = (payload.get("station") or "UNK").upper()
        legacy = payload.get("legacy") if isinstance(payload.get("legacy"), dict) else payload
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        transition_context = payload.get("transition_context") if isinstance(payload.get("transition_context"), dict) else {}
        market_context = payload.get("market_context") if isinstance(payload.get("market_context"), dict) else {}
        eligibility_evaluation = payload.get("eligibility_evaluation") if isinstance(payload.get("eligibility_evaluation"), dict) else {}
        suppression = payload.get("suppression") if isinstance(payload.get("suppression"), dict) else {}
        execution_context = payload.get("execution_context") if isinstance(payload.get("execution_context"), dict) else {}

        tf = summary.get("temp_f", legacy.get("temp_f"))
        pf = legacy.get("prev_temp_f")
        df = legacy.get("delta_f")
        transition_correlation = legacy.get("transition_correlation")

        if tf is None:
            result["delivery_blocking_stage"] = "payload"
            result["delivery_blocking_reason"] = "MISSING_TEMP_F"
            return result

        instant_bucket_changed = bool(legacy.get("instant_bucket_changed"))
        settlement_bucket_changed = bool(legacy.get("settlement_bucket_changed"))
        reference_timestamp_utc = legacy.get("obs_time") or payload.get("timestamp_utc")
        transition_correlation = payload.get("legacy", {}).get("transition_correlation")
        recent_transition_active = _is_recent_transition_active(
            reference_timestamp_utc=reference_timestamp_utc,
            transition_correlation=transition_correlation if isinstance(transition_correlation, dict) else None,
        )
        # Alert gating: market evaluation proceeds when transition is current-cycle
        # or deterministically active within the configured transition window.
        if (
            not instant_bucket_changed
            and not settlement_bucket_changed
            and not recent_transition_active
            and not transition_correlation
        ):
            _annotate_transition_history_market_eval(
                station=station,
                transition_correlation=transition_correlation,
                alerts_sent=0,
                evaluation_outcome="SUPPRESSED_NO_TRANSITION",
                suppression_reason="NO_TRANSITION",
            )
            result["delivery_blocking_stage"] = "transition_gate"
            result["delivery_blocking_reason"] = "NO_TRANSITION"
            return result

        rate_limit_reference_dt = _parse_iso_utc_optional(reference_timestamp_utc)
        if rate_limit_reference_dt is None and isinstance(transition_correlation, dict):
            rate_limit_reference_dt = _parse_iso_utc_optional(transition_correlation.get("timestamp_utc"))
        if rate_limit_reference_dt is None:
            result["delivery_blocking_stage"] = "rate_limit_gate"
            result["delivery_blocking_reason"] = "MISSING_REFERENCE_TIMESTAMP"
            return result

        now_ts = rate_limit_reference_dt.timestamp()
        with _KALSHI_RATE_LIMIT_LOCK:
            last_call_ts = _KALSHI_LAST_CALL_TS.get(station)
            # Throttle causal implication: suppress duplicate near-term Kalshi checks
            # while leaving previously recorded transition history intact.
            if last_call_ts is not None and (now_ts - last_call_ts) < _KALSHI_CALL_THROTTLE_SECONDS:
                result["delivery_blocking_stage"] = "rate_limit_gate"
                result["delivery_blocking_reason"] = "KALSHI_CALL_THROTTLE"
                return result

        try:
            from core.kalshi_monitor import (
                _LAST_PROXIMITY_REGIME,
                _PROXIMITY_LOCK,
                _PROXIMITY_RANK,
                _get_active_stations,
                _parse_target_market_types,
                build_structured_snapshot_from_cache,
                classify_proximity,
                enqueue_station_hydration,
                get_hydration_prerequisite_state_snapshot,
                hydration_queue_snapshot,
                process_ladder_transition,
                send_composed_weather_market_alert,
            )

            active = _get_active_stations()
            station_is_active = active is None or station in active
            should_alert_on_missing = os.getenv("ALERT_ON_MISSING_LADDER", "false").lower() in ("1", "true", "yes", "y")
            if bool(payload.get("debug_force_missing_ladder_alert")):
                should_alert_on_missing = True
            target_market_types = _parse_target_market_types(
                os.getenv("KALSHI_TARGET_MARKET_TYPE")
            )
            if not target_market_types:
                target_market_types = {"HIGH"}
            debug_target_market_types = payload.get("debug_target_market_types")
            if isinstance(debug_target_market_types, (list, tuple, set)):
                forced_market_types = {
                    str(market_type).strip().upper()
                    for market_type in debug_target_market_types
                    if str(market_type).strip()
                }
                if forced_market_types:
                    target_market_types = forced_market_types

            evaluated_market_attempts = 0
            no_eligible_market_count = 0
            market_alerts_sent = 0
            suppression_reason = None
            saw_terminal_state = False
            markets_considered_count = 0
            eligible_markets_count = 0
            hydrated_any = False
            market_context_seeded = bool(market_context.get("event_ticker"))
            rejection_breakdown = {
                "outside_price_band": 0,
                "wrong_series": 0,
                "expired_market": 0,
                "settlement_mismatch": 0,
                "unknown_reason": 0,
            }

            hydration_prereq_snapshot = get_hydration_prerequisite_state_snapshot() or {}
            hydration_state = hydration_prereq_snapshot.get(station) or {}
            if station_is_active:
                for market_type_token in sorted(target_market_types):
                    evaluated_market_attempts += 1
                    snapshot = build_structured_snapshot_from_cache(station, {market_type_token})
                    debug_snapshot_override = payload.get("debug_market_snapshot_override")
                    if isinstance(debug_snapshot_override, dict):
                        snapshot = {
                            **snapshot,
                            **debug_snapshot_override,
                        }
                        snapshot["markets"] = list(debug_snapshot_override.get("markets") or [])
                    markets = snapshot.get("markets") or []
                    hydration_usable = bool(hydration_state.get("cache_valid"))
                    hydrated_any = hydrated_any or hydration_usable or bool(snapshot.get("cache_written"))
                    raw_market_count = int(snapshot.get("raw_market_count") or 0)
                    filtered_market_count = int(snapshot.get("filtered_market_count") or 0)
                    pre_directional_market_count = int(snapshot.get("pre_directional_market_count") or len(markets))
                    empty_reason = str(snapshot.get("empty_reason") or "")
                    rejection_counts = snapshot.get("rejection_counts") or {}

                    markets_considered_count += raw_market_count
                    eligible_markets_count += pre_directional_market_count

                    wrong_series_rejections = int(rejection_counts.get("city_token_mismatch") or 0) + int(rejection_counts.get("market_type_mismatch") or 0)
                    settlement_mismatch_rejections = int(rejection_counts.get("date_mismatch") or 0)
                    expired_rejections = int(rejection_counts.get("inactive_market") or 0)
                    outside_price_band_rejections = max(filtered_market_count - len(markets), 0)
                    known_rejections = wrong_series_rejections + settlement_mismatch_rejections + expired_rejections + outside_price_band_rejections
                    unknown_rejections = max(raw_market_count - len(markets) - known_rejections, 0)

                    rejection_breakdown["wrong_series"] += wrong_series_rejections
                    rejection_breakdown["settlement_mismatch"] += settlement_mismatch_rejections
                    rejection_breakdown["expired_market"] += expired_rejections
                    rejection_breakdown["outside_price_band"] += outside_price_band_rejections
                    rejection_breakdown["unknown_reason"] += unknown_rejections

                    _ALERT_LOGGER.info(
                        "EVAL ladder_check station=%s type=%s market_type=%s markets_found=%s",
                        station,
                        market_type_token,
                        market_type_token,
                        len(markets),
                    )

                    if not markets:
                        no_eligible_market_count += 1
                        enqueue_station_hydration(
                            station,
                            reason=f"alert_market_eval_{hydration_state.get('status') or 'cache_missing'}",
                        )
                        hydration_queue = hydration_queue_snapshot(reference_ts=now_ts) or {}
                        warmup_window_seconds = int(os.getenv("HYDRATION_MISSING_LADDER_WARMUP_SECONDS", "900"))
                        station_in_queue = station in set(hydration_queue.get("queued_stations") or [])
                        backoff_until_ts = float((hydration_queue.get("backoff_until") or {}).get(station) or 0.0)
                        in_backoff_flow = backoff_until_ts > now_ts
                        attempted_recently = False
                        evaluated_at_utc = _parse_iso_utc_optional(hydration_state.get("evaluated_at_utc"))
                        if evaluated_at_utc is not None:
                            attempted_recently = (now_ts - evaluated_at_utc.timestamp()) <= warmup_window_seconds
                        warmup_suppressed = (
                            bool(hydration_state.get("attempted"))
                            and bool(hydration_state.get("series_discovered"))
                            and not bool(hydration_state.get("markets_cached"))
                            and is_scheduler_running()
                            and (station_in_queue or in_backoff_flow or attempted_recently)
                        )
                        if warmup_suppressed:
                            suppression_reason = "ladder_hydration_warmup"
                            _ALERT_LOGGER.info(
                                "SUPPRESS ladder_missing station=%s type=%s reason=ladder_hydration_warmup",
                                station,
                                market_type_token,
                            )
                            continue
                        if should_alert_on_missing:
                            try:
                                if _to_local:
                                    local_date = _to_local(station, rate_limit_reference_dt).date().isoformat()
                                else:
                                    local_date = rate_limit_reference_dt.date().isoformat()
                            except Exception:
                                local_date = rate_limit_reference_dt.date().isoformat()

                            dedupe_key = f"{station}_{market_type_token}_{local_date}"

                            with _MISSING_LADDER_LOCK:
                                if dedupe_key in _MISSING_LADDER_DEDUPE:
                                    continue
                            missing_alert_type = "ladder_selection_empty" if empty_reason == "no_directional_ladder_match" else "ladder_missing"
                            missing_message_label = "Ladder selection empty" if missing_alert_type == "ladder_selection_empty" else "Ladder missing"
                            cause, explanation = _resolve_missing_ladder_cause(empty_reason)
                            _ALERT_LOGGER.info(
                                "WARN %s station=%s market_type=%s reason=%s cause=%s",
                                missing_alert_type,
                                station,
                                market_type_token,
                                empty_reason or "unknown",
                                cause,
                            )
                            content = (
                                f"⚠️ {missing_message_label} — "
                                f"station={station} type={market_type_token} temp={tf}°F "
                                f"reason={empty_reason or 'unknown'} "
                                f"cause={cause}"
                            )

                            if explanation:
                                content += f" explanation={explanation}"

                            webhook_status_code = None
                            webhook_exception = None
                            webhook_response_text = None
                            try:
                                response = requests.post(
                                    webhook,
                                    json={"content": content},
                                    timeout=10,
                                )
                                webhook_status_code = int(response.status_code)
                                webhook_response_text = str(getattr(response, "text", "") or "")[:200] or None
                            except Exception as exc:
                                webhook_exception = str(exc)

                            result = {
                                "delivery_attempted": True,
                                "delivery_succeeded": bool(
                                    webhook_status_code is not None and 200 <= webhook_status_code < 300
                                ),
                                "webhook_status_code": webhook_status_code,
                                "webhook_exception": webhook_exception,
                                "webhook_response_text": webhook_response_text,
                            }
                            _audit_alert(
                                station=station,
                                market_type=market_type_token,
                                event_ticker="",
                                alert_type=missing_alert_type,
                                direction=None,
                                temp_f=float(tf),
                                bucket_index=None,
                                metadata={
                                    "status_code": webhook_status_code,
                                    "webhook_exception": webhook_exception,
                                    "empty_reason": empty_reason or None,
                                    "cause": cause,
                                    "explanation": explanation,
                                },
                            )
                            if webhook_status_code is not None and 200 <= webhook_status_code < 300:
                                with _MISSING_LADDER_LOCK:
                                    _MISSING_LADDER_DEDUPE[dedupe_key] = True
                        continue

                    transition = process_ladder_transition(
                        station=station,
                        market_type=market_type_token,
                        snapshot=snapshot,
                        current_temp=tf,
                    )

                    transition_active = bool(transition.get("should_alert"))
                    if not transition_active:
                        transition_active = recent_transition_active

                    if transition_active:
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
                        send_result = send_composed_weather_market_alert(
                            station=station,
                            market_types={market_type_token},
                            transition_reason=transition.get("reason") or "window_active",
                            prev_temp_f=pf,
                            now_temp_f=tf,
                            delta_f=df,
                            obs_time_utc=legacy.get("obs_time"),
                        )
                        if send_result:
                            result = {
                                "delivery_attempted": bool(
                                    send_result.get("delivery_attempted")
                                    or send_result.get("webhook_status_code") is not None
                                    or send_result.get("webhook_exception") is not None
                                    or send_result.get("webhook_response_text") is not None
                                ),
                                "delivery_succeeded": bool(send_result.get("delivery_succeeded", bool(send_result.get("ok")))),
                                "webhook_status_code": send_result.get("webhook_status_code"),
                                "webhook_exception": send_result.get("webhook_exception"),
                                "webhook_response_text": send_result.get("webhook_response_text"),
                            }
                        if send_result and send_result.get("ok"):
                            market_alerts_sent += 1
                            send_event_ticker = send_result.get("event_ticker") or event_ticker
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
                                bucket_index=send_result.get("bucket_index"),
                                metadata={
                                    "reason": transition.get("reason") or "window_active",
                                    "attention_phrase": send_result.get("attention_phrase"),
                                    "alert_context": send_result.get("alert_context"),
                                },
                            )
                    else:
                        if transition.get("terminal_state_blocked"):
                            saw_terminal_state = True
                        raw_reason = (transition.get("reason") or "").strip().upper()
                        if raw_reason:
                            suppression_reason = raw_reason

                    if not market_context_seeded:
                        nearest_market = markets[0] if markets else None
                        market_context.update(
                            {
                                "series_ticker": (nearest_market or {}).get("series_ticker"),
                                "event_ticker": (nearest_market or {}).get("event_ticker"),
                                "market_type": market_type_token,
                                "strike": (nearest_market or {}).get("strike"),
                                "hydrated": hydration_usable,
                            }
                        )
                        market_context_seeded = True

                    nearest_market = markets[0] if markets else None
                    nearest_strike = (nearest_market or {}).get("strike")
                    proximity_regime_tightened = False
                    try:
                        observed_value = float(tf)
                    except (TypeError, ValueError):
                        observed_value = None

                    if nearest_strike is not None and observed_value is not None:
                        distance = abs(float(nearest_strike) - observed_value)
                        new_regime = classify_proximity(distance)
                        regime_key = (station, market_type_token)
                        with _PROXIMITY_LOCK:
                            old_regime = _LAST_PROXIMITY_REGIME.get(regime_key)
                            proximity_regime_tightened = (
                                old_regime is None
                                or _PROXIMITY_RANK[new_regime] > _PROXIMITY_RANK[old_regime]
                            )
                            _LAST_PROXIMITY_REGIME[regime_key] = new_regime
                            market_context["proximity_regime"] = new_regime
                        if new_regime == "CRITICAL":
                            enqueue_station_hydration(station, reason="proximity_critical")

                    if settlement_bucket_changed:
                        enqueue_station_hydration(station, reason="settlement_bucket_changed")


            # Alert eligibility summary: transition -> market evaluation -> suppression
            # or alert outcome is recorded for deterministic post-run introspection.
            if evaluated_market_attempts > 0:
                if market_alerts_sent > 0:
                    evaluation_outcome = "ALERT_SENT"
                elif saw_terminal_state:
                    evaluation_outcome = "TERMINAL_STATE"
                elif no_eligible_market_count == evaluated_market_attempts:
                    evaluation_outcome = "NO_ELIGIBLE_MARKET"
                else:
                    reason_token = suppression_reason or "MARKET_RULE"
                    evaluation_outcome = f"SUPPRESSED_{reason_token}"

                _annotate_transition_history_market_eval(
                    station=station,
                    transition_correlation=transition_correlation,
                    alerts_sent=market_alerts_sent,
                    evaluation_outcome=evaluation_outcome,
                    suppression_reason=suppression_reason,
                    market_eligibility_runtime={
                        "markets_considered_count": markets_considered_count,
                        "eligible_markets_count": eligible_markets_count,
                        "rejected_markets_count": max(markets_considered_count - eligible_markets_count, 0),
                        "rejection_breakdown": rejection_breakdown,
                    },
                )

                payload["schema_version"] = ALERT_SCHEMA_VERSION
                payload["timestamp_utc"] = payload.get("timestamp_utc") or _now_utc_iso()
                payload["station"] = station
                payload["summary"] = summary
                payload["transition_context"] = transition_context
                payload["market_context"] = market_context

                suppressed = market_alerts_sent == 0
                reason_category = "NO_TRANSITION"
                reason_text = suppression_reason or ""
                classification = "MARKET_ELIGIBLE" if not suppressed else "MARKET_SUPPRESSED"
                if not hydrated_any:
                    classification = "HYDRATION_BLOCKED"
                    reason_category = "HYDRATION_BLOCK"
                    reason_text = "hydration_cache_unavailable"
                elif no_eligible_market_count == evaluated_market_attempts:
                    reason_category = "NO_ELIGIBLE_MARKET"
                    reason_text = reason_text or "no_eligible_market"
                elif suppression_reason == "TERMINAL_STATE":
                    reason_category = "SETTLEMENT_MISMATCH"

                payload["classification"] = classification
                summary["headline"] = f"{station} {classification.lower().replace('_', ' ')}"
                summary["transition"] = transition_context.get("transition_type") or "transition"
                summary["temp_f"] = tf
                summary["instant_bucket"] = transition_context.get("instant_after")
                summary["settlement_bucket"] = transition_context.get("settlement_bucket")

                eligibility_evaluation.update(
                    {
                        "markets_considered": markets_considered_count,
                        "eligible_markets": eligible_markets_count,
                        "rejected_markets": max(markets_considered_count - eligible_markets_count, 0),
                        "rejection_breakdown": rejection_breakdown,
                    }
                )
                suppression.update(
                    {
                        "suppressed": suppressed,
                        "reason": reason_text,
                        "reason_category": reason_category,
                    }
                )
                execution_context.update(
                    {
                        "execution_domain": "production",
                        "hydration_state": {
                            "hydrated": hydrated_any,
                            "station_is_active": station_is_active,
                        },
                        "scheduler_poll_count": get_metrics().get("poll_count"),
                    }
                )
                payload["eligibility_evaluation"] = eligibility_evaluation
                payload["suppression"] = suppression
                payload["execution_context"] = execution_context

            with _KALSHI_RATE_LIMIT_LOCK:
                _KALSHI_LAST_CALL_TS[station] = now_ts
        except Exception as e:
            result = {
                "delivery_succeeded": False,
                "webhook_status_code": None,
                "webhook_exception": str(e),
                "webhook_response_text": None,
            }
            print(f"[ERROR] station={station} function=_send_alert: {e}")
    except Exception as e:
        result = {
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": str(e),
            "webhook_response_text": None,
        }
        print(f"[ERROR] station={payload.get('station') or 'UNK'} function=_send_alert: {e}")
    return result


# =========================
# Window + fetch routers (STRICT: no auto-fallback)
# =========================
def _compute_window(icao: str, minutes: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None):
    """
    Compute a rolling start/end window in UTC.
    - If we've seen this ICAO, start from (last_seen - OVERLAP_SECONDS).
    - If first run, use now - BOOTSTRAP_LOOKBACK_MINUTES.
    Returns: (start_iso_z, end_iso_z, start_dt, end_dt)
    """
    if cfg is None:
        cfg = get_default_config()
    lookback = int(minutes if minutes is not None else cfg.get("lookback_min", 3))

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    window_end = now - timedelta(seconds=PUBLICATION_LAG_BUFFER_SECONDS)

    with _STATE_LOCK:
        last_seen = _STATE["last_seen_iso"].get(icao)

    if last_seen:
        start_dt = _parse_iso(last_seen) - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start_dt = window_end - timedelta(minutes=BOOTSTRAP_LOOKBACK_MINUTES)

    end_dt = window_end
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

    from core.kalshi_monitor import (
        _current_kalshi_execution_domain,
        ensure_ladder_hydration_prerequisite,
        get_hydration_prerequisite_state_snapshot,
        hydrate_station_ladder_snapshot,
    )

    now_iso = _now_utc_iso()
    hydration = ensure_ladder_hydration_prerequisite(icao)
    if hydration.get("status") != "cache_valid":
        with _STATE_LOCK:
            _STATE["ingestion_runtime"][icao] = {
                "last_poll_attempt_utc": now_iso,
                "last_fetch_status": "skipped_ladder_not_hydrated",
                "fetched_observation_count": 0,
                "ingested_observation_count": 0,
                "rejected_observation_count": 0,
                "rejection_reasons": [],
                "latest_raw_observation_timestamp": None,
                "latest_accepted_observation_timestamp": _STATE["last_seen_iso"].get(icao),
                "window_start_utc": start_dt.isoformat(),
                "window_end_utc": end_dt.isoformat(),
                "sample_rejected_observations": [],
            }
        return {
            "status": "degraded",
            "icao": icao,
            "source": chosen,
            "poll_skipped_reason": "ladder_not_hydrated",
            "hydration": hydration,
        }

    try:
        obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
        with _STATE_LOCK:
            pre_ingest_last_seen = _STATE["last_seen_iso"].get(icao)
        rejection_diagnostics = _compute_rejection_reasons(
            icao,
            obs_list,
            last_seen_iso=pre_ingest_last_seen,
            window_start=start_dt,
            window_end=end_dt,
        )
        rejection_reason_counts = rejection_diagnostics["reasons"]
        ing, al = _ingest_obs(icao, obs_list, cfg, window_start=start_dt, window_end=end_dt)
        with _STATE_LOCK:
            latest = _STATE["last_obs"].get(icao)
            _STATE["ingestion_runtime"][icao] = {
                "last_poll_attempt_utc": now_iso,
                "last_fetch_status": "ok",
                "fetched_observation_count": len(obs_list),
                "ingested_observation_count": ing,
                "rejected_observation_count": max(0, len(obs_list) - ing),
                "rejection_reasons": [
                    {"reason": reason, "count": count}
                    for reason, count in sorted(rejection_reason_counts.items())
                ],
                "window_start_utc": start_dt.isoformat(),
                "window_end_utc": end_dt.isoformat(),
                "latest_raw_observation_timestamp": (obs_list[-1].get("obs_time") if obs_list else None),
                "latest_accepted_observation_timestamp": _STATE["last_seen_iso"].get(icao),
                "sample_rejected_observations": rejection_diagnostics["sample_rejected_observations"],
            }
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
        with _STATE_LOCK:
            _STATE["ingestion_runtime"][icao] = {
                "last_poll_attempt_utc": now_iso,
                "last_fetch_status": "error",
                "fetched_observation_count": 0,
                "ingested_observation_count": 0,
                "rejected_observation_count": 0,
                "rejection_reasons": [{"reason": "fetch_or_ingest_exception", "count": 1}],
                "latest_raw_observation_timestamp": None,
                "latest_accepted_observation_timestamp": _STATE["last_seen_iso"].get(icao),
                "window_start_utc": start_dt.isoformat(),
                "window_end_utc": end_dt.isoformat(),
                "sample_rejected_observations": [],
            }
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
# Scheduler causal flow per loop: hydrate market prerequisites, fetch
# observations, ingest deterministically, then update immutable metrics.
def _poll_once(logger=None):
    from core.kalshi_monitor import kalshi_execution_domain

    with kalshi_execution_domain("production"):
        ensure_state_loaded()
        cfg = get_default_config()
        stations = _resolve_live_polling_stations(cfg)

        from core.kalshi_monitor import (
            enqueue_station_hydration,
            ensure_ladder_hydration_prerequisite,
            _parse_target_market_types,
            process_hydration_queue_worker,
        )

        hydration_by_station = {}
        ingestion_admission = {}
        for icao in stations:
            hydration = ensure_ladder_hydration_prerequisite(icao)
            hydration_by_station[icao] = hydration

            if hydration.get("status") in {"cache_missing", "cache_stale"}:
                enqueue_station_hydration(icao, reason=str(hydration.get("reason") or hydration.get("status")))

            hydration_passed = hydration.get("status") == "cache_valid"
            ingestion_admission[icao] = {
                "hydration_passed": hydration_passed,
                "admitted_to_fetch": True,
                "skip_reason": None,
                "market_phase_enabled": hydration_passed,
                "evaluated_at_utc": _now_utc_iso(),
            }
            if hydration.get("status") != "cache_valid" and logger:
                logger.warning(
                    f"market_phase_blocked_reason=ladder_not_hydrated station={icao} hydration_status={hydration.get('status')}"
                )

        try:
            hydration_market_types = _parse_target_market_types(
                os.getenv("KALSHI_TARGET_MARKET_TYPE")
            )
            if not hydration_market_types:
                hydration_market_types = {"HIGH"}
            process_hydration_queue_worker(market_types=hydration_market_types)
        except Exception as e:
            if logger:
                logger.warning(f"poll_hydration_worker_failed: {e}")

        chosen = cfg["default_source"] or "nws"

        total_ing = 0
        total_alerts = 0
        for icao in stations:
            hydration = hydration_by_station.get(icao) or {}
            poll_attempt_utc = _now_utc_iso()
            market_phase_enabled = hydration.get("status") == "cache_valid"
            try:
                start_iso, end_iso, start_dt, end_dt = _compute_window(icao, cfg.get("lookback_min", 3), cfg)
                obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
                with _STATE_LOCK:
                    pre_ingest_last_seen = _STATE["last_seen_iso"].get(icao)
                rejection_diagnostics = _compute_rejection_reasons(
                    icao,
                    obs_list,
                    last_seen_iso=pre_ingest_last_seen,
                    window_start=start_dt,
                    window_end=end_dt,
                )
                rejection_reason_counts = rejection_diagnostics["reasons"]
                ing, al = _ingest_obs(
                    icao,
                    obs_list,
                    cfg,
                    allow_alert_delivery=True,
                    persist_cache=True,
                    window_start=start_dt,
                    window_end=end_dt,
                )
                total_ing += ing
                total_alerts += al
                with _STATE_LOCK:
                    _STATE["ingestion_runtime"][icao] = {
                        "last_poll_attempt_utc": poll_attempt_utc,
                        "last_fetch_status": "ok",
                        "fetched_observation_count": len(obs_list),
                        "ingested_observation_count": ing,
                        "rejected_observation_count": max(0, len(obs_list) - ing),
                        "rejection_reasons": [
                            {"reason": reason, "count": count}
                            for reason, count in sorted(rejection_reason_counts.items())
                        ],
                        "window_start_utc": start_dt.isoformat(),
                        "window_end_utc": end_dt.isoformat(),
                        "latest_raw_observation_timestamp": (obs_list[-1].get("obs_time") if obs_list else None),
                        "latest_accepted_observation_timestamp": _STATE["last_seen_iso"].get(icao),
                        "sample_rejected_observations": rejection_diagnostics["sample_rejected_observations"],
                    }
            except Exception as e:
                with _STATE_LOCK:
                    _STATE["ingestion_runtime"][icao] = {
                        "last_poll_attempt_utc": poll_attempt_utc,
                        "last_fetch_status": "error",
                        "fetched_observation_count": 0,
                        "ingested_observation_count": 0,
                        "rejected_observation_count": 0,
                        "rejection_reasons": [{"reason": "fetch_or_ingest_exception", "count": 1}],
                        "latest_raw_observation_timestamp": None,
                        "latest_accepted_observation_timestamp": _STATE["last_seen_iso"].get(icao),
                        "window_start_utc": None,
                        "window_end_utc": None,
                        "sample_rejected_observations": [],
                    }
                if logger:
                    logger.error(f"poll failed for {icao} ({chosen}): {e}")

        with _STATE_LOCK:
            _STATE["ingestion_admission"].update(ingestion_admission)
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
