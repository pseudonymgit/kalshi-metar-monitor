import copy
import threading
from types import MappingProxyType
from typing import Any, Dict, Optional

from core.security_boundaries import (
    enforce_authoritative_state_mutation_boundary,
    verify_observability_read_only,
)

_STATE_LOCK = threading.Lock()

# Authoritative runtime state owner for METAR monitor domains.
_STATE: Dict[str, Any] = {
    "stations": [],
    "last_obs": {},
    "last_seen_iso": {},
    "last_reset_date_local": {},
    "last_observed_integer": {},
    "running_daily_max": {},
    "last_settlement_bucket": {},
    "last_instant_bucket": {},
    "cfg": {},
    "poll_count": 0,
    "last_poll_utc": None,
    "last_loop_utc": None,
    "timeout_count": 0,
    "last_timeout_station": None,
    "last_timeout_utc": None,
}


def state_lock() -> threading.Lock:
    return _STATE_LOCK


def state_ref() -> Dict[str, Any]:
    return _STATE


def read_temperature_state(icao: str) -> Dict[str, Optional[float]]:
    with _STATE_LOCK:
        return {
            "last_observed_integer": _STATE["last_observed_integer"].get(icao),
            "running_daily_max": _STATE["running_daily_max"].get(icao),
            "last_settlement_bucket": _STATE["last_settlement_bucket"].get(icao),
            "last_instant_bucket": _STATE["last_instant_bucket"].get(icao),
        }


def set_latest_observation(icao: str, obs: Dict[str, Any], obs_time: str) -> None:
    enforce_authoritative_state_mutation_boundary("set_latest_observation")
    with _STATE_LOCK:
        _STATE["last_obs"][icao] = obs
        _STATE["last_seen_iso"][icao] = obs_time


def commit_temperature_state(
    icao: str,
    curr_floor: int,
    running_daily_max: float,
    settlement_bucket: int,
    instant_bucket: int,
) -> None:
    enforce_authoritative_state_mutation_boundary("commit_temperature_state")
    with _STATE_LOCK:
        _STATE["last_observed_integer"][icao] = curr_floor
        _STATE["running_daily_max"][icao] = running_daily_max
        _STATE["last_settlement_bucket"][icao] = settlement_bucket
        _STATE["last_instant_bucket"][icao] = instant_bucket


def reset_station_daily_state(icao: str, local_day: str) -> None:
    enforce_authoritative_state_mutation_boundary("reset_station_daily_state")
    with _STATE_LOCK:
        _STATE["last_observed_integer"].pop(icao, None)
        _STATE["running_daily_max"].pop(icao, None)
        _STATE["last_settlement_bucket"].pop(icao, None)
        _STATE["last_instant_bucket"].pop(icao, None)
        _STATE["last_reset_date_local"][icao] = local_day


def clear_latest_observation(icao: str) -> None:
    enforce_authoritative_state_mutation_boundary("clear_latest_observation")
    with _STATE_LOCK:
        _STATE["last_seen_iso"].pop(icao, None)
        _STATE["last_obs"].pop(icao, None)


def immutable_public_state_snapshot() -> Dict[str, Any]:
    with _STATE_LOCK:
        snapshot = {
            "stations": tuple(_STATE["stations"]),
            "last_obs": MappingProxyType(copy.deepcopy(_STATE["last_obs"])),
            "last_seen_iso": MappingProxyType(copy.deepcopy(_STATE["last_seen_iso"])),
            "last_reset_date_local": MappingProxyType(copy.deepcopy(_STATE["last_reset_date_local"])),
            "last_observed_integer": MappingProxyType(copy.deepcopy(_STATE["last_observed_integer"])),
            "running_daily_max": MappingProxyType(copy.deepcopy(_STATE["running_daily_max"])),
            "last_settlement_bucket": MappingProxyType(copy.deepcopy(_STATE["last_settlement_bucket"])),
            "last_instant_bucket": MappingProxyType(copy.deepcopy(_STATE["last_instant_bucket"])),
            "cfg": MappingProxyType(copy.deepcopy(_STATE["cfg"])),
            "poll_count": _STATE["poll_count"],
            "last_poll_utc": _STATE["last_poll_utc"],
            "last_loop_utc": _STATE["last_loop_utc"],
        }
    verify_observability_read_only(snapshot)
    return snapshot
