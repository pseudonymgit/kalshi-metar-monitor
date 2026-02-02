# core/metar_monitor.py
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
    "last_obs": {},      # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": dict} }
    "last_alert": {},    # { ICAO: {"temp_f": float, "at": ISO} }
    "cfg": {},
    "metrics": {"poll_count": 0, "last_poll_utc": None},
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()

# ─────────────────────────────────────────
# Config / helpers
# ─────────────────────────────────────────
def get_default_config() -> Dict[str, Any]:
    return {
        "stations": json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]')),
        "poll_seconds": int(os.getenv("METAR_POLL_SECONDS", "60")),
        "delta_f": float(os.getenv("TEMP_ALERT_DELTA_F", "1.0")),
        "webhook": os.getenv("ALERT_WEBHOOK_URL", ""),
        "ingest_secret": os.getenv("ALERT_INGEST_SECRET", ""),
        "cache_file": os.getenv("METAR_CACHE_FILE", "/opt/render/project/src/data/metar_state.json"),
        "autostart": os.getenv("METAR_AUTOSTART", "true").lower() in ("1","true","yes","y"),
    }

def _awc_headers() -> Dict[str, str]:
    # AWC expects a real UA + contact; adding From is recommended.
    ua = os.getenv("AWC_USER_AGENT") or "KalshiMetarMonitor/1.0 (+contact: daniel.gabriel@gmail.com)"
    from_email = os.getenv("AWC_FROM_EMAIL", "daniel.gabriel@gmail.com")
    return {
        "User-Agent": ua,
        "From": from_email,
        "Accept": "application/json",
        "Connection": "close",
    }

def _awc_station_url(icao: str, hours: int = 2) -> str:
    # One-station (friendlier), JSON format:
    # https://aviationweather.gov/adds/dataserver_current/httpparam
    return (
        "https://aviationweather.gov/adds/dataserver_current/httpparam"
        f"?dataSource=metars&requestType=retrieve&format=JSON"
        f"&stationString={icao}&hoursBeforeNow={float(hours)}&mostRecent=true"
    )

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
            "metrics": _STATE["metrics"],
        }

# ─────────────────────────────────────────
# AWC (aviationweather.gov) fetch
# ─────────────────────────────────────────
def _parse_awc_response(j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # AWC JSON shape: { data: { METAR: [ { station_id, temp_c, observation_time, ... } ] } }
    try:
        arr = (j or {}).get("data", {}).get("METAR", [])
        if not arr:
            return None
        m = arr[0]
        temp_c = m.get("temp_c")
        obs_time = m.get("observation_time")
        if temp_c is None or obs_time is None:
            return None
        return {
            "temp_f": round(_c_to_f(float(temp_c)), 1),
            "obs_time": obs_time,
            "raw": m
        }
    except Exception:
        return None

def _fetch_latest_awc(icao: str, hours_back: int = 2) -> Dict[str, Any]:
    url = _awc_station_url(icao, hours_back)
    try:
        r = requests.get(url, headers=_awc_headers(), timeout=15)
        r.raise_for_status()
        obs = _parse_awc_response(r.json())
        if not obs:
            return {"status": "ok", "icao": icao, "observation": None}
        return {"status": "ok", "icao": icao, "observation": obs}
    except Exception as e:
        return {"status": "error", "icao": icao, "error": str(e)}

def fetch_now(icaos: List[str]) -> Dict[str, Any]:
    """Batch fetch by iterating one-station calls (AWC-friendly)."""
    ensure_state_loaded()
    observations = {}
    for i, icao in enumerate(icaos):
        res = _fetch_latest_awc(icao)
        if res.get("status") == "ok" and res.get("observation"):
            observations[icao] = res["observation"]
            with _STATE_LOCK:
                _STATE["last_obs"][icao] = res["observation"]
        else:
            observations[icao] = None
        # small polite delay between stations
        time.sleep(0.25)
    with _STATE_LOCK:
        _STATE["metrics"]["poll_count"] += 1
        _STATE["metrics"]["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        _save_cache(_STATE["cfg"]["cache_file"], {"last_obs": _STATE["last_obs"]})
    return {"status": "ok", "stations": icaos, "observations": observations}

def get_latest_metar(icao: str) -> Dict[str, Any]:
    """Single-station latest via AWC. Always fetch fresh (effectively live)."""
    ensure_state_loaded()
    res = _fetch_latest_awc(icao)
    if res.get("status") == "ok" and res.get("observation"):
        with _STATE_LOCK:
            _STATE["last_obs"][icao] = res["observation"]
            _STATE["metrics"]["poll_count"] += 1
            _STATE["metrics"]["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            _save_cache(_STATE["cfg"]["cache_file"], {"last_obs": _STATE["last_obs"]})
        return res["observation"]
    return {"error": res.get("error", "no data"), "icao": icao}

# ─────────────────────────────────────────
# Alerts & watchlist
# ─────────────────────────────────────────
def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    if not webhook:
        return
    try:
        requests.post(
            webhook,
            json=payload,
            timeout=10,
            headers={"User-Agent": os.getenv("AWC_USER_AGENT", "KalshiMetarMonitor/1.0 (+contact: daniel.gabriel@gmail.com)")}
        )
    except Exception:
        pass

def set_watchlist(icaos: List[str]) -> Dict[str, Any]:
    if not icaos or not isinstance(icaos, list):
        return {"error": "POST JSON must include non-empty 'icaos' list"}
    cleaned = sorted({x.strip().upper() for x in icaos if x and isinstance(x, str)})
    with _STATE_LOCK:
        _STATE["stations"] = cleaned
        _STATE["cfg"]["stations"] = cleaned
        _save_cache(_STATE["cfg"]["cache_file"], {"last_obs": _STATE["last_obs"]})
    return {"ok": True, "watchlist": {"count": len(cleaned), "watchlist": cleaned}}

def get_watchlist() -> Dict[str, Any]:
    ensure_state_loaded()
    with _STATE_LOCK:
        wl = list(_STATE["stations"])
    return {"count": len(wl), "watchlist": wl}

def get_metrics() -> Dict[str, Any]:
    ensure_state_loaded()
    with _STATE_LOCK:
        m = dict(_STATE["metrics"])
        m["watchlist_size"] = len(_STATE["stations"])
    return m

# ─────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────
def _poll_once(logger=None):
    ensure_state_loaded()
    cfg = get_default_config()
    stations = cfg["stations"]

    alerts = []
    for icao in stations:
        res = _fetch_latest_awc(icao)
        if res.get("status") != "ok" or not res.get("observation"):
            continue
        obs = res["observation"]
        with _STATE_LOCK:
            prev = _STATE["last_obs"].get(icao)
            _STATE["last_obs"][icao] = obs
            if prev:
                delta = round(obs["temp_f"] - float(prev.get("temp_f", obs["temp_f"])), 1)
                if abs(delta) >= cfg["delta_f"]:
                    alert = {
                        "type": "temp_change",
                        "station": icao,
                        "prev_temp_f": prev["temp_f"],
                        "temp_f": obs["temp_f"],
                        "delta_f": delta,
                        "obs_time": obs["obs_time"],
                        "at_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                    }
                    alerts.append(alert)
                    _STATE["last_alert"][icao] = {"temp_f": obs["temp_f"], "at": alert["at_utc"]}
        time.sleep(0.25)  # be nice

    with _STATE_LOCK:
        _STATE["metrics"]["poll_count"] += 1
        _STATE["metrics"]["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        _save_cache(cfg["cache_file"], {"last_obs": _STATE["last_obs"]})

    for a in alerts:
        _send_alert(cfg["webhook"], a)
    if logger:
        logger.info(f"AWC poll: {len(stations)} stations, {len(alerts)} alerts")

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
