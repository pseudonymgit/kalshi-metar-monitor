# core/metar_monitor.py

import os
import json
import threading
import requests
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

# =========================
# In-memory state
# =========================
_STATE_LOCK = threading.Lock()
_STATE = {
    "stations": [],
    "last_obs": {},      # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": dict|str, "source": str} }
    "last_alert": {},    # { ICAO: {"temp_f": float, "at": ISO} }
    "cfg": {},
    "poll_count": 0,
    "last_poll_utc": None,
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()

# =========================
# Config
# =========================
def get_default_config() -> Dict[str, Any]:
    return {
        "stations": json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]')),
        "poll_seconds": int(os.getenv("METAR_POLL_SECONDS", "60")),
        "delta_f": float(os.getenv("TEMP_ALERT_DELTA_F", "1.0")),
        "webhook": os.getenv("ALERT_WEBHOOK_URL", ""),
        "cache_file": os.getenv("METAR_CACHE_FILE", "/opt/render/project/src/data/metar_state.json"),
        # Source control
        # Default source if caller doesn’t specify ?source=...
        "default_source": os.getenv("METAR_DEFAULT_SOURCE", "nws").lower(),
        # If strict is true, DO NOT fall back to other sources automatically.
        "strict": os.getenv("METAR_STRICT", "true").lower() in ("1", "true", "yes", "y"),
        # Friendly headers for NWS
        "http_from": os.getenv("HTTP_FROM_EMAIL", "you@example.com"),
        "http_agent": os.getenv("HTTP_USER_AGENT", "KalshiMetarMonitor/1.0 (+you@example.com)"),
        # IEM lookback (hours) for last obs
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
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Best effort cache
        pass

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
            "stations": list(_STATE["stations"]),
            "last_obs": dict(_STATE["last_obs"]),
            "last_alert": dict(_STATE["last_alert"]),
            "cfg": dict(_STATE["cfg"]),
            "poll_count": _STATE["poll_count"],
            "last_poll_utc": _STATE["last_poll_utc"],
        }

# =========================
# Sources: NWS, tgftp, IEM
# =========================
def _headers_for_nws(cfg) -> Dict[str, str]:
    return {
        "User-Agent": cfg["http_agent"],
        "From": cfg["http_from"],
        "Accept": "application/geo+json",
    }

def _fetch_nws_latest(icao: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # api.weather.gov requires a real UA + From
    url = f"https://api.weather.gov/stations/{icao}/observations/latest"
    r = requests.get(url, headers=_headers_for_nws(cfg), timeout=15)
    r.raise_for_status()
    j = r.json()
    props = j.get("properties", {}) if isinstance(j, dict) else {}
    val = props.get("temperature", {}).get("value")
    if val is None:
        return None
    temp_f = round(_c_to_f(float(val)), 1)
    ts = props.get("timestamp") or datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    return {"temp_f": temp_f, "obs_time": ts, "raw": props, "source": "nws"}

def _parse_temp_from_metar_line(line: str) -> Optional[float]:
    # Try group like 12/01 or M02/M05
    for token in line.split():
        if "/" in token and len(token) <= 6:
            left, _, _right = token.partition("/")
            try:
                c = -float(left[1:]) if left.startswith("M") else float(left)
                return round(_c_to_f(c), 1)
            except Exception:
                continue
    return None

def _fetch_tgftp_latest(icao: str) -> Optional[Dict[str, Any]]:
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if not lines:
        return None
    ts_line = lines[0].strip()
    metar_line = lines[-1].strip()
    temp_f = _parse_temp_from_metar_line(metar_line)
    if temp_f is None:
        return None
    try:
        obs_time = datetime.strptime(ts_line, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        obs_time = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    return {"temp_f": temp_f, "obs_time": obs_time, "raw": metar_line, "source": "tgftp"}

def _fetch_iem_latest(icao: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hrs = int(cfg.get("iem_hours", 3))
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={icao}&data=tmpf&tz=UTC&format=json&hours={hrs}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", [])
    for row in reversed(data):
        tmpf = row.get("tmpf")
        if tmpf is not None:
            valid = row.get("valid")
            try:
                dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                dt = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            return {"temp_f": round(float(tmpf), 1), "obs_time": dt, "raw": row, "source": "iem"}
    return None

# =========================
# Fetch entry points
# =========================
def _fetch_one(icao: str, source: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = (source or cfg["default_source"] or "nws").lower()
    if s == "nws":
        return _fetch_nws_latest(icao, cfg)
    if s == "tgftp":
        return _fetch_tgftp_latest(icao)
    if s == "iem":
        return _fetch_iem_latest(icao, cfg)
    # Unknown source => None (strict, no fallback)
    return None

def fetch_now(stations: List[str], source: Optional[str] = None) -> Dict[str, Any]:
    """
    Strict: if a source is given (or defaulted), we only use that source—no fallback.
    """
    ensure_state_loaded()
    cfg = get_default_config()
    chosen = (source or cfg["default_source"] or "nws").lower()

    observations: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for icao in stations:
        try:
            obs = _fetch_one(icao, chosen, cfg)
            observations[icao] = obs
            # Save to cache if we got a value
            if obs:
                with _STATE_LOCK:
                    _STATE["last_obs"][icao] = obs
        except Exception as e:
            observations[icao] = None
            errors[icao] = str(e)

    # Persist last_obs (best effort)
    with _STATE_LOCK:
        _save_cache(cfg["cache_file"], {"last_obs": _STATE["last_obs"]})

    out = {"status": "ok", "stations": stations, "observations": observations, "source": chosen}
    if errors:
        out["errors"] = errors
        # still ok; caller decides what to do per-station
    return out

def get_latest_metar(icao: str, source: Optional[str] = None) -> Dict[str, Any]:
    res = fetch_now([icao], source=source)
    obs = res.get("observations", {}).get(icao)
    if obs:
        return {"icao": icao, "source": res.get("source"), **obs}
    # Return error + carry source choice
    err = res.get("errors", {}).get(icao)
    return {"icao": icao, "source": res.get("source"), "error": err or "no observation"}

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
        return {
            "last_poll_utc": _STATE["last_poll_utc"],
            "poll_count": _STATE["poll_count"],
            "watchlist_size": len(_STATE["stations"] or get_default_config()["stations"]),
        }

# =========================
# Scheduler
# =========================
def _poll_once(logger=None):
    ensure_state_loaded()
    cfg = get_default_config()
    stations = _STATE["stations"] or cfg["stations"]
    chosen = cfg["default_source"] or "nws"

    for icao in stations:
        try:
            obs = _fetch_one(icao, chosen, cfg)
            if obs:
                with _STATE_LOCK:
                    _STATE["last_obs"][icao] = obs
        except Exception as e:
            if logger:
                logger.error(f"poll failed for {icao} ({chosen}): {e}")

    with _STATE_LOCK:
        _STATE["poll_count"] += 1
        _STATE["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        _save_cache(cfg["cache_file"], {"last_obs": _STATE["last_obs"]})

    if logger:
        logger.info(f"METAR poll ({chosen}): stations={len(stations)}")

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
