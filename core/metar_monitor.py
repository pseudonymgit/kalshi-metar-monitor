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
}

_SCHEDULER_THREAD = None
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
        "asos_recent_minutes": int(os.getenv("ASOS_RECENT_MINUTES", "30")),  # NEW
    }


# ---------- IEM ASOS (single source) ----------

def _iem_recent_url(icao: str, minutes: int) -> str:
    """
    Returns JSON with recent ASOS observations for a single station.
    Example:
      https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=KDEN&recent=45&tz=UTC&data=tmpf&format=json
    """
    base = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    return f"{base}?station={icao}&recent={minutes}&tz=UTC&data=tmpf&format=json"

def _parse_asos_time(ts: str) -> Optional[datetime]:
    # IEM returns e.g. "2025-11-04 23:16" in UTC (because tz=UTC)
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _fetch_iem_latest(icao: str, minutes: int) -> Optional[Dict[str, Any]]:
    """
    Fetch recent minute(s) and return the most recent non-null tmpf.
    Returns:
      { "temp_f": float, "obs_time": ISO, "raw": row_dict } or None
    """
    url = _iem_recent_url(icao, minutes)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    j = r.json()

    data = j.get("data") or j.get("observations") or []
    if not isinstance(data, list):
        return None

    latest = None
    latest_dt = None

    for row in data:
        # IEM JSON typically: { "station": "KDEN", "valid": "YYYY-MM-DD HH:MM", "tmpf": 45.1, ... }
        ts = row.get("valid") or row.get("utc_valid") or row.get("time")
        tmpf = row.get("tmpf")
        if tmpf is None or ts is None:
            continue
        dt = _parse_asos_time(ts)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest = {
                "temp_f": round(float(tmpf), 1),
                "obs_time": dt.isoformat(),
                "raw": row,
            }

    return latest


# ---------- Cache helpers ----------

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


# ---------- Public state helpers ----------

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

def fetch_now(stations: List[str]) -> Dict[str, Any]:
    """
    Single-shot fetch from IEM only.
    """
    ensure_state_loaded()
    cfg = get_default_config()
    minutes = int(cfg["asos_recent_minutes"])

    out = {}
    errors = {}

    for icao in stations:
        try:
            obs = _fetch_iem_latest(icao, minutes)
            if obs:
                out[icao] = obs
            else:
                errors[icao] = "no recent tmpf"
        except Exception as e:
            errors[icao] = str(e)

    return {
        "status": "ok" if out else "error",
        "stations": stations,
        "observations": out,
        "errors": errors,
    }


# ---------- Alerts + scheduler ----------

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
    minutes = int(cfg["asos_recent_minutes"])

    updates = {}
    alerts = []

    for icao in stations:
        try:
            obs = _fetch_iem_latest(icao, minutes)
        except Exception as e:
            if logger: logger.error(f"IEM fetch failed for {icao}: {e}")
            obs = None

        if not obs:
            continue

        with _STATE_LOCK:
            prev = _STATE["last_obs"].get(icao)
            _STATE["last_obs"][icao] = obs
            updates[icao] = obs

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

        # small pause to be polite; remove if you prefer full speed
        time.sleep(0.2)

    # persist cache
    with _STATE_LOCK:
        cache = {"last_obs": _STATE["last_obs"]}
    _save_cache(cfg["cache_file"], cache)

    for a in alerts:
        _send_alert(cfg["webhook"], a)

    if logger:
        logger.info(f"IEM poll: {len(updates)} stations, {len(alerts)} alerts")

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
