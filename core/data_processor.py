"""
Data processor Module

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

# ─── Module-level state (moved from metar_monitor.py monolith) ───
_STATE_LOCK = state_lock()
_STATE = state_ref()
_SCHEDULER_THREAD = None
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STOP = threading.Event()
_MISSING_LADDER_DEDUPE = {}
_MISSING_LADDER_LOCK = threading.Lock()
_KALSHI_RATE_LIMIT_LOCK = threading.Lock()
_KALSHI_LAST_CALL_TS = {}
_ALERT_LOGGER = logging.getLogger(__name__)
_LAST_SETTLEMENT_UP_TS = {}
_LAST_NWS_FETCH_DIAGNOSTIC = {}
_SIGNAL_OBSERVATION_WINDOWS: Dict[str, deque] = {}
_SIGNAL_STATION_LAST_EMIT: Dict[str, float] = {}
_SIGNAL_BOUNDARY_LAST_EMIT: Dict[Tuple[str, int, int], float] = {}
_SIGNAL_EPOCH_COUNTER: Dict[str, int] = {}
_SIGNAL_GOLDILOCKS_EPOCH_TRACKER: Dict[Tuple[str, int], Dict[str, Any]] = {}
_LATEST_SIGNAL_RUNTIME: Dict[str, Dict[str, Any]] = {}
_SIGNAL_LOCK = threading.RLock()
_SIGNAL_STATION_COOLDOWN_SECONDS = 300
_SIGNAL_BOUNDARY_COOLDOWN_SECONDS = 900
_SIGNAL_MOMENTUM_WINDOW_SIZE = 3
_KALSHI_CALL_THROTTLE_SECONDS = 1.0
_MISSING_LADDER_WARMUP_SECONDS = 3600
_last_seen_iso = {}
_last_obs = {}
__all__ = ['reset_station_daily_state', 'get_default_config', 'ensure_state_loaded', 'get_state', 'get_latest_station_signal_runtime', 'log_near_miss', 'log_near_miss_if_cooldown', 'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market', 'log_near_miss_if_epoch_alert_emitted']


# Layer 4: Webhook signature verification
def _compute_goldilocks_confidence(tracker: Dict[str, Any], is_down: bool = False) -> Tuple[float, Dict[str, Any]]:
    """Compute confidence score for goldilocks signal based on epoch tracker data.
    
    R4-1.5: Asymmetric confidence split for up vs down reversion.
    
    Physical reasoning:
    - Spike up → reversion down (is_down=False): transient solar insolation peaks,
      warm-air advection pulses. Reversion is strong and reliable because the
      forcing is ephemeral. Higher confidence base.
    - Spike down → reversion up (is_down=True): transient cold-air drainage,
      evaporative cooling from precipitation. Reversion is weaker and less
      reliable — cold-air drainage can persist, evaporative cooling can be
      sustained if ground remains wet. Lower confidence base + discount.
    
    Args:
        tracker: Goldilocks epoch tracker dict with confidence data points
        is_down: True if this is a goldilocks_momentum_down signal (downward reversion)
        
    Returns:
        Tuple of (confidence_score, confidence_factors_dict)
    """
    # Use appropriate daily field based on reversion direction
    # is_down=False: spike up → reversion down, check is_daily_high
    # is_down=True: spike down → reversion up, check is_daily_low
    if is_down:
        is_daily_extreme = tracker.get("is_daily_low", False)
    else:
        is_daily_extreme = tracker.get("is_daily_high", False)
        
    daily_high_margin = float(tracker.get("daily_high_margin", 0.0) or 0.0)
    observations_since_spike = int(tracker.get("observations_since_spike", 0) or 0)
    day_fraction_at_spike = float(tracker.get("day_fraction_at_spike", 0.0) or 0.0)
    
    # Compute confidence score
    # R4-1.5: Asymmetric base confidence
    # Up reversion (spike up → drop back): base 0.40 — more reliable
    # Down reversion (spike down → bounce back): base 0.25 — less reliable
    if is_down:
        base = 0.25 if is_daily_extreme else 0.0
        # Down reversion: discount on bonuses too (cold-air drainage can persist)
        bonus_margin = min(daily_high_margin * 0.10, 0.15)  # reduced from 0.15/0.2
        bonus_obs = min(observations_since_spike * 0.015, 0.15)  # reduced from 0.02/0.2
        bonus_time = day_fraction_at_spike * 0.15  # reduced from 0.2
        
        confidence = base + bonus_margin + bonus_obs + bonus_time
        # Additional discount factor for down-reversion uncertainty
        confidence *= 0.85  # 15% discount for down-reversion signals
    else:
        # Up reversion: keep original calculation with slight boost
        base = 0.40 if is_daily_extreme else 0.0
        bonus_margin = min(daily_high_margin * 0.15, 0.20)  # up to +0.20
        bonus_obs = min(observations_since_spike * 0.02, 0.20)  # up to +0.20
        bonus_time = day_fraction_at_spike * 0.20  # up to +0.20
        
        confidence = base + bonus_margin + bonus_obs + bonus_time
        # Slight boost for up-reversion reliability
        confidence *= 1.05  # 5% boost for up-reversion signals
    
    confidence = max(0.0, min(1.0, confidence))  # clamp to [0.0, 1.0]
    
    confidence_factors = {
        "is_daily_extreme": is_daily_extreme,  # is_daily_high or is_daily_low depending on is_down
        "is_daily_high": tracker.get("is_daily_high", False),  # for backward compat
        "is_daily_low": tracker.get("is_daily_low", False),  # for backward compat
        "daily_high_margin": daily_high_margin,
        "observations_since_spike": observations_since_spike,
        "day_fraction_at_spike": day_fraction_at_spike,
        "reversion_direction": "down" if is_down else "up",
        "asymmetric_base": base,
    }
    
    return confidence, confidence_factors
def _icao_tz_name(icao: str) -> str:
    return station_timezone_name(icao)
def reset_station_daily_state(icao: str, local_day: str) -> None:
    # This wrapper is the required reset seam because it also clears local
    # suppression/runtime state; resetting authoritative state alone is insufficient.
    _reset_station_daily_state_authoritative(icao, local_day)
    _LAST_SETTLEMENT_UP_TS.pop((icao or "").strip().upper(), None)
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
def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0
def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
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
def _observation_seconds(obs_time: str) -> Optional[float]:
    obs_dt = _parse_iso_utc_optional(obs_time)
    if obs_dt is None:
        return None
    return obs_dt.timestamp()
def _current_signal_epoch(station: str) -> int:
    return int(_SIGNAL_EPOCH_COUNTER.get(station, 0) or 0)
def _record_signal_runtime(station: str, runtime: Dict[str, Any]) -> None:
    with _SIGNAL_LOCK:
        _LATEST_SIGNAL_RUNTIME[(station or "").strip().upper()] = copy.deepcopy(runtime)
def _get_signal_runtime(station: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    normalized_station = (station or "").strip().upper()
    with _SIGNAL_LOCK:
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
        direction=ALERT_TYPE_DIRECTION.get(
            str(signal_context.get("signal_type") or "signal"), "REVERSAL"
        ),
        temp_f=float(temp_f),
        bucket_index=int(math.floor(temp_f)),
        metadata={"signal_context": copy.deepcopy(signal_context)},
    )
    # Layer 1: Queue signal alert for delivery with retry logic
    alert_id = f"signal:{station}:{signal_context.get('dedupe_key') or obs_time.replace('Z', '').replace(':', '').replace('-', '')}"
    _queue_alert_for_delivery(alert_id, cfg.get("webhook", ""), payload)
def get_latest_station_signal_runtime(station: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    return _get_signal_runtime(station)
def _evaluate_deterministic_signal_layer(
    *,
    station: str,
    now_f: float,
    obs_time: str,
    transition_type: Optional[str],
    settlement_bucket: int,
    previous_settlement_bucket: Optional[int],
    hydration_cache_valid: bool,
    eligible_markets_count: int,
    cfg: Dict[str, Any],
    temperature_state: Optional[Dict[str, Any]] = None,
) -> None:
    station = (station or "").strip().upper()
    obs_seconds = _observation_seconds(obs_time)
    if obs_seconds is None:
        return
    
    # Read temperature state if not provided
    if temperature_state is None:
        temperature_state = read_temperature_state(station)

    pending_signal_context = None
    pending_runtime_record = None
    early_exit = False

    with _SIGNAL_LOCK:
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
            runtime["outcome_classification"] = OUTCOME_HYDRATION_BLOCKED
            _ALERT_LOGGER.debug(
                "signal_suppression station=%s reason=HYDRATION_CACHE_INVALID "
                "reason_detail=market_exists_but_hydration_incomplete"
            )
            # Near-miss audit: hydration blocked
            log_near_miss_if_no_eligible_market(
                station=station,
                signal_detected=True,
                discovered_markets_count=eligible_markets_count,
                hydration_valid=False,
            )
            pending_runtime_record = runtime
            early_exit = True
        elif int(eligible_markets_count) <= 0:
            # Tier 1 bypass: goldilocks/reversion alerts are protected side features
            # that fire regardless of market eligibility. Check them before early-exit.
            epoch_key = (station, int(epoch_id))
            tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)
            if isinstance(tracker, dict) and not bool(tracker.get("alert_emitted")):
                old_bucket = tracker.get("previous_settlement_bucket") or tracker.get("settlement_bucket_at_up")
                if bool(tracker.get("exceeded_by_one_or_more")) and bool(tracker.get("reverted_below_settlement")):
                    confidence_score, confidence_factors = _compute_goldilocks_confidence(tracker)
                    pending_signal_context = {
                        "signal_type": "goldilocks_reversion_alert",
                        "signal_version": 1,
                        "station": station,
                        "obs_time": obs_time,
                        "dedupe_key": f"goldilocks_reversion_alert:{station}:{epoch_id}",
                        "cooldown_applied": False,
                        "cooldown_seconds": 0,
                        "settlement_bucket_at_up": int(tracker.get("previous_settlement_bucket") or tracker.get("settlement_bucket_at_up") or settlement_bucket),
                        "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),
                        "reverted_temp": float(now_f),
                        "epoch_id": int(epoch_id),
                        "confidence": confidence_score,
                        "confidence_factors": confidence_factors,
                        "tier_1_bypass": True,
                    }
                    tracker["alert_emitted"] = True
                    runtime.update({"signal_type": "goldilocks_reversion_alert", "signal_emitted": True, "suppression_reason": None, "outcome_classification": OUTCOME_ALERT_SENT})
                    _ALERT_LOGGER.info(
                        "tier_1_bypass station=%s signal=goldilocks_reversion_alert "
                        "reason=no_eligible_market_but_tier_1_protected"
                    )
            if pending_signal_context is None and runtime.get("signal_type") is None:
                runtime["suppression_reason"] = "NO_ELIGIBLE_MARKETS"
                runtime["outcome_classification"] = OUTCOME_NO_ELIGIBLE_MARKET
                _ALERT_LOGGER.debug(
                    "signal_suppression station=%s reason=NO_ELIGIBLE_MARKETS "
                    "reason_detail=no_market_exists_for_station"
                )
                pending_runtime_record = runtime
                early_exit = True
            else:
                # Tier 1 signal fired despite no eligible market
                pending_runtime_record = runtime
        else:
            if station_cooldown_active:
                runtime["suppression_reason"] = "STATION_COOLDOWN_ACTIVE"
                runtime["outcome_classification"] = OUTCOME_ELIGIBLE_NOT_ALERTABLE
                _ALERT_LOGGER.debug(
                    "signal_suppression station=%s reason=STATION_COOLDOWN_ACTIVE "
                    "reason_detail=market_exists_but_station_in_cooldown"
                )
                # Near-miss audit: station cooldown
                if station_last_emit is not None:
                    remaining = _SIGNAL_STATION_COOLDOWN_SECONDS - (obs_seconds - station_last_emit)
                    remaining = max(0, remaining)
                    log_near_miss_if_cooldown(
                        station=station,
                        cooldown_type="STATION",
                        remaining_seconds=remaining,
                        total_seconds=_SIGNAL_STATION_COOLDOWN_SECONDS,
                    )

            next_integer = int(math.floor(now_f)) + 1
            distance_to_integer = float(next_integer) - float(now_f)
            momentum = None
            pressure_to_boundary_seconds = None
            boundary_cooldown_active = False
            boundary_key = (station, int(next_integer), int(epoch_id))
            boundary_last_emit = _SIGNAL_BOUNDARY_LAST_EMIT.get(boundary_key)
            if boundary_last_emit is not None and (obs_seconds - boundary_last_emit) < _SIGNAL_BOUNDARY_COOLDOWN_SECONDS:
                boundary_cooldown_active = True
                # Near-miss audit: boundary cooldown
                remaining = _SIGNAL_BOUNDARY_COOLDOWN_SECONDS - (obs_seconds - boundary_last_emit)
                remaining = max(0, remaining)
                log_near_miss_if_cooldown(
                    station=station,
                    cooldown_type="BOUNDARY",
                    remaining_seconds=remaining,
                    total_seconds=_SIGNAL_BOUNDARY_COOLDOWN_SECONDS,
                    boundary_level=next_integer,
                    epoch_id=epoch_id,
                )
            runtime["cooldown_state"]["boundary_active"] = boundary_cooldown_active

            # Near-miss audit: distance to boundary conditions
            if 0.0 < distance_to_integer <= 0.10:
                # Check if we're near boundary but conditions not fully met
                momentum_ok = momentum is not None and momentum >= 0.002
                if not momentum_ok:
                    log_near_miss_if_distance_to_boundary(
                        station=station,
                        distance=distance_to_integer,
                        momentum=momentum,
                        threshold_distance=0.10,
                        threshold_momentum=0.002,
                    )
            elif distance_to_integer <= 0.10:
                # Too far to be near-miss, log a low-severity event
                log_near_miss(
                    station=station,
                    near_miss_type="TOO_FAR_FROM_BOUNDARY",
                    severity="LOW",
                    details={
                        "distance_to_boundary": distance_to_integer,
                        "max_allowed_distance": 0.10,
                    },
                )

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
                pending_signal_context = {
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
                # Persist station cooldown
                _persist_signal_state(
                    f"station_cooldown:{station}",
                    {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                )
                _SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds
                # Persist boundary cooldown
                _persist_signal_state(
                    f"boundary_cooldown:{station}:{boundary_key[1]}:{boundary_key[2]}",
                    {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS},
                )
                runtime.update({"signal_type": "near_boundary_momentum_up", "signal_emitted": True, "suppression_reason": None, "outcome_classification": OUTCOME_ALERT_SENT})
                runtime["cooldown_state"]["station_active"] = True
                runtime["cooldown_state"]["boundary_active"] = True

            if pending_signal_context is None:
                epoch_key = (station, int(epoch_id))
                tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)
                if transition_type == "settlement_up":
                    # Store the OLD settlement bucket for proper "exceeded by one or more" calculation
                    old_bucket = int(previous_settlement_bucket) if previous_settlement_bucket is not None else settlement_bucket
                    
                    # Compute day fraction at spike time (for confidence scoring)
                    spike_obs_dt = _parse_iso(obs_time)
                    day_fraction_at_spike = None
                    if spike_obs_dt is not None:
                        station_tz_name = station_timezone_name(station)
                        if ZoneInfo and station_tz_name:
                            try:
                                spike_local = spike_obs_dt.astimezone(ZoneInfo(station_tz_name))
                                spike_local_day_start = spike_local.replace(hour=0, minute=0, second=0, microsecond=0)
                                day_seconds = (spike_local - spike_local_day_start).total_seconds()
                                day_fraction_at_spike = day_seconds / 86400.0  # 24 hours in seconds
                            except Exception:
                                day_fraction_at_spike = 0.5  # fallback to middle of day
                        else:
                            # Fallback if zoneinfo unavailable
                            day_fraction_at_spike = 0.5
                    
                    # Snapshot running_daily_max at spike moment
                    running_daily_max_at_spike = temperature_state["running_daily_max"] if temperature_state else None
                    
                    tracker = {
                        "settlement_bucket_at_up": int(settlement_bucket),
                        "previous_settlement_bucket": old_bucket,
                        "max_temp_after_up": float(now_f),
                        # Use OLD bucket for "exceeded by one or more" calculation
                        "exceeded_by_one_or_more": float(now_f) >= float(old_bucket) + 1.2,
                        "reverted_below_settlement": float(now_f) <= float(old_bucket) - 0.2,
                        "alert_emitted": False,
                        "settlement_up_obs_time": obs_time,
                        # NEW: Confidence data points for goldilocks
                        "is_daily_high": False,  # Will be updated on each observation
                        "daily_high_margin": 0.0,  # Will be updated on each observation
                        "observations_since_spike": 0,
                        "day_fraction_at_spike": day_fraction_at_spike,
                        "running_daily_max_at_spike": running_daily_max_at_spike,
                        # Fields for goldilocks_momentum_down variant
                        "is_daily_low": False,  # Will be updated on each observation
                        "daily_low_margin": 0.0,  # Will be updated on each observation
                    }
                    _SIGNAL_GOLDILOCKS_EPOCH_TRACKER[epoch_key] = tracker
                    # Persist epoch tracker
                    _persist_signal_state(
                        f"goldilocks_tracker:{station}:{epoch_id}",
                        tracker,
                    )
                elif isinstance(tracker, dict):
                    # Update running daily high/low tracking for confidence scoring
                    prev_max_temp = float(tracker.get("max_temp_after_up") or 0)
                    curr_max_temp = float(now_f)
                    
                    # Update max_temp_after_up
                    tracker["max_temp_after_up"] = max(prev_max_temp, curr_max_temp)
                    
                    # Get the current running_daily_max from authoritative state for comparison
                    current_running_daily_max = temperature_state.get("running_daily_max")
                    running_daily_max_at_spike = tracker.get("running_daily_max_at_spike")
                    
                    # Check if current spike is the daily high (within 0.1°F tolerance)
                    if current_running_daily_max is not None:
                        # For goldilocks_reversion (up) signal: is this spike the daily high?
                        tracker["is_daily_high"] = curr_max_temp >= current_running_daily_max - 0.1
                        # Compute margin above previous daily high
                        if running_daily_max_at_spike is not None:
                            tracker["daily_high_margin"] = max(0.0, curr_max_temp - running_daily_max_at_spike)
                    
                    # Increment observations_since_spike if exceeded_by_one_or_more is true
                    if bool(tracker.get("exceeded_by_one_or_more")):
                        tracker["observations_since_spike"] = int(tracker.get("observations_since_spike", 0)) + 1
                    
                    # Use stored OLD bucket for "exceeded by one or more" calculation
                    old_bucket = tracker.get("previous_settlement_bucket") or tracker.get("settlement_bucket_at_up")
                    tracker["exceeded_by_one_or_more"] = bool(tracker.get("exceeded_by_one_or_more")) or (
                        tracker["max_temp_after_up"] >= float(old_bucket) + 1.2
                    )
                    tracker["reverted_below_settlement"] = bool(tracker.get("reverted_below_settlement")) or (
                        float(now_f) <= float(old_bucket) - 0.2
                    )
                    
                    # Check for goldilocks_momentum_down variant
                    # For downward momentum, check if we've reached the daily low
                    if current_running_daily_max is not None:
                        # Note: running_daily_max is actually the running max, not low
                        # We need to track daily low separately - but for now we use the same tracker pattern
                        # The momentum_down signal uses the same tracker but looks for reversion patterns
                        tracker["is_daily_low"] = curr_max_temp <= current_running_daily_max + 0.1  # Placeholder for low tracking
                        if running_daily_max_at_spike is not None:
                            tracker["daily_low_margin"] = max(0.0, running_daily_max_at_spike - curr_max_temp)

                if isinstance(tracker, dict):
                    if bool(tracker.get("alert_emitted")):
                        runtime["suppression_reason"] = "EPOCH_ALERT_ALREADY_EMITTED"
                        runtime["outcome_classification"] = OUTCOME_ELIGIBLE_NOT_ALERTABLE
                        _ALERT_LOGGER.debug(
                            "signal_suppression station=%s reason=EPOCH_ALERT_ALREADY_EMITTED "
                            "reason_detail=market_exists_but_epoch_alert_already_sent"
                        )
                        # Near-miss audit: epoch alert already emitted
                        log_near_miss_if_epoch_alert_emitted(
                            station=station,
                            epoch_id=epoch_id,
                            signal_type="goldilocks_reversion_alert",
                        )
                    elif station_cooldown_active:
                        runtime["suppression_reason"] = "STATION_COOLDOWN_ACTIVE"
                        runtime["outcome_classification"] = OUTCOME_ELIGIBLE_NOT_ALERTABLE
                        _ALERT_LOGGER.debug(
                            "signal_suppression station=%s reason=STATION_COOLDOWN_ACTIVE "
                            "reason_detail=market_exists_but_station_in_cooldown"
                        )
                    elif bool(tracker.get("exceeded_by_one_or_more")) and bool(tracker.get("reverted_below_settlement")):
                        # Compute confidence score for goldilocks reversion
                        confidence_score, confidence_factors = _compute_goldilocks_confidence(tracker)
                        
                        pending_signal_context = {
                            "signal_type": "goldilocks_reversion_alert",
                            "signal_version": 1,
                            "station": station,
                            "obs_time": obs_time,
                            "dedupe_key": f"goldilocks_reversion_alert:{station}:{epoch_id}",
                            "cooldown_applied": True,
                            "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,
                            # Use stored previous settlement bucket
                            "settlement_bucket_at_up": int(tracker.get("previous_settlement_bucket") or tracker.get("settlement_bucket_at_up")),
                            "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),
                            "reverted_temp": float(now_f),
                            "epoch_id": int(epoch_id),
                            # NEW: Confidence scoring data
                            "confidence": confidence_score,
                            "confidence_factors": confidence_factors,
                        }
                        tracker["alert_emitted"] = True
                        _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
                        # Persist station cooldown
                        _persist_signal_state(
                            f"station_cooldown:{station}",
                            {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                        )
                        runtime.update({"signal_type": "goldilocks_reversion_alert", "signal_emitted": True, "suppression_reason": None, "outcome_classification": OUTCOME_ALERT_SENT})
                        runtime["cooldown_state"]["station_active"] = True

                # LOW momentum detection (downward temperature trend)
                momentum_down = None
                distance_from_integer = float(now_f) - float(int(math.floor(now_f)))
                monotonic_down = False
                increasing_time = False
                movement_down = False
                if len(window) == _SIGNAL_MOMENTUM_WINDOW_SIZE:
                    x1, x2, x3 = window[0], window[1], window[2]
                    monotonic_down = x1["temp_f"] >= x2["temp_f"] >= x3["temp_f"]
                    # Timestamps increase from oldest (x1) to newest (x3)
                    increasing_time = x1["seconds"] < x2["seconds"] < x3["seconds"]
                    movement_down = (x1["temp_f"] - x3["temp_f"]) >= 0.05
                    total_seconds = x3["seconds"] - x1["seconds"]
                    if increasing_time and total_seconds > 0:
                        momentum_down = abs((x1["temp_f"] - x3["temp_f"]) / total_seconds)

                # Near-miss audit: distance from integer (downward)
                if 0.0 < distance_from_integer <= 0.10:
                    # Check if we're near boundary but conditions not fully met
                    momentum_down_ok = momentum_down is not None and momentum_down >= 0.002
                    if not momentum_down_ok:
                        log_near_miss_if_distance_to_boundary(
                            station=station,
                            distance=distance_from_integer,
                            momentum=momentum_down if momentum_down is not None else None,
                            threshold_distance=0.10,
                            threshold_momentum=0.002,
                        )
                        # Log specific near-miss for downward momentum
                        if momentum_down is not None and momentum_down < 0.002:
                            log_near_miss(
                                station=station,
                                near_miss_type="MOMENTUM_BELOW_THRESHOLD",
                                severity="MEDIUM",
                                details={
                                    "momentum": momentum_down,
                                    "momentum_threshold": 0.002,
                                    "momentum_deficit": 0.002 - momentum_down,
                                    "direction": "down",
                                    "distance_from_integer": distance_from_integer,
                                },
                                suppressed_alert_type="near_boundary_momentum_down",
                            )
                elif distance_from_integer <= 0.10:
                    # Too far from boundary
                    log_near_miss(
                        station=station,
                        near_miss_type="TOO_FAR_FROM_BOUNDARY",
                        severity="LOW",
                        details={
                            "distance_to_boundary": distance_from_integer,
                            "max_allowed_distance": 0.10,
                            "direction": "down",
                        },
                    )

                # LOW momentum signals for downward transitions
                if transition_type in ("instant_down", "reversion_after_settlement"):
                    # Check near_boundary_momentum_down
                    near_boundary_down_all = False
                    if 0.0 < distance_from_integer <= 0.10:
                        near_boundary_down_all = bool(
                            monotonic_down
                            and increasing_time
                            and movement_down
                            and momentum_down is not None
                            and momentum_down >= 0.002
                        )
                    if near_boundary_down_all and not station_cooldown_active and not boundary_cooldown_active:
                        boundary_key = (station, int(math.floor(now_f)), int(epoch_id))
                        pending_signal_context = {
                            "signal_type": "near_boundary_momentum_down",
                            "signal_version": 1,
                            "station": station,
                            "obs_time": obs_time,
                            "dedupe_key": f"near_boundary_momentum_down:{station}:{epoch_id}:{int(math.floor(now_f))}",
                            "cooldown_applied": True,
                            "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS,
                            "distance_from_integer": distance_from_integer,
                            "momentum_f_per_sec": momentum_down,
                            "momentum_window_size": _SIGNAL_MOMENTUM_WINDOW_SIZE,
                            "lower_integer_boundary": int(math.floor(now_f)),
                            "pressure_from_boundary_seconds": distance_from_integer / momentum_down if momentum_down else None,
                        }
                        _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
                        # Persist station cooldown
                        _persist_signal_state(
                            f"station_cooldown:{station}",
                            {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                        )
                        _SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds
                        # Persist boundary cooldown
                        _persist_signal_state(
                            f"boundary_cooldown:{station}:{boundary_key[1]}:{boundary_key[2]}",
                            {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS},
                        )
                        runtime.update({"signal_type": "near_boundary_momentum_down", "signal_emitted": True, "suppression_reason": None, "outcome_classification": OUTCOME_ALERT_SENT})
                        runtime["cooldown_state"]["station_active"] = True
                        runtime["cooldown_state"]["boundary_active"] = True

                    # Check goldilocks_momentum_down
                    if pending_signal_context is None:
                        epoch_key = (station, int(epoch_id))
                        tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)
                        if isinstance(tracker, dict):
                            current_goldilocks = tracker.get("momentum_down_observed", False)
                            if not current_goldilocks and tracker.get("exceeded_by_one_or_more"):
                                # Check if temperature has dropped below the settlement bucket threshold
                                if float(now_f) <= float(tracker.get("settlement_bucket_at_up") or settlement_bucket) - 0.2:
                                    # Compute confidence score for goldilocks momentum down
                                    # For momentum_down, is_daily_low is actually tracking whether we hit the daily high (inverted)
                                    # The logic is inverted: we want to know if the reversion point was at the daily high
                                    confidence_score, confidence_factors = _compute_goldilocks_confidence(tracker, is_down=True)
                                    
                                    # Near-miss audit: goldilocks confidence below threshold
                                    if confidence_score < 0.30:  # Threshold for goldilocks
                                        log_near_miss(
                                            station=station,
                                            near_miss_type="LOW_CONFIDENCE_GOLDILOCKS",
                                            severity="MEDIUM",
                                            details={
                                                "confidence": confidence_score,
                                                "confidence_threshold": 0.30,
                                                "confidence_deficit": 0.30 - confidence_score,
                                                "reversion_direction": "down",
                                            },
                                            suppressed_alert_type="goldilocks_momentum_down",
                                        )
                                    
                                    pending_signal_context = {
                                        "signal_type": "goldilocks_momentum_down",
                                        "signal_version": 1,
                                        "station": station,
                                        "obs_time": obs_time,
                                        "dedupe_key": f"goldilocks_momentum_down:{station}:{epoch_id}",
                                        "cooldown_applied": True,
                                        "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,
                                        "settlement_bucket_at_up": int(tracker.get("settlement_bucket_at_up") or settlement_bucket),
                                        "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),
                                        "reverted_temp": float(now_f),
                                        "momentum_down": momentum_down,
                                        "epoch_id": int(epoch_id),
                                        # NEW: Confidence scoring data
                                        "confidence": confidence_score,
                                        "confidence_factors": confidence_factors,
                                    }
                                    tracker["momentum_down_observed"] = True
                                    _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
                                    # Persist station cooldown
                                    _persist_signal_state(
                                        f"station_cooldown:{station}",
                                        {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                                    )
                                    runtime.update({"signal_type": "goldilocks_momentum_down", "signal_emitted": True, "suppression_reason": None, "outcome_classification": OUTCOME_ALERT_SENT})
                                    runtime["cooldown_state"]["station_active"] = True

                if runtime["signal_type"] is None and runtime["suppression_reason"] is None:
                    runtime["suppression_reason"] = "NO_SIGNAL_CONDITION_MATCH"
                    runtime["outcome_classification"] = OUTCOME_NO_SIGNAL_CONDITION_MATCH
                    _ALERT_LOGGER.debug(
                        "signal_suppression station=%s reason=NO_SIGNAL_CONDITION_MATCH "
                        "reason_detail=market_exists_but_no_signal_condition_met"
                    )
                    # Near-miss audit: no signal condition matched
                    log_near_miss(
                        station=station,
                        near_miss_type="NO_SIGNAL_CONDITION_MATCH",
                        severity="LOW",
                        details={
                            "observation_window_size": len(window),
                            "required_window_size": _SIGNAL_MOMENTUM_WINDOW_SIZE,
                            "momentum_down_available": momentum_down is not None,
                            "distance_from_integer": distance_from_integer,
                        },
                    )
                pending_runtime_record = runtime

    if pending_runtime_record is not None:
        _record_signal_runtime(station, pending_runtime_record)

    if early_exit:
        return

    if pending_signal_context is not None:
        _emit_signal_alert(station=station, obs_time=obs_time, temp_f=now_f, signal_context=pending_signal_context, cfg=cfg)
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
        previous_settlement_bucket=previous_settlement_bucket,
        hydration_cache_valid=hydration_cache_valid,
        eligible_markets_count=eligible_markets_count,
        cfg=cfg,
        temperature_state={
            "running_daily_max": new_running_max,
        },
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
    
    # Layer 1: Queue alert for delivery with retry logic
    # Generate unique alert ID for tracking
    alert_id = f"temp_change:{icao}:{obs_time.replace('Z', '').replace(':', '').replace('-', '')}"
    
    return _queue_alert_for_delivery(alert_id, cfg.get("webhook", ""), payload)
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
    # Layer 3: Execution domain guard - block alert delivery outside production
    try:
        from core.kalshi_monitor import _current_kalshi_execution_domain
        current_domain = _current_kalshi_execution_domain()
        allowed_domains = {"prod", "sbox", "dev"}
        if current_domain not in allowed_domains:
            result = {
                "delivery_attempted": True,
                "delivery_succeeded": False,
                "webhook_status_code": None,
                "webhook_exception": f"Execution domain blocked: domain={current_domain}",
                "webhook_response_text": None,
                "delivery_blocking_stage": "execution_domain",
                "delivery_blocking_reason": f"DOMAIN_BLOCKED_{current_domain.upper()}",
            }
            return result
    except Exception:
        # If import fails or domain check fails, allow delivery (fail-open for safety)
        pass
    
    result = {
        "delivery_attempted": False,
        "delivery_succeeded": False,
        "webhook_status_code": None,
        "webhook_exception": None,
        "webhook_response_text": None,
        "delivery_blocking_stage": None,
        "delivery_blocking_reason": None,
    }

    station = (payload.get("station") or "UNK").upper()
    legacy = payload.get("legacy") if isinstance(payload.get("legacy"), dict) else payload
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    transition_correlation = legacy.get("transition_correlation")
    alert_path_truth = {
        "send_alert_entered": True,
        "blocking_stage": None,
        "suppression_or_non_emission_reason": None,
        "webhook_send_attempted": False,
        "webhook_send_succeeded": False,
        "webhook_send_failed": False,
    }
    webhook_send_attempted = False
    webhook_send_succeeded = False
    webhook_send_failed = False
    webhook_failure_reason = None

    def _persist_alert_path_truth(**updates: Any) -> None:
        nonlocal alert_path_truth
        if updates:
            alert_path_truth = {
                **alert_path_truth,
                **updates,
            }
        _annotate_transition_history_alert_path_truth(
            station=station,
            transition_correlation=transition_correlation,
            alert_path_truth=alert_path_truth,
        )

    def _record_webhook_outcome(
        attempted: bool,
        succeeded: bool,
        *,
        status_code: Optional[Any] = None,
        exception: Optional[Any] = None,
    ) -> None:
        nonlocal webhook_send_attempted, webhook_send_succeeded, webhook_send_failed, webhook_failure_reason
        webhook_send_attempted = webhook_send_attempted or bool(attempted)
        if not attempted:
            return
        if succeeded:
            webhook_send_succeeded = True
            return
        webhook_send_failed = True
        if exception:
            webhook_failure_reason = str(exception)
        elif status_code is not None:
            webhook_failure_reason = f"HTTP_{status_code}"
        elif webhook_failure_reason is None:
            webhook_failure_reason = "WEBHOOK_SEND_FAILED"

    _persist_alert_path_truth()

    if not webhook:
        result["delivery_blocking_stage"] = "config"
        result["delivery_blocking_reason"] = "WEBHOOK_MISSING"
        _persist_alert_path_truth(
            blocking_stage="config",
            suppression_or_non_emission_reason="WEBHOOK_MISSING",
        )
        return result
    try:
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
            _persist_alert_path_truth(
                blocking_stage="payload",
                suppression_or_non_emission_reason="MISSING_TEMP_F",
            )
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
                evaluated_market_types=[],
            )
            result["delivery_blocking_stage"] = "transition_gate"
            result["delivery_blocking_reason"] = "NO_TRANSITION"
            _persist_alert_path_truth(
                blocking_stage="transition_gate",
                suppression_or_non_emission_reason="NO_TRANSITION",
            )
            return result

        rate_limit_reference_dt = _parse_iso_utc_optional(reference_timestamp_utc)
        if rate_limit_reference_dt is None and isinstance(transition_correlation, dict):
            rate_limit_reference_dt = _parse_iso_utc_optional(transition_correlation.get("timestamp_utc"))
        if rate_limit_reference_dt is None:
            result["delivery_blocking_stage"] = "rate_limit_gate"
            result["delivery_blocking_reason"] = "MISSING_REFERENCE_TIMESTAMP"
            _persist_alert_path_truth(
                blocking_stage="rate_limit_gate",
                suppression_or_non_emission_reason="MISSING_REFERENCE_TIMESTAMP",
            )
            return result

        now_ts = rate_limit_reference_dt.timestamp()
        with _KALSHI_RATE_LIMIT_LOCK:
            last_call_ts = _KALSHI_LAST_CALL_TS.get(station)
            # Throttle causal implication: suppress duplicate near-term Kalshi checks
            # while leaving previously recorded transition history intact.
            if last_call_ts is not None and (now_ts - last_call_ts) < _KALSHI_CALL_THROTTLE_SECONDS:
                result["delivery_blocking_stage"] = "rate_limit_gate"
                result["delivery_blocking_reason"] = "KALSHI_CALL_THROTTLE"
                _persist_alert_path_truth(
                    blocking_stage="rate_limit_gate",
                    suppression_or_non_emission_reason="KALSHI_CALL_THROTTLE",
                )
                return result

        try:
            from core.kalshi_monitor import (
                _LAST_PROXIMITY_REGIME,
                _PROXIMITY_LOCK,
                _PROXIMITY_RANK,
                _configured_target_market_types,
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
            target_market_types = _configured_target_market_types()
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
            evaluated_market_types = []  # Track market types that had eligible markets
            rejection_breakdown = {
                "directional_strike_rejections": 0,
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
                    snapshot = build_structured_snapshot_from_cache(
                        station,
                        {market_type_token},
                        observation_time_utc=reference_timestamp_utc,
                    )
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
                    empty_reason = str(snapshot.get("empty_reason") or "")
                    rejection_counts = snapshot.get("rejection_counts") or {}

                    markets_considered_count += raw_market_count
                    eligible_markets_count += len(markets)

                    wrong_series_rejections = int(rejection_counts.get("city_token_mismatch") or 0) + int(rejection_counts.get("market_type_mismatch") or 0)
                    settlement_mismatch_rejections = int(rejection_counts.get("date_mismatch") or 0)
                    expired_rejections = int(rejection_counts.get("inactive_market") or 0)
                    directional_strike_rejected = max(filtered_market_count - len(markets), 0)
                    known_rejections = wrong_series_rejections + settlement_mismatch_rejections + expired_rejections + directional_strike_rejected
                    unknown_rejections = max(raw_market_count - len(markets) - known_rejections, 0)

                    rejection_breakdown["wrong_series"] += wrong_series_rejections
                    rejection_breakdown["settlement_mismatch"] += settlement_mismatch_rejections
                    rejection_breakdown["expired_market"] += expired_rejections
                    rejection_breakdown["directional_strike_rejections"] += directional_strike_rejected
                    rejection_breakdown["unknown_reason"] += unknown_rejections

                    _ALERT_LOGGER.info(
                        "EVAL ladder_check station=%s type=%s market_type=%s markets_found=%s",
                        station,
                        market_type_token,
                        market_type_token,
                        len(markets),
                    )

                    # Track which market types were evaluated (for settlement epoch annotation)
                    evaluated_market_types.append(market_type_token)

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
                        correlated_transition = _find_correlated_transition_entry(
                            station=station,
                            transition_correlation=transition_correlation if isinstance(transition_correlation, dict) else None,
                        )
                        authoritative_reversion_after_settlement = (
                            isinstance(correlated_transition, dict)
                            and correlated_transition.get("transition_type") == "reversion_after_settlement"
                        )
                        warmup_suppressed = (
                            bool(hydration_state.get("attempted"))
                            and bool(hydration_state.get("series_discovered"))
                            and not bool(hydration_state.get("markets_cached"))
                            and is_scheduler_running()
                            and (station_in_queue or in_backoff_flow or attempted_recently)
                            and not authoritative_reversion_after_settlement
                        )
                        if warmup_suppressed:
                            suppression_reason = "LADDER_HYDRATION_WARMUP"
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
                                _MISSING_LADDER_DEDUPE[dedupe_key] = True
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
                            _record_webhook_outcome(
                                True,
                                bool(result.get("delivery_succeeded")),
                                status_code=webhook_status_code,
                                exception=webhook_exception,
                            )
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
                            _record_webhook_outcome(
                                bool(result.get("delivery_attempted")),
                                bool(result.get("delivery_succeeded")),
                                status_code=result.get("webhook_status_code"),
                                exception=result.get("webhook_exception"),
                            )
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
                            # Track market type that had eligible markets
                            evaluated_market_types.append(market_type_token)
                    else:
                        if transition.get("terminal_state_blocked"):
                            saw_terminal_state = True
                        raw_reason = (transition.get("reason") or "").strip().upper()
                        if raw_reason:
                            suppression_reason = raw_reason
                        else:
                            outcome_hint = (transition.get("outcome_hint") or "").strip().upper()
                            if outcome_hint:
                                suppression_reason = outcome_hint

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
                    if suppression_reason == "LADDER_HYDRATION_WARMUP":
                        evaluation_outcome = "SUPPRESSED_LADDER_HYDRATION_WARMUP"
                    else:
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
                    evaluated_market_types=evaluated_market_types,
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

                alert_path_updates = {
                    "webhook_send_attempted": webhook_send_attempted,
                    "webhook_send_succeeded": webhook_send_succeeded,
                    "webhook_send_failed": webhook_send_failed,
                }
                if market_alerts_sent > 0:
                    alert_path_updates.update(
                        {
                            "blocking_stage": None,
                            "suppression_or_non_emission_reason": None,
                        }
                    )
                elif webhook_send_attempted and not webhook_send_succeeded:
                    alert_path_updates.update(
                        {
                            "blocking_stage": "webhook_delivery",
                            "suppression_or_non_emission_reason": webhook_failure_reason or "WEBHOOK_SEND_FAILED",
                        }
                    )
                elif evaluation_outcome == "NO_ELIGIBLE_MARKET":
                    alert_path_updates.update(
                        {
                            "blocking_stage": "market_match",
                            "suppression_or_non_emission_reason": "NO_ELIGIBLE_MARKET",
                        }
                    )
                elif evaluation_outcome == "TERMINAL_STATE":
                    alert_path_updates.update(
                        {
                            "blocking_stage": "suppression_gate",
                            "suppression_or_non_emission_reason": "TERMINAL_STATE",
                        }
                    )
                elif safe_outcome := (evaluation_outcome or "").strip().upper():
                    if safe_outcome.startswith("SUPPRESSED_"):
                        alert_path_updates.update(
                            {
                                "blocking_stage": "suppression_gate",
                                "suppression_or_non_emission_reason": suppression_reason or safe_outcome,
                            }
                        )
                    else:
                        alert_path_updates.update(
                            {
                                "blocking_stage": "alert_emission",
                                "suppression_or_non_emission_reason": safe_outcome,
                            }
                        )

                _persist_alert_path_truth(**alert_path_updates)

            with _KALSHI_RATE_LIMIT_LOCK:
                _KALSHI_LAST_CALL_TS[station] = now_ts
        except Exception as e:
            result = {
                "delivery_succeeded": False,
                "webhook_status_code": None,
                "webhook_exception": str(e),
                "webhook_response_text": None,
            }
            _persist_alert_path_truth(
                webhook_send_attempted=webhook_send_attempted,
                webhook_send_succeeded=webhook_send_succeeded,
                webhook_send_failed=webhook_send_failed,
                blocking_stage="send_alert_exception",
                suppression_or_non_emission_reason=str(e),
            )
            print(f"[ERROR] station={station} function=_send_alert: {e}")
    except Exception as e:
        result = {
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": str(e),
            "webhook_response_text": None,
        }
        _persist_alert_path_truth(
            webhook_send_attempted=webhook_send_attempted,
            webhook_send_succeeded=webhook_send_succeeded,
            webhook_send_failed=webhook_send_failed,
            blocking_stage="send_alert_exception",
            suppression_or_non_emission_reason=str(e),
        )
        print(f"[ERROR] station={payload.get('station') or 'UNK'} function=_send_alert: {e}")
    return result
