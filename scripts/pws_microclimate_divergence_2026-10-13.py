#!/usr/bin/env python3
"""
pws_microclimate_divergence_2026-10-13.py

B3 — PWS microclimate divergence as its own LEVEL signal.

BACKGROUND
  The PWS trajectory test (scripts/pws_trajectory_test_2026-10-13.py) found that
  ASOS-vs-nearby-PWS microclimate divergence is a *level* phenomenon (a station
  offset / local siting gradient), not a *rate* one: it did not improve the
  ~1h rate-of-change trajectory. So it belongs in its own signal, not inside
  rate_1h.

QUESTION
  divergence[h] = ASOS_tmpf[h] - median(nearby_PWS_tmpf[h])   (hourly level)
  Does a *growing* divergence at hour X predict an ASOS rate shift at hour X+1?
  i.e. cross-correlate divergence[X] against forward rate  (T[X+1]-T[X]).

METHOD
  * Feed    : MADIS LDAD mesonet NetCDF (free, keyless, hourly, rolling ~5d).
  * Nearby  : PWS within R_MI of each target ASOS reporting temperature with
              >= MIN_COVER hour coverage.  Median (robust) not mean.
  * Series  : ASOS hourly mean tmpf from data/iem_asos_1min.db (production source).
  * Level   : raw divergence + anomaly divergence (level minus its own window
              mean -> isolates *growing/shrinking* divergence; a constant siting
              offset cancels, which is the whole point of a level signal).
  * Cross-corr: divergence[h] vs (a) contemporaneous rate (T[h]-T[h-1]),
              (b) forward rate (T[h+1]-T[h])  <- the predictive question,
              (c) backward rate (T[h]-T[h-1]) vs divergence[h-1] (lead check).

RESOURCE DISCIPLINE
  * Each ~33 MB MADIS file parsed in memory, discarded; only tiny per-station
    extraction cached (data/_pws_madis_cache/, deleted at end).
  * No temp DBs. Read-only on the live book. Nothing wired.

Usage:
    .venv-gefs/bin/python scripts/pws_microclimate_divergence_2026-10-13.py fetch
    .venv-gefs/bin/python scripts/pws_microclimate_divergence_2026-10-13.py analyze
    .venv-gefs/bin/python scripts/pws_microclimate_divergence_2026-10-13.py all
    .venv-gefs/bin/python scripts/pws_microclimate_divergence_2026-10-13.py clean
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import re
import shutil
import sqlite3
import statistics
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.io import netcdf_file

REPO = Path(__file__).resolve().parents[1]
ASOS_DB = REPO / "data" / "iem_asos_1min.db"
CACHE_DIR = REPO / "data" / "_pws_madis_cache"
OUT_DIR = REPO / "artifacts" / "pws-divergence"
OUT_JSON = OUT_DIR / "pws_microclimate_divergence_2026-10-13.json"

MADIS_BASE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF/"
UA = {"User-Agent": "weather-engine-research/1.0 (dan@openclaw.ai)"}

TARGETS = {
    "KLAX": (33.9425, -118.4081),
    "KATL": (33.6407, -84.4277),
    "KDEN": (39.8561, -104.6737),
    "KSEA": (47.4502, -122.3088),
    "KMIA": (25.7933, -80.2906),
}
R_MI = 5.0            # primary radius (analysis)
R_MI_FALLBACK = 12.0  # widen if too few stations
MIN_COVER = 0.5       # PWS must report in >=50% of shared hours
MIN_N_PWS = 3         # need >=3 PWS for a stable median in the divergence


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
# FETCH  (identical extraction to the proven trajectory test)
# --------------------------------------------------------------------------
def list_madis_files() -> list[str]:
    req = urllib.request.Request(MADIS_BASE, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    return sorted(set(re.findall(r"(2026\d{4}_\d{4}\.gz)", html)))


def fetch():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files = list_madis_files()
    print(f"MADIS files available: {len(files)}  ({files[0]} .. {files[-1]})", flush=True)
    rows = []
    for k, fn in enumerate(files):
        try:
            raw = gzip.open(io.BytesIO(
                urllib.request.urlopen(urllib.request.Request(MADIS_BASE + fn, headers=UA),
                                       timeout=120).read()), "rb").read()
        except Exception as e:
            print(f"  [{k+1}/{len(files)}] {fn} DOWNLOAD ERR {e}", flush=True)
            continue
        f = netcdf_file(io.BytesIO(raw), mmap=False)
        v = f.variables
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
    print(f"cached {len(rows)} PWS observations -> {CACHE_DIR / 'pws_rows.json'}", flush=True)


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
            acc[valid[:13]].append(tmpf)
        out[st] = {hr: sum(v) / len(v) for hr, v in acc.items()}
    c.close()
    return out


def _corr(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 4:
        return {"n": len(pairs), "r": None}
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in pairs)
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    r = round(num / (da * db), 4) if da and db else None
    return {"n": len(pairs), "r": r}


def _partial_corr(a, b, c):
    """Partial correlation of a,b controlling for c.

    Necessary here because div_anom[h] = ASOS[h] - PWS[h] and
    fwd_rate[h] = ASOS[h+1] - ASOS[h] BOTH embed ASOS[h]; a raw correlation is
    partly mechanical. Removing the shared ASOS[h] term leaves the genuine
    divergence<->rate association (if any).
    """
    rows = [(x, y, z) for x, y, z in zip(a, b, c)
            if x is not None and y is not None and z is not None]
    if len(rows) < 6:
        return {"n": len(rows), "r": None}
    xs = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    zs = [r[2] for r in rows]

    def resid(v):
        mv, mz = statistics.fmean(v), statistics.fmean(zs)
        cov = sum((vi - mv) * (zi - mz) for vi, zi in zip(v, zs))
        var = sum((zi - mz) ** 2 for zi in zs)
        slope = cov / var if var else 0.0
        return [vi - (mv + slope * (zi - mz)) for vi, zi in zip(v, zs)]

    ra, rb = resid(xs), resid(ys)
    mr_a, mr_b = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - mr_a) * (y - mr_b) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - mr_a) ** 2 for x in ra))
    db = math.sqrt(sum((y - mr_b) ** 2 for y in rb))
    return {"n": len(rows), "r": round(num / (da * db), 4) if da and db else None}


def _shift_null(a, b, n=500):
    """Circular-shift null: p(|r_shift| >= |r_obs|) over n shifts.

    A lead-lag claim must beat the autocorrelation-preserving null; a raw r of
    0.3 on ~120 overlapping hourly samples is NOT significant on its own.
    """
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 10:
        return {"n": len(pairs), "p": None, "r_obs": None}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    obs = _corr(xs, ys)["r"]
    if obs is None:
        return {"n": len(pairs), "p": None, "r_obs": None}
    m = len(ys)
    beat = 0
    for s in range(1, m):
        yshift = ys[s:] + ys[:s]
        r = _corr(xs, yshift)["r"]
        if r is not None and abs(r) >= abs(obs):
            beat += 1
    return {"n": len(pairs), "r_obs": obs, "p": round(beat / (m - 1), 4)}


def _spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 4:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    # Pearson on ranks
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(num / (dx * dy), 4) if dx and dy else None


def analyze():
    rows = json.loads((CACHE_DIR / "pws_rows.json").read_text())
    print(f"loaded {len(rows)} cached PWS rows", flush=True)

    def hr_key(fn):
        return f"{fn[:4]}-{fn[4:6]}-{fn[6:8]} {fn[9:11]}"

    all_hours = sorted({hr_key(r[0]) for r in rows})
    asos = asos_hourly(TARGETS.keys(), all_hours[0] + ":00", all_hours[-1] + ":59")

    report = {"generated": datetime.now(timezone.utc).isoformat(),
              "signal": "pws_microclimate_divergence",
              "definition": "ASOS_tmpf[h] - median(nearby PWS tmpf[h])",
              "window": {"hours": len(all_hours), "first": all_hours[0], "last": all_hours[-1]},
              "radius_mi": R_MI, "radius_fallback_mi": R_MI_FALLBACK,
              "min_cover": MIN_COVER, "min_n_pws": MIN_N_PWS,
              "targets": {}}

    for tgt, (_alat, _alon) in TARGETS.items():
        by_station = defaultdict(dict)
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
            if len(series) / max(1, len(all_hours)) >= MIN_COVER:
                picked.append((st, series))
        if len(picked) < MIN_N_PWS:
            picked = []
            for st, hrs in by_station.items():
                series = {h: sum(v) / len(v) for h, v in hrs.items()}
                if (len(series) / max(1, len(all_hours)) >= MIN_COVER
                        and meta[st]["dist_mi"] <= R_MI_FALLBACK):
                    picked.append((st, series))
        picked.sort(key=lambda x: meta[x[0]]["dist_mi"])

        asos_ser = asos[tgt]
        # hourly median PWS series (independent per hour; needs >=2 members)
        pws_med, pws_n = {}, {}
        for h in all_hours:
            vals = [s[h] for _, s in picked if h in s]
            if len(vals) >= 2:
                pws_med[h] = statistics.median(vals)
                pws_n[h] = len(vals)

        # raw divergence level
        div = {}
        for h in all_hours:
            if h in asos_ser and h in pws_med:
                div[h] = asos_ser[h] - pws_med[h]
        # anomaly divergence: remove window-mean offset (kills constant siting bias)
        dv = [div[h] for h in all_hours if h in div]
        mean_div = statistics.fmean(dv) if dv else 0.0
        div_anom = {h: v - mean_div for h, v in div.items()}

        # forward rate: T[h+1]-T[h] for each h
        fwd_rate = {}
        # contemporaneous rate: T[h]-T[h-1]
        con_rate = {}
        for i, h in enumerate(all_hours):
            if h in asos_ser and i + 1 < len(all_hours) and all_hours[i + 1] in asos_ser:
                fwd_rate[h] = asos_ser[all_hours[i + 1]] - asos_ser[h]
            if h in asos_ser and i - 1 >= 0 and all_hours[i - 1] in asos_ser:
                con_rate[h] = asos_ser[h] - asos_ser[all_hours[i - 1]]

        hours_div = all_hours
        d_raw = [div.get(h) for h in hours_div]
        d_anom = [div_anom.get(h) for h in hours_div]
        f_rate = [fwd_rate.get(h) for h in hours_div]
        c_rate = [con_rate.get(h) for h in hours_div]

        # Directional agreement: sign(div_anom[h]) vs sign(fwd_rate[h])
        def dir_agree(xs, ys):
            pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
            if not pairs:
                return None
            hit = sum(1 for x, y in pairs if (x > 0) == (y > 0) and x != 0 and y != 0)
            nz = [p for p in pairs if p[0] != 0 and p[1] != 0]
            return {"n": len(nz), "agree": hit,
                    "rate": round(hit / len(nz), 4) if nz else None}

        # lag sweep: div_anom[h] vs fwd_rate[h+k] for k in -2..2
        lag_sweep = {}
        for k in (-2, -1, 0, 1, 2):
            if k >= 0:
                a = [div_anom.get(h) for h in hours_div]
                b = [fwd_rate.get(hours_div[i + k]) if i + k < len(hours_div) else None
                     for i in range(len(hours_div))]
            else:
                a = [div_anom.get(hours_div[i + k]) if i + k >= 0 else None
                     for i in range(len(hours_div))]
                b = [fwd_rate.get(h) for h in hours_div]
            lag_sweep[str(k)] = _corr(a, b)

        report["targets"][tgt] = {
            "n_pws_picked": len(picked),
            "pws_stations": [{"id": st, "provider": meta[st]["provider"],
                              "type": meta[st]["type"], "dist_mi": meta[st]["dist_mi"]}
                             for st, _ in picked],
            "hours": len(hours_div),
            "hours_with_divergence": len(div),
            "pws_count_median": int(statistics.median(list(pws_n.values()))) if pws_n else 0,
            "divergence_level": {
                "mean_f": round(mean_div, 3),
                "sd_f": round(statistics.pstdev(dv), 3) if len(dv) > 2 else None,
                "min_f": round(min(dv), 3) if dv else None,
                "max_f": round(max(dv), 3) if dv else None,
            },
            "forward_rate_sd_f": (round(statistics.pstdev([x for x in f_rate if x is not None]), 3)
                                  if len([x for x in f_rate if x is not None]) > 2 else None),
            "corr_divraw_vs_fwdrate": _corr(d_raw, f_rate),
            "corr_divanom_vs_fwdrate": _corr(d_anom, f_rate),
            "corr_divanom_vs_conrate": _corr(d_anom, c_rate),
            "spearman_divanom_vs_fwdrate": _spearman(d_anom, f_rate),
            "partial_corr_divanom_vs_fwdrate_ctrl_asos": _partial_corr(
                d_anom, f_rate, [asos_ser.get(h) for h in hours_div]),
            "shift_null_divanom_vs_fwdrate": _shift_null(d_anom, f_rate),
            "dir_agree_divanom_vs_fwdrate": dir_agree(d_anom, f_rate),
            "lag_sweep_divanom_vs_fwdrate": lag_sweep,
        }
        t = report["targets"][tgt]
        print(f"{tgt}: pws={len(picked)} hours={len(hours_div)} div_only={len(div)} "
              f"mean_div={t['divergence_level']['mean_f']} sd_div={t['divergence_level']['sd_f']} "
              f"r_anom~fwd={t['corr_divanom_vs_fwdrate']['r']} "
              f"dir={t['dir_agree_divanom_vs_fwdrate']}", flush=True)

    # pool across stations
    pooled = {"n_targets_with_div": 0}
    allr = []
    for tgt, t in report["targets"].items():
        r = t["corr_divanom_vs_fwdrate"]["r"]
        if r is not None:
            allr.append(r)
    pooled["mean_r_anom_vs_fwdrate"] = round(statistics.fmean(allr), 4) if allr else None
    pooled["n_targets"] = len(allr)
    report["pooled"] = pooled

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2))
    print(f"wrote {OUT_JSON}", flush=True)


def cleanup():
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
