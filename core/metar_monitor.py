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
    "stations": [],       # List[str]
    "last_obs": {},       # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": dict} }
    "last_alert": {},     # { ICAO: {"temp_f": float, "at": ISO} }
    "cfg": {},            # config snapshot
}

# Simple in-memory metrics
_METRICS = {
    "requests_ok": 0,
    "requests_err": 0,
    "last_ok_at": None,     # ISO
    "last_error": None,     # str
    "poll_cycles": 0,
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
    }

def _awc_metar_url(stations: List[str]) -> str:
    station_str = ",".join(stations)
    base = "https://aviationweather.gov/adds/dataserver_current/httpparam"
    params = (
        "dataSource=metars&requestType=retrieve&format=JSON"
        f"&stationString={station_str}&hoursBeforeNow=2&mostRecent=true"
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def ensure_state_loaded() -> None:
    cfg = get_default_config()
    with _STATE_LOCK:
        if not _STATE["cfg"]:
            _STATE["cfg"] = cfg
        if not _STATE["stations"]:
            _STATE["stations"] = cfg["stations"]
        cache = _load_cache(cfg["cache_file"])
        if isinstance(cache, dict) and "last_obs" in cache and isinstance(cache["last_obs"], dict):
            _STATE["last_obs"].update(cache["last_obs"])

def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "stations": list(_STATE["stations"]),
            "last_obs": dict(_STATE["last_obs"]),
            "last_alert": dict(_STATE["last_alert"]),
            "cfg": dict(_STATE["cfg"]),
        }

def fetch_now(stations: List[str]) -> Dict[str, Any]:
    """One-shot fetch for specific stations (no state mutation)."""
    url = _awc_metar_url(stations)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}).get("METAR", []) or []
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
        _METRICS["requests_ok"] += 1
        _METRICS["last_ok_at"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        return {"status": "ok", "stations": stations, "observations": out}
    except Exception as e:
        _METRICS["requests_err"] += 1
        _METRICS["last_error"] = str(e)
        return {"status": "error", "error": str(e), "stations": stations}

def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    if not webhook:
        return
    try:
        requests.post(webhook, json=payload, timeout=10)
    except Exception:
        # Swallow alert errors; we still track METARs.
        pass

def _poll_once(logger=None) -> None:
    ensure_state_loaded()
    cfg = get_default_config()
    stations = cfg["stations"]
    url = _awc_metar_url(stations)

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}).get("METAR", []) or []
        _METRICS["requests_ok"] += 1
        _METRICS["last_ok_at"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    except Exception as e:
        _METRICS["requests_err"] += 1
        _METRICS["last_error"] = str(e)
        if logger:
            logger.error(f"METAR poll failed: {e}")
        return

    updates = {}
    alerts = []

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

            if prev is not None:
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

        # persist cache of last observations only
        _save_cache(cfg["cache_file"], {"last_obs": _STATE["last_obs"]})

    for a in alerts:
        _send_alert(cfg["webhook"], a)

    _METRICS["poll_cycles"] += 1
    if logger:
        logger.info(f"METAR poll: {len(updates)} stations, {len(alerts)} alerts")

def _scheduler_loop(logger, interval_sec: int) -> None:
    while not _SCHEDULER_STOP.is_set():
        _poll_once(logger)
        _SCHEDULER_STOP.wait(interval_sec)

def start_scheduler(logger, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Start background polling."""
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
    """Stop background polling (non-blocking)."""
    if not _SCHEDULER_THREAD:
        return True
    _SCHEDULER_STOP.set()
    return True

def is_scheduler_running() -> bool:
    return _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive()

# ─────────────────────────────────────────
# Public API expected by app.py
# ─────────────────────────────────────────

def get_latest_metar(icao: str) -> Dict[str, Any]:
    """
    Fetch the latest METAR for a single ICAO and return a compact summary.
    DOES NOT mutate the in-memory state (use the scheduler for that).
    """
    icao_u = (icao or "").upper().strip()
    if not icao_u:
        return {"error": "missing ICAO"}
    res = fetch_now([icao_u])
    if res.get("status") != "ok":
        return {"icao": icao_u, "error": res.get("error")}

    obs = res.get("observations", {}).get(icao_u)
    if not obs:
        return {"icao": icao_u, "error": "no data"}
    return {"icao": icao_u, **obs}

def set_watchlist(icaos: List[str]) -> Dict[str, Any]:
    """
    Replace the global watchlist (and persist it in cfg snapshot).
    """
    ensure_state_loaded()
    norm = sorted({(x or "").upper().strip() for x in icaos if isinstance(x, str) and x.strip()})
    with _STATE_LOCK:
        _STATE["stations"] = norm
        _STATE["cfg"]["stations"] = norm
        # Save current last_obs to cache (stations list lives only in memory/env)
        _save_cache(_STATE["cfg"]["cache_file"], {"last_obs": _STATE["last_obs"]})
    return {"watchlist": norm, "count": len(norm)}

def get_watchlist() -> Dict[str, Any]:
    ensure_state_loaded()
    with _STATE_LOCK:
        return {"watchlist": list(_STATE["stations"]), "count": len(_STATE["stations"])}

def get_metrics() -> Dict[str, Any]:
    ensure_state_loaded()
    with _STATE_LOCK:
        return {
            "requests_ok": _METRICS["requests_ok"],
            "requests_err": _METRICS["requests_err"],
            "poll_cycles": _METRICS["poll_cycles"],
            "last_ok_at": _METRICS["last_ok_at"],
            "last_error": _METRICS["last_error"],
            "watchlist_count": len(_STATE["stations"]),
            "cache_size": len(_STATE["last_obs"]),
            "scheduler_running": is_scheduler_running(),
        }
