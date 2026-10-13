#!/usr/bin/env python3
"""
pws_trajectory_test_2026-10-13.py

TASK 1 — PWS-augmented rate_1h trajectory test.

QUESTION
  Weather Underground's PWS network is 180k+ personal stations. We do NOT need
  Wunderground's keyed API: NOAA's MADIS LDAD mesonet feed
  (https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF/)
  is FREE, keyless, hourly, NetCDF, and already aggregates the same citizen
  networks (APRSWXNET/CWOP, MesoWest, university + DOT mesonets). It keeps a
  rolling ~5-day window in the realtime directory.

  Test: for 5 of our 20 ASOS stations (KLAX, KATL, KDEN, KSEA, KMIA), does
  adding nearby PWS/mesonet observations change the ~1h rate-of-change
  trajectory signal — lower variance? detectable microclimate divergence?

METHOD
  * Window  : every hourly MADIS mesonet file currently published (~5 days).
  * Nearby  : PWS within R_MI of each target ASOS, reporting temperature with
              >= MIN_COVER hour coverage.
  * Series  : ASOS hourly mean tmpf from data/iem_asos_1min.db (the same
              archive the production rate_1h family reads).
  * rate_1h : T(hour) - T(hour-1)   [~1-hour momentum; PWS feed is hourly so
              this is the 1-h rate at hourly resolution, not the 15-min grid]
  * Variants: (a) ASOS alone (baseline)
              (b) PWS mean alone
              (c) ASOS + PWS blend (simple mean)
  * De-bias  : each PWS has a siting/elevation offset vs its ASOS. We report
              raw AND offset-removed ("*_deb") rates, because a bias cancels
              in a difference while a real microclimate gradient does not.

RESOURCE DISCIPLINE
  * Each 33 MB MADIS file is parsed IN MEMORY and discarded; only the tiny
    per-station extraction is cached (data/_pws_madis_cache/, deleted at end).
  * No temp DBs. Read-only on the live book. Nothing wired.

Usage:
    .venv-gefs/bin/python scripts/pws_trajectory_test_2026-10-13.py fetch
    .venv-gefs/bin/python scripts/pws_trajectory_test_2026-10-13.py analyze
    .venv-gefs/bin/python scripts/pws_trajectory_test_2026-10-13.py all
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import sqlite3
import statistics
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.io import netcdf_file

REPO = Path(__file__).resolve().parents[1]
ASOS_DB = REPO / "data" / "iem_asos_1min.db"
CACHE_DIR = REPO / "data" / "_pws_madis_cache"
OUT_DIR = REPO / "artifacts" / "pws-trajectory"
OUT_JSON = OUT_DIR / "pws_trajectory_test_2026-10-13.json"

MADIS_BASE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF/"
UA = {"User-Agent": "weather-engine-research/1.0 (dan@openclaw.ai)"}

TARGETS = {
    "KLAX": (33.9425, -118.4081),
    "KATL": (33.6407, -84.4277),
    "KDEN": (39.8561, -104.6737),
    "KSEA": (47.4502, -122.3088),
    "KMIA": (25.7933, -80.2906),
}
R_MI = 5.0            # primary radius
R_MI_FALLBACK = 12.0  # widen if too few stations
MIN_COVER = 0.5       # PWS must report in >=50% of the shared hours
CITIZEN_PROVIDERS = {"APRSWXNET"}  # CWOP-style citizen stations


def _f(kelvin: float) -> float:
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0


def hav_vec(lat0, lon0, lat1, lon1):
    R = 3958.8
    la1, lo1 = np.radians(lat0), np.radians(lon0)
    la2, lo2 = np.radians(lat1), np.radians(lon1)
    return 2 * R * np.arcsin(np.sqrt(np.sin((la2 - la1) / 2) ** 2
                                     + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2))


def txt(v, i, field):
    return b"".join(v[field][i]).decode("ascii", "replace").strip()


# --------------------------------------------------------------------------
# FETCH
# --------------------------------------------------------------------------
def list_madis_files() -> list[str]:
    req = urllib.request.Request(MADIS_BASE, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    import re
    names = sorted(set(re.findall(r"(2026\d{4}_\d{4}\.gz)", html)))
    return names


def fetch():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files = list_madis_files()
    print(f"MADIS files available: {len(files)}  ({files[0]} .. {files[-1]})")
    # per-file extraction: {file: {station: {...}}}  -> we keep a flat list
    rows = []  # each: [file_ts, target, station, provider, type, dist_mi, tempF]
    for k, fn in enumerate(files):
        try:
            raw = gzip.open(io.BytesIO(
                urllib.request.urlopen(urllib.request.Request(MADIS_BASE + fn, headers=UA), timeout=120).read()), "rb").read()
        except Exception as e:
            print(f"  [{k+1}/{len(files)}] {fn} DOWNLOAD ERR {e}")
            continue
        f = netcdf_file(io.BytesIO(raw), mmap=False)
        v = f.variables
        n = v["stationName"].shape[0]
        lat = np.asarray(v["latitude"][:], dtype="float64")
        lon = np.asarray(v["longitude"][:], dtype="float64")
        temp = np.asarray(v["temperature"][:], dtype="float64")  # Kelvin
        valid = np.isfinite(temp) & (temp > 150.0) & (temp < 350.0)
        for tgt, (alat, alon) in TARGETS.items():
            d = hav_vec(alat, alon, lat, lon)
            m = valid & (d <= R_MI_FALLBACK)
            for i in np.where(m)[0]:
                rows.append([fn, tgt, txt(v, i, "stationName"),
                             txt(v, i, "dataProvider"), txt(v, i, "stationType"),
                             round(float(d[i]), 3), round(_f(float(temp[i])), 3)])
        f.close()
        del raw
        if (k + 1) % 20 == 0 or k == len(files) - 1:
            print(f"  [{k+1}/{len(files)}] {fn}: cumulative rows={len(rows)}", flush=True)
    (CACHE_DIR / "pws_rows.json").write_text(json.dumps(rows))
    print(f"cached {len(rows)} PWS observations -> {CACHE_DIR / 'pws_rows.json'}")


# --------------------------------------------------------------------------
# ANALYZE
# --------------------------------------------------------------------------
def asos_hourly(targets, start: str, end: str) -> dict:
    c = sqlite3.connect(f"file:{ASOS_DB}?mode=ro", uri=True)
    out = {}
    for st in targets:
        cur = c.execute(
            "SELECT valid, tmpf FROM observations WHERE station=? AND tmpf IS NOT NULL "
            "AND valid>=? AND valid<=?", (st, start, end))
        acc = defaultdict(list)
        for valid, tmpf in cur:
            hr = valid[:13]  # 'YYYY-MM-DD HH'
            acc[hr].append(tmpf)
        out[st] = {hr: sum(v) / len(v) for hr, v in acc.items()}
    c.close()
    return out


def _rates(series: dict, hours: list[str]) -> list[float]:
    """T(h) - T(h-1) over the supplied ordered hours; None where either missing."""
    out = []
    for i in range(1, len(hours)):
        a, b = series.get(hours[i]), series.get(hours[i - 1])
        out.append(None if (a is None or b is None) else round(a - b, 3))
    return out


def analyze():
    rows = json.loads((CACHE_DIR / "pws_rows.json").read_text())
    print(f"loaded {len(rows)} cached PWS rows")
    # file -> hour key
    def hr_key(fn):
        return f"{fn[:4]}-{fn[4:6]}-{fn[6:8]} {fn[9:11]}"
    # gather all hour keys present
    all_hours = sorted({hr_key(r[0]) for r in rows})

    asos = asos_hourly(TARGETS.keys(), all_hours[0] + ":00", all_hours[-1] + ":59")

    report = {"generated": datetime.now(timezone.utc).isoformat(),
              "window": {"hours": len(all_hours), "first": all_hours[0], "last": all_hours[-1]},
              "radius_mi": R_MI, "radius_fallback_mi": R_MI_FALLBACK,
              "min_cover": MIN_COVER, "targets": {}}

    for tgt in TARGETS:
        all_hours = sorted({hr_key(r[0]) for r in rows})
        # build per-station hourly series within R_MI
        by_station = defaultdict(dict)   # station -> {hour: tempF}
        meta = {}
        for fn, t, st, prov, typ, dist, tf in rows:
            if t != tgt:
                continue
            h = hr_key(fn)
            by_station[st].setdefault(h, []).append(tf)
            meta[st] = {"provider": prov, "type": typ, "dist_mi": dist}
        # pick stations inside primary radius with coverage
        picked = []
        for st, hrs in by_station.items():
            if meta[st]["dist_mi"] > R_MI:
                continue
            series = {h: sum(v) / len(v) for h, v in hrs.items()}
            cov = len(series) / max(1, len(all_hours))
            if cov >= MIN_COVER:
                picked.append((st, series, cov))
        if len(picked) < 2:  # widen
            picked = []
            for st, hrs in by_station.items():
                series = {h: sum(v) / len(v) for h, v in hrs.items()}
                cov = len(series) / max(1, len(all_hours))
                if cov >= MIN_COVER and meta[st]["dist_mi"] <= R_MI_FALLBACK:
                    picked.append((st, series, cov))
        picked.sort(key=lambda x: meta[x[0]]["dist_mi"])

        hours = all_hours
        asos_ser = asos[tgt]
        # PWS mean series (only hours where >=1 picked PWS reports)
        pws_mean, pws_n = {}, {}
        for h in hours:
            vals = [s[h] for _, s, _ in picked if h in s]
            if vals:
                pws_mean[h] = sum(vals) / len(vals)
                pws_n[h] = len(vals)
        # blend
        blend = {}
        for h in hours:
            if h in asos_ser and h in pws_mean:
                blend[h] = 0.5 * asos_ser[h] + 0.5 * pws_mean[h]
            elif h in asos_ser:
                blend[h] = asos_ser[h]
            elif h in pws_mean:
                blend[h] = pws_mean[h]

        # de-biased PWS: remove each PWS's mean offset from ASOS over overlap
        deb_series = {}
        for st, s, _ in picked:
            offs = [s[h] - asos_ser[h] for h in s if h in asos_ser]
            off = sum(offs) / len(offs) if offs else 0.0
            deb_series[st] = {h: s[h] - off for h in s}
        pws_deb = {}
        for h in hours:
            vals = [deb_series[st][h] for st, _, _ in picked if h in deb_series[st]]
            if vals:
                pws_deb[h] = sum(vals) / len(vals)

        r_asos = _rates(asos_ser, hours)
        r_pws = _rates(pws_mean, hours)
        r_deb = _rates(pws_deb, hours)
        r_blend = _rates(blend, hours)

        def stats(rr):
            v = [x for x in rr if x is not None]
            if len(v) < 3:
                return {"n": len(v)}
            return {"n": len(v), "mean": round(statistics.fmean(v), 3),
                    "sd": round(statistics.pstdev(v), 3),
                    "min": min(v), "max": max(v)}

        def corr(a, b):
            pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
            if len(pairs) < 3:
                return None
            xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            num = sum((x - mx) * (y - my) for x, y in pairs)
            dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            dy = math.sqrt(sum((y - my) ** 2 for y in ys))
            return round(num / (dx * dy), 3) if dx and dy else None

        report["targets"][tgt] = {
            "n_pws_picked": len(picked),
            "pws_stations": [{"id": st, "provider": meta[st]["provider"],
                              "type": meta[st]["type"], "dist_mi": meta[st]["dist_mi"],
                              "cover": round(cov, 3)} for st, _, cov in picked],
            "hours": len(hours),
            "pws_count_median": int(statistics.median(list(pws_n.values()))) if pws_n else 0,
            "rate_asos": stats(r_asos),
            "rate_pws_mean": stats(r_pws),
            "rate_pws_mean_debiased": stats(r_deb),
            "rate_blend": stats(r_blend),
            "corr_asos_vs_pws": corr(r_asos, r_pws),
            "corr_asos_vs_pws_deb": corr(r_asos, r_deb),
            "mean_abs_divergence_pws_vs_asos": (
                round(statistics.fmean(abs(x - y) for x, y in zip(r_asos, r_pws)
                                       if x is not None and y is not None), 3)
                if any(x is not None and y is not None for x, y in zip(r_asos, r_pws)) else None),
            "asos_hourly_sd": (round(statistics.pstdev([asos_ser[h] for h in hours if h in asos_ser]), 3)
                               if len([h for h in hours if h in asos_ser]) > 2 else None),
        }
        print(f"{tgt}: pws={len(picked)} hours={len(hours)} "
              f"sd(asos)={report['targets'][tgt]['rate_asos'].get('sd')} "
              f"sd(pws)={report['targets'][tgt]['rate_pws_mean'].get('sd')} "
              f"r={report['targets'][tgt]['corr_asos_vs_pws']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2))
    print(f"wrote {OUT_JSON}")


def cleanup():
    import shutil
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"removed {CACHE_DIR}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "analyze", "all", "clean"])
    a = ap.parse_args()
    if a.cmd in ("fetch", "all"):
        fetch()
    if a.cmd in ("analyze", "all"):
        analyze()
    if a.cmd == "clean":
        cleanup()
