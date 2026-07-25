"""
Health Monitor Module — P0 Operational Reliability

Extracted from metar_monitor.py during Phase 20.1 monolith decomposition.

Provides:
  - data_freshness_monitor: per-station freshness check with WARN/DEGRADED/HALT thresholds
  - station_polling_status: per-station last-poll verification
  - system_health_snapshot: aggregate health aggregation
"""

import os
import math
import logging
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

# ─── Module-level state ───
_AUDIT_LOCK = threading.Lock()
_HEALTH_LOGGER = logging.getLogger("health_monitor")

# ─── Freshness Thresholds (from Gray Room Round 7 Expert 4, Section 2.2) ───
FRESHNESS_THRESHOLDS = {
    "metar": {
        "warn_minutes": 15,
        "degrade_minutes": 30,
        "halt_minutes": 120,
    },
    "kalshi_prices": {
        "warn_minutes": 15,
        "degrade_minutes": 60,
        "halt_minutes": 240,
    },
    "kalshi_markets": {
        "warn_minutes": 60,
        "degrade_minutes": 180,
        "halt_minutes": 360,
    },
    "nwp_forecast": {
        "warn_minutes": 360,
        "degrade_minutes": 720,
        "halt_minutes": 1440,
    },
}

# Default: METAR polling cadence
DEFAULT_POLL_INTERVAL_SECONDS = 180  # 3 minutes

# Operation states
STATE_NORMAL = "normal"
STATE_WARN = "warn"
STATE_DEGRADED = "degraded"
STATE_HALTED = "halted"


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp string to datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_utc_iso() -> str:
    return _now_utc().isoformat()


def _safe_lag_seconds(now_utc: datetime, iso_timestamp: str) -> Optional[int]:
    """Compute seconds since the given ISO timestamp, returning None on failure."""
    if not iso_timestamp:
        return None
    try:
        return max(0, int((now_utc - datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))).total_seconds()))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# P0a — Data Freshness Monitor
# ─────────────────────────────────────────────────────────────────────────────

def check_station_freshness(
    station: str,
    last_seen_iso: Optional[str] = None,
    now_utc: Optional[datetime] = None,
    thresholds: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Check a single station's data freshness against defined thresholds.

    Args:
        station: ICAO station code (e.g., "KNYC")
        last_seen_iso: ISO 8601 timestamp of last accepted observation
        now_utc: Current UTC time (defaults to datetime.now(timezone.utc))
        thresholds: Threshold dict with warn_minutes, degrade_minutes, halt_minutes

    Returns:
        Dict with station, age_seconds, age_minutes, state, threshold_triggered
    """
    if thresholds is None:
        thresholds = FRESHNESS_THRESHOLDS["metar"]

    if now_utc is None:
        now_utc = _now_utc()

    result = {
        "station": (station or "").strip().upper(),
        "last_seen_iso": last_seen_iso,
        "age_seconds": None,
        "age_minutes": None,
        "state": STATE_HALTED,  # default to halted if no observation
        "threshold_triggered": "no_observation",
        "check_timestamp_utc": _now_utc_iso(),
    }

    if not last_seen_iso:
        return result

    lag_seconds = _safe_lag_seconds(now_utc, last_seen_iso)
    if lag_seconds is None:
        result["state"] = STATE_HALTED
        result["threshold_triggered"] = "invalid_timestamp"
        return result

    lag_minutes = lag_seconds / 60.0
    result["age_seconds"] = lag_seconds
    result["age_minutes"] = round(lag_minutes, 1)

    warn_sec = thresholds["warn_minutes"] * 60
    degrade_sec = thresholds["degrade_minutes"] * 60
    halt_sec = thresholds["halt_minutes"] * 60

    if lag_seconds >= halt_sec:
        result["state"] = STATE_HALTED
        result["threshold_triggered"] = "halt"
    elif lag_seconds >= degrade_sec:
        result["state"] = STATE_DEGRADED
        result["threshold_triggered"] = "degrade"
    elif lag_seconds >= warn_sec:
        result["state"] = STATE_WARN
        result["threshold_triggered"] = "warn"
    else:
        result["state"] = STATE_NORMAL
        result["threshold_triggered"] = None

    return result


def check_all_stations_freshness(
    state: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
    thresholds: Optional[Dict[str, int]] = None,
    station_filter: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Check freshness for all stations in the system state.

    Args:
        state: System state dict (from get_state()). If None, imports at runtime.
        now_utc: Current UTC time
        thresholds: Freshness thresholds
        station_filter: Optional list of stations to check (default: all in state)

    Returns:
        Dict with overall state, per-station results, summary counts
    """
    if state is None:
        try:
            from core.data_processor import get_state
            state = get_state()
        except Exception as e:
            _HEALTH_LOGGER.warning("check_all_stations_freshness: get_state failed: %s", e)
            state = {}

    if now_utc is None:
        now_utc = _now_utc()

    last_seen = state.get("last_seen_iso", {})
    ingestion_runtime = state.get("ingestion_runtime", {})

    if station_filter:
        stations_to_check = [s for s in station_filter if s in last_seen or s in ingestion_runtime]
    else:
        # Union of all known stations
        stations_to_check = sorted(set(list(last_seen.keys()) + list(ingestion_runtime.keys())))

    per_station = []
    summary_counts = {STATE_NORMAL: 0, STATE_WARN: 0, STATE_DEGRADED: 0, STATE_HALTED: 0}

    for station in stations_to_check:
        freshness = check_station_freshness(
            station=station,
            last_seen_iso=last_seen.get(station),
            now_utc=now_utc,
            thresholds=thresholds,
        )
        # Add ingestion runtime info if available
        runtime = ingestion_runtime.get(station, {})
        freshness["last_poll_attempt_utc"] = runtime.get("last_poll_attempt_utc")
        freshness["last_fetch_status"] = runtime.get("last_fetch_status", "unknown")
        freshness["fetched_observation_count"] = int(runtime.get("fetched_observation_count", 0))

        per_station.append(freshness)
        state_key = freshness["state"]
        if state_key in summary_counts:
            summary_counts[state_key] += 1

    # Determine overall system state
    if summary_counts[STATE_HALTED] > 0:
        overall_state = STATE_HALTED
    elif summary_counts[STATE_DEGRADED] > 0:
        overall_state = STATE_DEGRADED
    elif summary_counts[STATE_WARN] > 0:
        overall_state = STATE_WARN
    else:
        overall_state = STATE_NORMAL

    return {
        "overall_state": overall_state,
        "check_timestamp_utc": _now_utc_iso(),
        "thresholds_used": thresholds or FRESHNESS_THRESHOLDS["metar"],
        "stations_checked": len(stations_to_check),
        "summary_counts": summary_counts,
        "stations": per_station,
        "warned_stations": [s["station"] for s in per_station if s["state"] == STATE_WARN],
        "degraded_stations": [s["station"] for s in per_station if s["state"] == STATE_DEGRADED],
        "halted_stations": [s["station"] for s in per_station if s["state"] == STATE_HALTED],
    }


def get_freshness_log_lines(
    freshness_result: Dict[str, Any],
    include_healthy: bool = False,
) -> List[str]:
    """Format a freshness check result into human-readable log lines."""
    lines = []
    lines.append(f"[HEALTH] Freshness check: overall_state={freshness_result['overall_state']}")
    lines.append(f"[HEALTH]   Stations checked: {freshness_result['stations_checked']}")
    lines.append(f"[HEALTH]   Summary: normal={freshness_result['summary_counts'].get('normal',0)} "
                 f"warn={freshness_result['summary_counts'].get('warn',0)} "
                 f"degraded={freshness_result['summary_counts'].get('degraded',0)} "
                 f"halted={freshness_result['summary_counts'].get('halted',0)}")

    for s in freshness_result.get("stations", []):
        if s["state"] == STATE_NORMAL and not include_healthy:
            continue
        age = s.get("age_minutes", "N/A")
        lines.append(
            f"[HEALTH]   {s['station']}: state={s['state']} "
            f"age={age}min triggered={s['threshold_triggered']} "
            f"last_seen={s.get('last_seen_iso','never')}"
        )

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# P0e — Scheduler Execution Per Station
# ─────────────────────────────────────────────────────────────────────────────

def get_station_polling_status(
    station: str,
    state: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
    expected_poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """
    Check if a specific station has been polled within its expected interval.

    Args:
        station: ICAO station code
        state: System state dict
        now_utc: Current UTC time
        expected_poll_interval_seconds: Expected polling interval (default 180s)

    Returns:
        Dict with poll status, last_poll_attempt, lag_seconds, within_interval
    """
    if state is None:
        try:
            from core.data_processor import get_state
            state = get_state()
        except Exception:
            state = {}

    if now_utc is None:
        now_utc = _now_utc()

    normalized_station = (station or "").strip().upper()
    ingestion_runtime = state.get("ingestion_runtime", {}).get(normalized_station, {})
    last_poll_attempt_utc = ingestion_runtime.get("last_poll_attempt_utc")

    result = {
        "station": normalized_station,
        "last_poll_attempt_utc": last_poll_attempt_utc,
        "expected_poll_interval_seconds": expected_poll_interval_seconds,
        "poll_lag_seconds": None,
        "within_interval": False,
        "state": "unknown",
        "check_timestamp_utc": _now_utc_iso(),
    }

    if not last_poll_attempt_utc:
        result["state"] = "never_polled"
        return result

    poll_lag = _safe_lag_seconds(now_utc, last_poll_attempt_utc)
    if poll_lag is None:
        result["state"] = "invalid_poll_timestamp"
        return result

    result["poll_lag_seconds"] = poll_lag
    result["within_interval"] = poll_lag <= expected_poll_interval_seconds

    # 3× interval = stale, 10× = critical
    if poll_lag > expected_poll_interval_seconds * 10:
        result["state"] = "critical"
    elif poll_lag > expected_poll_interval_seconds * 3:
        result["state"] = "stale"
    elif poll_lag > expected_poll_interval_seconds:
        result["state"] = "delayed"
    else:
        result["state"] = "healthy"

    # Also check freshness from last_seen_iso
    last_seen_iso = state.get("last_seen_iso", {}).get(normalized_station)
    if last_seen_iso:
        obs_lag = _safe_lag_seconds(now_utc, last_seen_iso)
        if obs_lag is not None:
            result["observation_lag_seconds"] = obs_lag

    return result


def get_all_stations_polling_status(
    state: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
    expected_poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    station_filter: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Check polling status for all active stations.

    Returns:
        Dict with per-station polling status and aggregate summary
    """
    if state is None:
        try:
            from core.data_processor import get_state
            state = get_state()
        except Exception:
            state = {}

    if now_utc is None:
        now_utc = _now_utc()

    ingestion_runtime = state.get("ingestion_runtime", {})
    if station_filter:
        stations_to_check = [s for s in station_filter if s in ingestion_runtime]
    else:
        stations_to_check = sorted(ingestion_runtime.keys())

    per_station = []
    healthy_count = 0
    stale_count = 0
    critical_count = 0
    never_polled_count = 0

    for station in stations_to_check:
        status = get_station_polling_status(
            station=station,
            state=state,
            now_utc=now_utc,
            expected_poll_interval_seconds=expected_poll_interval_seconds,
        )
        per_station.append(status)
        state_key = status["state"]
        if state_key == "healthy":
            healthy_count += 1
        elif state_key in ("stale", "delayed"):
            stale_count += 1
        elif state_key == "critical":
            critical_count += 1
        elif state_key == "never_polled":
            never_polled_count += 1

    return {
        "check_timestamp_utc": _now_utc_iso(),
        "expected_poll_interval_seconds": expected_poll_interval_seconds,
        "stations_checked": len(stations_to_check),
        "summary": {
            "healthy": healthy_count,
            "stale_or_delayed": stale_count,
            "critical": critical_count,
            "never_polled": never_polled_count,
        },
        "stations": per_station,
    }


# ─────────────────────────────────────────────────────────────────────────────
# System Health Snapshot (aggregate)
# ─────────────────────────────────────────────────────────────────────────────

def compute_system_health_snapshot(
    ingestion_snapshot: Optional[Dict[str, Any]] = None,
    hydration_snapshot: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute aggregate system health from freshness and hydration data.

    Args:
        ingestion_snapshot: Result from check_all_stations_freshness()
        hydration_snapshot: Hydration snapshot (from _build_runtime_authority_hydration_snapshot)
        state: System state

    Returns:
        Dict with overall system health assessment
    """
    if ingestion_snapshot is None:
        ingestion_snapshot = check_all_stations_freshness(state=state)

    if hydration_snapshot is None:
        hydration_snapshot = {}

    system_state = ingestion_snapshot.get("overall_state", STATE_HALTED)
    healthy_count = ingestion_snapshot.get("summary_counts", {}).get(STATE_NORMAL, 0)
    total_count = ingestion_snapshot.get("stations_checked", 0)
    healthy_pct = (healthy_count / max(total_count, 1)) * 100.0

    hydration_ready = 0
    hydration_total = 0
    if isinstance(hydration_snapshot, dict):
        hydration_stations = hydration_snapshot.get("stations", {})
        if isinstance(hydration_stations, dict):
            hydration_total = len(hydration_stations)
            hydration_ready = sum(
                1 for v in hydration_stations.values()
                if isinstance(v, dict) and v.get("hydration_prerequisite", {}).get("cache_valid")
            )

    # Scheduler status
    scheduler_running = False
    try:
        from core.metar_monitor import is_scheduler_running
        scheduler_running = is_scheduler_running()
    except Exception:
        pass

    return {
        "system_state": system_state,
        "healthy_station_pct": round(healthy_pct, 1),
        "healthy_stations": healthy_count,
        "total_stations": total_count,
        "warn_stations": ingestion_snapshot.get("summary_counts", {}).get(STATE_WARN, 0),
        "degraded_stations": ingestion_snapshot.get("summary_counts", {}).get(STATE_DEGRADED, 0),
        "halted_stations": ingestion_snapshot.get("summary_counts", {}).get(STATE_HALTED, 0),
        "scheduler_running": scheduler_running,
        "hydration_cache_ready": hydration_ready,
        "hydration_cache_total": hydration_total,
        "check_timestamp_utc": _now_utc_iso(),
    }