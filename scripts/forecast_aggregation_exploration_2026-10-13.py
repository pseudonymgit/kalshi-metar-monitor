#!/usr/bin/env python3
"""
forecast_aggregation_exploration_2026-10-13.py

TASK — Multi-source Tmax aggregation + outlier-trimmed consensus.

Dan's directive (2026-10-13): aggregate every independent Tmax forecast we can
get for our 20 ASOS/Kalshi stations, test whether an outlier-trimmed consensus
tightens the target range vs the best single source and vs the pure NWP family.
Include scripted, polite web scraping (weather.com / accuweather / wunderground).

Sources (Tmax = daily maximum of 2 m air temp, °F):
  Offline / in-repo (no network):
    nws        NWS official forecast archive  (data/forecast_disagreement_live.db)
    gfs        deterministic GFS              (data/nwp_forecasts.db, °C)
    ecmwf      deterministic ECMWF IFS        (data/nwp_forecasts.db, °C)
    icon       DWD ICON                       (data/nwp_forecasts.db, °C)
    gem        CMC GEM                        (data/nwp_forecasts.db, °C)
    gefs_ens   GEFS 31-member mean            (data/nwp_forecasts.db, °C)
  Free keyless API (network):
    om_*       Open-Meteo Historical Forecast API: gfs, ecmwf_ifs, icon, gem,
               meteofrance, ukmo, jma, knmi, dmi  (9 independent NWP pipelines)
  Scraped (network, polite 2 s/host, HTML cached to disk then deleted):
    wu         wunderground.com daily Tmax
    wc         weather.com  (attempt; JS-rendered → usually unparsed)
    accu       accuweather.com (attempt; 403)

Truth: data/ghcn_settlements.db (GHCN-Daily, Kalshi settlement source), °F.

Backtest window: 2026-09-13 .. 2026-10-12 (last 30 days). Lead time for every
source is the SAME calendar horizon (the forecast valid for its own target day,
fetched on the target day), so no lead-time advantage is handed to any source.

READ-ONLY on live book. HTML cache + fetch cache are gitignored and deleted at
the end of `collect` (small re-fetchable artifacts only).

Usage:
    python3 scripts/forecast_aggregation_exploration_2026-10-13.py collect
    python3 scripts/forecast_aggregation_exploration_2026-10-13.py analyze
    python3 scripts/forecast_aggregation_exploration_2026-10-13.py all
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.station_registry import CANONICAL_STATIONS, STATIC_MAPPING  # noqa: E402

# ── constants ────────────────────────────────────────────────────────────
WIN_START = date(2026, 9, 13)
WIN_END = date(2026, 10, 12)
TODAY_UTC = datetime.now(ZoneInfo("UTC")).date()

UA = "weather-engine-research/1.0 (forecast-aggregation-study; internal research)"
DELAY_S = 2.0                                            # ≥2 s between requests to any host
HTML_CACHE = REPO / "data" / "_agg_html_cache"          # gitignored, deleted after parse
OM_CACHE = REPO / "data" / "_agg_om_cache.json"         # gitignored
SCRAPE_CACHE = REPO / "data" / "_agg_scrape_cache.json"  # gitignored
ART = REPO / "artifacts" / "forecast-aggregation"
NWP_DB = REPO / "data" / "nwp_forecasts.db"
FDL_DB = REPO / "data" / "forecast_disagreement_live.db"
GHCN_DB = REPO / "data" / "ghcn_settlements.db"
ASOS_DB = REPO / "data" / "iem_asos_1min.db"

# station coords (from scripts/nwp_collect.py CITIES, verified 2026-07-02)
COORDS = {
    "KATL": (33.64, -84.43), "KAUS": (30.20, -97.67), "KBOS": (42.37, -71.01),
    "KDCA": (38.85, -77.04), "KDEN": (39.86, -104.67), "KDFW": (32.90, -97.04),
    "KHOU": (29.98, -95.36), "KLAS": (36.08, -115.16), "KLAX": (33.94, -118.41),
    "KMDW": (41.79, -87.75), "KMIA": (25.80, -80.29), "KMSP": (44.88, -93.22),
    "KMSY": (29.99, -90.26), "KNYC": (40.71, -74.01), "KOKC": (35.39, -97.60),
    "KPHL": (39.87, -75.24), "KPHX": (33.43, -112.01), "KSAT": (29.53, -98.47),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.38),
}

OM_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless",
             "meteofrance_seamless", "ukmo_seamless", "jma_seamless",
             "knmi_seamless", "dmi_seamless"]
OM_SHORT = {"gfs_seamless": "om_gfs", "ecmwf_ifs025": "om_ecmwf",
            "icon_seamless": "om_icon", "gem_seamless": "om_gem",
            "meteofrance_seamless": "om_mf", "ukmo_seamless": "om_ukmo",
            "jma_seamless": "om_jma", "knmi_seamless": "om_knmi",
            "dmi_seamless": "om_dmi"}
NWPDB_MODELS = ["gfs", "ecmwf", "icon", "gem"]

# source family tags for the coverage matrix
FAMILY = {
    "nws": "nws", "gfs": "nwp", "ecmwf": "nwp", "icon": "nwp", "gem": "nwp",
    "gefs_ens": "nwp", "wu": "web", "wc": "web", "accu": "web", "wttr": "web",
}
for m in OM_SHORT.values():
    FAMILY[m] = "om"


def c2f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


def _pack_cache(panel):
    """{(st,date): {src: v}} → {st: {date: {src: v}}} (JSON str keys)."""
    out = {}
    for (st, d), vals in panel.items():
        out.setdefault(st, {})[d] = vals
    return out


def _unpack_cache(obj):
    out = {}
    for st, days in obj.items():
        for d, vals in days.items():
            out[(st, d)] = vals
    return out


def log(msg):
    print(f"[agg] {msg}", flush=True)


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str, timeout: int = 25):
    return json.loads(http_get(url, timeout).decode("utf-8", "replace"))


# ── offline collectors ───────────────────────────────────────────────────
def collect_nwpdb():
    """Deterministic NWP + GEFS mean from local DB, °C→°F, aligned to target day."""
    if not NWP_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{NWP_DB}?mode=ro", uri=True)
    cur = con.cursor()
    out = {}
    for model in NWPDB_MODELS:
        rows = cur.execute(
            "SELECT target_date,station,value FROM nwp_forecasts "
            "WHERE model=? AND variable='temperature_2m_max' "
            "AND target_date BETWEEN ? AND ?",
            (model, WIN_START.isoformat(), WIN_END.isoformat())).fetchall()
        for d, s, v in rows:
            if s in CANONICAL_STATIONS and v is not None:
                out.setdefault((s, d), {})[model] = c2f(v)
    # GEFS: mean of all members per station/target
    rows = cur.execute(
        "SELECT target_date,station,variable,value FROM nwp_forecasts "
        "WHERE model='gefs_ens' AND variable LIKE 'temperature_2m_max_member%' "
        "AND target_date BETWEEN ? AND ?",
        (WIN_START.isoformat(), WIN_END.isoformat())).fetchall()
    agg = {}
    for d, s, var, v in rows:
        if s in CANONICAL_STATIONS and v is not None:
            agg.setdefault((s, d), []).append(v)
    for (s, d), vals in agg.items():
        if vals:
            out.setdefault((s, d), {})["gefs_ens"] = c2f(sum(vals) / len(vals))
    con.close()
    return out


def collect_nws_archive():
    """Official NWS forecast archive (fetched on target day, local date)."""
    if not FDL_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{FDL_DB}?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT station,collection_date,nws_forecast_high_f,gfs_forecast_high_f "
        "FROM daily_forecast_snapshots WHERE collection_date BETWEEN ? AND ?",
        (WIN_START.isoformat(), WIN_END.isoformat())).fetchall()
    con.close()
    out = {}
    for s, d, nws, gf in rows:
        if s in CANONICAL_STATIONS:
            if nws is not None:
                out.setdefault((s, d), {})["nws"] = float(nws)
    return out


def collect_om_historical(allow_network=True, force=False):
    """Open-Meteo Historical Forecast API — 9 independent NWP pipelines per
    station, station-local daily dates so the daily max aligns to the local
    settlement day. One request per station (20 total)."""
    cache = {}
    if OM_CACHE.exists():
        cache = _unpack_cache(json.loads(OM_CACHE.read_text()))
    if not allow_network:
        return cache
    if cache and not force:
        log("  om cache hit (use --force to refetch)")
        return cache
    out = {}
    for st in CANONICAL_STATIONS:
        lat, lon = COORDS[st]
        tz = STATIC_MAPPING.get(st, {}).get("tz", "UTC")
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&daily=temperature_2m_max"
               f"&start_date={WIN_START.isoformat()}&end_date={WIN_END.isoformat()}"
               f"&models={','.join(OM_MODELS)}&timezone={urllib.parse.quote(tz)}")
        try:
            d = get_json(url)
            daily = d.get("daily", {})
            times = daily.get("time", [])
            for mk in OM_MODELS:
                key = "temperature_2m_max_" + mk
                series = daily.get(key)
                if not series:
                    continue
                short = OM_SHORT[mk]
                for i, t in enumerate(times):
                    v = series[i] if i < len(series) else None
                    if v is not None:
                        out.setdefault((st, t), {})[short] = c2f(v)
            log(f"  om {st}: {len(times)} days")
        except Exception as e:  # noqa: BLE001
            log(f"  om {st} FAILED: {e}")
        time.sleep(DELAY_S)
    OM_CACHE.write_text(json.dumps(_pack_cache(out)))
    return out


# ── scrapers (polite) ────────────────────────────────────────────────────
def _split_arr(s):
    return [x.strip().strip('"') for x in s.split(",")]


def parse_wu_daily(html, target_iso):
    """wunderground embedded daily JSON. Daily block has adjacency:
    'temperatureMax':[...], 'temperatureMin':[...], 'validTimeLocal':[...]
    validTimeLocal entries are 'YYYY-MM-DDT07:00:00±hhmm'."""
    tmax = [m.group(1) for m in re.finditer(r'"temperatureMax":\[(.*?)\]', html)]
    vtl = [m.group(1) for m in re.finditer(r'"validTimeLocal":\[(.*?)\]', html)]
    for tm in tmax:
        temps = _split_arr(tm)
        for vt in vtl:
            times = _split_arr(vt)
            if len(times) != len(temps):
                continue
            if not times or "T07:00" not in times[0]:
                continue
            for i, t in enumerate(times):
                if t[:10] == target_iso and temps[i] not in ("null", ""):
                    try:
                        return float(temps[i])
                    except ValueError:
                        pass
    return None


def parse_wc_high(html):
    """weather.com today page — look for an embedded numeric high. Usually the
    high is JS-rendered; return None if not statically present."""
    for pat in (r'"temperatureMax"\s*:\s*(-?\d+(?:\.\d+)?)',
                r'"high"\s*:\s*(-?\d+(?:\.\d+)?)',
                r'"tempHighF"\s*:\s*(-?\d+(?:\.\d+)?)'):
        m = re.search(pat, html)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def scrape_current(allow_network=True):
    """One pass over 20 stations for wunderground + weather.com (today's Tmax).
    HTML cached to disk, parsed, cache deleted. Returns {station: {src: tmax}}."""
    out = {}
    if not allow_network:
        return out
    HTML_CACHE.mkdir(parents=True, exist_ok=True)
    agent_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    for st in CANONICAL_STATIONS:
        lat, lon = COORDS[st]
        tz = STATIC_MAPPING.get(st, {}).get("tz", "UTC")
        today_local = datetime.now(ZoneInfo(tz)).date().isoformat()
        rec = {}
        # wunderground
        for url in (f"https://www.wunderground.com/weather/{st}",):
            try:
                raw = http_get_ua(url, agent_ua)
                (HTML_CACHE / f"wu_{st}.html").write_bytes(raw)
                v = parse_wu_daily(raw.decode("utf-8", "replace"), today_local)
                if v is not None:
                    rec["wu"] = v
            except Exception as e:  # noqa: BLE001
                log(f"  wu {st} FAILED: {e}")
            time.sleep(DELAY_S)
        # weather.com (lat,lon)
        try:
            raw = http_get_ua(f"https://weather.com/weather/today/l/{lat},{lon}", agent_ua)
            (HTML_CACHE / f"wc_{st}.html").write_bytes(raw)
            v = parse_wc_high(raw.decode("utf-8", "replace"))
            if v is not None:
                rec["wc"] = v
        except Exception as e:  # noqa: BLE001
            log(f"  wc {st} FAILED: {e}")
        time.sleep(DELAY_S)
        out[st] = rec
        log(f"  scrape {st}: {rec}")
    # delete html cache after parse (per constraint)
    for f in HTML_CACHE.glob("*.html"):
        f.unlink()
    try:
        HTML_CACHE.rmdir()
    except OSError:
        pass
    SCRAPE_CACHE.write_text(json.dumps(out))
    return out


def scrape_wttr():
    """wttr.in wraps multiple sources → single consensus; free, no key."""
    out = {}
    for st in CANONICAL_STATIONS:
        try:
            d = get_json(f"https://wttr.in/{st}?format=j2")
            w = d.get("weather", [])
            if w and w[0].get("maxtempF"):
                out[st] = {"wttr": float(w[0]["maxtempF"])}
        except Exception as e:  # noqa: BLE001
            log(f"  wttr {st} FAILED: {e}")
        time.sleep(DELAY_S)
    return out


def collect_om_live():
    """Open-Meteo live API for today's Tmax (proof-of-integration, current day)."""
    out = {}
    for st in CANONICAL_STATIONS:
        lat, lon = COORDS[st]
        tz = STATIC_MAPPING.get(st, {}).get("tz", "UTC")
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
               f"&forecast_days=1&timezone={urllib.parse.quote(tz)}")
        try:
            d = get_json(url)
            v = d.get("daily", {}).get("temperature_2m_max", [None])[0]
            if v is not None:
                out[st] = {"om_live": c2f(v)}
        except Exception as e:  # noqa: BLE001
            log(f"  om_live {st} FAILED: {e}")
        time.sleep(DELAY_S)
    return out


def http_get_ua(url, ua, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── truth ────────────────────────────────────────────────────────────────
def load_truth():
    con = sqlite3.connect(f"file:{GHCN_DB}?mode=ro", uri=True)
    cur = con.cursor()
    q = ",".join("?" * len(CANONICAL_STATIONS))
    rows = cur.execute(
        f"SELECT station,date,high_f FROM ghcn_settlements "
        f"WHERE station IN ({q}) AND date BETWEEN ? AND ?",
        (*CANONICAL_STATIONS, WIN_START.isoformat(), WIN_END.isoformat())).fetchall()
    con.close()
    return {(s, d): v for s, d, v in rows if v is not None}


# ── analysis ─────────────────────────────────────────────────────────────
# family presets for consensus variants
CORE_NWPDB = ["gfs", "ecmwf", "icon", "gem", "gefs_ens"]


def _mae(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _listen(series):
    return sum(series) / len(series)


def consensus_variants(vals, src_mae, core_only=False):
    """vals: {src: tmax}; src_mae: {src: global MAE}. Returns dict of
    {variant: value}. All variants are pure source-side aggregations."""
    items = sorted(vals.items(), key=lambda kv: kv[1])
    xs = [v for _, v in items]
    out = {}
    out["raw_mean"] = _listen(xs)
    out["trim_minmax"] = _listen(xs[1:-1]) if len(xs) > 2 else out["raw_mean"]
    out["median"] = statistics.median(xs)
    out["trim_pair3"] = _listen(xs[3:-3]) if len(xs) > 6 else out["raw_mean"]
    # family mean: local NWP DB members present
    fam = [v for s, v in vals.items() if s in CORE_NWPDB]
    out["nwp_family"] = _listen(fam) if fam else None
    # core + nws
    fam2 = [v for s, v in vals.items() if s in CORE_NWPDB or s == "nws"]
    out["nwp_plus_nws"] = _listen(fam2) if fam2 else None
    # oracle reliable subset: keep sources with MAE <= best*1.5
    if src_mae:
        b = min(src_mae.values())
        rel = [v for s, v in vals.items() if src_mae.get(s, 99) <= b * 1.5]
        out["reliable_subset"] = _listen(rel) if rel else None
        out["reliable_n"] = len(rel)
        # inverse-MAE weighted over all present sources
        wsum = sum(1.0 / src_mae[s] for s in vals if s in src_mae)
        if wsum > 0:
            out["inv_mae_all"] = sum(v / src_mae[s] for s, v in vals.items()
                                     if s in src_mae) / wsum
        # inverse-MAE weighted over reliable subset only
        relw = sum(1.0 / src_mae[s] for s in vals if s in src_mae and src_mae[s] <= b * 1.5)
        if relw > 0:
            out["inv_mae_rel"] = sum(v / src_mae[s] for s, v in vals.items()
                                     if s in src_mae and src_mae[s] <= b * 1.5) / relw
        # quality top-5 (homogeneous ensemble for the Venn test)
        top5 = sorted(src_mae, key=src_mae.get)[:5]
        t5 = [v for s, v in vals.items() if s in top5]
        out["top5_subset"] = _listen(t5) if t5 else None
        out["top5_n"] = len(t5)
    return out


def analyze(offline, om, scrape, wttr, om_live):
    truth = load_truth()
    panel = {}
    for src in (offline, om):
        for k, d in src.items():
            panel.setdefault(k, {}).update(d)

    srcs = sorted({s for d in panel.values() for s in d})
    days = sorted({d for (_, d) in panel})
    sts = sorted({s for (s, _) in panel})
    coverage = {s: sum(1 for d in panel.values() if s in d) for s in srcs}

    # pass 1: per-source global MAE (needed for reliable-subset variant)
    src_err = {s: [] for s in srcs}
    for (st, d), vals in panel.items():
        t = truth.get((st, d))
        if t is None:
            continue
        for s, x in vals.items():
            if x is not None:
                src_err[s].append(abs(x - t))
    src_mae = {s: (sum(e) / len(e)) for s, e in src_err.items() if len(e) >= 20}

    # pass 2: build rows with variants + signed errors
    rows = []
    for (st, d), vals in sorted(panel.items()):
        t = truth.get((st, d))
        if t is None:
            continue
        clean = {s: x for s, x in vals.items() if x is not None}
        if len(clean) < 3:
            continue
        var = consensus_variants(clean, src_mae)
        xsv = sorted(clean.values())
        rng = xsv[-1] - xsv[0]
        # robust spread: trimmed range (drop 1 each side)
        rng_t = (xsv[-2] - xsv[1]) if len(xsv) > 4 else rng
        best_src = min(clean, key=lambda s: abs(clean[s] - t))
        row = {
            "station": st, "date": d, "truth": t, "n_sources": len(clean),
            "range": rng, "range_trim": rng_t, "std": statistics.pstdev(xsv),
            "best_src": best_src,
            "err_best_single": abs(clean[best_src] - t),
            "bias_best_single": clean[best_src] - t,
            "sources": clean,
        }
        for k, v in var.items():
            if k in ("reliable_n", "top5_n"):
                row[k] = v
                continue
            row[k] = v
            row[f"err_{k}"] = abs(v - t) if v is not None else None
            row[f"bias_{k}"] = (v - t) if v is not None else None
        if src_mae:
            top5 = sorted(src_mae, key=src_mae.get)[:5]
            t5v = sorted(v for s, v in clean.items() if s in top5)
            row["range_top5"] = (t5v[-1] - t5v[0]) if len(t5v) >= 3 else None
            row["err_top5"] = abs(var["top5_subset"] - t) if var.get("top5_subset") is not None else None
        rows.append(row)

    variants = ["raw_mean", "trim_minmax", "median", "trim_pair3",
                "nwp_family", "nwp_plus_nws", "reliable_subset",
                "inv_mae_all", "inv_mae_rel", "top5_subset"]
    res = {}
    for v in variants:
        res[v] = {"mae": _mae([r.get(f"err_{v}") for r in rows]),
                  "bias": _mae([r.get(f"bias_{v}") for r in rows])}
    res["best_single_row"] = {"mae": _mae([r["err_best_single"] for r in rows]),
                              "bias": _mae([r["bias_best_single"] for r in rows])}

    # range distribution
    def bucket(rngs):
        b = {"<=1": 0, "1-2": 0, "2-3": 0, "3-5": 0, "5-8": 0, ">8": 0}
        for x in rngs:
            if x <= 1: b["<=1"] += 1
            elif x <= 2: b["1-2"] += 1
            elif x <= 3: b["2-3"] += 1
            elif x <= 5: b["3-5"] += 1
            elif x <= 8: b["5-8"] += 1
            else: b[">8"] += 1
        return b

    def grp(rngs_hi, rngs_lo):
        g = [r for r in rows if rngs_lo <= r["range"] <= rngs_hi]
        return {
            "n": len(g),
            "mae_trim_pair3": _mae([r["err_trim_pair3"] for r in g]),
            "mae_median": _mae([r["err_median"] for r in g]),
            "mae_raw": _mae([r["err_raw_mean"] for r in g]),
            "mae_best_single": _mae([r["err_best_single"] for r in g]),
        }

    venn = {
        "tight_le2": grp(2.0, 0.0),
        "mid_2_5": grp(5.0, 2.01),
        "wide_ge5": grp(1e9, 5.0),
    }

    # Venn test on the homogeneous top-5 quality subset
    def grp_t5(lo, hi):
        g = [r for r in rows if r.get("range_top5") is not None
             and lo <= r["range_top5"] <= hi]
        return {
            "n": len(g),
            "mae_top5": _mae([r["err_top5"] for r in g]),
            "mae_median": _mae([r["err_median"] for r in g]),
            "mae_best_single": _mae([r["err_best_single"] for r in g]),
        }

    venn_top5 = {
        "tight_le2": grp_t5(0.0, 2.0),
        "mid_2_4": grp_t5(2.01, 4.0),
        "wide_ge4": grp_t5(4.01, 1e9),
    }

    # paired comparison vs best single source on the rows where it is present
    paired = {}
    bsrc = min(src_mae, key=src_mae.get) if src_mae else None
    if bsrc:
        for v in variants:
            d = []
            better = 0
            for r in rows:
                if bsrc in r["sources"] and r.get(f"err_{v}") is not None:
                    de = r[f"err_{v}"] - abs(r["sources"][bsrc] - r["truth"])
                    d.append(de)
                    better += 1 if de < 0 else 0
            if len(d) >= 50:
                paired[v] = {"n": len(d), "mean_delta_err": sum(d) / len(d),
                             "frac_better": better / len(d)}

    # per-station consensus (trim_pair3) MAE
    per_station = {}
    for s in sts:
        g = [r for r in rows if r["station"] == s]
        per_station[s] = {
            "n": len(g),
            "mae_trim_pair3": _mae([r["err_trim_pair3"] for r in g]),
            "mae_raw": _mae([r["err_raw_mean"] for r in g]),
            "mean_range": _mae([r["range"] for r in g]),
            "mae_best_single": _mae([r["err_best_single"] for r in g]),
        }

    # duplicate-source detection (near-identical series)
    dups = []
    for i, a in enumerate(srcs):
        for b in srcs[i + 1:]:
            both = [(r["sources"][a], r["sources"][b]) for r in rows
                    if a in r["sources"] and b in r["sources"]]
            if len(both) >= 100:
                d = sum(abs(x - y) for x, y in both) / len(both)
                if d < 0.5:
                    dups.append({"a": a, "b": b, "mean_abs_diff_f": d, "n": len(both)})

    worst = sorted(rows, key=lambda r: -r["range"])[:5]

    summary = {
        "window": [WIN_START.isoformat(), WIN_END.isoformat()],
        "n_station_days_scored": len(rows),
        "n_stations": len(sts), "n_days": len(days), "n_sources": len(srcs),
        "coverage": coverage,
        "per_source_mae": {k: {"mae": v, "n": len(src_err[k])} for k, v in
                           sorted(src_mae.items(), key=lambda kv: kv[1])},
        "consensus": res,
        "best_single_source": min(src_mae, key=src_mae.get) if src_mae else None,
        "best_single_mae": min(src_mae.values()) if src_mae else None,
        "range_distribution": bucket([r["range"] for r in rows]),
        "range_trim_distribution": bucket([r["range_trim"] for r in rows]),
        "venn": venn,
        "venn_top5": venn_top5,
        "paired_vs_best_single": paired,
        "per_station": per_station,
        "duplicate_sources": dups,
        "mean_range": _mae([r["range"] for r in rows]),
        "mean_range_trim": _mae([r["range_trim"] for r in rows]),
        "worst5": [{"station": r["station"], "date": r["date"], "truth": r["truth"],
                    "range": r["range"], "std": r["std"], "n": r["n_sources"],
                    "raw": r["raw_mean"], "trim3": r["trim_pair3"], "med": r["median"]}
                   for r in worst],
        "current_coverage": {st: sorted(set(list(scrape.get(st, {}).keys()) +
                                            list(wttr.get(st, {}).keys()) +
                                            list(om_live.get(st, {}).keys())))
                             for st in CANONICAL_STATIONS},
    }
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "consensus_results.json").write_text(json.dumps(summary, indent=2))
    (ART / "station_day_rows.json").write_text(json.dumps(rows, indent=2))
    return summary


# ── main ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "analyze", "all"])
    ap.add_argument("--force", action="store_true", help="refetch OM cache")
    args = ap.parse_args()

    offline, om, scrape, wttr, om_live = {}, {}, {}, {}, {}

    if args.cmd in ("collect", "all"):
        log("offline: nwp_forecasts.db")
        offline = collect_nwpdb()
        log(f"  nwpdb entries: {len(offline)}")
        log("offline: NWS archive")
        nws = collect_nws_archive()
        for k, d in nws.items():
            offline.setdefault(k, {}).update(d)
        log("network: Open-Meteo historical (9 models × 20 stations)")
        om = collect_om_historical(force=args.force)
        log(f"  om entries: {len(om)}")

    if args.cmd == "analyze":
        if OM_CACHE.exists():
            om = _unpack_cache(json.loads(OM_CACHE.read_text()))
        offline = collect_nwpdb()
        nws = collect_nws_archive()
        for k, d in nws.items():
            offline.setdefault(k, {}).update(d)
        if SCRAPE_CACHE.exists():
            scrape = json.loads(SCRAPE_CACHE.read_text())

    if args.cmd == "all":
        log("network: scrapers (wunderground, weather.com) + wttr + OM live")
        scrape = scrape_current()
        wttr = scrape_wttr()
        om_live = collect_om_live()

    if args.cmd in ("analyze", "all"):
        log("analyzing")
        summary = analyze(offline, om, scrape, wttr, om_live)
        print(json.dumps(summary, indent=2)[:4000])


if __name__ == "__main__":
    main()
