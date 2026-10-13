#!/usr/bin/env python3
"""
om_gfs (Open-Meteo bias-corrected GFS) NWP fetcher + state builder.

WHY (validated, 2026-10-13)
---------------------------
The om_gfs strike-crossing study (scripts/om_gfs_strike_crossing_test_2026-10-13.py,
artifacts/om-gfs-strike-crossing/) found that Open-Meteo's `gfs_seamless` pipeline
(downscaled/bias-corrected GFS) is a strictly better NWP input than our raw local
GFS on BOTH axes over 2026-04-21..2026-10-09:

  * level MAE        0.680 F (bias-corrected) vs 2.095 F raw local GFS  (-1.415 F)
  * strike-crossing  Brier 0.1079 vs 0.1597

It is however COLLINEAR with the rate_1h nowcast (fcerr 0.99 >= 0.85), so it is
NOT a diversity member. This module exists ONLY for the LEVER/level-axis
substitution: when enabled, the SBOX bridge's NWP component uses om_gfs
(bias-corrected) instead of the raw local GFS-seeded WS-P distribution.

Source: Open-Meteo, free + keyless.
  * backfill : https://historical-forecast-api.open-meteo.com/v1/forecast
               ?daily=temperature_2m_max&models=gfs_seamless&start_date=..&end_date=..
  * live     : https://api.open-meteo.com/v1/forecast
               ?daily=temperature_2m_max&models=gfs_seamless&past_days=..&forecast_days=..
  Both return °C when no temperature_unit is requested -> converted to °F here.
  Requests are spaced >= DELAY_S apart (Open-Meteo is a free service).

Flag: USE_OM_GFS_NWP=1 enables the substitution. Default OFF -> the bridge is
byte-identical to its pre-change behaviour (raw local GFS-seeded WS-P).

READ-ONLY everywhere except the gitignored response cache
(data/_omgfs_nwp_cache.json, covered by the repo's `*.json` ignore rule).
Deterministic math only. No AI/ML.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.intraday.distribution_adapter import DistributionState
from core.station_registry import get_station_coordinates
from core.station_time import station_timezone_name

REPO = Path(__file__).resolve().parents[2]
SETTLE_DB = REPO / "data" / "metar_backfill.db"
CACHE_PATH = REPO / "data" / "_omgfs_nwp_cache.json"

# ── flag ─────────────────────────────────────────────────────────────────────
OM_GFS_FLAG_ENV = "USE_OM_GFS_NWP"


def om_gfs_enabled() -> bool:
    """True when the om_gfs NWP substitution is switched on."""
    return os.environ.get(OM_GFS_FLAG_ENV, "0") == "1"


# ── constants (embedded, must match the validated study recipe) ─────────────
OM_MODEL = "gfs_seamless"
DELAY_S = 1.2                     # >= 1 s between requests (Open-Meteo polite)
MAX_ATTEMPTS = 3
UA = "weather-engine/1.0 (om_gfs_nwp; internal)"
HIST_BASE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_BASE = "https://api.open-meteo.com/v1/forecast"

# Rolling per-station bias window (days of settled truth before the target day).
BIAS_WINDOW_DAYS = 30
MIN_BIAS_OBS = 3
MAX_BIAS_MAGNITUDE_F = 5.0

# Intraday rate_1h nowcast sigma (matches predictor_diversity_study SIGMA_RATE).
SIGMA_RATE_F = 1.5
# Default dispersion multiplier k in sigma = k * SIGMA_RATE_F. The study's own
# om_gfs dispersion-fitted station sigma was ~1.60 F (k ~ 1.07); the shipped
# default k=2.0 (sigma 3.0) is deliberately CONSERVATIVE and is not claimed to
# reproduce the validated 0.680 F level MAE on the crossing axis.
SIGMA_K_DEFAULT = 2.0

_BULK_UA_LATLON: Optional[Dict[str, Tuple[float, float]]] = None

# process-lifetime response cache (lazy) so a bridge pass does one disk read.
_CACHE_MEMO: Optional[Dict[str, float]] = None
_BIAS_MEMO: Dict[tuple, Optional[float]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def c2f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9.0 / 5.0 + 32.0


def _log(msg: str) -> None:
    print(f"[om_gfs] {msg}", flush=True)


def _coords() -> Dict[str, Tuple[float, float]]:
    """Station -> (lat, lon); cached. Missing stations are dropped."""
    global _BULK_UA_LATLON
    if _BULK_UA_LATLON is not None:
        return _BULK_UA_LATLON
    from core.station_registry import CANONICAL_STATIONS
    out: Dict[str, Tuple[float, float]] = {}
    for st in CANONICAL_STATIONS:
        lat, lon = get_station_coordinates(st)
        if lat is None or lon is None:
            continue
        try:
            out[st] = (float(lat), float(lon))
        except (TypeError, ValueError):
            continue
    _BULK_UA_LATLON = out
    return out


def _daterange(start: str, end: str) -> List[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ── disk cache ───────────────────────────────────────────────────────────────
def _cache_load() -> Dict[str, float]:
    global _CACHE_MEMO
    if _CACHE_MEMO is not None:
        return _CACHE_MEMO
    if CACHE_PATH.exists():
        try:
            _CACHE_MEMO = {k: float(v) for k, v in json.loads(CACHE_PATH.read_text()).items()}
        except Exception:
            _CACHE_MEMO = {}
    else:
        _CACHE_MEMO = {}
    return _CACHE_MEMO


def _cache_flush() -> None:
    if _CACHE_MEMO is None:
        return
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_CACHE_MEMO))
    except OSError as e:  # never let a cache write break a fetch
        _log(f"cache write failed: {type(e).__name__}: {e}")


def _cache_key(station: str, ds: str, src: str) -> str:
    return f"{station}|{ds}|{src}"


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _build_url(station: str, lat: float, lon: float, tz: str,
               start_date: str, end_date: str, live: bool) -> str:
    if live:
        today = date.today()
        past = max(0, (today - date.fromisoformat(start_date)).days)
        fwd = max(1, (date.fromisoformat(end_date) - today).days + 1)
        q = (f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
             f"&models={OM_MODEL}&past_days={past}&forecast_days={fwd}"
             f"&timezone={urllib.parse.quote(tz)}")
        return LIVE_BASE + q
    q = (f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
         f"&models={OM_MODEL}&start_date={start_date}&end_date={end_date}"
         f"&timezone={urllib.parse.quote(tz)}")
    return HIST_BASE + q


def _parse_daily(d: dict) -> Dict[str, float]:
    daily = d.get("daily", {}) or {}
    times = daily.get("time", []) or []
    series = daily.get("temperature_2m_max_" + OM_MODEL) or daily.get("temperature_2m_max")
    out: Dict[str, float] = {}
    if not series:
        return out
    for i, t in enumerate(times):
        v = series[i] if i < len(series) else None
        if v is None:
            continue
        f = c2f(v)
        if f is not None:
            out[t] = f
    return out


def _fetch_one(station: str, lat: float, lon: float, tz: str,
               start_date: str, end_date: str, live: bool) -> Dict[str, float]:
    """Fetch one station's daily Tmax over [start, end] (one request)."""
    url = _build_url(station, lat, lon, tz, start_date, end_date, live)
    last_err: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(DELAY_S * (2 ** attempt))
        try:
            data = json.loads(_http_get(url).decode("utf-8", "replace"))
            return _parse_daily(data)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"{station} fetch failed: {type(last_err).__name__}: {last_err}")


# ── public fetch API ─────────────────────────────────────────────────────────
def fetch_om_gfs_many(stations: List[str], start_date: str, end_date: str, *,
                      live: bool = False, force: bool = False,
                      delay_s: Optional[float] = None) -> Dict[Tuple[str, str], float]:
    """{(station, iso_date): om_gfs Tmax °F} for every requested station-date.

    One HTTP request per station (spanning the whole date range), spaced
    >= delay_s (default DELAY_S) apart. Results are cached to disk; a fully
    cached station needs no network. Per-station failures are logged and skipped
    so one bad station cannot abort the batch.
    """
    delay = DELAY_S if delay_s is None else float(delay_s)
    src = "live" if live else "hist"
    coords = _coords()
    cache = _cache_load()
    wanted = _daterange(start_date, end_date)
    out: Dict[Tuple[str, str], float] = {}
    dirty = False

    for st in stations:
        if st not in coords:
            continue
        have_all = all(_cache_key(st, d, src) in cache for d in wanted)
        if force or not have_all:
            lat, lon = coords[st]
            tz = station_timezone_name(st)
            try:
                got = _fetch_one(st, lat, lon, tz, start_date, end_date, live)
            except Exception as e:  # noqa: BLE001
                _log(f"{st} FAILED: {e}")
                got = {}
            for d, v in got.items():
                cache[_cache_key(st, d, src)] = float(v)
                dirty = True
            time.sleep(delay)
        for d in wanted:
            v = cache.get(_cache_key(st, d, src))
            if v is not None:
                out[(st, d)] = v
    if dirty:
        _cache_flush()
    return out


# ── rolling per-station bias correction ─────────────────────────────────────
def rolling_bias_f(station: str, as_of_date: str, *,
                   window_days: int = BIAS_WINDOW_DAYS,
                   min_obs: int = MIN_BIAS_OBS,
                   live: bool = False,
                   cache: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Mean(om_gfs - settled truth) over the `window_days` before `as_of_date`.

    NO LOOKAHEAD: the window ends the day before `as_of_date`, so a decision on
    `as_of_date` can be reproduced from data available then. `min_obs` settled
    days are required; otherwise None is returned and the caller applies no bias
    (raw om_gfs) rather than a fabricated correction. The correction is capped at
    +/- MAX_BIAS_MAGNITUDE_F.
    """
    key = (station, as_of_date, window_days, live)
    if key in _BIAS_MEMO:
        return _BIAS_MEMO[key]

    try:
        d1 = date.fromisoformat(as_of_date) - timedelta(days=1)
        d0 = d1 - timedelta(days=window_days - 1)
    except ValueError:
        _BIAS_MEMO[key] = None
        return None

    src = "live" if live else "hist"
    cmap = cache if cache is not None else _cache_load()

    truth: Dict[str, float] = {}
    try:
        conn = sqlite3.connect(f"file:{SETTLE_DB}?mode=ro", uri=True)
        try:
            for ds, v in conn.execute(
                    "SELECT local_trading_date, settlement_temp_f FROM settlement_truth "
                    "WHERE station=? AND market_type='HIGH' AND trustworthy=1 "
                    "AND settlement_temp_f IS NOT NULL "
                    "AND local_trading_date >= ? AND local_trading_date <= ?",
                    (station, d0.isoformat(), d1.isoformat())):
                truth[ds] = float(v)
        finally:
            conn.close()
    except sqlite3.Error:
        truth = {}

    resid = []
    for ds, settled in truth.items():
        v = cmap.get(_cache_key(station, ds, src))
        if v is not None:
            resid.append(v - settled)

    if len(resid) < min_obs:
        _BIAS_MEMO[key] = None
        return None
    b = sum(resid) / len(resid)
    b = max(-MAX_BIAS_MAGNITUDE_F, min(MAX_BIAS_MAGNITUDE_F, b))
    _BIAS_MEMO[key] = b
    return b


# ── state builder ────────────────────────────────────────────────────────────
def from_om_gfs(station: str, om_gfs_tmax_f: float, date_str: str, *,
                sigma: Optional[float] = None, high_so_far: Optional[float] = None,
                bias_f: Optional[float] = None) -> DistributionState:
    """Build a DistributionState from an om_gfs daily Tmax (°F).

    mu    = om_gfs_raw - rolling_bias   (the validated bias-corrected recipe).
    sigma = k * SIGMA_RATE_F (default k=SIGMA_K_DEFAULT) or an explicit value.

    The WS-P GEFS bias/offset/month terms are deliberately NOT re-applied: om_gfs
    is already bias-corrected upstream, so stacking the GEFS-fitted terms would
    double-correct. This matches the study's om_gfs_bc column.
    """
    sig = float(SIGMA_K_DEFAULT * SIGMA_RATE_F) if sigma is None else float(sigma)
    sig = max(0.8, sig)
    mu = float(om_gfs_tmax_f) - float(bias_f or 0.0)
    return DistributionState(station, date_str, mu, sig,
                             high_so_far=high_so_far, base_sigma=sig)


def om_gfs_state_for_day(station: str, date_str: str, *, live: bool = True,
                         sigma: Optional[float] = None,
                         high_so_far: Optional[float] = None) -> Optional[DistributionState]:
    """Cache-aware one-call convenience: fetch + bias-correct -> DistributionState.

    Returns None (never raises) when the source has no value for the day, so the
    caller can fall back to the raw local GFS-seeded distribution.
    """
    try:
        got = fetch_om_gfs_many([station], date_str, date_str, live=live)
    except Exception as e:  # noqa: BLE001
        _log(f"{station} fetch error: {type(e).__name__}: {e}")
        return None
    v = got.get((station, date_str))
    if v is None:
        return None
    bias = rolling_bias_f(station, date_str, live=live)
    return from_om_gfs(station, v, date_str, sigma=sigma,
                       high_so_far=high_so_far, bias_f=bias)
