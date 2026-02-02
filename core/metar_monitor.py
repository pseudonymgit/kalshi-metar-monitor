import os
import json
import time
import threading
import requests
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

_STATE_LOCK = threading.Lock()
_STATE = {
    "stations": [],
    "last_obs": {},      # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": dict|str} }
    "last_alert": {},    # { ICAO: {"temp_f": float, "at": ISO} }
    "cfg": {},
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()

# ----------------------------
# Config + helpers
# ----------------------------
def get_default_config() -> Dict[str, Any]:
    return {
        "stations": json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]')),
        "poll_seconds": int(os.getenv("METAR_POLL_SECONDS", "60")),
        "delta_f": float(os.getenv("TEMP_ALERT_DELTA_F", "1.0")),
        "webhook": os.getenv("ALERT_WEBHOOK_URL", ""),
        "ingest_secret": os.getenv("ALERT_INGEST_SECRET", ""),
        "cache_file": os.getenv("METAR_CACHE_FILE", "/opt/render/project/src/data/metar_state.json"),
        "autostart": os.getenv("METAR_AUTOSTART", "true").lower() in ("1","true","yes","y"),
        # source order for "auto"
        "source_order": (os.getenv("METAR_SOURCE_ORDER", "nws,tgftp,iem").split(",")),
        # explicit default source if not provided in query
        "default_source": os.getenv("METAR_DEFAULT_SOURCE", "auto"),
        # network headers
        "http_from": os.getenv("HTTP_FROM_EMAIL", "you@example.com"),
        "http_agent": os.getenv("HTTP_USER_AGENT", "KalshiMetarMonitor/1.0 (+you@example.com)"),
        # IEM params
        "iem_hours": int(os.getenv("IEM_LOOKBACK_HOURS", "3")),
    }

def _c_to_f(c: float) -> float:
    return c * 9.0/5.0 + 32.0

def _load_cache(path: str) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_cache(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

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

def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "stations": _STATE["stations"],
            "last_obs": _STATE["last_obs"],
            "last_alert": _STATE["last_alert"],
            "cfg": _STATE["cfg"],
        }

# ----------------------------
# Source: NWS latest JSON
# ----------------------------
def _headers_for_nws(cfg) -> Dict[str, str]:
    return {
        "User-Agent": cfg["http_agent"],
        "From": cfg["http_from"],
        "Accept": "application/geo+json",
    }

def fetch_nws_latest(icao: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"https://api.weather.gov/stations/{icao}/observations/latest"
    r = requests.get(url, headers=_headers_for_nws(cfg), timeout=15)
    r.raise_for_status()
    j = r.json()
    props = j.get("properties", {}) if isinstance(j, dict) else {}
    temp_c = None
    if "temperature" in props and isinstance(props["temperature"], dict):
        val = props["temperature"].get("value")
        if val is not None:
            temp_c = float(val)
    ts = props.get("timestamp")
    if temp_c is None:
        return None
    return {
        "temp_f": round(_c_to_f(temp_c), 1),
        "obs_time": ts or datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "raw": props,
    }

# ----------------------------
# Source: NOAA tgftp (raw METAR text)
# ----------------------------
def _parse_temp_from_metar_line(line: str) -> Optional[float]:
    """
    Parse temperature in C from METAR line groups of the form 'XX/YY' or 'MXX/MYY'.
    Returns temp F if found, else None.
    """
    # Typical raw: "KDEN 012353Z 09004KT 10SM FEW050 12/01 A3012 RMK ..."
    # Find group like 12/01 or M02/M05
    parts = line.split()
    for p in parts:
        if "/" in p and len(p) <= 6:
            left, _, right = p.partition("/")
            # we just need the air temp (left)
            try:
                if left.startswith("M"):
                    c = -float(left[1:])
                else:
                    c = float(left)
                return round(_c_to_f(c), 1)
            except Exception:
                continue
    return None

def fetch_tgftp_latest(icao: str, _cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    txt = r.text.strip().splitlines()
    # Usually two lines: timestamp on line 1; METAR on line 2
    if not txt:
        return None
    ts_line = txt[0].strip()
    metar_line = txt[-1].strip() if len(txt) > 1 else txt[0].strip()
    temp_f = _parse_temp_from_metar_line(metar_line)
    if temp_f is None:
        return None
    # tgftp timestamp is like '2026/02/02 02:53'
    try:
        obs_time = datetime.strptime(ts_line, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        obs_time = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    return {"temp_f": temp_f, "obs_time": obs_time, "raw": metar_line}

# ----------------------------
# Source: IEM ASOS JSON
# ----------------------------
def fetch_iem_latest(icao: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hrs = int(cfg.get("iem_hours", 3))
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={icao}&data=tmpf&tz=UTC&format=json&hours={hrs}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    # Format: {"data":[{"station":"KDEN","valid":"2026-02-02 02:56","tmpf": 28.0}, ...]}
    data = j.get("data", [])
    # pick last with tmpf present
    for row in reversed(data):
        tmpf = row.get("tmpf")
        if tmpf is not None:
            obs_time = row.get("valid")
            # valid is "YYYY-MM-DD HH:MM" (UTC)
            try:
                dt = datetime.strptime(obs_time, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                dt = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            return {"temp_f": round(float(tmpf), 1), "obs_time": dt, "raw": row}
    return None

# ----------------------------
# Unified fetch + public API used by app.py
# ----------------------------
def _fetch_one(icao: str, source: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = source.lower()
    if s == "nws":
        return fetch_nws_latest(icao, cfg)
    if s == "tgftp":
        return fetch_tgftp_latest(icao, cfg)
    if s == "iem":
        return fetch_iem_latest(icao, cfg)
    # auto: try order
    for cand in cfg["source_order"]:
        cand = cand.strip().lower()
        try:
            out = _fetch_one(icao, cand, cfg) if cand != "auto" else None
            if out:
                return out
        except Exception:
            continue
    return None

def fetch_now(stations: List[str], source: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch now for the given ICAO list from the chosen 'source' or auto order.
    """
    ensure_state_loaded()
    cfg = get_default_config()
    use_source = (source or cfg["default_source"] or "auto").lower()
    observations: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for icao in stations:
        try:
            obs = _fetch_one(icao, use_source, cfg)
            observations[icao] = obs
        except Exception as e:
            observations[icao] = None
            errors[icao] = str(e)

    status = "ok" if any(observations.values()) else ("error" if errors else "ok")
    out = {"status": status, "stations": stations, "observations": observations}
    if errors:
        out["errors"] = errors
    return out

def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    if not webhook:
        return
    try:
        requests.post(webhook, json=payload, timeout=10)
    except Exception:
        pass

def _poll_once(logger=None):
    ensure_state_loaded()
    cfg = get_default_config()
    stations = cfg["stations"]
    use_source = (cfg["default_source"] or "auto").lower()

    updates = {}
    alerts = []

    # loop per-station using selected source / auto
    for icao in stations:
        try:
            obs = _fetch_one(icao, use_source, cfg)
        except Exception as e:
            if logger: logger.error(f"poll {_fetch_one.__name__} failed for {icao}: {e}")
            obs = None

        if not obs:
            continue

        temp_f = obs["temp_f"]
        obs_time = obs["obs_time"]

        with _STATE_LOCK:
            prev = _STATE["last_obs"].get(icao)
            _STATE["last_obs"][icao] = {"temp_f": temp_f, "obs_time": obs_time, "raw": obs.get("raw")}
            updates[icao] = _STATE["last_obs"][icao]

            if prev:
                delta = round(temp_f - float(prev.get("temp_f", temp_f)), 1)
                if abs(delta) >= cfg["delta_f"]:
                    alert = {
                        "type": "temp_change",
                        "station": icao,
                        "prev_temp_f": prev["temp_f"],
                        "temp_f": temp_f,
                        "delta_f": delta,
                        "obs_time": obs_time,
                        "at_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                        "source": use_source,
                    }
                    alerts.append(alert)
                    _STATE["last_alert"][icao] = {"temp_f": temp_f, "at": alert["at_utc"]}

    # persist cache
    with _STATE_LOCK:
        cache = {"last_obs": _STATE["last_obs"]}
    _save_cache(cfg["cache_file"], cache)

    for a in alerts:
        _send_alert(cfg["webhook"], a)

    if logger:
        logger.info(f"METAR poll ({use_source}): {len(updates)} stations, {len(alerts)} alerts")

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
