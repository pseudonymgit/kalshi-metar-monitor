# CHANGELOG (last 10 broad changes):
# 1. [2026-03-04 Harden replay parity capture without monkeypatching]
#


from datetime import datetime
from typing import Any, Dict, List, Optional

from core.kalshi_monitor import kalshi_execution_domain
from core.metar_monitor import get_transition_history, run_replay_for_station_day
from core.transition_emitter import transition_capture_hook


def _canonical_transition(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "station": (entry.get("station") or "").strip().upper(),
        "transition_type": entry.get("transition_type"),
        "instant_bucket_before": entry.get("instant_bucket_before"),
        "instant_bucket_after": entry.get("instant_bucket_after"),
        "settlement_bucket": entry.get("settlement_bucket"),
        "running_max": entry.get("running_max"),
        "current_temp": entry.get("current_temp"),
        "timestamp_utc": entry.get("timestamp_utc"),
        "metadata": entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {},
    }


def _timestamps_non_decreasing(transitions: List[Dict[str, Any]]) -> bool:
    last_ts: Optional[datetime] = None
    for transition in transitions:
        raw_ts = transition.get("timestamp_utc")
        if not raw_ts:
            continue
        try:
            current_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except Exception:
            continue
        if last_ts is not None and current_ts < last_ts:
            return False
        last_ts = current_ts
    return True


def validate_replay_parity(station: str, trading_day: str) -> Dict[str, Any]:
    normalized_station = (station or "").strip().upper()
    normalized_day = (trading_day or "").strip()
    datetime.strptime(normalized_day, "%Y-%m-%d")

    runtime_transitions = [
        _canonical_transition(item)
        for item in get_transition_history(station=normalized_station, day=normalized_day, limit=200)
    ]
    runtime_transitions = list(reversed(runtime_transitions))

    captured_replay_transitions: List[Dict[str, Any]] = []

    def _capture_transition_event(
        station: str,
        transition_type: Optional[str],
        instant_bucket_before: Optional[int],
        instant_bucket_after: int,
        settlement_bucket: int,
        running_max: float,
        current_temp: float,
        metadata: Optional[Dict[str, Any]] = None,
        transition_correlation: Optional[Dict[str, Any]] = None,
    ) -> None:
        transition_event_id = None
        event_timestamp_utc = (metadata or {}).get("obs_time")
        if isinstance(transition_correlation, dict):
            raw_id = transition_correlation.get("transition_event_id")
            if isinstance(raw_id, int):
                transition_event_id = raw_id
            raw_timestamp = transition_correlation.get("timestamp_utc")
            if raw_timestamp is not None:
                event_timestamp_utc = str(raw_timestamp)
        captured_replay_transitions.append(
            {
                "station": (station or "").strip().upper(),
                "transition_type": transition_type,
                "instant_bucket_before": instant_bucket_before,
                "instant_bucket_after": instant_bucket_after,
                "settlement_bucket": settlement_bucket,
                "running_max": running_max,
                "current_temp": current_temp,
                "timestamp_utc": event_timestamp_utc,
                "metadata": dict(metadata or {}),
                "transition_event_id": transition_event_id,
            }
        )

    with kalshi_execution_domain("replay"):
        with transition_capture_hook(_capture_transition_event):
            run_replay_for_station_day(station=normalized_station, date_local=normalized_day)

    replay_transitions = [_canonical_transition(item) for item in captured_replay_transitions]

    divergence = None
    parity = True

    if not _timestamps_non_decreasing(runtime_transitions) or not _timestamps_non_decreasing(replay_transitions):
        parity = False
        divergence = {
            "type": "TIMESTAMP_ORDER_MISMATCH",
            "first_divergence_index": 0,
            "runtime_transition": runtime_transitions[0] if runtime_transitions else None,
            "replay_transition": replay_transitions[0] if replay_transitions else None,
        }
    elif len(runtime_transitions) != len(replay_transitions):
        first_divergence_index = min(len(runtime_transitions), len(replay_transitions))
        parity = False
        divergence = {
            "type": "TRANSITION_COUNT_MISMATCH",
            "first_divergence_index": first_divergence_index,
            "runtime_transition": runtime_transitions[first_divergence_index]
            if first_divergence_index < len(runtime_transitions)
            else None,
            "replay_transition": replay_transitions[first_divergence_index]
            if first_divergence_index < len(replay_transitions)
            else None,
        }
    else:
        for idx, (runtime_transition, replay_transition) in enumerate(zip(runtime_transitions, replay_transitions)):
            comparable_runtime = {k: v for k, v in runtime_transition.items() if k != "timestamp_utc"}
            comparable_replay = {k: v for k, v in replay_transition.items() if k != "timestamp_utc"}
            if comparable_runtime != comparable_replay:
                parity = False
                divergence = {
                    "type": "TRANSITION_MISMATCH",
                    "first_divergence_index": idx,
                    "runtime_transition": runtime_transition,
                    "replay_transition": replay_transition,
                }
                break

    return {
        "station": normalized_station,
        "trading_day": normalized_day,
        "runtime_transition_count": len(runtime_transitions),
        "replay_transition_count": len(replay_transitions),
        "parity": parity,
        "divergence": divergence,
    }
