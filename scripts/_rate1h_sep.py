#!/usr/bin/env python3
"""_rate1h_sep.py — shared rate_1h separation index over the ASOS 1-min archive.

Replicates scripts/sweep_intraday_rate1h_crossing.py EXACTLY (same grid, same
15-min buckets, same ~1h momentum, same strict "next integer strike" label),
but keeps per-STATION and per-(station,window) granularity so downstream
graders can score individual trades by the station that actually traded.

READ-ONLY. Caches the small per-station/per-cell table to
data/rate1h_station_sep.json so the 20M-row ASOS pass is paid only once.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ASOS = REPO / "data" / "iem_asos_1min.db"
SWEEP = REPO / "data" / "sweep_intraday_rate1h_crossing.json"
CACHE = REPO / "data" / "rate1h_station_sep.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
THRESHOLDS = [0.5, 1.0]
MOM_LOOKBACK = 4
BUCKET_MIN = 15
MIN_CELL = 10

_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def load_bucket_means():
    """{(station, local_date, minute_of_day): mean_tmpf_F} — 15-min UTC buckets."""
    c = sqlite3.connect(f"file:{ASOS}?mode=ro", uri=True)
    cur = c.execute("SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL")
    acc = defaultdict(lambda: [0.0, 0])
    for st, valid, tmpf in cur:
        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        loc = dt.astimezone(ZoneInfo(_ICAO_TZ.get(st, "America/New_York")))
        mod = dt.hour * 60 + (dt.minute // BUCKET_MIN) * BUCKET_MIN
        slot = acc[(st, loc.date().isoformat(), mod)]
        slot[0] += float(tmpf)
        slot[1] += 1
    c.close()
    return {k: v[0] / v[1] for k, v in acc.items() if v[1] > 0}


def build_samples(bucket_means):
    """[(station, local_date, hour, rate_1h, crossed)] — sweep `evaluate`. """
    by_sd = defaultdict(dict)
    for (st, ld, mod), m in bucket_means.items():
        by_sd[(st, ld)][mod] = m
    samples = []
    for (st, ld), mods in by_sd.items():
        ordered = sorted(mods.items())
        minutes = [m for m, _ in ordered]
        means = [v for _, v in ordered]
        dmax = max(means)
        for h in GRID_HOURS:
            dec_mod = h * 60
            if dec_mod not in mods:
                continue
            idx = minutes.index(dec_mod)
            if idx < MOM_LOOKBACK:
                continue
            prior = means[idx - MOM_LOOKBACK: idx]
            if len(prior) < MOM_LOOKBACK:
                continue
            rate_1h = means[idx] - sum(prior) / len(prior)
            high_so_far = max(means[: idx + 1])
            strike = math.floor(high_so_far) + 1.0
            crossed = 1 if dmax > strike else 0
            samples.append((st, ld, h, rate_1h, crossed))
    return samples


def _sep(rows, threshold):
    up = [s for s in rows if s[3] >= threshold]
    dn = [s for s in rows if s[3] <= -threshold]
    pu = sum(s[4] for s in up) / len(up) if up else 0.0
    pd = sum(s[4] for s in dn) / len(dn) if dn else 0.0
    return {"sep_pp": round(100 * (pu - pd), 2), "n_up": len(up), "n_down": len(dn)}


def build(threshold=1.0, force=False):
    """Return the separation index dict (from cache unless force)."""
    if CACHE.exists() and not force:
        cached = json.loads(CACHE.read_text())
        if cached.get("threshold") == threshold:
            return cached
    samples = build_samples(load_bucket_means())
    stations = sorted({s[0] for s in samples})
    windows = sorted({s[2] for s in samples})

    per_station = {}
    for st in stations:
        v = _sep([s for s in samples if s[0] == st], threshold)
        v["n_up"] = v["n_up"]
        per_station[st] = v

    per_station_window = {}
    for st in stations:
        for h in windows:
            key = f"{st}|{h:02d}Z"
            per_station_window[key] = _sep(
                [s for s in samples if s[0] == st and s[2] == h], threshold)

    per_window = {}
    for h in windows:
        per_window[f"{h:02d}Z"] = _sep([s for s in samples if s[2] == h], threshold)

    idx = {
        "threshold": threshold,
        "samples": len(samples),
        "stations": stations,
        "windows_utc": windows,
        "min_cell": MIN_CELL,
        "per_station": per_station,
        "per_station_window": per_station_window,
        "per_window": per_window,
    }
    CACHE.write_text(json.dumps(idx, indent=1))
    return idx


def station_median_sep(idx, station):
    """Median of the station's per-window separations (fallback: per-station)."""
    vals = [v["sep_pp"] for k, v in idx["per_station_window"].items()
            if k.startswith(station + "|")]
    if vals:
        vals.sort()
        return vals[len(vals) // 2]
    return idx["per_station"].get(station, {}).get("sep_pp")


def sep_for_trade(idx, station, window_hour):
    """Prefer the station-window cell (if sample-sufficient), else the station row."""
    key = f"{station}|{window_hour:02d}Z"
    cell = idx["per_station_window"].get(key)
    if cell and cell["n_up"] >= MIN_CELL and cell["n_down"] >= MIN_CELL:
        return cell["sep_pp"], f"station-window {window_hour:02d}Z"
    st = idx["per_station"].get(station)
    if st and st["n_up"] >= MIN_CELL and st["n_down"] >= MIN_CELL:
        return st["sep_pp"], "station-all-windows"
    if cell:
        return cell["sep_pp"], f"station-window {window_hour:02d}Z (thin)"
    return (st or {}).get("sep_pp"), "station-all-windows (thin)"


def validate_against_sweep(idx):
    """Cross-check our window-level separations vs the published sweep JSON."""
    sweep = json.loads(SWEEP.read_text())
    checks = []
    for w, entry in sweep["windows"].items():
        thr = entry["thresholds"].get("\u00b11.0")
        if not thr:
            continue
        checks.append({
            "window": w,
            "sweep_sep_pp": thr["separation"]["sep_pp"],
            "sweep_n_up": thr["separation"]["n_up"],
            "sweep_n_down": thr["separation"]["n_down"],
            "our_sep_pp": idx["per_window"].get(w, {}).get("sep_pp"),
            "our_n_up": idx["per_window"].get(w, {}).get("n_up"),
            "our_n_down": idx["per_window"].get(w, {}).get("n_down"),
        })
    return checks


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    idx = build(force=force)
    print(f"samples={idx['samples']} stations={len(idx['stations'])} "
          f"windows={idx['windows_utc']} -> {CACHE}")
    print("per-station sep (pp @±1.0):")
    for st, v in idx["per_station"].items():
        print(f"  {st}: {v['sep_pp']:6.2f}  (n_up={v['n_up']}, n_down={v['n_down']})")
