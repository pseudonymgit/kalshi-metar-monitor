import base64
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Optional reuse of Phase 1 timezone helper
try:
    from core.metar_monitor import _to_local
except Exception:
    _to_local = None

try:
    from core.metar_monitor import get_state as get_metar_state
except Exception:
    get_metar_state = None
    
_last_market_state = {}
_last_composed_sent = {}
_last_market_check_summary = {}
_ladder_state = {}
_ladder_event_keys = {}
_LADDER_LOCK = threading.Lock()

_STATION_CITY_TOKEN_MAP = {
    "KDEN": "DEN",
    "KLAX": "LAX",
    "KNYC": "NY",
    "KPHL": "PHIL",
    "KMDW": "CHI",
    "KMIA": "MIA",
    "KAUS": "AUS",
}


def get_default_config():
    return {
        "base_url": (os.getenv("KALSHI_BASE_URL") or "").strip(),
        "key_id": (os.getenv("KALSHI_KEY_ID") or "").strip(),
        "key_pem": os.getenv("KALSHI_PRIVATE_KEY_PEM") or "",
    }


def _sign_request_rsa(private_key_pem, timestamp, method, path, body=""):
    message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    signature = private_key.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _kalshi_get(path):
    cfg = get_default_config()
    base_url = cfg["base_url"].rstrip("/")
    key_id = cfg["key_id"]
    key_pem = cfg["key_pem"]

    if not base_url:
        raise ValueError("KALSHI_BASE_URL is not configured")
    if "/trade-api/v2" in path:
        raise ValueError("path must not include /trade-api/v2")
    if not key_id or not key_pem:
        raise ValueError("Kalshi auth is not configured")

    normalized_path = path if path.startswith("/") else f"/{path}"
    timestamp = str(int(time.time() * 1000))
    signature = _sign_request_rsa(
        key_pem,
        timestamp,
        "GET",
        normalized_path,
        "",
    )

    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }

    response = requests.get(
        f"{base_url}{normalized_path}",
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_state():
    cfg = get_default_config()
    auth_configured = bool(cfg["base_url"] and cfg["key_id"] and cfg["key_pem"])
    return {
        "base_url": cfg["base_url"],
        "auth_configured": auth_configured,
    }


def get_metrics():
    cfg = get_default_config()
    return {
        "base_url_configured": bool(cfg["base_url"]),
        "auth_configured": bool(cfg["base_url"] and cfg["key_id"] and cfg["key_pem"]),
    }


def _kalshi_public_get(path):
    base_url = (
        os.getenv("KALSHI_PUBLIC_BASE_URL")
        or "https://api.elections.kalshi.com/trade-api/v2"
    ).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    response = requests.get(f"{base_url}{normalized_path}", timeout=10)
    response.raise_for_status()
    return response.json()


def get_public_markets(limit=5):
    """
    Fetch public Kalshi markets (no authentication).
    """
    data = _kalshi_public_get(f"/markets?limit={int(limit)}")
    return {
        "cursor": data.get("cursor"),
        "count": len(data.get("markets", [])),
        "markets": data.get("markets", []),
    }


def _get_all_public_markets(max_pages=5, page_limit=200):
    markets = []
    cursor = None

    for _ in range(max_pages):
        path = f"/markets?limit={int(page_limit)}"
        if cursor:
            path = f"{path}&cursor={cursor}"

        data = _kalshi_public_get(path)
        markets.extend(data.get("markets", []))

        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


def _format_change(prev, curr):
    return f"{prev} → {curr}"


def _parse_target_market_types(raw_types):
    if not raw_types:
        return set()
    valid_tokens = {"HIGH", "LOW"}
    return {
        token
        for token in (part.strip().upper() for part in raw_types.split(","))
        if token in valid_tokens
    }


def _get_active_stations():
    raw = (os.getenv("KALSHI_ACTIVE_STATIONS") or "").strip()
    if not raw:
        return None
    return {
        token.strip().upper()
        for token in raw.split(",")
        if token.strip()
    }


def _station_local_kalshi_date_token(station):
    now_utc = datetime.now(timezone.utc)

    if _to_local:
        try:
            now_local = _to_local(station, now_utc)
        except Exception:
            now_local = now_utc
    else:
        now_local = now_utc

    return now_local.strftime("%y%b%d").upper()

def _build_weather_event_ticker(station: str, market_type: str):
    city_token = _STATION_CITY_TOKEN_MAP.get(station)
    if not city_token:
        return None

    date_token = _station_local_kalshi_date_token(station)
    return f"KX{market_type}{city_token}-{date_token}"


def _filter_structured_markets(markets, station, market_types):
    normalized_station = (station or "").strip().upper()
    city_token = _STATION_CITY_TOKEN_MAP.get(normalized_station)

    if not city_token:
        return []

    date_token = _station_local_kalshi_date_token(normalized_station)

    filtered = []

    for market in markets:
        ticker = (market.get("ticker") or "").upper()
        status = market.get("status")

        if status and status != "active":
            continue

        if city_token not in ticker:
            continue

        if date_token not in ticker:
            continue

        if market_types and not any(mt in ticker for mt in market_types):
            continue

        filtered.append(market)

    return filtered


def _extract_strike_from_ticker(ticker):
    if not ticker:
        return None
    match = re.search(r"B(\d+)$", ticker)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def build_structured_snapshot(station: str, market_types: set):
    normalized_station = (station or "").strip().upper()

    selected_types = {
        token.strip().upper()
        for token in (market_types or set())
        if token and token.strip().upper() in {"HIGH", "LOW"}
    }

    fetched_markets = []
    market_types_to_fetch = sorted(selected_types) if selected_types else ["HIGH", "LOW"]

    for market_type in market_types_to_fetch:
        event_ticker = _build_weather_event_ticker(normalized_station, market_type)

        if not event_ticker:
            continue

        data = _kalshi_public_get(f"/markets?event_ticker={event_ticker}")
        fetched_markets.extend(data.get("markets", []))

    filtered_markets = _filter_structured_markets(
        fetched_markets,
        normalized_station,
        selected_types,
    )

    markets = []

    for market in filtered_markets:
        ticker = market.get("ticker") or ""

        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")

        if strike_type == "between" and floor is not None:
            strike = int(floor)
        elif strike_type == "less" and cap is not None:
            strike = int(cap)
        elif strike_type == "greater" and floor is not None:
            strike = int(floor)
        else:
            strike = _extract_strike_from_ticker(ticker)

        if strike is None:
            continue

        markets.append(
            {
                "ticker": ticker,
                "strike": strike,
                "strike_type": strike_type,
                "floor_strike": floor,
                "cap_strike": cap,
                "event_ticker": market.get("event_ticker"),
                "last_price": market.get("last_price"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
                "status": market.get("status"),
            }
        )

    markets.sort(key=lambda x: x["strike"])
    observed_value = None
    if get_metar_state:
        try:
            observed_value = (
                get_metar_state().get("latest", {})
                .get(normalized_station, {})
                .get("temp_f")
            )
        except Exception:
            pass

    return {
        "station": normalized_station,
        "market_types": sorted(selected_types),
        "markets": markets,
        "observed": {"current_temp_f": observed_value},
    }


def _build_ladder_structure(markets):
    ladder = []

    for market in markets or []:
        strike_type = market.get("strike_type")
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")

        if strike_type == "between" and floor_strike is not None and cap_strike is not None:
            ladder.append(
                {
                    "kind": "between",
                    "low": int(floor_strike),
                    "high": int(cap_strike),
                }
            )
        elif strike_type == "less" and cap_strike is not None:
            ladder.append(
                {
                    "kind": "less",
                    "threshold": int(cap_strike),
                }
            )
        elif strike_type == "greater" and floor_strike is not None:
            ladder.append(
                {
                    "kind": "greater",
                    "threshold": int(floor_strike),
                }
            )

    def _sort_key(item):
        if item["kind"] == "between":
            return item["low"]
        return item["threshold"]

    ladder.sort(key=_sort_key)
    return ladder


def _determine_bucket(temp_f, ladder, market_type):
    if temp_f is None or not ladder:
        return (None, False)

    market_type = (market_type or "").upper()
    between_buckets = [item for item in ladder if item.get("kind") == "between"]

    if not between_buckets:
        return (None, False)

    if market_type == "HIGH":
        first_between = between_buckets[0]
        if temp_f < first_between["low"]:
            return (None, False)

        greater_rungs = [item for item in ladder if item.get("kind") == "greater"]
        if greater_rungs and temp_f > greater_rungs[-1]["threshold"]:
            return (len(between_buckets), True)

    elif market_type == "LOW":
        last_between = between_buckets[-1]
        if temp_f > last_between["high"]:
            return (None, False)

        less_rungs = [item for item in ladder if item.get("kind") == "less"]
        if less_rungs and temp_f < less_rungs[0]["threshold"]:
            return (-1, True)

    for idx, bucket in enumerate(between_buckets):
        if bucket["low"] <= temp_f < bucket["high"]:
            return (idx, False)

    return (None, False)


def process_ladder_transition(station, market_type, snapshot, current_temp):
    markets = (snapshot or {}).get("markets") or []
    if not markets:
        return {"should_alert": False, "reason": None}

    event_ticker = markets[0].get("event_ticker")
    if not event_ticker:
        return {"should_alert": False, "reason": None}

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

    return {
        "should_alert": should_alert,
        "reason": reason,
        "bucket_index": bucket_index,
        "direction": direction,
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
):
    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot(normalized_station, market_types)
    markets = snapshot.get("markets", [])
    current_temp_f = (snapshot.get("observed") or {}).get("current_temp_f")
    market_types_list = snapshot.get("market_types", [])

    if not markets:
        return {"ok": False, "reason": "no_markets"}

    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return {"ok": False, "reason": "missing_webhook"}

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
    if "up" in reason_lower:
        direction_up = True
    elif "down" in reason_lower:
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
    if _to_local:
        try:
            local_dt = _to_local(normalized_station, datetime.now(timezone.utc))
            local_time_display = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            local_time_display = "N/A"

    temp_display = "N/A" if current_temp_f is None else f"{float(current_temp_f):.1f}"
    prev_display = "N/A" if prev_temp_f is None else f"{float(prev_temp_f):.1f}"
    now_display = "N/A" if now_temp_f is None else f"{float(now_temp_f):.1f}"
    delta_display = "N/A" if delta_f is None else f"{float(delta_f):+.1f}"
    header = f"{title_emoji} {normalized_station} {market_type or 'WEATHER'} — Ladder Cross {direction_icon}"
    ladder_block = "\n".join(ladder_rows)
    content = (
        f"{header}\n"
        f"Prev: {prev_display}°F\n"
        f"Now: {now_display}°F\n"
        f"Δ: {delta_display}°F\n"
        f"{temp_display}°F  →  Entered {current_label}\n"
        f"Local time: {local_time_display}\n\n"
        f"Event: {event_ticker}\n"
        f"https://kalshi.com/markets/{event_ticker}\n\n"
        "LADDER\n"
        "────────────────────────────────\n"
        f"{ladder_block}\n"
        "────────────────────────────────\n\n"
        f"Next rung: {distance_info}"
    )

    payload = {
        "content": content,
        "embeds": [],
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    if not (200 <= response.status_code < 300):
        return {"ok": False, "reason": "webhook_failed"}

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
        "markets_included": len(markets),
        "observed": current_temp_f,
        "event_ticker": event_ticker,
        "bucket_index": current_index,
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
