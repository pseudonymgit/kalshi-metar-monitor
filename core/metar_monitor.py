# core/metar_monitor.py

import os
import json
import csv
import math
import logging
import sqlite3
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
    "last_loop_utc": None,
    "timeout_count": 0,
    "last_timeout_station": None,
    "last_timeout_utc": None,
}

_SCHEDULER_THREAD = None
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_MISSING_LADDER_DEDUPE = {}
_MISSING_LADDER_LOCK = threading.Lock()
_ALERT_LOGGER = logging.getLogger(__name__)


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
            "last_loop_utc": _STATE["last_loop_utc"],
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

        alerts += _process_temperature_event(
            icao=icao,
            temp_f=float(obs["temp_f"]),
            obs_time=ts,
            cfg=cfg,
            last_temp_f=last_temp,
            allow_alert_delivery=True,
        )

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


def _process_temperature_event(
    icao: str,
    temp_f: float,
    obs_time: str,
    cfg: Dict[str, Any],
    last_temp_f: Optional[float] = None,
    allow_alert_delivery: bool = True,
    ignore_window: bool = False,
) -> int:
    prev_f = float(last_temp_f) if last_temp_f is not None else float(temp_f)
    now_f = float(temp_f)

    _maybe_daily_reset_local(icao, obs_time)

    curr_floor = int(math.floor(now_f))
    with _STATE_LOCK:
        last_observed_integer = _STATE["last_observed_integer"].get(icao)

    alerts = 0
    if (
        (ignore_window or _within_alert_window_local(icao, obs_time))
        and last_observed_integer is not None
        and curr_floor != last_observed_integer
    ):
        _ALERT_LOGGER.info(
            "EVENT integer_cross station=%s market_type=ALL prev_int=%s curr_int=%s",
            icao,
            last_observed_integer,
            curr_floor,
        )
        if allow_alert_delivery:
            d = round(now_f - prev_f, 1)
            _emit_alert(
                icao,
                prev_f=prev_f,
                now_f=now_f,
                delta_f=d,
                obs_time=obs_time,
                cfg=cfg,
            )
        alerts = 1

    with _STATE_LOCK:
        _STATE["last_observed_integer"][icao] = curr_floor

    return alerts


def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")


def _run_alert_retention() -> None:
    try:
        days = int(os.getenv("ALERT_RETENTION_DAYS", "180"))
        max_rows = int(os.getenv("ALERT_RETENTION_MAX_ROWS", "200000"))
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    """
                    DELETE FROM alerts
                    WHERE created_utc < datetime('now', ?)
                    """,
                    (f"-{days} days",),
                )
                conn.execute(
                    """
                    DELETE FROM alerts
                    WHERE id NOT IN (
                        SELECT id FROM alerts
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    """,
                    (max_rows,),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("alert_retention_failed error=%s", e)


def _audit_alert(
    station: str,
    market_type: str,
    event_ticker: str,
    alert_type: str,
    direction: Optional[str],
    temp_f: Optional[float],
    bucket_index: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        market_type TEXT,
                        event_ticker TEXT,
                        alert_type TEXT,
                        direction TEXT,
                        temp_f REAL,
                        bucket_index INTEGER,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO alerts (
                        created_utc,
                        station,
                        market_type,
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now_utc_iso(),
                        (station or "").upper(),
                        (market_type or "").upper(),
                        event_ticker,
                        alert_type,
                        direction,
                        temp_f,
                        bucket_index,
                        json.dumps(metadata or {}, sort_keys=True),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _ALERT_LOGGER.warning("audit_log_write_failed station=%s error=%s", station, e)


def get_recent_alerts(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        limit = min(max(int(limit), 1), 500)
        db_path = _alert_db_path()
        if not os.path.exists(db_path):
            return []

        with _AUDIT_LOCK:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                rows = conn.execute(
                    """
                    SELECT id, created_utc, station, market_type,
                           event_ticker, alert_type, direction,
                           temp_f, bucket_index, metadata_json
                    FROM alerts
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()

        alerts: List[Dict[str, Any]] = []
        for row in rows:
            metadata = {}
            if row[9]:
                metadata = json.loads(row[9])
            alerts.append(
                {
                    "id": row[0],
                    "created_utc": row[1],
                    "station": row[2],
                    "market_type": row[3],
                    "event_ticker": row[4],
                    "alert_type": row[5],
                    "direction": row[6],
                    "temp_f": row[7],
                    "bucket_index": row[8],
                    "metadata": metadata,
                }
            )
        return alerts
    except Exception:
        return []


def _simulate_temperature_for_testing(
    icao: str,
    temp_f: float,
    logger=None,
    allow_alert_delivery: bool = False,
) -> Dict[str, Any]:
    ensure_state_loaded()
    cfg = get_default_config()
    ts = _now_utc_iso()
    icao = (icao or "").strip().upper()

    _maybe_daily_reset_local(icao, ts)

    with _STATE_LOCK:
        last_temp = _STATE["last_obs"].get(icao, {}).get("temp_f")
        previous_integer = _STATE["last_observed_integer"].get(icao)
        _STATE["last_obs"][icao] = _obs_tuple(float(temp_f), ts, {"simulated": True}, "simulated")
        _STATE["last_seen_iso"][icao] = ts

    alerts = _process_temperature_event(
        icao=icao,
        temp_f=float(temp_f),
        obs_time=ts,
        cfg=cfg,
        last_temp_f=last_temp,
        allow_alert_delivery=allow_alert_delivery,
        ignore_window=True,
    )

    with _STATE_LOCK:
        current_integer = _STATE["last_observed_integer"].get(icao)

    delivery_attempted = allow_alert_delivery and alerts > 0

    if logger:
        logger.info(f"Simulated ladder event for {icao} at {temp_f}F (alerts={alerts})")

    return {
        "ok": True,
        "icao": icao,
        "temp_f": float(temp_f),
        "alerts_generated": alerts,
        "delivery_requested": allow_alert_delivery,
        "delivery_attempted": delivery_attempted,
        "window_bypassed": True,
        "previous_integer": previous_integer,
        "current_integer": current_integer,
        "crossed_integer": previous_integer is not None and previous_integer != current_integer,
    }


def _send_alert(webhook: str, payload: Dict[str, Any]) -> None:
    if not webhook:
        return
    try:
        station = (payload.get("station") or "UNK").upper()
        tf = payload.get("temp_f")
        pf = payload.get("prev_temp_f")
        df = payload.get("delta_f")

        if tf is None:
            return

        try:
            from core.kalshi_monitor import (
                _get_active_stations,
                _parse_target_market_types,
                build_structured_snapshot,
                process_ladder_transition,
                send_composed_weather_market_alert,
            )

            active = _get_active_stations()
            station_is_active = active is None or station in active
            should_alert_on_missing = os.getenv("ALERT_ON_MISSING_LADDER", "false").lower() in ("1", "true", "yes", "y")
            target_market_types = _parse_target_market_types(
                os.getenv("KALSHI_TARGET_MARKET_TYPE")
            )
            if not target_market_types:
                target_market_types = {"HIGH"}

            if station_is_active:
                for market_type_token in sorted(target_market_types):
                    snapshot = build_structured_snapshot(station, {market_type_token})
                    markets = snapshot.get("markets") or []
                    _ALERT_LOGGER.info(
                        "EVAL ladder_check station=%s type=%s market_type=%s markets_found=%s",
                        station,
                        market_type_token,
                        market_type_token,
                        len(markets),
                    )

                    if not markets:
                        if should_alert_on_missing:
                            try:
                                if _to_local:
                                    local_date = _to_local(station, datetime.now(timezone.utc)).date().isoformat()
                                else:
                                    local_date = datetime.now(timezone.utc).date().isoformat()
                            except Exception:
                                local_date = datetime.now(timezone.utc).date().isoformat()

                            dedupe_key = f"{station}_{market_type_token}_{local_date}"

                            with _MISSING_LADDER_LOCK:
                                if dedupe_key in _MISSING_LADDER_DEDUPE:
                                    continue
                            _ALERT_LOGGER.info(
                                "WARN ladder_missing station=%s type=%s market_type=%s",
                                station,
                                market_type_token,
                                market_type_token,
                            )
                            response = requests.post(
                                webhook,
                                json={"content": f"⚠️ Ladder missing — station={station} type={market_type_token} temp={tf}°F"},
                                timeout=10,
                            )
                            if 200 <= response.status_code < 300:
                                with _MISSING_LADDER_LOCK:
                                    _MISSING_LADDER_DEDUPE[dedupe_key] = True
                                _audit_alert(
                                    station=station,
                                    market_type=market_type_token,
                                    event_ticker="",
                                    alert_type="ladder_missing",
                                    direction=None,
                                    temp_f=float(tf),
                                    bucket_index=None,
                                    metadata={"status_code": response.status_code},
                                )
                        continue

                    transition = process_ladder_transition(
                        station=station,
                        market_type=market_type_token,
                        snapshot=snapshot,
                        current_temp=tf,
                    )

                    if transition.get("should_alert"):
                        direction = transition.get("direction") or "UP"
                        bucket_index = transition.get("bucket_index")
                        event_ticker = (markets[0] or {}).get("event_ticker") or ""
                        _ALERT_LOGGER.info(
                            "EVENT ladder_transition station=%s type=%s market_type=%s direction=%s bucket=%s",
                            station,
                            market_type_token,
                            market_type_token,
                            direction,
                            bucket_index,
                        )
                        _audit_alert(
                            station=station,
                            market_type=market_type_token,
                            event_ticker=event_ticker,
                            alert_type="ladder_transition",
                            direction=direction,
                            temp_f=float(tf),
                            bucket_index=bucket_index,
                            metadata={"reason": transition.get("reason")},
                        )
                        result = send_composed_weather_market_alert(
                            station=station,
                            market_types={market_type_token},
                            transition_reason=transition.get("reason"),
                            prev_temp_f=pf,
                            now_temp_f=tf,
                            delta_f=df,
                        )
                        if result and result.get("ok"):
                            send_event_ticker = result.get("event_ticker") or event_ticker
                            _ALERT_LOGGER.info(
                                "SEND composed_alert station=%s type=%s market_type=%s event=%s",
                                station,
                                market_type_token,
                                market_type_token,
                                send_event_ticker,
                            )
                            _audit_alert(
                                station=station,
                                market_type=market_type_token,
                                event_ticker=send_event_ticker,
                                alert_type="composed_alert_sent",
                                direction=direction,
                                temp_f=float(tf),
                                bucket_index=result.get("bucket_index"),
                                metadata={"reason": transition.get("reason")},
                            )
        except Exception as e:
            print(f"[ERROR] station={station} function=_send_alert: {e}")
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
        last_loop_utc = _STATE["last_loop_utc"]
        poll_count = _STATE["poll_count"]
        watch_ct = len(_STATE["stations"] or get_default_config()["stations"])
        timeout_count = _STATE["timeout_count"]
        last_timeout_station = _STATE["last_timeout_station"]
        last_timeout_utc = _STATE["last_timeout_utc"]
    return {
        "last_poll_utc": last_poll_utc,
        "last_loop_utc": last_loop_utc,
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

def _scheduler_loop(logger, interval_sec: int):
    loop_count = 0
    while not _SCHEDULER_STOP.is_set():
        try:
            _poll_once(logger)
            with _STATE_LOCK:
                _STATE["last_loop_utc"] = _now_utc_iso()
        except Exception as e:
            if logger:
                logger.exception(f"METAR scheduler loop error: {e}")
        loop_count += 1
        if loop_count % 100 == 0:
            _run_alert_retention()
        _SCHEDULER_STOP.wait(interval_sec)


def start_scheduler(logger, cfg=None) -> bool:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
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


def ensure_scheduler_started(logger, cfg=None) -> bool:
    return start_scheduler(logger, cfg=cfg)


def stop_scheduler() -> bool:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if not _SCHEDULER_THREAD:
            return True
        _SCHEDULER_STOP.set()
        _SCHEDULER_THREAD.join(timeout=5)
        _SCHEDULER_THREAD = None
    return True


def is_scheduler_running() -> bool:
    return _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive()
