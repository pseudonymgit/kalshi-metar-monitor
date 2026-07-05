"""Observability-only alert pipeline integrity checks."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


FINDING_ALERT_PIPELINE_GAP = "ALERT_PIPELINE_GAP"
FINDING_TRANSITION_WITHOUT_EVALUATION = "TRANSITION_WITHOUT_EVALUATION"
FINDING_SUPPRESSION_WITHOUT_REASON = "SUPPRESSION_WITHOUT_REASON"
FINDING_HYDRATION_DRIFT = "HYDRATION_DRIFT"
FINDING_MARKET_DISCOVERY_REGRESSION = "MARKET_DISCOVERY_REGRESSION"
FINDING_STATION_ALERT_SILENCE = "STATION_ALERT_SILENCE"
FINDING_HYDRATION_STALL_CONDITION = "HYDRATION_STALL_CONDITION"

_ACCEPTED_SUPPRESSION_REASON_TOKENS = {
    "NO_TRANSITION",
    "MARKET_RULE",
}

# Valid suppression reasons for L3
class ValidSuppressionReasons:
    """Valid suppression reason tokens for L3 enforcement."""
    DUPLICATE = "DUPLICATE"
    COOLDOWN = "COOLDOWN"
    RATE_LIMIT = "RATE_LIMIT"
    DOMAIN_BLOCKED = "DOMAIN_BLOCKED"
    UNKNOWN = "UNKNOWN"


def _parse_iso_utc(raw_value: Optional[str]) -> Optional[datetime]:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finding(*, station: str, finding_type: str, timestamp: str, supporting_metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "station": station,
        "timestamp": timestamp,
        "finding_type": finding_type,
        "supporting_metrics": supporting_metrics,
    }


def build_alert_integrity_findings(
    *,
    station_universe: Dict[str, Any],
    transitions: List[Dict[str, Any]],
    latest_evaluations: Dict[str, Dict[str, Any]],
    hydration_snapshot: Dict[str, Any],
    recent_alerts: List[Dict[str, Any]],
    hydration_stall_signal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    evaluation_window_seconds = max(30, int(os.getenv("ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS", "300")))
    silence_window_seconds = max(300, int(os.getenv("ALERT_INTEGRITY_SILENCE_WINDOW_SECONDS", "3600")))

    stations = sorted(
        {
            station
            for station in (station_universe.get("stations") or [])
            if station
        }
    )
    if not stations:
        stations = sorted(
            {
                station
                for station in (station_universe.get("configured_stations") or set())
                .union(station_universe.get("discovered_stations") or set())
                .union(station_universe.get("watchlist_stations") or set())
                if station
            }
        )

    transitions_by_station: Dict[str, List[Dict[str, Any]]] = {}
    for row in transitions or []:
        station = (row.get("station") or "").strip().upper()
        if station:
            transitions_by_station.setdefault(station, []).append(row)

    alerts_by_station: Dict[str, List[Dict[str, Any]]] = {}
    for alert in recent_alerts or []:
        station = (alert.get("station") or "").strip().upper()
        if station:
            alerts_by_station.setdefault(station, []).append(alert)

    hydration_stations = (hydration_snapshot.get("stations") or {}) if isinstance(hydration_snapshot, dict) else {}
    discovered_stations = {
        (station or "").strip().upper()
        for station in (station_universe.get("discovered_stations") or set())
        if station
    }

    findings: List[Dict[str, Any]] = []

    stall_signal = hydration_stall_signal if isinstance(hydration_stall_signal, dict) else {}

    for station in stations:
        station_transitions = transitions_by_station.get(station) or []
        latest_transition = station_transitions[0] if station_transitions else {}
        latest_transition_ts = _parse_iso_utc(
            latest_transition.get("timestamp")
            or latest_transition.get("created_utc")
            or latest_transition.get("transition_timestamp_utc")
        )

        latest_eval = latest_evaluations.get(station) or {}
        latest_eval_ts = _parse_iso_utc(latest_eval.get("latest_evaluation_timestamp_utc"))

        if latest_transition_ts and (now_utc - latest_transition_ts <= timedelta(seconds=evaluation_window_seconds)):
            missing_eval = latest_eval_ts is None or latest_eval_ts < latest_transition_ts
            if missing_eval:
                metrics = {
                    "evaluation_window_seconds": evaluation_window_seconds,
                    "latest_transition_timestamp_utc": latest_transition_ts.isoformat(),
                    "latest_evaluation_timestamp_utc": latest_eval.get("latest_evaluation_timestamp_utc"),
                }
                findings.append(
                    _finding(
                        station=station,
                        finding_type=FINDING_ALERT_PIPELINE_GAP,
                        timestamp=now_iso,
                        supporting_metrics=metrics,
                    )
                )
                findings.append(
                    _finding(
                        station=station,
                        finding_type=FINDING_TRANSITION_WITHOUT_EVALUATION,
                        timestamp=now_iso,
                        supporting_metrics=metrics,
                    )
                )

        raw_suppression_reason = latest_eval.get("latest_suppression_reason")
        suppression_reason = str(raw_suppression_reason).strip() if raw_suppression_reason is not None else ""
        raw_eval_outcome = latest_eval.get("latest_evaluation_outcome")
        eval_outcome = str(raw_eval_outcome).strip().upper() if raw_eval_outcome is not None else ""
        outcome_reason_token = None
        if eval_outcome.startswith("SUPPRESSED_"):
            outcome_reason_token = eval_outcome[len("SUPPRESSED_") :]

        has_valid_reason_token = bool(
            suppression_reason
            or (outcome_reason_token and outcome_reason_token in _ACCEPTED_SUPPRESSION_REASON_TOKENS)
        )

        if "SUPPRESS" in eval_outcome and not has_valid_reason_token:
            findings.append(
                _finding(
                    station=station,
                    finding_type=FINDING_SUPPRESSION_WITHOUT_REASON,
                    timestamp=latest_eval.get("latest_evaluation_timestamp_utc") or now_iso,
                    supporting_metrics={
                        "latest_evaluation_outcome": eval_outcome,
                        "latest_alerts_sent": latest_eval.get("latest_alerts_sent"),
                    },
                )
            )

        hydration_station = hydration_stations.get(station) or {}
        cache_present = bool(hydration_station.get("cache_present"))
        prereq = hydration_station.get("hydration_prerequisite") if isinstance(hydration_station.get("hydration_prerequisite"), dict) else {}
        series_discovered = bool(prereq.get("series_discovered"))
        if cache_present and not series_discovered:
            findings.append(
                _finding(
                    station=station,
                    finding_type=FINDING_HYDRATION_DRIFT,
                    timestamp=now_iso,
                    supporting_metrics={
                        "cache_present": cache_present,
                        "series_discovered": series_discovered,
                        "cache_valid": bool(prereq.get("cache_valid")),
                    },
                )
            )

        is_active_station = bool(station_transitions)
        if is_active_station and station not in discovered_stations:
            findings.append(
                _finding(
                    station=station,
                    finding_type=FINDING_MARKET_DISCOVERY_REGRESSION,
                    timestamp=now_iso,
                    supporting_metrics={
                        "recent_transition_count": len(station_transitions),
                        "currently_discovered": False,
                    },
                )
            )

        station_alerts = alerts_by_station.get(station) or []
        if station_transitions and not station_alerts:
            if latest_transition_ts and (now_utc - latest_transition_ts <= timedelta(seconds=silence_window_seconds)):
                findings.append(
                    _finding(
                        station=station,
                        finding_type=FINDING_STATION_ALERT_SILENCE,
                        timestamp=now_iso,
                        supporting_metrics={
                            "silence_window_seconds": silence_window_seconds,
                            "transition_count": len(station_transitions),
                            "alert_count": 0,
                            "latest_transition_timestamp_utc": latest_transition_ts.isoformat(),
                        },
                    )
                )

    if bool(stall_signal.get("hydration_stall_condition")):
        findings.append(
            _finding(
                station=(stall_signal.get("station") or "GLOBAL"),
                finding_type=FINDING_HYDRATION_STALL_CONDITION,
                timestamp=now_iso,
                supporting_metrics={
                    "hydration_cache_not_written": bool(stall_signal.get("hydration_cache_not_written")),
                    "transitions_seen_today": int(stall_signal.get("transitions_seen_today") or 0),
                    "alerts_sent_today": int(stall_signal.get("alerts_sent_today") or 0),
                    "hydration_stall_condition": True,
                },
            )
        )

    return {
        "generated_utc": now_iso,
        "evaluation_window_seconds": evaluation_window_seconds,
        "silence_window_seconds": silence_window_seconds,
        "hydration_stall_signal": {
            "station": stall_signal.get("station"),
            "hydration_reason": stall_signal.get("hydration_reason"),
            "hydration_cache_not_written": bool(stall_signal.get("hydration_cache_not_written")),
            "transitions_seen_today": int(stall_signal.get("transitions_seen_today") or 0),
            "alerts_sent_today": int(stall_signal.get("alerts_sent_today") or 0),
            "hydration_stall_condition": bool(stall_signal.get("hydration_stall_condition")),
        },
        "finding_count": len(findings),
        "findings": findings,
    }
