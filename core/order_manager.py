"""
Order manager Module

Extracted from kalshi_monitor.py during Phase 20.1 monolith decomposition.
"""



import base64
import copy
import contextvars
import json
import logging
import os
import re

# Layer 4: LOW market discovery regex
LOW_TICKER_PATTERN = re.compile(r"^LOW-\d{6}$")
import sqlite3
import threading
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

import re

# Layer 4: LOW market discovery regex
LOW_TICKER_PATTERN = re.compile(r"^LOW-\d{6}$")
from flask import has_request_context, request

from core.authoritative_state import immutable_public_state_snapshot
from core.station_time import parse_iso_utc, station_local_day_key, to_station_local
from core.metar_monitor import _now_utc_iso
from core.alert_schema import ALERT_SCHEMA_VERSION
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

_last_market_state = {}
_last_composed_sent = {}
_last_market_check_summary = {}
_ladder_state = {}
_ladder_event_keys = {}
_LADDER_LOCK = threading.Lock()
_SERIES_LOCK = threading.Lock()
_PROXIMITY_LOCK = threading.Lock()
_SERIES_BY_STATION = {}
_SERIES_DISCOVERED = False
_SERIES_MARKETS_CACHE = {}
_SERIES_EVENTS_CACHE = {}
_HYDRATION_PREREQUISITE_STATE = {}
_SERIES_DISCOVERY_ATTEMPT_COUNT = 0
_LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
_LAST_SERIES_DISCOVERY_ERROR = None
_DISCOVERED_WEATHER_MARKETS = []
_DISCOVERED_WEATHER_MARKETS_BY_STATION = {}
_MARKETS_CACHE_POPULATION_COUNT = 0
_LAST_HYDRATION_EXECUTION = {}
_hydration_queue = []
_hydration_backoff_until = {}
_last_hydration_request_ts = 0.0
_LAST_PROXIMITY_REGIME = {}
_PROXIMITY_RANK = {
    "FAR": 0,
    "APPROACHING": 1,
    "NEAR": 2,
    "CRITICAL": 3,
}

# ─── Station city token map (compatibility shim) ───
# This is now derived from core.station_registry. Kept here for backward compatibility
# with app.py and other modules that import it directly.
# Do NOT hardcode station lists elsewhere — use station_registry.get_all_stations() instead.
try:
    from core.station_registry import get_station_mapping as _registry_get_mapping
    _STATION_CITY_TOKEN_MAP = {
        icao: info.get("kalshi_token", "")
        for icao, info in _registry_get_mapping().items()
    }
except Exception:
    # Fallback if station_registry import fails (shouldn't happen in normal operation)
    _STATION_CITY_TOKEN_MAP = {
        "KDEN": "DEN", "KLAX": "LAX", "KNYC": "NYC", "KPHL": "PHIL",
        "KMDW": "CHI", "KMIA": "MIA", "KAUS": "AUS",
    }
__all__ = ['kalshi_execution_domain', 'set_kalshi_execution_domain', 'reset_kalshi_execution_domain', 'process_ladder_transition', 'send_composed_weather_market_alert', 'check_public_market_changes', 'get_ladder_state_snapshot']



class kalshi_execution_domain:
    def __init__(self, domain: str):
        self._domain = (domain or "production").strip().lower() or "production"
        self._token = None

    def __enter__(self):
        self._token = _KALSHI_EXECUTION_DOMAIN.set(self._domain)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _KALSHI_EXECUTION_DOMAIN.reset(self._token)
def _current_kalshi_execution_domain() -> str:
    domain = str(_KALSHI_EXECUTION_DOMAIN.get() or "production").strip().lower() or "production"
    # Ensure domain is normalized to canonical form
    return {
        "production": "prod",
        "prod": "prod",
        "sandbox": "sbox",
        "sbox": "sbox",
        "sbx": "sbox",
        "development": "dev",
        "dev": "dev",
    }.get(domain, domain)
def set_kalshi_execution_domain(domain: str):
    domain_val = (domain or "production").strip().lower() or "production"
    # Normalize long-form domain names to canonical short names
    normalized = {
        "production": "prod",
        "prod": "prod",
        "sandbox": "sbox",
        "sbox": "sbox",
        "sbx": "sbox",
        "dev": "development",
        "development": "development",
    }.get(domain_val, domain_val)  
    return _KALSHI_EXECUTION_DOMAIN.set(normalized)
def reset_kalshi_execution_domain(token) -> None:
    _KALSHI_EXECUTION_DOMAIN.reset(token)
def process_ladder_transition(station, market_type, snapshot, current_temp):
    markets = (snapshot or {}).get("markets") or []
    if not markets:
        return {
            "should_alert": False,
            "reason": None,
            "outcome_hint": "NO_ELIGIBLE_MARKET",
        }

    event_ticker = markets[0].get("event_ticker")
    if not event_ticker:
        return {
            "should_alert": False,
            "reason": None,
            "outcome_hint": "NO_ELIGIBLE_MARKET",
        }

    ladder = _build_ladder_structure(markets)
    bucket_index, final_now = _determine_bucket(current_temp, ladder, market_type)

    normalized_station = (station or "").strip().upper()
    normalized_market_type = (market_type or "").strip().upper()
    with _LADDER_LOCK:
        prev_event_state_key = _ladder_event_keys.get((normalized_station, normalized_market_type))
        state_key = f"{normalized_station}_{normalized_market_type}_{event_ticker}"

        if prev_event_state_key and prev_event_state_key != state_key:
            _ladder_state.pop(prev_event_state_key, None)

        _ladder_event_keys[(normalized_station, normalized_market_type)] = state_key

        state = _ladder_state.get(
            state_key,
            {"inside": False, "bucket_index": None, "final_hit": False},
        )

        should_alert = False
        reason = None
        terminal_state_blocked = bool(final_now and state.get("final_hit"))
        prior_bucket_index = state.get("bucket_index")

        if not state["inside"] and bucket_index is not None:
            should_alert = True
            reason = "entry"
        elif state["inside"] and bucket_index is not None and bucket_index != state.get("bucket_index"):
            should_alert = True
            reason = "bucket"

        if final_now and not state.get("final_hit"):
            should_alert = True
            reason = "final"

        if bucket_index is None:
            state["inside"] = False
            state["bucket_index"] = None
            state["final_hit"] = False
        else:
            state["inside"] = True
            state["bucket_index"] = bucket_index
            if final_now:
                state["final_hit"] = True

        _ladder_state[state_key] = state

    direction = None
    if should_alert:
        if prior_bucket_index is not None and bucket_index is not None and bucket_index != prior_bucket_index:
            direction = "UP" if bucket_index > prior_bucket_index else "DOWN"
        else:
            direction = "UP"

    outcome_hint = None
    if terminal_state_blocked:
        outcome_hint = "TERMINAL_STATE"
    elif not should_alert and reason is None:
        outcome_hint = "ELIGIBLE_NOT_ALERTABLE"

    return {
        "should_alert": should_alert,
        "reason": reason,
        "bucket_index": bucket_index,
        "direction": direction,
        "terminal_state_blocked": terminal_state_blocked,
        "outcome_hint": outcome_hint,
    }
def _send_kalshi_market_alert(ticker, prev_state, curr_state):
    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return False

    fields = [{"name": "Ticker", "value": str(ticker)}]

    prev_price = None if not prev_state else prev_state.get("last_price")
    prev_status = None if not prev_state else prev_state.get("status")
    curr_price = curr_state.get("last_price")
    curr_status = curr_state.get("status")

    if prev_state is None or prev_price != curr_price:
        fields.append(
            {
                "name": "Last Price",
                "value": _format_change(prev_price, curr_price),
            }
        )

    if prev_state is None or prev_status != curr_status:
        fields.append(
            {
                "name": "Status",
                "value": _format_change(prev_status, curr_status),
            }
        )

    payload = {
        "content": None,
        "embeds": [
            {
                "title": "Kalshi Market Update",
                "fields": fields,
                "footer": {
                    "text": "Kalshi Monitor (Public Mode)",
                },
            }
        ],
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    return 200 <= response.status_code < 300
def send_composed_weather_market_alert(
    station: str,
    market_types: set,
    transition_reason: str = None,
    prev_temp_f=None,
    now_temp_f=None,
    delta_f=None,
    obs_time_utc=None,
):
    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot_from_cache(
        normalized_station,
        market_types,
        observation_time_utc=obs_time_utc,
    )
    markets = snapshot.get("markets", [])
    current_temp_f = (snapshot.get("observed") or {}).get("current_temp_f")
    market_types_list = snapshot.get("market_types", [])

    if not markets:
        enqueue_station_hydration(normalized_station, reason="alert_send_cache_missing")
        return {"ok": False, "reason": "no_markets"}

    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return {
            "ok": False,
            "reason": "missing_webhook",
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": None,
            "webhook_response_text": None,
        }

    def _to_price(value, fallback):
        chosen = value if value is not None else fallback
        if chosen is None:
            return "N/A"
        try:
            return str(int(round(float(chosen))))
        except (TypeError, ValueError):
            return "N/A"

    def _sort_key(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        if strike_type == "less":
            return float("-inf")
        if strike_type == "greater":
            return float("inf")
        if floor is None:
            return float("inf")
        return float(floor)

    sorted_markets = sorted(markets, key=_sort_key)

    current_index = None
    for idx, market in enumerate(sorted_markets):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        if current_temp_f is None:
            continue
        if strike_type == "less" and cap is not None and current_temp_f <= float(cap):
            current_index = idx
            break
        if (
            strike_type == "between"
            and floor is not None
            and cap is not None
            and float(floor) <= current_temp_f < float(cap)
        ):
            current_index = idx
            break
        if strike_type == "greater" and floor is not None and current_temp_f >= float(floor):
            current_index = idx
            break

    if current_index is None and sorted_markets:
        current_index = 0 if current_temp_f is None else len(sorted_markets) - 1

    market_type = market_types_list[0] if market_types_list else ""
    event_ticker = (sorted_markets[0] if sorted_markets else {}).get("event_ticker") or "N/A"

    previous_bucket_index = None
    previous_temp_f = None
    previous_context = getattr(send_composed_weather_market_alert, "_prev_context", {})
    context_key = f"{normalized_station}_{market_type}_{event_ticker}"
    if isinstance(previous_context, dict):
        prior = previous_context.get(context_key) or {}
        previous_bucket_index = prior.get("bucket_index")
        previous_temp_f = prior.get("temp_f")

    reason_lower = (transition_reason or "").lower()
    if reason_lower == "up":
        direction_up = True
    elif reason_lower == "down":
        direction_up = False
    elif (
        previous_bucket_index is not None
        and current_index is not None
        and previous_bucket_index != current_index
    ):
        direction_up = current_index > previous_bucket_index
    elif (
        previous_temp_f is not None
        and current_temp_f is not None
        and float(current_temp_f) != float(previous_temp_f)
    ):
        direction_up = float(current_temp_f) > float(previous_temp_f)
    else:
        direction_up = True

    direction_icon = "⬆️" if direction_up else "⬇️"

    def _label_for_market(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        if strike_type == "less" and cap is not None:
            return f"{int(float(cap))} or below"
        if strike_type == "greater" and floor is not None:
            return f"{int(float(floor))} or higher"
        if strike_type == "between" and floor is not None and cap is not None:
            return f"{int(float(floor))}–{int(float(cap))}"
        strike = market.get("strike")
        return str(int(float(strike))) if strike is not None else "N/A"

    row_labels = [_label_for_market(market) for market in sorted_markets]
    label_width = max((len(label) for label in row_labels), default=0)

    ladder_rows = []
    for idx, market in enumerate(sorted_markets):
        yes_price = _to_price(market.get("yes_bid"), market.get("yes_ask"))
        no_price = _to_price(market.get("no_bid"), market.get("no_ask"))
        label = row_labels[idx].ljust(label_width)
        prefix = "▶   " if idx == current_index else "    "
        suffix = "  ← CURRENT" if idx == current_index else ""
        ladder_rows.append(
            f"{prefix}{label}  YES {yes_price}¢  NO {no_price}¢{suffix}"
        )

    current_market = sorted_markets[current_index] if sorted_markets and current_index is not None else None
    current_label = _label_for_market(current_market) if current_market else "N/A"

    strike_type_for_title = (current_market or {}).get("strike_type")
    if strike_type_for_title == "less":
        title_emoji = "🧊"
    elif strike_type_for_title == "greater":
        title_emoji = "🔥"
    else:
        title_emoji = "🌡️"

    distance_info = "MAX REACHED"

    def _ordered_bounds(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        low = float("-inf") if strike_type == "less" else (
            float(floor) if floor is not None else float("-inf")
        )
        high = float("inf") if strike_type == "greater" else (
            float(cap) if cap is not None else float("inf")
        )
        return (low, high)

    ordered_markets = sorted(
        sorted_markets,
        key=lambda m: (_ordered_bounds(m)[0], _ordered_bounds(m)[1]),
    )

    ordered_index = None
    if current_market is not None:
        current_ticker = current_market.get("ticker")
        for idx, market in enumerate(ordered_markets):
            if market.get("ticker") == current_ticker:
                ordered_index = idx
                break

    if current_market is not None and current_temp_f is not None and ordered_index is not None:
        if direction_up:
            if ordered_index < len(ordered_markets) - 1:
                next_market = ordered_markets[ordered_index + 1]
                boundary = next_market.get("floor_strike")
                if boundary is not None:
                    distance = round(float(boundary) - float(current_temp_f), 1)
                    distance_info = f"{distance:.1f}°F"
            else:
                distance_info = "MAX REACHED"
        else:
            if ordered_index > 0:
                next_market = ordered_markets[ordered_index - 1]
                boundary = next_market.get("cap_strike")
                if boundary is not None:
                    distance = round(float(current_temp_f) - float(boundary), 1)
                    distance_info = f"{distance:.1f}°F"
            else:
                distance_info = "MIN REACHED"

    local_time_display = "N/A"
    local_dt = None
    try:
        local_dt = to_station_local(normalized_station, datetime.now(timezone.utc))
        local_time_display = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        local_time_display = "N/A"
        local_dt = None

    temp_display = "N/A" if current_temp_f is None else f"{float(current_temp_f):.1f}"
    prev_display = "N/A" if prev_temp_f is None else f"{float(prev_temp_f):.1f}"
    now_display = "N/A" if now_temp_f is None else f"{float(now_temp_f):.1f}"
    delta_display = "N/A" if delta_f is None else f"{float(delta_f):+.1f}"

    epoch_context = _load_current_epoch_context(
        normalized_station,
        market_type,
        obs_time_utc,
    )
    epoch_context["previous_relevant_bucket"] = previous_bucket_index
    epoch_context["current_relevant_bucket"] = current_index

    station_local_timestamp = None
    obs_dt = parse_iso_utc(obs_time_utc)
    if obs_dt is not None:
        try:
            station_local_timestamp = to_station_local(normalized_station, obs_dt).isoformat()
        except Exception:
            station_local_timestamp = None
    epoch_context["station_local_timestamp"] = station_local_timestamp

    attention_phrase = _derive_attention_phrase(epoch_context)

    hydration_snapshot = get_last_hydration_execution_snapshot().get(normalized_station, {})
    hydration_status = "READY" if hydration_snapshot.get("cache_written") else "BLOCKED"
    hydration_evaluated_at = parse_iso_utc(hydration_snapshot.get("evaluated_at_utc"))
    ladder_cache_age_seconds = None
    if hydration_evaluated_at is not None:
        reference_dt = obs_dt if obs_dt is not None else datetime.now(timezone.utc)
        ladder_cache_age_seconds = max(
            0,
            int((reference_dt - hydration_evaluated_at).total_seconds()),
        )
    markets_considered_count = int(hydration_snapshot.get("raw_market_count") or len(markets))
    eligible_markets_count = len(markets)
    rejected_markets_count = max(markets_considered_count - eligible_markets_count, 0)
    rejection_counts = hydration_snapshot.get("rejection_counts") or {}
    rejection_breakdown = {
        "directional_strike_rejected": max(int(hydration_snapshot.get("filtered_market_count") or 0) - eligible_markets_count, 0),
        "wrong_series": int(rejection_counts.get("city_token_mismatch") or 0) + int(rejection_counts.get("market_type_mismatch") or 0),
        "expired_market": int(rejection_counts.get("inactive_market") or 0),
        "settlement_mismatch": int(rejection_counts.get("date_mismatch") or 0),
        "unknown_reason": max(
            rejected_markets_count
            - (
                max(int(hydration_snapshot.get("filtered_market_count") or 0) - eligible_markets_count, 0)
                + int(rejection_counts.get("city_token_mismatch") or 0)
                + int(rejection_counts.get("market_type_mismatch") or 0)
                + int(rejection_counts.get("inactive_market") or 0)
                + int(rejection_counts.get("date_mismatch") or 0)
            ),
            0,
        ),
    }
    transition_type = (transition_reason or "").strip().lower() or None
    instant_bucket_before = previous_bucket_index
    instant_bucket_after = current_index
    settlement_bucket = epoch_context.get("settlement_bucket")
    running_max = epoch_context.get("running_max")
    timestamp_utc = obs_time_utc
    temperature_f = now_temp_f
    reason_token = (transition_reason or "crossed").strip().lower() or "crossed"
    decision = "FIRED"
    reason = transition_reason or "ladder_transition"
    alert_type = "ladder_transition"
    direction = "UP" if direction_up else "DOWN"
    event_ticker_value = event_ticker
    summary = (
        f"{normalized_station} {transition_type or reason_token} detected; "
        f"{eligible_markets_count} eligible markets; "
        f"alert fired ({reason})"
    )

    header = f"{title_emoji} {normalized_station} {market_type or 'WEATHER'} — Ladder Cross {direction_icon}"
    ladder_block = "\n".join(ladder_rows)
    content = (
        f"{header}\n"
        f"Structure: {attention_phrase}\n"
        f"Prev: {prev_display}°F\n"
        f"Now: {now_display}°F\n"
        f"Δ: {delta_display}°F\n"
        f"{temp_display}°F  →  Entered {current_label}\n"
        f"Local time: {local_time_display}\n\n"
        f"Epoch: S={epoch_context.get('settlement_bucket')}"
        f" P={epoch_context.get('prior_settlement_bucket')}"
        f" J={epoch_context.get('settlement_jump_magnitude')}"
        f" Status={epoch_context.get('epoch_status')}"
        f" Rev={epoch_context.get('reversion_occurred')}"
        f" RevAt={epoch_context.get('first_reversion_timestamp_utc')}"
        f" Exc={epoch_context.get('max_excursion_above_settlement')}"
        f" Terminal={epoch_context.get('terminal_state_reached')}\n"
        f"Relevant bucket: prev={epoch_context.get('previous_relevant_bucket')} curr={epoch_context.get('current_relevant_bucket')}\n"
        f"Station local obs: {epoch_context.get('station_local_timestamp')}\n\n"
        f"Event: {event_ticker}\n"
        f"https://kalshi.com/markets/{event_ticker}\n\n"
        "LADDER\n"
        "────────────────────────────────\n"
        f"{ladder_block}\n"
        "────────────────────────────────\n\n"
        f"Next rung: {distance_info}"
    )

    market_open = bool((current_market or {}).get("open", True))
    market_expired = bool((current_market or {}).get("expired", False))
    market_range = {
        "strike_type": (current_market or {}).get("strike_type"),
        "floor_strike": (current_market or {}).get("floor_strike"),
        "cap_strike": (current_market or {}).get("cap_strike"),
        "label": current_label,
    }
    hydration_state = {
        "status": hydration_status,
        "series_discovered": bool(hydration_snapshot.get("series_ticker")),
    }
    payload = {
        "content": content,
        "embeds": [],
        "schema_version": ALERT_SCHEMA_VERSION,
        "alert_schema_version": ALERT_SCHEMA_VERSION,
        "timestamp_utc": timestamp_utc,
        "station": normalized_station,
        "classification": "MARKET_ELIGIBLE",
        "alert_summary": {
            "station": normalized_station,
            "transition_type": transition_type,
            "settlement_bucket": settlement_bucket,
            "market_symbol": event_ticker_value,
            "alert_classification": "MARKET_ELIGIBLE",
        },
        "transition_correlation": {
            "transition_event_id": None,
            "timestamp_utc": obs_time_utc,
            "instant_bucket_before": instant_bucket_before,
            "instant_bucket_after": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
            "running_max": running_max,
        },
        "market_evaluation": {
            "market_symbol": event_ticker_value,
            "market_range": market_range,
            "market_open": market_open,
            "market_expired": market_expired,
            "eligibility_result": "ELIGIBLE",
        },
        "suppression_context": {
            "suppression_reason": "",
            "settlement_mismatch": False,
            "expired_market": market_expired,
            "hydration_blocked": not bool(hydration_snapshot.get("cache_written")),
            "execution_domain_blocked": _current_kalshi_execution_domain() in _FORBIDDEN_KALSHI_DOMAINS,
        },
        "diagnostic_metadata": {
            "alert_schema_version": ALERT_SCHEMA_VERSION,
            "execution_domain": _current_kalshi_execution_domain(),
            "hydration_state": hydration_state,
            "ladder_cache_age_seconds": ladder_cache_age_seconds,
            "evaluation_timestamp": timestamp_utc,
        },
        "alert_classification": "MARKET_ELIGIBLE",
        "summary": {
            "headline": summary,
            "transition": transition_type,
            "temp_f": temperature_f,
            "instant_bucket": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
        },
        "transition_context": {
            "transition_type": transition_type,
            "instant_before": instant_bucket_before,
            "instant_after": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
            "running_max": running_max,
            "obs_time": obs_time_utc,
        },
        "market_context": {
            "series_ticker": (sorted_markets[0] if sorted_markets else {}).get("series_ticker"),
            "event_ticker": event_ticker_value,
            "market_type": market_type,
            "strike": (current_market or {}).get("strike"),
            "proximity_regime": classify_proximity(abs(float((current_market or {}).get("strike") or 0) - float(temperature_f))) if temperature_f is not None and (current_market or {}).get("strike") is not None else None,
            "hydrated": bool(hydration_snapshot.get("cache_written")),
        },
        "eligibility_evaluation": {
            "markets_considered": markets_considered_count,
            "eligible_markets": eligible_markets_count,
            "rejected_markets": rejected_markets_count,
            "rejection_breakdown": rejection_breakdown,
        },
        "suppression": {
            "suppressed": False,
            "reason": "",
            "reason_category": "NO_TRANSITION",
        },
        "execution_context": {
            "execution_domain": _current_kalshi_execution_domain(),
            "hydration_state": {
                **hydration_state,
                "ladder_cache_age_seconds": ladder_cache_age_seconds,
            },
            "scheduler_poll_count": None,
            "station_local_timestamp": station_local_timestamp or (local_dt.isoformat() if local_dt else None),
        },
        "alert_context": {
            "attention_phrase": attention_phrase,
            **epoch_context,
        },
        "alert_decision": {
            "decision": decision,
            "reason": reason,
            "alert_type": alert_type,
            "bucket_index": current_index,
            "direction": direction,
            "event_ticker": event_ticker_value,
        },
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        return {
            "ok": False,
            "reason": "webhook_exception",
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": str(e),
            "webhook_response_text": None,
        }
    webhook_response_text = str(getattr(response, "text", "") or "")[:200] or None
    if not (200 <= response.status_code < 300):
        return {
            "ok": False,
            "reason": "webhook_failed",
            "delivery_succeeded": False,
            "webhook_status_code": int(response.status_code),
            "webhook_exception": None,
            "webhook_response_text": webhook_response_text,
        }

    key = f"{normalized_station}_{','.join(sorted(snapshot.get('market_types', [])))}"
    _last_composed_sent[key] = datetime.utcnow().isoformat() + "Z"

    if not isinstance(previous_context, dict):
        previous_context = {}
    previous_context[context_key] = {
        "bucket_index": current_index,
        "temp_f": current_temp_f,
    }
    send_composed_weather_market_alert._prev_context = previous_context

    return {
        "ok": True,
        "delivery_succeeded": True,
        "webhook_status_code": int(response.status_code),
        "webhook_exception": None,
        "webhook_response_text": webhook_response_text,
        "markets_included": len(markets),
        "observed": current_temp_f,
        "event_ticker": event_ticker,
        "bucket_index": current_index,
        "attention_phrase": attention_phrase,
        "alert_context": {
            "attention_phrase": attention_phrase,
            **epoch_context,
        },
    }
def check_public_market_changes(limit=5):
    global _last_market_check_summary

    markets_data = get_public_markets(limit=limit)
    markets = markets_data.get("markets", [])
    target_station = (os.getenv("KALSHI_TARGET_STATION") or "").strip().upper()
    target_market_types = _parse_target_market_types(
        os.getenv("KALSHI_TARGET_MARKET_TYPE")
    )

    if target_station:
        markets = _filter_structured_markets(
            markets,
            target_station,
            target_market_types,
        )

    raw_allowlist = (os.getenv("KALSHI_ALERT_TICKERS") or "").strip()
    alert_allowlist = None
    if raw_allowlist:
        alert_allowlist = {
            ticker.strip()
            for ticker in raw_allowlist.split(",")
            if ticker.strip()
        }

    if not _last_market_state:
        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue

            _last_market_state[ticker] = {
                "last_price": market.get("last_price"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
                "status": market.get("status"),
            }

        summary = {
            "markets_checked": len(markets),
            "changes_detected": 0,
            "alerts_sent": 0,
        }
        _last_market_check_summary = summary
        return summary

    changes_detected = 0
    alerts_sent = 0

    for market in markets:
        ticker = market.get("ticker")
        if not ticker:
            continue

        curr_state = {
            "last_price": market.get("last_price"),
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "no_bid": market.get("no_bid"),
            "no_ask": market.get("no_ask"),
            "status": market.get("status"),
        }

        prev_state = _last_market_state.get(ticker)
        should_alert = (
            prev_state is None
            or prev_state.get("last_price") != curr_state.get("last_price")
            or prev_state.get("status") != curr_state.get("status")
        )

        if should_alert:
            changes_detected += 1
            if (
                alert_allowlist is None or ticker in alert_allowlist
            ) and _send_kalshi_market_alert(ticker, prev_state, curr_state):
                alerts_sent += 1

        _last_market_state[ticker] = curr_state

    summary = {
        "markets_checked": len(markets),
        "changes_detected": changes_detected,
        "alerts_sent": alerts_sent,
    }
    _last_market_check_summary = summary
    return summary
def get_ladder_state_snapshot():
    """
    Read-only snapshot of in-memory ladder state.

    Returns:
        {
            "ladder_state": {"<normalized_key>": state_dict},
            "ladder_event_keys": {"<station_market_type>": "<normalized_key>"},
            "total_state_keys": int
        }
    """

    def _normalize_key(raw_key):
        if isinstance(raw_key, tuple) and len(raw_key) == 2:
            station, market_type = raw_key
            return f"{station}_{market_type}"
        return raw_key

    with _LADDER_LOCK:
        ladder_state_copy = {
            _normalize_key(state_key): dict(state_value)
            for state_key, state_value in _ladder_state.items()
        }
        ladder_event_keys_copy = {
            f"{station}_{market_type}": _normalize_key(state_key)
            for (station, market_type), state_key in _ladder_event_keys.items()
        }

    return {
        "ladder_state": ladder_state_copy,
        "ladder_event_keys": ladder_event_keys_copy,
        "total_state_keys": len(ladder_state_copy),
    }
def _persist_signal_state(signal_name: str, state_dict: Dict[str, Any]) -> None:
    """Persist signal state to SQLite (L0-T1)."""
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Ensure schema exists
            _ensure_alert_schema()
            state_json = json.dumps(state_dict, sort_keys=True)
            now_iso = _now_utc_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO signal_layer_state (
                    signal_name, state_json, updated_at
                ) VALUES (?, ?, ?)
                """,
                (signal_name, state_json, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("signal_state_persist_failed name=%s error=%s", signal_name, e)
def _load_signal_state(signal_name: str) -> Optional[Dict[str, Any]]:
    """Load signal state from SQLite."""
    try:
        db_path = _alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            row = conn.execute(
                "SELECT state_json FROM signal_layer_state WHERE signal_name = ?",
                (signal_name,),
            ).fetchone()
            if not row:
                return None
            return json.loads(row[0])
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("signal_state_load_failed name=%s error=%s", signal_name, e)
        return None
def _persist_market_cache(market_id: str, station: str, cache_dict: Dict[str, Any]) -> None:
    """Persist market cache entry to SQLite (L0-T2).
    
    Updates market cache with hydration timestamp for restart survival.
    """
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Ensure schema exists
            _ensure_alert_schema()
            cache_json = json.dumps(cache_dict, sort_keys=True)
            now_iso = _now_utc_iso()
            
            # Update cache_dict with hydration timestamp
            cache_dict_with_hydration = dict(cache_dict)
            cache_dict_with_hydration["last_hydrated_utc"] = now_iso
            cache_json = json.dumps(cache_dict_with_hydration, sort_keys=True)
            
            conn.execute(
                """
                INSERT OR REPLACE INTO market_cache (
                    market_id, station, cache_json, discovered_at, updated_at, last_hydrated_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (market_id, station, cache_json, now_iso, now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("market_cache_persist_failed market_id=%s error=%s", market_id, e)
def _load_market_cache(market_id: str) -> Optional[Dict[str, Any]]:
    """Load market cache entry from SQLite."""
    try:
        db_path = _alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            row = conn.execute(
                "SELECT station, cache_json FROM market_cache WHERE market_id = ?",
                (market_id,),
            ).fetchone()
            if not row:
                return None
            station, cache_json = row
            cache_data = json.loads(cache_json)
            # Include station in returned dict for compatibility
            cache_data["station"] = station
            return cache_data
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("market_cache_load_failed market_id=%s error=%s", market_id, e)
        return None
def _load_all_market_cache() -> Dict[str, Dict[str, Any]]:
    """Load all market cache entries from SQLite for startup hydration."""
    result = {}
    try:
        db_path = _alert_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            rows = conn.execute(
                "SELECT market_id, station, cache_json, last_hydrated_utc FROM market_cache"
            ).fetchall()
            for market_id, station, cache_json, last_hydrated_utc in rows:
                try:
                    cache_data = json.loads(cache_json)
                    # Merge last_hydrated_utc into cache if present
                    if last_hydrated_utc and "last_hydrated_utc" not in cache_data:
                        cache_data["last_hydrated_utc"] = last_hydrated_utc
                    result[market_id] = {
                        "station": station,
                        "cache": cache_data,
                    }
                except Exception:
                    continue
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("market_cache_load_all_failed error=%s", e)
    return result
def _hydrate_all_market_cache() -> int:
    """Load all market cache from SQLite and hydrate in-memory state.
    
    Returns:
        Number of cache entries hydrated
    """
    loaded = _load_all_market_cache()
    hydrate_count = 0
    
    with _SERIES_LOCK:
        for market_id, entry in loaded.items():
            if not market_id.startswith("series:"):
                continue
            # Extract station and ticker from market_id format: "series:STATION:SERIES"
            parts = market_id.split(":")
            if len(parts) < 3:
                continue
            station = parts[1]
            series_ticker = ":".join(parts[2:])
            
            cache_data = entry.get("cache", {})
            if not cache_data.get("markets"):
                continue
            
            # Hydrate in-memory cache
            _SERIES_MARKETS_CACHE[series_ticker] = cache_data
            hydrate_count += 1
    
    if hydrate_count > 0:
        _LOGGER.info("market_cache_hydrated_entries=%d", hydrate_count)
    
    return hydrate_count
def _ensure_alert_schema() -> None:
    """Ensure Kalshi-specific schema tables exist (L1 - HIGH-1)."""
    db_path = _alert_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with _SERIES_LOCK:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Kalshi rate limit counter (L1-T4)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kalshi_rate_limit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL,
                    request_time TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Signal layer state persistence (L0-T1)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_layer_state (
                    signal_name TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Market cache persistence (L0-T2)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_cache (
                    market_id TEXT PRIMARY KEY,
                    station TEXT NOT NULL,
                    cache_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_hydrated_utc TEXT
                )
                """
            )
            conn.commit()
            
            # Upgrade: Add last_hydrated_utc column if missing (existing databases)
            try:
                cursor = conn.execute("PRAGMA table_info(market_cache)")
                columns = {row[1] for row in cursor.fetchall()}
                if "last_hydrated_utc" not in columns:
                    conn.execute("ALTER TABLE market_cache ADD COLUMN last_hydrated_utc TEXT")
                    conn.commit()
                    _LOGGER.info("market_cache_schema_upgraded added_last_hydrated_utc")
            except Exception as e:
                _LOGGER.info("market_cache_schema_upgrade_skipped error=%s", str(e))
            
        finally:
            conn.close()
def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")
def _load_current_epoch_context(station: str, market_type: str, obs_time_utc: str):
    normalized_station = (station or "").strip().upper()
    normalized_market_type = (market_type or "").strip().upper() or None
    local_trading_date = station_local_day_key(normalized_station, obs_time_utc)
    if not normalized_station or local_trading_date == "unknown":
        return {}

    try:
        conn = sqlite3.connect(f"file:{_alert_db_path()}?mode=ro", uri=True, timeout=1)
    except Exception:
        return {}

    try:
        row = conn.execute(
            """
            SELECT settlement_bucket,
                   prior_settlement_bucket,
                   settlement_jump_magnitude,
                   epoch_status,
                   reversion_occurred,
                   first_reversion_timestamp_utc,
                   max_excursion_above_settlement,
                   terminal_state_reached
            FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND local_trading_date = ?
            ORDER BY CASE WHEN epoch_status = 'open' THEN 0 ELSE 1 END,
                     id DESC
            LIMIT 1
            """,
            (normalized_station, normalized_market_type, normalized_market_type, local_trading_date),
        ).fetchone()
        if not row:
            return {}

        return {
            "settlement_bucket": row[0],
            "prior_settlement_bucket": row[1],
            "settlement_jump_magnitude": row[2],
            "epoch_status": row[3],
            "reversion_occurred": bool(row[4]),
            "first_reversion_timestamp_utc": row[5],
            "max_excursion_above_settlement": row[6],
            "terminal_state_reached": bool(row[7]),
        }
    except Exception:
        return {}
    finally:
        conn.close()
def _derive_attention_phrase(epoch_context):
    if bool(epoch_context.get("terminal_state_reached")):
        return "TERMINAL STATE"
    if bool(epoch_context.get("reversion_occurred")):
        return "REVERTED AFTER SETTLEMENT"

    settlement_bucket = epoch_context.get("settlement_bucket")
    prior_settlement_bucket = epoch_context.get("prior_settlement_bucket")
    if (
        isinstance(settlement_bucket, int)
        and isinstance(prior_settlement_bucket, int)
        and settlement_bucket > prior_settlement_bucket
    ):
        return "NEW SETTLEMENT / NO REVERSION YET"

    if str(epoch_context.get("epoch_status") or "").lower() == "open":
        return "OPEN EPOCH / ALERTABLE"

    return "EPOCH CONTEXT AVAILABLE"
