import base64
import os
import re
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


def _station_local_kalshi_date_token(station):
    now_utc = datetime.now(timezone.utc)

    if _to_local:
        try:
            now_local = _to_local(station, now_utc)
        except Exception:
            now_local = now_utc
    else:
        now_local = now_utc

    return now_local.strftime("%d%b%y").upper()


def _filter_structured_markets(markets, station, market_types):
    normalized_station = (station or "").strip().upper()
    city_token = _STATION_CITY_TOKEN_MAP.get(normalized_station)

    if not city_token:
        return []

    date_token = _station_local_kalshi_date_token(normalized_station)

    filtered = []
    for market in markets:
        ticker = market.get("ticker") or ""

        if market.get("status") != "open":
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

    filtered_markets = _filter_structured_markets(
        _get_all_public_markets(),
        normalized_station,
        selected_types,
    )

    markets = []
    for market in filtered_markets:
        ticker = market.get("ticker") or ""
        strike = _extract_strike_from_ticker(ticker)
        if strike is None:
            continue

        markets.append(
            {
                "ticker": ticker,
                "strike": strike,
                "last_price": market.get("last_price"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
            }
        )

    markets.sort(key=lambda x: x["strike"])

    observed_value = None
    if get_metar_state:
        try:
            observed_value = (
                (get_metar_state().get("last_obs") or {})
                .get(normalized_station, {})
                .get("temp_f")
            )
        except Exception:
            observed_value = None

    return {
        "station": normalized_station,
        "market_types": sorted(selected_types) if selected_types else ["HIGH", "LOW"],
        "observed": {
            "current_temp_f": observed_value,
        },
        "markets": markets,
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


def send_composed_weather_market_alert(station: str, market_types: set):
    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot(normalized_station, market_types)
    markets = snapshot.get("markets", [])
    current_temp_f = (snapshot.get("observed") or {}).get("current_temp_f")
    market_types_list = snapshot.get("market_types", [])

    if not markets:
        return {"ok": False, "reason": "no_markets"}

    if current_temp_f is not None:
        import math

        threshold = int(math.floor(float(current_temp_f)))
        filtered_markets = []

        for market in markets:
            strike = market.get("strike")
            ticker = market.get("ticker") or ""

            if strike is None:
                continue

            if "HIGH" in market_types_list and "HIGH" in ticker:
                if strike < threshold:
                    continue

            if "LOW" in market_types_list and "LOW" in ticker:
                if strike > threshold:
                    continue

            filtered_markets.append(market)

        markets = filtered_markets

        if not markets:
            return {"ok": False, "reason": "no_markets_after_filter"}

    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return {"ok": False, "reason": "missing_webhook"}

    ladder_lines = []
    for market in markets:
        strike = market.get("strike")
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        ladder_lines.append(f"{strike}°F → YES {yes_bid} / {yes_ask}")

    ladder_text = "\n".join(ladder_lines)
    if len(ladder_text) > 1000:
        ladder_text = ladder_text[:1000] + "\n… (truncated)"

    payload = {
        "content": None,
        "embeds": [
            {
                "title": f"{normalized_station} Weather Market Snapshot",
                "fields": [
                    {
                        "name": "Observed Temp",
                        "value": str(current_temp_f) if current_temp_f is not None else "N/A",
                    },
                    {
                        "name": "Ladder",
                        "value": ladder_text,
                    },
                ],
                "footer": {
                    "text": "Kalshi Monitor (Public Mode)",
                },
            }
        ],
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    if not (200 <= response.status_code < 300):
        return {"ok": False, "reason": "webhook_failed"}

    return {
        "ok": True,
        "markets_included": len(markets),
        "observed": current_temp_f,
    }


def check_public_market_changes(limit=5):
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

        return {
            "markets_checked": len(markets),
            "changes_detected": 0,
            "alerts_sent": 0,
        }

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

    return {
        "markets_checked": len(markets),
        "changes_detected": changes_detected,
        "alerts_sent": alerts_sent,
    }
