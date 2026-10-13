#!/usr/bin/env python3
"""
ai_nwp_independence_rate1h_sweep_2026-10-13.py

TASK 1 — AI-NWP independence
  Are ECMWF AIFS and Google GraphCast a genuine SECOND degree of freedom, or
  another collinear re-export of the deterministic NWP stack?

  Sources (Open-Meteo Historical Forecast API, free, no key):
    ecmwf_ifs025      ECMWF IFS HRES  (9 km, deterministic baseline)
    ecmwf_aifs025     ECMWF AIFS      (experimental AI-NWP)
    gfs_graphcast025  NOAA/Google GraphCast (AI-NWP)
  + GEFS ensemble mean (local `data/nwp_forecasts.db`, model='gefs_ens').

  Axis: strict strike-crossing (`crossed = settled_max > T`; `==` is a LOSS),
  T = floor(high-so-far at 13:30 station-local pin)+1  — IDENTICAL to
  scripts/predictor_diversity_study_2026-10-13.py.

  THE correlation that matters is NOT the raw tmax Pearson (a 2-year seasonal
  ramp makes every pair look r>0.95). It is the FORECAST-ERROR correlation
  r(mu_a - truth, mu_b - truth): two sources that make the same errors carry
  the same information. We report raw, anomaly, error, and P(cross) axes.

TASK 2 — rate_1h multi-window sweep
  Generalize the ~1h intraday momentum to 2..6 h windows and score separation
  on the strict strike-crossing axis, replicating
  scripts/sweep_intraday_rate1h_crossing.py EXACTLY at W=1 (UTC grid, UTC
  15-min buckets, station-local date, dmax = max bucket MEAN).

READ-ONLY on the live book. Fetch cache: data/_om_ai_cache.db (DELETED at
end; refetchable). Artifact: artifacts/ai-nwp-independence/*.

Usage:
    .venv-gefs/bin/python scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py fetch
    .venv-gefs/bin/python scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py score [--reps 1000]
    .venv-gefs/bin/python scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py all
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.station_time import station_timezone_name  # noqa: E402

ASOS = REPO / "data" / "iem_asos_1min.db"
NWP = REPO / "data" / "nwp_forecasts.db"
METAR = REPO / "data" / "metar_backfill.db"
OM_CACHE = REPO / "data" / "_om_ai_cache.db"
BUCKET_CACHE = REPO / "data" / "_ai_nwp_buckets_cache.json"
OUT_DIR = REPO / "artifacts" / "ai-nwp-independence"
OUT_JSON = OUT_DIR / "ai_nwp_independence_rate1h_sweep_2026-10-13.json"

# --- Task 1 config (mirrors the diversity study) ---------------------------
MIGRATION_START = "2024-03-01"   # AIFS archive opens here (verified)
MIGRATION_END = "2026-10-09"     # last canonical ASOS settlement day
SIGMA_DET_F = 4.0
SIGMA_GEFS_MIN = 1.5
DECISION_PIN_MOD = 13 * 60 + 30  # 13:30 station-local (diversity convention)
CORR_FLAG = 0.85
CORR_DECORR = 0.70               # task threshold: below => genuinely decorrelated
CORR_COPY = 0.90                 # task threshold: above => another copy

# --- Task 2 config (mirrors the standalone sweep) --------------------------
GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]   # 10..20 = the requested band
REPORT_HOURS = [10, 12, 14, 16, 17, 18, 20]
BUCKET_MIN = 15
WINDOWS_H = [1, 2, 3, 4, 5, 6]   # hour-windows; 1 == published rate_1h family
THRESHOLDS = [1.0, 0.5]
DEFAULT_REPS = 1000

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]

# verified against core.station_registry.get_station_coordinates
COORDS = {
    "KATL": (33.6407, -84.4277), "KAUS": (30.1945, -97.6699),
    "KBOS": (42.3656, -71.0096), "KDCA": (38.8512, -77.0402),
    "KDEN": (39.8561, -104.6737), "KDFW": (32.8998, -97.0403),
    "KHOU": (29.6454, -95.2789), "KLAS": (36.0840, -115.1537),
    "KLAX": (33.9425, -118.4081), "KMDW": (41.7868, -87.7522),
    "KMIA": (25.7959, -80.2870), "KMSP": (44.8848, -93.2223),
    "KMSY": (29.9934, -90.2580), "KNYC": (40.6413, -73.7781),
    "KOKC": (35.3931, -97.6007), "KPHL": (39.8744, -75.2424),
    "KPHX": (33.4342, -112.0116), "KSAT": (29.5337, -98.4698),
    "KSEA": (47.4502, -122.3088), "KSFO": (37.6213, -122.3790),
}

OM_MODELS = ["ecmwf_ifs025", "ecmwf_aifs025", "gfs_graphcast025"]
# artifact source keys
OM_KEYS = {
    "ecmwf_ifs025": "ifs_hres",
    "ecmwf_aifs025": "aifs",
    "gfs_graphcast025": "graphcast",
}
OM_VARNAME = {
    "ifs_hres": "temperature_2m_max_ecmwf_ifs025",
    "aifs": "temperature_2m_max_ecmwf_aifs025",
    "graphcast": "temperature_2m_max_gfs_graphcast025",
}


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# ===========================================================================
# Open-Meteo fetch (cached)
# ===========================================================================
def _om_cache_init():
    c = sqlite3.connect(OM_CACHE)
    c.execute("CREATE TABLE IF NOT EXISTS om_cache ("
              "station TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT)")
    c.commit()
    return c


def fetch_openmeteo(stations, start, end, sleep_s=1.1):
    c = _om_cache_init()
    have = {r[0] for r in c.execute("SELECT station FROM om_cache")}
    todo = [s for s in stations if s not in have]
    print(f"[fetch] {len(have)} cached, {len(todo)} to fetch", flush=True)
    for i, st in enumerate(todo, 1):
        lat, lon = COORDS[st]
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
               f"&daily=temperature_2m_max&models={','.join(OM_MODELS)}&timezone=GMT")
        payload = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "weather-engine-research/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read().decode()
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
                wait = 2 ** attempt
                print(f"[fetch] {st} attempt {attempt+1} failed: {e}; retry in {wait}s", flush=True)
                time.sleep(wait)
        if payload is None:
            print(f"[fetch] {st} FAILED permanently", flush=True)
            continue
        c.execute("INSERT OR REPLACE INTO om_cache VALUES (?,?,?)",
                  (st, payload, datetime.now(timezone.utc).isoformat()))
        c.commit()
        try:
            d = json.loads(payload)
            nn = sum(1 for k in ('temperature_2m_max_ecmwf_ifs025',
                                 'temperature_2m_max_ecmwf_aifs025',
                                 'temperature_2m_max_gfs_graphcast025')
                     for v in d.get("daily", {}).get(k, []) if v is not None)
            print(f"[fetch] {i}/{len(todo)} {st} ok ({nn} non-null cells)", flush=True)
        except Exception:
            print(f"[fetch] {i}/{len(todo)} {st} ok", flush=True)
        time.sleep(sleep_s)
    c.close()


def load_openmeteo():
    """{(key, station, date_iso): tmax_F}."""
    c = _om_cache_init()
    out = defaultdict(dict)
    cov = defaultdict(lambda: [None, None, 0])
    for st, payload in c.execute("SELECT station, payload FROM om_cache"):
        d = json.loads(payload)
        times = d.get("daily", {}).get("time", [])
        for k in ("ifs_hres", "aifs", "graphcast"):
            vals = d.get("daily", {}).get(OM_VARNAME[k], [])
            for ds, v in zip(times, vals):
                if v is None:
                    continue
                f = float(v) * 9.0 / 5.0 + 32.0
                out[(k, st, ds)] = f
                cov[k][2] += 1
                if cov[k][0] is None or ds < cov[k][0]:
                    cov[k][0] = ds
                if cov[k][1] is None or ds > cov[k][1]:
                    cov[k][1] = ds
    c.close()
    return out, {k: {"date_min": v[0], "date_max": v[1], "n_cells": v[2]}
                 for k, v in cov.items()}


# ===========================================================================
# GEFS + truth
# ===========================================================================
def load_gefs():
    c = ro(NWP)
    members = defaultdict(dict)
    for st, td, var, v in c.execute(
            "SELECT station, target_date, variable, value FROM nwp_forecasts "
            "WHERE model='gefs_ens' AND variable LIKE 'temperature_2m_max_member%'"):
        if v is not None:
            members[(st, td)][var] = float(v) * 9.0 / 5.0 + 32.0
    c.close()
    out = {}
    for k, d in members.items():
        vals = list(d.values())
        n = len(vals)
        mu = sum(vals) / n
        var = sum((x - mu) ** 2 for x in vals) / n
        out[k] = {"mu": mu, "sd": math.sqrt(var), "n": n}
    return out


def load_truth():
    c = ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL"):
        out[(st, ds)] = float(v)
    c.close()
    return out


# ===========================================================================
# ASOS -> 15-min buckets, ONE scan, BOTH conventions
#   utc mod  : matches sweep_intraday_rate1h_crossing.py  (Task 2)
#   local mod: matches predictor_diversity_study (13:30 local pin) (Task 1)
# ===========================================================================
def load_asos_buckets(force=False, cache=BUCKET_CACHE):
    if cache.exists() and not force:
        raw = json.loads(cache.read_text())
        out = {}
        for k, v in raw.items():
            st, ld = k.split("|", 1)
            out[(st, ld)] = {
                "utc": {int(m): f for m, f in v["utc"].items()},
                "local": {int(m): f for m, f in v["local"].items()},
                "dmax_utcmean": v["dmax_utcmean"],
                "dmax_raw": v["dmax_raw"],
            }
        print(f"[asos] loaded {len(out)} station-days from cache", flush=True)
        return out

    tz = {st: ZoneInfo(station_timezone_name(st)) for st in STATIONS}
    offcache = {}
    ub = defaultdict(lambda: [0.0, 0])   # (st,ld,utc_mod) -> [sum,n]
    lb = defaultdict(lambda: [0.0, 0])   # (st,ld,local_mod) -> [sum,n]
    dmax_raw = {}
    c = ro(ASOS)
    cur = c.execute("SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL")
    n = 0
    for st, valid, tmpf in cur:
        n += 1
        if n % 5_000_000 == 0:
            print(f"[asos] scanned {n:,}", flush=True)
        tzs = tz.get(st)
        if tzs is None:
            continue
        try:
            ds = valid[0:10]
            hh = int(valid[11:13])
            mm = int(valid[14:16])
        except (ValueError, TypeError):
            continue
        # UTC minute-of-day bucket
        umod = (hh * 60 + mm) // BUCKET_MIN * BUCKET_MIN
        key = (st, ds, hh)
        off = offcache.get(key)
        if off is None:
            try:
                rep = datetime.strptime(ds + f" {hh:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                off = int(rep.astimezone(tzs).utcoffset().total_seconds())
            except Exception:
                off = 0
            offcache[key] = off
        # Euclidean split of station-local minute-of-day. `tot` is NEGATIVE
        # before the local day starts for US stations; Python // and % are
        # floor/Euclidean, so (tot//1440, tot%1440) is exact and equals
        # utc_dt.astimezone(station_tz). No pre-dawn cross-day bug (verified).
        tot = hh * 60 + mm + off // 60
        ldate = (date.fromisoformat(ds) + timedelta(days=tot // 1440)).isoformat()
        lmod = (tot % 1440) // BUCKET_MIN * BUCKET_MIN
        a = ub[(st, ldate, umod)]
        a[0] += tmpf
        a[1] += 1
        b = lb[(st, ldate, lmod)]
        b[0] += tmpf
        b[1] += 1
        mk = (st, ldate)
        if dmax_raw.get(mk) is None or tmpf > dmax_raw[mk]:
            dmax_raw[mk] = float(tmpf)
    c.close()

    utc = defaultdict(dict)
    for (st, ld, m), (s, cnt) in ub.items():
        utc[(st, ld)][m] = s / cnt
    loc = defaultdict(dict)
    for (st, ld, m), (s, cnt) in lb.items():
        loc[(st, ld)][m] = s / cnt

    out = {}
    for k in set(utc) | set(loc) | set(dmax_raw):
        u = utc.get(k, {})
        out[k] = {
            "utc": u,
            "local": loc.get(k, {}),
            "dmax_utcmean": max(u.values()) if u else None,
            "dmax_raw": dmax_raw.get(k),
        }
    ser = {f"{st}|{ld}": {
        "utc": {str(m): v for m, v in rec["utc"].items()},
        "local": {str(m): v for m, v in rec["local"].items()},
        "dmax_utcmean": rec["dmax_utcmean"],
        "dmax_raw": rec["dmax_raw"],
    } for (st, ld), rec in out.items()}
    cache.write_text(json.dumps(ser))
    print(f"[asos] wrote {len(out)} station-days -> {cache}", flush=True)
    return out


# ===========================================================================
# stats helpers (identical to the diversity study)
# ===========================================================================
def cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return cdf((mu - x) / sigma)


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def dir_acc(ps, ys):
    return sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / len(ps)


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def _rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    return pearson(_rank(xs), _rank(ys))


# ===========================================================================
# TASK 1
# ===========================================================================
DET_SOURCES = ["ifs_hres", "aifs", "graphcast"]  # deterministic sources
ALL_SOURCES = ["ifs_hres", "aifs", "graphcast", "gefs"]


def hsf_at_pin_local(rec):
    if not rec:
        return None
    b = rec["local"]
    vals = [b[m] for m in b if m <= DECISION_PIN_MOD]
    return max(vals) if vals else None


def build_grid(om, gefs, truth, asos):
    d0 = date.fromisoformat(MIGRATION_START)
    d1 = date.fromisoformat(MIGRATION_END)
    days = []
    d = d0
    while d <= d1:
        days.append(d.isoformat())
        d += timedelta(days=1)

    grid = {}
    for st in STATIONS:
        for ds in days:
            hsf = hsf_at_pin_local(asos.get((st, ds)))
            if hsf is None:
                continue
            T = float(math.floor(hsf) + 1.0)
            settled = truth.get((st, ds))
            rec = {
                "station": st, "date": ds, "strike": T,
                "settled_max": settled, "hsf_pin": hsf,
                "y": None if settled is None else (1 if settled > T else 0),
            }
            for k in DET_SOURCES:
                mu = om.get((k, st, ds))
                rec[k] = mu
                rec[k + "_p"] = p_ge(mu, SIGMA_DET_F, T) if mu is not None else None
            g = gefs.get((st, ds))
            if g is not None:
                rec["gefs"] = g["mu"]
                rec["gefs_sd"] = g["sd"]
                rec["gefs_n"] = g["n"]
                rec["gefs_p"] = p_ge(g["mu"], max(SIGMA_GEFS_MIN, g["sd"]), T)
            else:
                rec["gefs"] = rec["gefs_sd"] = rec["gefs_n"] = rec["gefs_p"] = None
            grid[(st, ds)] = rec
    return grid


def task1_per_source(grid):
    out = {}
    for s in ALL_SOURCES:
        pk = s + "_p"
        rows = [r for r in grid.values() if r.get(pk) is not None and r.get("y") is not None]
        if not rows:
            out[s] = {"n": 0}
            continue
        ps = [r[pk] for r in rows]
        ys = [r["y"] for r in rows]
        dates = sorted(r["date"] for r in rows)
        sts = [r["station"] for r in rows]
        out[s] = {
            "n": len(rows),
            "brier": round(brier(ps, ys), 4),
            "dir_acc": round(dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "brier_base_rate": round(sum((y - sum(ys) / len(ys)) ** 2 for y in ys) / len(ys), 4),
            "date_min": dates[0], "date_max": dates[-1],
            "n_stations": len(set(sts)),
            "mean_p": round(sum(ps) / len(ps), 4),
        }
        # per-station Brier for stability
        per = {}
        for st in sorted(set(sts)):
            r2 = [r for r in rows if r["station"] == st]
            p2 = [r[pk] for r in r2]
            y2 = [r["y"] for r in r2]
            per[st] = {"n": len(r2), "brier": round(brier(p2, y2), 4),
                       "dir_acc": round(dir_acc(p2, y2), 4)}
        out[s]["per_station"] = per
    return out


def corr_pair(grid, a, b, axisfmt):
    rows = [r for r in grid.values()
            if r.get(axisfmt.format(a)) is not None and r.get(axisfmt.format(b)) is not None]
    if len(rows) < 3:
        return {"n": len(rows), "pearson": None, "spearman": None, "flag": False}
    xs = [r[axisfmt.format(a)] for r in rows]
    ys = [r[axisfmt.format(b)] for r in rows]
    rp = pearson(xs, ys)
    rs = spearman(xs, ys)
    return {"n": len(rows),
            "pearson": None if rp != rp else round(rp, 4),
            "spearman": None if rs != rs else round(rs, 4),
            "flag_0.85": bool(abs(rp) > CORR_FLAG) if rp == rp else False}


def corr_matrix(grid, axisfmt):
    mat = {}
    for i, a in enumerate(ALL_SOURCES):
        for b in ALL_SOURCES[i + 1:]:
            mat[f"{a}|{b}"] = corr_pair(grid, a, b, axisfmt)
    return mat


def corr_errors(grid):
    """Error axis: e_s = mu_s - settled (needs settled truth). THE key test."""
    mat = {}
    for i, a in enumerate(ALL_SOURCES):
        for b in ALL_SOURCES[i + 1:]:
            rows = [r for r in grid.values()
                    if r.get(a) is not None and r.get(b) is not None and r.get("settled_max") is not None]
            if len(rows) < 3:
                mat[f"{a}|{b}"] = {"n": len(rows), "pearson": None}
                continue
            xa = [r[a] - r["settled_max"] for r in rows]
            xb = [r[b] - r["settled_max"] for r in rows]
            rp = pearson(xa, xb)
            mat[f"{a}|{b}"] = {"n": len(rows),
                              "pearson": None if rp != rp else round(rp, 4),
                              "flag_0.85": bool(abs(rp) > CORR_FLAG) if rp == rp else False}
    return mat


def anomaly_axis(grid):
    """Subtract the pairwise-common mean per (station, calendar-day) — remove the
    seasonal cycle so the correlation reflects synoptic (day-to-day) agreement.
    Here: subtract the station's own running 31-day centred mean of each source.
    Simpler + defensible: subtract the IFS-HRES seasonal mean per (station, doy)."""
    # per (station, doy) mean of ifs_hres over all available years -> seasonal curve
    seas = defaultdict(lambda: [0.0, 0])
    for r in grid.values():
        if r.get("ifs_hres") is None:
            continue
        doy = date.fromisoformat(r["date"]).timetuple().tm_yday
        a = seas[(r["station"], doy)]
        a[0] += r["ifs_hres"]
        a[1] += 1
    curves = {k: v[0] / v[1] for k, v in seas.items()}
    for r in grid.values():
        doy = date.fromisoformat(r["date"]).timetuple().tm_yday
        c = curves.get((r["station"], doy))
        for s in DET_SOURCES:
            mu = r.get(s)
            r[s + "_anom"] = None if (mu is None or c is None) else mu - c
    mat = {}
    for i, a in enumerate(DET_SOURCES):
        for b in DET_SOURCES[i + 1:]:
            rows = [r for r in grid.values()
                    if r.get(a + "_anom") is not None and r.get(b + "_anom") is not None]
            if len(rows) < 3:
                mat[f"{a}|{b}"] = {"n": len(rows), "pearson": None}
                continue
            rp = pearson([r[a + "_anom"] for r in rows], [r[b + "_anom"] for r in rows])
            mat[f"{a}|{b}"] = {"n": len(rows),
                              "pearson": None if rp != rp else round(rp, 4)}
    return mat


def blend_test(grid):
    """Best AI vs GEFS vs a blend, on overlapping station-days with truth."""
    out = {}
    for ai in ("aifs", "graphcast"):
        rows = [r for r in grid.values()
                if r.get(ai + "_p") is not None and r.get("gefs_p") is not None and r.get("y") is not None]
        if len(rows) < 20:
            out[ai] = {"n": len(rows)}
            continue
        ys = [r["y"] for r in rows]
        p_ai = [r[ai + "_p"] for r in rows]
        p_ge = [r["gefs_p"] for r in rows]
        p_bl = [(a + g) / 2.0 for a, g in zip(p_ai, p_ge)]
        # also with the deterministic IFS baseline
        rows2 = [r for r in rows if r.get("ifs_hres_p") is not None]
        out[ai] = {
            "n": len(rows),
            "brier_ai": round(brier(p_ai, ys), 4),
            "brier_gefs": round(brier(p_ge, ys), 4),
            "brier_ai_plus_gefs": round(brier(p_bl, ys), 4),
            "dir_ai": round(dir_acc(p_ai, ys), 4),
            "dir_gefs": round(dir_acc(p_ge, ys), 4),
            "dir_blend": round(dir_acc(p_bl, ys), 4),
        }
        if len(rows2) >= 20:
            ys2 = [r["y"] for r in rows2]
            m3 = [(r[ai + "_p"] + r["gefs_p"] + r["ifs_hres_p"]) / 3.0 for r in rows2]
            out[ai]["n_with_ifs"] = len(rows2)
            out[ai]["brier_ai_gefs_ifs"] = round(brier(m3, ys2), 4)
            out[ai]["brier_ifs_alone"] = round(brier([r["ifs_hres_p"] for r in rows2], ys2), 4)
    return out


# ===========================================================================
# TASK 2 — window sweep
# ===========================================================================
def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def build_sweep_samples(asos):
    """Replicate the standalone sweep and add longer symmetric windows.

    W = 1  : published rate_1h = bucket[m] - mean(4 prior buckets)  (exact)
    W >= 2 : symmetric contrast = mean(last 4W buckets) - mean(prev 4W buckets)

    Only the station-local daily MAX of bucket means (dmax) is used for the
    label, exactly as sweep_intraday_rate1h_crossing.py.
    """
    by_sd = {}
    for (st, ld), rec in asos.items():
        if rec["utc"]:
            by_sd[(st, ld)] = rec["utc"]
    samples = []
    for (st, ld), mods in by_sd.items():
        ordered = sorted(mods.items())
        minutes = [m for m, _ in ordered]
        means = [v for _, v in ordered]
        dmax = max(means)
        for h in GRID_HOURS:
            dec = h * 60
            if dec not in mods:
                continue
            idx = minutes.index(dec)
            high_so_far = max(means[:idx + 1])
            strike = math.floor(high_so_far) + 1.0
            crossed = 1 if dmax > strike else 0
            # published rate_1h
            rate1 = None
            if idx >= 4:
                rate1 = means[idx] - sum(means[idx - 4:idx]) / 4.0
            # symmetric windows
            rates = {}
            for W in WINDOWS_H:
                k = 4 * W
                if idx - 2 * k + 1 >= 0:
                    recent = _mean(means[idx - k + 1: idx + 1])
                    prior = _mean(means[idx - 2 * k + 1: idx - k + 1])
                    if recent is not None and prior is not None:
                        rates[W] = recent - prior
                elif W == 1 and rate1 is not None:
                    # keep W=1 populated where the symmetric form is short but
                    # the published form is defined (matches the published table)
                    pass
            samples.append({
                "st": st, "ld": ld, "h": h, "rate1": rate1,
                "rates": rates, "hsf": high_so_far, "crossed": crossed,
            })
    return samples


def _sep_cell(rows, key, t):
    up = [r for r in rows if r[key] is not None and r[key] >= t]
    dn = [r for r in rows if r[key] is not None and r[key] <= -t]
    if not up or not dn:
        return None
    pu = sum(r["crossed"] for r in up) / len(up)
    pd = sum(r["crossed"] for r in dn) / len(dn)
    return {"p_up": round(pu, 4), "n_up": len(up), "p_down": round(pd, 4),
            "n_down": len(dn), "sep_pp": round(100 * (pu - pd), 2)}


def _boot_ci(rows, key, t, reps, seed):
    # precompute per local-date contributions
    by_date = defaultdict(lambda: [0, 0, 0, 0])  # su,nu,sd,nd
    for r in rows:
        v = r[key]
        if v is None:
            continue
        d = by_date[r["ld"]]
        if v >= t:
            d[0] += r["crossed"]; d[1] += 1
        elif v <= -t:
            d[2] += r["crossed"]; d[3] += 1
    dates = list(by_date)
    if len(dates) < 5:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        su = nu = sd = nd = 0
        for _ in dates:
            d = by_date[rng.choice(dates)]
            su += d[0]; nu += d[1]; sd += d[2]; nd += d[3]
        if nu and nd:
            stats.append(su / nu - sd / nd)
    if not stats:
        return None
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[int(0.975 * len(stats)) - 1]
    return [round(100 * lo, 2), round(100 * hi, 2)]


def task2_sweep(samples, reps, seed=12345):
    out = {"windows_h": WINDOWS_H, "grid_hours_utc": GRID_HOURS,
           "note": ("W=1 uses the published symmetric-form contrast; "
                    "'rate1h_published' is the exact published rate_1h for reference."),
           "by_window": {}, "by_hour": {}, "grid": {}}
    for W in WINDOWS_H:
        key = f"W{W}"
        rows = [r for r in samples if r["rates"].get(W) is not None]
        out["by_window"][key] = {"n": len(rows)}
    # per (window, hour) at each threshold
    for W in WINDOWS_H:
        for h in GRID_HOURS:
            rows = [r for r in samples if r["h"] == h and r["rates"].get(W) is not None]
            cellstore = {}
            for t in THRESHOLDS:
                # need a key accessor; use a shim list of dicts with fixed key
                shim = [{"v": r["rates"][W], "crossed": r["crossed"], "ld": r["ld"]} for r in rows]
                c = _sep_cell(shim, "v", t)
                if c is not None:
                    c["ci95_pp"] = _boot_ci(shim, "v", t, reps, seed + W * 100 + h)
                cellstore[f"+/-{t}"] = c
            out["grid"][f"{W}h|{h:02d}Z"] = cellstore
    # published rate_1h reference (W=1 anchor)
    for h in GRID_HOURS:
        rows = [r for r in samples if r["h"] == h and r["rate1"] is not None]
        shim = [{"v": r["rate1"], "crossed": r["crossed"], "ld": r["ld"]} for r in rows]
        cell = _sep_cell(shim, "v", 1.0)
        if cell is not None:
            cell["ci95_pp"] = _boot_ci(shim, "v", 1.0, reps, seed + h)
        out["grid"][f"pub1h|{h:02d}Z"] = {"n": len(rows), "+/-1.0": cell}
    out["normalized"] = task2_normalized(samples)
    out["rate_scale"] = task2_rate_scale(samples)
    return out


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def task2_normalized(samples):
    """Scale-free separation: within each (W,hour) cell take the top and bottom
    15% of rate_W, so every window gets the SAME 15%/15% split. This removes the
    'longer windows shrink the rate scale' confound from the +/-t tables."""
    grid = {}
    for W in WINDOWS_H:
        for h in GRID_HOURS:
            vals = sorted(r["rates"][W] for r in samples
                          if r["h"] == h and r["rates"].get(W) is not None)
            if len(vals) < 50:
                continue
            hi_t = _quantile(vals, 0.85)
            lo_t = _quantile(vals, 0.15)
            up = [r for r in samples if r["h"] == h and r["rates"].get(W) is not None
                  and r["rates"][W] >= hi_t]
            dn = [r for r in samples if r["h"] == h and r["rates"].get(W) is not None
                  and r["rates"][W] <= lo_t]
            if not up or not dn:
                continue
            pu = sum(r["crossed"] for r in up) / len(up)
            pd = sum(r["crossed"] for r in dn) / len(dn)
            grid[f"{W}h|{h:02d}Z"] = {
                "n_up": len(up), "n_down": len(dn),
                "hi_t": round(hi_t, 3), "lo_t": round(lo_t, 3),
                "p_up": round(pu, 4), "p_down": round(pd, 4),
                "sep_pp": round(100 * (pu - pd), 2),
            }
    return grid


def task2_rate_scale(samples):
    """Pooled stdev of rate_W per window: shows the scale compression that the
    fixed +/-t tables are sensitive to."""
    out = {}
    for W in WINDOWS_H:
        vals = [r["rates"][W] for r in samples if r["rates"].get(W) is not None]
        if len(vals) < 2:
            continue
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))
        out[f"W{W}"] = {"n": len(vals), "mean": round(mu, 4), "sd": round(sd, 4)}
    return out


# ===========================================================================
# main
# ===========================================================================
def run_fetch():
    fetch_openmeteo(STATIONS, MIGRATION_START, MIGRATION_END)


def run_score(reps):
    t0 = time.time()
    om, om_cov = load_openmeteo()
    print(f"[score] open-meteo cells: {om_cov}", flush=True)
    gefs = load_gefs()
    truth = load_truth()
    asos = load_asos_buckets()
    print(f"[score] gefs station-days={len(gefs)} truth={len(truth)} asos={len(asos)}", flush=True)

    grid = build_grid(om, gefs, truth, asos)
    t1 = task1_per_source(grid)
    c_raw = corr_matrix(grid, "{}")
    c_pcross = corr_matrix(grid, "{}_p")
    c_err = corr_errors(grid)
    c_anom = anomaly_axis(grid)
    blend = blend_test(grid)

    samples = build_sweep_samples(asos)
    sweep = task2_sweep(samples, reps)
    print(f"[score] sweep samples={len(samples)}", flush=True)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": [MIGRATION_START, MIGRATION_END],
        "strike_rule": "T = floor(high-so-far at 13:30 station-local pin)+1",
        "label_rule": "crossed = settled_max > T (strict; == is a LOSS)",
        "sigma_det_F": SIGMA_DET_F,
        "corr_thresholds": {"decorrelated_below": CORR_DECORR, "copy_above": CORR_COPY},
        "openmeteo_coverage": om_cov,
        "task1_per_source": t1,
        "task1_corr_temp_raw": c_raw,
        "task1_corr_anomaly": c_anom,
        "task1_corr_forecast_error": c_err,
        "task1_corr_pcross": c_pcross,
        "task1_blend": blend,
        "task2_sweep": sweep,
        "grid_summary": {
            "n_station_days": len(grid),
            "n_with_truth": sum(1 for r in grid.values() if r.get("y") is not None),
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2))
    print(f"[score] wrote {OUT_JSON}  ({time.time()-t0:.0f}s)", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["fetch", "score", "all"])
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--force-buckets", action="store_true")
    a = ap.parse_args()
    if a.stage in ("fetch", "all"):
        run_fetch()
    if a.stage in ("score", "all"):
        if a.force_buckets:
            load_asos_buckets(force=True)
        run_score(a.reps)


if __name__ == "__main__":
    main()
