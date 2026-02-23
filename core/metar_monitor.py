# core/metar_monitor.py

import os
import json
import csv
import math
import threading
import requests
from io import StringIO
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

# zoneinfo (Python 3.9+). If unavailable, we'll no-op ET/local conversions.
try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None

# -------- Constants --------
ET_TZ_NAME = "America/New_York"
OVERLAP_SECONDS = 120               # small overlap to avoid missing late arrivals
FIRST_RUN_CUSHION_SEC = 300         # first contact: add 5 min cushion

# Station → local timezone name (expand as you add stations)
_ICAO_TZ = {
    "KDEN": "America/Denver",
    "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago",
    "KAUS": "America/Chicago",
    "KMIA": "America/New_York",
    "KPHL": "America/New_York",
    "KNYC": "America/New_York",  # replace with actual ICAO if you change source
}


def _icao_tz_name(icao: str) -> str:
    return _ICAO_TZ.get(icao.upper(), "America/New_York")


# =========================
# In-memory state
# =========================
_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "stations": [],
    "last_obs": {},              # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": any, "source": str} }
    "last_alert": {},            # (kept for compatibility; not used by int-cross logic)
    "last_seen_iso": {},         # { ICAO: ISO of latest obs we ingested }
    "last_reset_date_local": {}, # { ICAO: "YYYY-MM-DD" } daily local reset marker
    "last_observed_integer": {}, # { ICAO: int } last observed floored integer temperature
    "cfg": {},
    "poll_count": 0,
    "last_poll_utc": None,
    "timeout_count": 0,
    "last_timeout_station": None,
    "last_timeout_utc": None,
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()


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
    if ZoneInfo is None:
        return dt_utc
    return dt_utc.astimezone(ZoneInfo(_icao_tz_name(icao)))


def _within_alert_window_local(icao: str, dt_iso: str) -> bool:
    """True only if station local hour is in [11, 19)."""
    dt_utc = _parse_iso(dt_iso)
    dt_local = _to_local(icao, dt_utc)
    return 11 <= dt_local.hour < 19


def _maybe_daily_reset_local(icao: str, dt_iso: str) -> None:
    """Reset integer-cross memory once per *local* day for this station."""
    dt_utc = _parse_iso(dt_iso)
    dt_local = _to_local(icao, dt_utc)
    local_day = dt_local.date().isoformat()
    with _STATE_LOCK:
        last = _STATE["last_reset_date_local"].get(icao)
        if last != local_day:
            _STATE["last_observed_integer"].pop(icao, None)
            _STATE["last_reset_date_local"][icao] = local_day


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
        if "last_obs" in cache:
            _STATE["last_obs"].update(cache["last_obs"])
        if "last_seen_iso" in cache:
            _STATE["last_seen_iso"].update(cache["last_seen_iso"])

        # Persist daily-reset + last observed integer across restarts
        if "last_reset_date_local" in cache:
            _STATE["last_reset_date_local"].update(cache["last_reset_date_local"])
        if "last_observed_integer" in cache:
            _STATE["last_observed_integer"].update(cache["last_observed_integer"])


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "stations": list(_STATE["stations"]),
            "last_obs": dict(_STATE["last_obs"]),
            "last_alert": dict(_STATE["last_alert"]),
            "last_seen_iso": dict(_STATE["last_seen_iso"]),
            "last_reset_date_local": dict(_STATE["last_reset_date_local"]),
            "last_observed_integer": dict(_STATE["last_observed_integer"]),
            "cfg": dict(_STATE["cfg"]),
            "poll_count": _STATE["poll_count"],
            "last_poll_utc": _STATE["last_poll_utc"],
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


def _nws_range_url(icao: str, start_iso_z: str, end_iso_z: str, limit: int = 200) -> str:
    return (
        f"https://api.weather.gov/stations/{icao}/observations"
        f"?start={start_iso_z}&end={end_iso_z}&limit={min(limit, 200)}"
    )


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
    url = _nws_range_url(icao, start_iso_z, end_iso_z)
    try:
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    except requests.exceptions.Timeout:
        _record_timeout(icao)
        r = requests.get(url, headers=_headers_for_nws(cfg), timeout=10)
    r.raise_for_status()
    return _parse_nws_collection(r.json())


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
def _ingest_obs(icao: str, new_obs: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[int, int]:
    """
    Ingests observations in chronological order.
    Returns (ingested_count, alerts_count).
    """
    if not new_obs:
        return (0, 0)

    with _STATE_LOCK:
        last_seen_iso = _STATE["last_seen_iso"].get(icao)
        last_temp = _STATE["last_obs"].get(icao, {}).get("temp_f")

    ingested = 0
    alerts = 0

    for obs in new_obs:
        ts = obs["obs_time"]
        if last_seen_iso and _parse_iso(ts) <= _parse_iso(last_seen_iso):
            continue

        # store
        with _STATE_LOCK:
            _STATE["last_obs"][icao] = obs
            _STATE["last_seen_iso"][icao] = ts

        ingested += 1

        # integer-boundary alerting (both directions), only during local 11–19
        now_f = float(obs["temp_f"])
        prev_f = float(last_temp) if last_temp is not None else now_f

        # daily reset keyed to local date
        _maybe_daily_reset_local(icao, ts)

        curr_floor = int(math.floor(now_f))
        with _STATE_LOCK:
            last_observed_integer = _STATE["last_observed_integer"].get(icao)

        if (
            _within_alert_window_local(icao, ts)
            and last_observed_integer is not None
            and curr_floor != last_observed_integer
        ):
            d = round(now_f - prev_f, 1)
            _emit_alert(
                icao,
                prev_f=prev_f,
                now_f=now_f,
                delta_f=d,
                obs_time=ts,
                cfg=cfg,
            )
            alerts += 1

        with _STATE_LOCK:
            _STATE["last_observed_integer"][icao] = curr_floor

        last_temp = obs["temp_f"]

    with _STATE_LOCK:
        _save_cache(cfg["cache_file"], {
            "last_obs": _STATE["last_obs"],
            "last_seen_iso": _STATE["last_seen_iso"],
            "last_reset_date_local": _STATE["last_reset_date_local"],
            "last_observed_integer": _STATE["last_observed_integer"],
        })

    return (ingested, alerts)


def _emit_alert(icao: str, prev_f: float, now_f: float, delta_f: float, obs_time: str, cfg: Dict[str, Any]) -> None:
    payload = {
        "type": "temp_change",
        "station": icao,
        "prev_temp_f": prev_f,
        "temp_f": now_f,
        "delta_f": delta_f,
        "obs_time": obs_time,
        "at_utc": _now_utc_iso(),
    }
    _send_alert(cfg.get("webhook", ""), payload)


def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    print(f"[DEBUG] Alert triggered station={station} tf={tf}")
    if not webhook:
        return
    try:
        station = (payload.get("station") or "UNK").upper()
        tf = payload.get("temp_f")
        pf = payload.get("prev_temp_f")
        df = payload.get("delta_f")
        ts_utc = payload.get("obs_time")

        # Station-local timestamp for display
        ts_local = _iso_to_tz(ts_utc, _icao_tz_name(station))

        if tf is not None:
            try:
                from core.kalshi_monitor import (
                    _get_active_stations,
                    build_structured_snapshot,
                    process_ladder_transition,
                    send_composed_weather_market_alert,
                )

                active = _get_active_stations()
                station_is_active = (
                    active is None or station in active
                )
                ladder_present = False
                composed_sent = False

                if station_is_active:
                    for market_type_token in ["HIGH", "LOW"]:
                        snapshot = build_structured_snapshot(station, {market_type_token})
                        markets = snapshot.get("markets") or []

                        if not markets:
                            continue

                        ladder_present = True

                        transition = process_ladder_transition(
                            station=station,
                            market_type=market_type_token,
                            snapshot=snapshot,
                            current_temp=tf,
                        )

                        if transition.get("should_alert"):
                            result = send_composed_weather_market_alert(
                                station=station,
                                market_types={market_type_token},
                                transition_reason=transition.get("reason"),
                            )
                            if result and result.get("ok"):
                                composed_sent = True

                if ladder_present and composed_sent:
                    # Suppress raw integer METAR alert if ladder markets exist
                    return
            except Exception as e:
                print(f"[ERROR] station={station} function=_send_alert: {e}")

        if "discord.com/api/webhooks" in webhook:
            content = (
                f"**Temp Integer Cross** — {station}: "
                f"{int(math.floor(float(pf)))} → {int(math.floor(float(tf)))} "
                f"(now {tf}°F, Δ {df:+}) @ {ts_local}"
            )
            body = {
                "content": content,
                "embeds": [{
                    "title": f"{station} Temperature Integer Crossing",
                    "fields": [
                        {"name": "Prev °F", "value": str(pf), "inline": True},
                        {"name": "Now °F",  "value": str(tf), "inline": True},
                        {"name": "Δ °F",    "value": f"{df:+}", "inline": True},
                        {"name": "Obs (local)", "value": str(ts_local), "inline": False},
                        {"name": "Obs (UTC)",   "value": str(ts_utc),   "inline": False},
                    ],
                    "timestamp": payload.get("at_utc"),
                    "footer": {"text": "METAR monitor"},
                }]
            }
            response = requests.post(webhook, json=body, timeout=10)
        else:
            response = requests.post(webhook, json=payload, timeout=10)
    except Exception as e:
        print(f"[ERROR] station={payload.get('station') or 'UNK'} function=_send_alert: {e}")


# =========================
# Window + fetch routers (STRICT: no auto-fallback)
# =========================
def _compute_window(icao: str, minutes: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None):
    """
    Compute a rolling start/end window in UTC.
    - If we've seen this ICAO, start from (last_seen - OVERLAP_SECONDS).
    - If first run, use now - lookback_min - FIRST_RUN_CUSHION_SEC.
    Returns: (start_iso_z, end_iso_z, start_dt, end_dt)
    """
    if cfg is None:
        cfg = get_default_config()
    lookback = int(minutes if minutes is not None else cfg.get("lookback_min", 3))

    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    with _STATE_LOCK:
        last_seen = _STATE["last_seen_iso"].get(icao)

    if last_seen:
        start_dt = _parse_iso(last_seen) - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start_dt = now - timedelta(minutes=lookback) - timedelta(seconds=FIRST_RUN_CUSHION_SEC)

    end_dt = now
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

    try:
        obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
        ing, al = _ingest_obs(icao, obs_list, cfg)
        with _STATE_LOCK:
            latest = _STATE["last_obs"].get(icao)
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
        poll_count = _STATE["poll_count"]
        watch_ct = len(_STATE["stations"] or get_default_config()["stations"])
        timeout_count = _STATE["timeout_count"]
        last_timeout_station = _STATE["last_timeout_station"]
        last_timeout_utc = _STATE["last_timeout_utc"]
    return {
        "last_poll_utc": last_poll_utc,
        "last_poll_et": _iso_to_tz(last_poll_utc, ET_TZ_NAME),
        "poll_count": poll_count,
        "watchlist_size": watch_ct,
        "timeout_count": timeout_count,
        "last_timeout_station": last_timeout_station,
        "last_timeout_utc": last_timeout_utc,
    }


# =========================
# Scheduler
# =========================
def _poll_once(logger=None):
    ensure_state_loaded()
    cfg = get_default_config()
    stations = _STATE["stations"] or cfg["stations"]
    chosen = cfg["default_source"] or "nws"

    total_ing = 0
    total_alerts = 0
    for icao in stations:
        try:
            start_iso, end_iso, start_dt, end_dt = _compute_window(icao, cfg.get("lookback_min", 3), cfg)
            obs_list = _fetch_range_strict(icao, chosen, start_iso, end_iso, start_dt, end_dt, cfg)
            ing, al = _ingest_obs(icao, obs_list, cfg)
            total_ing += ing
            total_alerts += al
        except Exception as e:
            if logger:
                logger.error(f"poll failed for {icao} ({chosen}): {e}")

    with _STATE_LOCK:
        _STATE["poll_count"] += 1
        _STATE["last_poll_utc"] = _now_utc_iso()
        _save_cache(cfg["cache_file"], {
            "last_obs": _STATE["last_obs"],
            "last_seen_iso": _STATE["last_seen_iso"],
            "last_reset_date_local": _STATE["last_reset_date_local"],
            "last_observed_integer": _STATE["last_observed_integer"],
        })

    if logger:
        logger.info(f"METAR poll ({chosen}): stations={len(stations)} ingested={total_ing} alerts={total_alerts}")


def _scheduler_loop(logger, interval_sec: int):
    while not _SCHEDULER_STOP.is_set():
        _poll_once(logger)
        _SCHEDULER_STOP.wait(interval_sec)


def start_scheduler(logger, cfg=None) -> bool:
    global _SCHEDULER_THREAD
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


def stop_scheduler() -> bool:
    if not _SCHEDULER_THREAD:
        return True
    _SCHEDULER_STOP.set()
    return True


def is_scheduler_running() -> bool:
    return _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive()
