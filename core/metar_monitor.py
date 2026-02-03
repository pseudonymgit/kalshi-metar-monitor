# core/metar_monitor.py

import os
import json
import threading
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

# =========================
# In-memory state
# =========================
_STATE_LOCK = threading.Lock()
_STATE = {
    "stations": [],
    "last_obs": {},        # { ICAO: {"temp_f": float, "obs_time": ISO, "raw": any, "source": str} }
    "last_alert": {},      # { ICAO: {"temp_f": float, "at": ISO} }
    "last_seen_iso": {},   # { ICAO: ISO string of latest obs we've processed }
    "cfg": {},
    "poll_count": 0,
    "last_poll_utc": None,
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()

OVERLAP_SECONDS = 120  # 2 minutes overlap to survive small API lag/skew

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
        "default_source": os.getenv("METAR_DEFAULT_SOURCE", "nws").lower(),  # "nws" | "iem" | "tgftp"
        "strict": os.getenv("METAR_STRICT", "true").lower() in ("1", "true", "yes", "y"),
        "sources": [s.strip().lower() for s in os.getenv("METAR_SOURCES", "nws").split(",") if s.strip()],

        # NWS etiquette headers (strongly recommended)
        "http_from": os.getenv("HTTP_FROM_EMAIL", "you@example.com"),
        "http_agent": os.getenv("HTTP_USER_AGENT", "KalshiMetarMonitor/1.1 (+you@example.com)"),

        # range window minutes for each poll
        "lookback_min": int(os.getenv("METAR_LOOKBACK_MIN", "3")),

        # IEM range window fallback (we still filter to start..end)
        "iem_hours": int(os.getenv("IEM_LOOKBACK_HOURS", "1")),
    }

def _c_to_f(c: float) -> float:
    return c * 9.0/5.0 + 32.0

def _parse_iso(s: str) -> datetime:
    # tolerate both with and without timezone
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

def _now_utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

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
        if "last_seen_iso" in cache:
            _STATE["last_seen_iso"].update(cache["last_seen_iso"])

def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "stations": list(_STATE["stations"]),
            "last_obs": dict(_STATE["last_obs"]),
            "last_alert": dict(_STATE["last_alert"]),
            "last_seen_iso": dict(_STATE["last_seen_iso"]),
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
        "Accept": "application/ld+json, application/geo+json, application/json",
    }

def _nws_range_url(icao: str, start_iso: str, end_iso: str, limit: int = 200) -> str:
    # https://api.weather.gov/stations/KDEN/observations?start=...&end=...&limit=200
    return (
        f"https://api.weather.gov/stations/{icao}/observations"
        f"?start={start_iso}&end={end_iso}&limit={limit}"
    )

def _iem_range_url(icao: str, hours: int) -> str:
    # We’ll still filter client-side to start..end
    return (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={icao}&data=tmpf&tz=UTC&format=json&hours={hours}"
    )

def _tgftp_latest_url(icao: str) -> str:
    return f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"

# =========================
# Parse → canonical obs tuple
# =========================
def _obs_tuple(temp_f: float, ts_iso: str, raw: Any, source: str) -> Dict[str, Any]:
    return {"temp_f": round(float(temp_f), 1), "obs_time": ts_iso, "raw": raw, "source": source}

def _parse_nws_collection(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    feats = j.get("features", [])
    for f in feats:
        props = f.get("properties", {})
        val_c = props.get("temperature", {}).get("value")
        ts = props.get("timestamp")
        if val_c is None or not ts:
            continue
        out.append(_obs_tuple(_c_to_f(float(val_c)), ts, props, "nws"))
    # sort by time asc
    out.sort(key=lambda x: _parse_iso(x["obs_time"]))
    return out

def _parse_iem_json(j: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in j.get("data", []):
        tmpf = row.get("tmpf")
        valid = row.get("valid")  # 'YYYY-mm-dd HH:MM' UTC
        if tmpf is None or not valid:
            continue
        try:
            ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < start_dt or ts > end_dt:
            continue
        out.append(_obs_tuple(float(tmpf), ts.isoformat(), row, "iem"))
    out.sort(key=lambda x: _parse_iso(x["obs_time"]))
    return out

def _parse_tgftp_text(text: str) -> Optional[Dict[str, Any]]:
    lines = text.strip().splitlines()
    if not lines:
        return None
    ts_line = lines[0].strip()
    metar_line = lines[-1].strip()
    # Try find temp from token "12/01" or "M02/M05"
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
def _fetch_range_nws(icao: str, start_iso: str, end_iso: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = _nws_range_url(icao, start_iso, end_iso)
    r = requests.get(url, headers=_headers_for_nws(cfg), timeout=20)
    r.raise_for_status()
    j = r.json()
    return _parse_nws_collection(j)

def _fetch_range_iem(icao: str, start_dt: datetime, end_dt: datetime, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = _iem_range_url(icao, int(cfg.get("iem_hours", 1)))
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    j = r.json()
    return _parse_iem_json(j, start_dt, end_dt)

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
    delta_thr = float(cfg["delta_f"])

    # Process in ascending time; skip obs_time <= last_seen_iso
    for obs in new_obs:
        ts = obs["obs_time"]
        if last_seen_iso and _parse_iso(ts) <= _parse_iso(last_seen_iso):
            continue

        # Store obs
        with _STATE_LOCK:
            _STATE["last_obs"][icao] = obs
            _STATE["last_seen_iso"][icao] = ts

        ingested += 1

        # Alert logic: compare to previous temperature
        if last_temp is not None:
            d = round(float(obs["temp_f"]) - float(last_temp), 1)
            if abs(d) >= delta_thr:
                _emit_alert(icao, prev_f=last_temp, now_f=obs["temp_f"], delta_f=d, obs_time=ts, cfg=cfg)
                alerts += 1

        last_temp = obs["temp_f"]

    # Persist cache (best effort)
    with _STATE_LOCK:
        _save_cache(cfg["cache_file"], {
            "last_obs": _STATE["last_obs"],
            "last_seen_iso": _STATE["last_seen_iso"],
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
    if not webhook:
        return
    try:
        if "discord.com/api/webhooks" in webhook:
            station = payload.get("station", "UNK")
            tf = payload.get("temp_f")
            pf = payload.get("prev_temp_f")
            df = payload.get("delta_f")
            ts = payload.get("obs_time")
            content = f"**METAR Temp Change** — {station}: {pf}→{tf} °F (Δ {df:+}) @ {ts}"
            body = {
                "content": content,
                "embeds": [{
                    "title": f"{station} Temperature Update",
                    "fields": [
                        {"name": "Prev °F", "value": str(pf), "inline": True},
                        {"name": "Now °F",  "value": str(tf), "inline": True},
                        {"name": "Δ °F",    "value": f"{df:+}", "inline": True},
                    ],
                    "timestamp": payload.get("at_utc"),
                    "footer": {"text": "METAR monitor"},
                }]
            }
            requests.post(webhook, json=body, timeout=10)
        else:
            requests.post(webhook, json=payload, timeout=10)
    except Exception:
        pass

# =========================
# Public fetch helpers (strict by chosen/default source)
# =========================
def _compute_window(icao: str, cfg: Dict[str, Any]) -> Tuple[str, str, datetime, datetime]:
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    lookback = int(cfg["lookback_min"])
    with _STATE_LOCK:
        last_seen = _STATE["last_seen_iso"].get(icao)
    if last_seen:
        start_dt = _parse_iso(last_seen) - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start_dt = now - timedelta(minutes=lookback)
    end_dt = now
    return (start_dt.isoformat(), end_dt.isoformat(), start_dt, end_dt)

def _fetch_range_strict(icao: str, chosen: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    start_iso, end_iso, start_dt, end_dt = _compute_window(icao, cfg)
    if chosen == "nws":
        return _fetch_range_nws(icao, start_iso, end_iso, cfg)
    if chosen == "iem":
        return _fetch_range_iem(icao, start_dt, end_dt, cfg)
    if chosen == "tgftp":
        # TGFTP only gives latest; still return in list form
        return _fetch_latest_tgftp(icao)
    return []

def fetch_now(stations: List[str], source: Optional[str] = None) -> Dict[str, Any]:
    """
    Strict range fetch. No fallback unless you turn strict off.
    """
    ensure_state_loaded()
    cfg = get_default_config()
    chosen = (source or cfg["default_source"] or "nws").lower()

    observations: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    total_ing = 0
    total_alerts = 0

    for icao in stations:
        try:
            obs_list = _fetch_range_strict(icao, chosen, cfg)
            ing, al = _ingest_obs(icao, obs_list, cfg)
            total_ing += ing
            total_alerts += al
            # Return the latest we know (may still be None if nothing arrived)
            with _STATE_LOCK:
                observations[icao] = _STATE["last_obs"].get(icao)
        except Exception as e:
            observations[icao] = None
            errors[icao] = str(e)

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
    res = fetch_now([icao], source=source)
    obs = res.get("observations", {}).get(icao)
    if obs:
        return {"icao": icao, "source": res.get("source"), **obs}
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

    total_ing = 0
    total_alerts = 0
    for icao in stations:
        try:
            obs_list = _fetch_range_strict(icao, chosen, cfg)
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
