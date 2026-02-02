# core/metar_monitor.py

import os
import json
import time
import threading
import requests
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

# ---- AWC polite headers (required – AWC may 403 without a UA/contact) ----
_AWC_HEADERS = {
    "User-Agent": os.getenv(
        "AWC_USER_AGENT",
        "KalshiMetarMonitor/1.0 (contact: youremail@example.com)"
    )
}

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "stations": [],
    "last_obs": {},      # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": dict} }
    "last_alert": {},    # { ICAO: {"temp_f": float, "at": ISO} }
    "cfg": {},
    "last_poll_utc": None,
    "poll_count": 0,
}

_SCHEDULER_THREAD: Optional[threading.Thread] = None
_SCHEDULER_STOP = threading.Event()


def get_default_config() -> Dict[str, Any]:
    return {
        "stations": json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]')),
        "poll_seconds": int(os.getenv("METAR_POLL_SECONDS", "60")),
        "delta_f": float(os.getenv("TEMP_ALERT_DELTA_F", "1.0")),
        "webhook": os.getenv("ALERT_WEBHOOK_URL", ""),
        "ingest_secret": os.getenv("ALERT_INGEST_SECRET", ""),
        "cache_file": os.getenv("METAR_CACHE_FILE", "/opt/render/project/src/data/metar_state.json"),
        "autostart": os.getenv("METAR_AUTOSTART", "true").lower() in ("1","true","yes","y"),
        # how far back to look when requesting "mostRecent"
        "hours_before_now": float(os.getenv("METAR_HOURS_BEFORE_NOW", "2")),
    }


def _awc_metar_url(stations: List[str], hours_before_now: float) -> str:
    station_str = ",".join(stations)
    base = "https://aviationweather.gov/adds/dataserver_current/httpparam"
    params = (
        "dataSource=metars&requestType=retrieve&format=JSON"
        f"&stationString={station_str}&hoursBeforeNow={hours_before_now}&mostRecent=true"
    )
    return f"{base}?{params}"


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


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
    except Exception:
        # path may be in CWD; ignore
        pass
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
            _STATE["stations"] = list(cfg["stations"])
        cache = _load_cache(cfg["cache_file"])
        if "last_obs" in cache:
            _STATE["last_obs"].update(cache["last_obs"])
        if "stations" in cache and not _STATE["stations"]:
            _STATE["stations"] = cache["stations"]


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "stations": list(_STATE["stations"]),
            "last_obs": dict(_STATE["last_obs"]),
            "last_alert": dict(_STATE["last_alert"]),
            "cfg": dict(_STATE["cfg"]),
            "last_poll_utc": _STATE["last_poll_utc"],
            "poll_count": _STATE["poll_count"],
            "scheduler_running": is_scheduler_running(),
        }


def fetch_now(stations: List[str]) -> Dict[str, Any]:
    """Fetch METARs for the given stations (does not mutate state)."""
    ensure_state_loaded()
    cfg = get_default_config()
    url = _awc_metar_url(stations, cfg["hours_before_now"])
    try:
        r = requests.get(url, timeout=15, headers=_AWC_HEADERS)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}).get("METAR", [])
        out = {}
        for m in data:
            icao = (m.get("station_id") or "").upper()
            temp_c = m.get("temp_c")
            obs_time = m.get("observation_time")
            if icao and temp_c is not None:
                out[icao] = {
                    "temp_f": round(_c_to_f(float(temp_c)), 1),
                    "obs_time": obs_time,
                    "raw": m,
                }
        return {"status": "ok", "stations": stations, "observations": out}
    except Exception as e:
        return {"status": "error", "error": str(e), "stations": stations}


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
    stations = list(cfg["stations"]) if _STATE["stations"] == [] else list(_STATE["stations"])
    url = _awc_metar_url(stations, cfg["hours_before_now"])
    try:
        r = requests.get(url, timeout=15, headers=_AWC_HEADERS)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}).get("METAR", [])
    except Exception as e:
        if logger:
            logger.error(f"METAR poll failed: {e}")
        return

    updates = {}
    alerts: List[Dict[str, Any]] = []

    with _STATE_LOCK:
        for m in data:
            icao = (m.get("station_id") or "").upper()
            temp_c = m.get("temp_c")
            obs_time = m.get("observation_time")
            if not icao or temp_c is None:
                continue

            temp_f = round(_c_to_f(float(temp_c)), 1)
            prev = _STATE["last_obs"].get(icao)
            _STATE["last_obs"][icao] = {"temp_f": temp_f, "obs_time": obs_time, "raw": m}
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
                    }
                    alerts.append(alert)
                    _STATE["last_alert"][icao] = {"temp_f": temp_f, "at": alert["at_utc"]}

        _STATE["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        _STATE["poll_count"] = int(_STATE.get("poll_count", 0)) + 1

        cache = {
            "last_obs": _STATE["last_obs"],
            "stations": _STATE["stations"],
        }
        _save_cache(cfg["cache_file"], cache)

    for a in alerts:
        _send_alert(cfg["webhook"], a)

    if logger:
        logger.info(f"METAR poll: {len(updates)} stations, {len(alerts)} alerts")


def _scheduler_loop(logger, interval_sec: int):
    while not _SCHEDULER_STOP.is_set():
        _poll_once(logger)
        _SCHEDULER_STOP.wait(interval_sec)


def start_scheduler(logger, cfg=None) -> bool:
    """Start background poller; idempotent."""
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


# -----------------------------
# NEW: API helpers for app.py
# -----------------------------

def get_latest_metar(icao: str) -> Dict[str, Any]:
    """Return the latest temp for a single ICAO; fetch on-demand and update state."""
    if not icao:
        return {"error": "Missing ICAO"}
    icao = icao.upper().strip()
    ensure_state_loaded()

    # Try on-demand fetch for freshness
    res = fetch_now([icao])
    if res.get("status") == "ok":
        obs = res["observations"].get(icao)
        if obs:
            with _STATE_LOCK:
                _STATE["last_obs"][icao] = obs
                _STATE["last_poll_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            return {"icao": icao, "temp_f": obs["temp_f"], "obs_time": obs["obs_time"], "raw": obs["raw"]}

    # Fallback to cached state if API returns nothing
    with _STATE_LOCK:
        cached = _STATE["last_obs"].get(icao)
    if cached:
        return {"icao": icao, "temp_f": cached["temp_f"], "obs_time": cached["obs_time"], "raw": cached["raw"]}

    return {"error": res.get("error") or "No observation available", "icao": icao}


def set_watchlist(icaos: List[str]) -> Dict[str, Any]:
    """Replace the global watchlist."""
    if not icaos or not isinstance(icaos, list):
        return {"error": "icaos must be a non-empty list"}
    icaos = [s.upper().strip() for s in icaos if s and isinstance(s, str)]
    if not icaos:
        return {"error": "no valid ICAOs provided"}
    ensure_state_loaded()
    with _STATE_LOCK:
        _STATE["stations"] = icaos
        # persist
        cache = {
            "last_obs": _STATE["last_obs"],
            "stations": _STATE["stations"],
        }
        _save_cache(_STATE["cfg"]["cache_file"], cache)
    return {"ok": True, "watchlist": icaos, "count": len(icaos)}


def get_watchlist() -> Dict[str, Any]:
    ensure_state_loaded()
    with _STATE_LOCK:
        wl = list(_STATE["stations"])
    return {"watchlist": wl, "count": len(wl)}


def get_metrics() -> Dict[str, Any]:
    st = get_state()
    return {
        "scheduler_running": st["scheduler_running"],
        "watchlist_size": st["stations"] and len(st["stations"]) or 0,
        "poll_count": st["poll_count"],
        "last_poll_utc": st["last_poll_utc"],
        "obs_count": len(st["last_obs"]),
    }
