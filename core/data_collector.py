"""
Data collector Module

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
__all__ = ['verify_webhook_signature', 'fetch_window', 'fetch_latest', 'fetch_now', 'get_latest_metar', 'set_watchlist', 'get_watchlist', 'get_metrics', 'set_live_station_universe_resolver', 'start_scheduler', 'ensure_scheduler_started', 'stop_scheduler', 'is_scheduler_running', 'log_near_miss', 'log_near_miss_if_cooldown', 'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market', 'log_near_miss_if_epoch_alert_emitted']


# Layer 4: Webhook signature verification
def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify webhook signature using HMAC-SHA256.
    
    Args:
        payload: Raw webhook payload bytes
        signature: X-Webhook-Signature header value
        secret: Webhook secret
        
    Returns:
        True if signature is valid, False otherwise
    """
    if not secret or not signature:
        return False
    
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
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
def _obs_tuple(
    temp_f: float,
    ts_iso: str,
    raw: Any,
    source: str,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    obs = {
        "temp_f": round(float(temp_f), 1),
        "obs_time": ts_iso,
        "raw": raw,
        "source": source,
        "temperature_c": None,
        "dewpoint_c": None,
        "wind_direction_deg": None,
        "wind_speed_kt": None,
        "wind_gust_kt": None,
        "altimeter_in_hg": None,
        "sea_level_pressure_mb": None,
        "visibility_mi": None,
        "ceiling_ft": None,
        "cloud_layers": None,
        "weather_codes": None,
        "observation_age_seconds": None,
        "station_id": None,
    }
    if extras:
        obs.update({k: v for k, v in extras.items() if k in obs})
    return obs
def _to_float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None
def _to_int_or_none(value: Any) -> Optional[int]:
    numeric = _to_float_or_none(value)
    if numeric is None:
        return None
    return int(round(numeric))
def _nws_extract_weather_codes(props: Dict[str, Any]) -> Optional[List[str]]:
    codes: List[str] = []
    present_weather = props.get("presentWeather") if isinstance(props, dict) else None
    if isinstance(present_weather, list):
        for item in present_weather:
            if not isinstance(item, dict):
                continue
            for key in ("rawString", "weather", "weatherCode", "value"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    token = val.strip().upper()
                    if token not in codes:
                        codes.append(token)
    raw_message = props.get("rawMessage") if isinstance(props, dict) else None
    if isinstance(raw_message, str) and raw_message:
        for token in re.findall(r"\b(?:\+|-)?[A-Z]{2,6}\b", raw_message):
            normalized = token.upper()
            if normalized not in codes:
                codes.append(normalized)
    return codes or None
def _nws_optional_metrics(props: Dict[str, Any], station: str) -> Dict[str, Any]:
    station_id = props.get("stationIdentifier")
    if not station_id and isinstance(props.get("station"), str):
        station_id = props["station"].rstrip("/").split("/")[-1]
    metrics: Dict[str, Any] = {
        "temperature_c": _to_float_or_none((props.get("temperature") or {}).get("value")),
        "dewpoint_c": _to_float_or_none((props.get("dewpoint") or {}).get("value")),
        "wind_direction_deg": _to_int_or_none((props.get("windDirection") or {}).get("value")),
        "wind_speed_kt": None,
        "wind_gust_kt": None,
        "altimeter_in_hg": None,
        "sea_level_pressure_mb": None,
        "visibility_mi": None,
        "ceiling_ft": None,
        "cloud_layers": None,
        "weather_codes": _nws_extract_weather_codes(props),
        "observation_age_seconds": _to_float_or_none(props.get("observation_age_seconds")),
        "station_id": (station_id or station).strip().upper() if (station_id or station) else None,
    }

    wind_speed_mps = _to_float_or_none((props.get("windSpeed") or {}).get("value"))
    if wind_speed_mps is not None:
        metrics["wind_speed_kt"] = round(wind_speed_mps * 1.943844, 1)

    wind_gust_mps = _to_float_or_none((props.get("windGust") or {}).get("value"))
    if wind_gust_mps is not None:
        metrics["wind_gust_kt"] = round(wind_gust_mps * 1.943844, 1)

    altimeter_pa = _to_float_or_none((props.get("barometricPressure") or {}).get("value"))
    if altimeter_pa is not None:
        metrics["altimeter_in_hg"] = round(altimeter_pa / 3386.389, 2)

    sea_level_pressure_pa = _to_float_or_none((props.get("seaLevelPressure") or {}).get("value"))
    if sea_level_pressure_pa is not None:
        metrics["sea_level_pressure_mb"] = round(sea_level_pressure_pa / 100.0, 1)

    visibility_m = _to_float_or_none((props.get("visibility") or {}).get("value"))
    if visibility_m is not None:
        metrics["visibility_mi"] = round(visibility_m / 1609.344, 2)

    ceiling_m = _to_float_or_none((props.get("ceiling") or {}).get("value"))
    if ceiling_m is not None:
        metrics["ceiling_ft"] = int(round(ceiling_m * 3.28084))

    cloud_layers = props.get("cloudLayers")
    if isinstance(cloud_layers, list):
        normalized_layers: List[Dict[str, Any]] = []
        for layer in cloud_layers:
            if not isinstance(layer, dict):
                continue
            layer_base_m = _to_float_or_none((layer.get("base") or {}).get("value"))
            normalized_layers.append(
                {
                    "base_ft": int(round(layer_base_m * 3.28084)) if layer_base_m is not None else None,
                    "amount": layer.get("amount"),
                }
            )
        metrics["cloud_layers"] = normalized_layers or None

    return metrics
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
        station_identifier = props.get("stationIdentifier")
        out.append(
            _obs_tuple(
                _c_to_f(float(val_c)),
                ts,
                props,
                "nws",
                extras=_nws_optional_metrics(props, station_identifier or ""),
            )
        )
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
        out.append(_obs_tuple(tf, ts.isoformat(), row, "iem", extras={"station_id": (row.get("station") or "").strip().upper() or None}))
    out.sort(key=lambda x: _parse_iso(x["obs_time"]))
    return out
def _parse_tgftp_text(text: str) -> Optional[Dict[str, Any]]:
    lines = text.strip().splitlines()
    if not lines:
        return None
    ts_line = lines[0].strip()
    metar_line = lines[-1].strip()

    def _parse_metar_temp_c(token: str) -> Optional[float]:
        left = (token or "").strip().partition("/")[0]
        if not left or left == "////":
            return None
        match = re.fullmatch(r"(M?)(\d{2})", left)
        if not match:
            return None
        sign, digits = match.groups()
        value = float(digits)
        return -value if sign == "M" else value

    # Find temp from token like 12/01 or M02/M05
    temp_f: Optional[float] = None
    for token in metar_line.split():
        if "/" not in token or len(token) > 9:
            continue
        c = _parse_metar_temp_c(token)
        if c is None:
            continue
        temp_f = _c_to_f(c)
        break
    if temp_f is None:
        return None

    try:
        ts = datetime.strptime(ts_line, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        ts = _now_utc_iso()
    station_token = metar_line.split()[0].strip().upper() if metar_line.split() else None
    return _obs_tuple(temp_f, ts, metar_line, "tgftp", extras={"station_id": station_token})
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
    return _obs_tuple(_c_to_f(float(val_c)), ts, props, "nws", extras=_nws_optional_metrics(props, icao))
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
        
        # Fix 4: KNYC data sparsity monitoring
        if icao.strip().upper() == "KNYC":
            _check_knyc_observation_gap(icao, ts)

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
def _poll_once(logger=None):
    from core.kalshi_monitor import kalshi_execution_domain

    with kalshi_execution_domain("production"):
        ensure_state_loaded()
        cfg = get_default_config()
        stations = _resolve_live_polling_stations(cfg)

        from core.kalshi_monitor import (
            _configured_target_market_types,
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
            hydration_market_types = _configured_target_market_types()
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
        # Layer 1: Retry failed alert deliveries on each cycle
        try:
            _retry_delivery_batch()
        except Exception as retry_err:
            if logger:
                logger.warning(f"METAR retry batch failed: {retry_err}")
        _SCHEDULER_STOP.wait(interval_sec)
def _check_knyc_observation_gap(station: str, current_obs_time: str) -> None:
    """Monitor KNYC METAR observation gaps for data sparsity alerts.
    
    Logs a warning when the gap between consecutive KNYC observations
    exceeds 3600 seconds (1 hour), indicating potential data quality issues.
    
    Args:
        station: Station ICAO code (checked for KNYC)
        current_obs_time: Current observation timestamp in ISO format
    """
    normalized_station = (station or "").strip().upper()
    if normalized_station != "KNYC":
        return
    
    try:
        current_dt = _parse_iso(current_obs_time)
        if current_dt is None:
            return
        
        # Get last observation time from state
        with _STATE_LOCK:
            last_obs = _STATE["last_obs"].get(normalized_station)
        
        if not last_obs:
            return
        
        last_obs_time = last_obs.get("obs_time")
        if not last_obs_time:
            return
        
        last_dt = _parse_iso(last_obs_time)
        if last_dt is None:
            return
        
        gap_seconds = (current_dt - last_dt).total_seconds()
        
        # Alert threshold: 3600 seconds (1 hour)
        if gap_seconds > 3600:
            _ALERT_LOGGER.warning(
                "knyc_data_sparsity station=%s gap_seconds=%d threshold_seconds=3600 "
                "reason=KNYC_observation_interval_exceeds_one_hour_data_quality_flag",
                normalized_station,
                int(gap_seconds)
            )
    except Exception as e:
        # Silently handle monitoring errors to avoid disrupting main flow
        pass
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
