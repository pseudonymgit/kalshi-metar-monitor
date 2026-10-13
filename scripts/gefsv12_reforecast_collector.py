#!/usr/bin/env python3
"""
gefsv12_reforecast_collector.py — Item 4 of the Brier improvement sprint.

PURPOSE
  Collect GEFSv12 REFORECAST daily 2-m maximum temperature for the 20 canonical
  Kalshi stations, into data/gefsv12_reforecast.db. GEFSv12 reforecast is the
  best free resource we lack for bias-correction and analog post-processing
  (fixed-model, fixed-resolution, multi-member, 2000-2019).

  ▸ THIS SCRIPT IS PREPARED, NOT RUN. The bulk backfill is deliberately NOT
    started (per Dan's instruction). Use `probe` to validate the path, then
    `collect` at your discretion.

SOURCE (AWS Open Data, keyless, no token)
  Bucket : noaa-gefs-retrospective
  Path   : GEFSv12/reforecast/<YYYY>/<YYYYMMDD>00/<member>/Days:1-10/
              tmax_2m_<YYYYMMDD>00_<member>.grib2(.idx)
  Members: c00 (control) + p01..p04  → 5 members per init
  Cadence: ONE cycle per day (00Z). One file per member covers forecast
           Days 1-10; a second file (Days:10-16) covers days 10-16.
  Grid   : global 0.25° (721 x 1440), lat 90..-90, lon 0..359.75
  Record : TMAX:2 m above ground, stepped (3h and 6h) forecast-window maxima.
           The `.idx` sidecar gives exact byte offsets → we fetch ONLY the
           6-hour-window records (~700 KB each) rather than the ~59 MB file.

DAILY MAX
  GEFS encodes TMAX as window maxima, e.g. "0-3", "0-6", "6-9", "6-12",
  "12-15", "12-18", "18-21", "18-24", "24-27", ... The four 6-hour windows
  per 24 h partition the forecast day, so
      daily_max(day d) = max over 6h-window records with ceil(end_hour/24) == d
  The 3-hour records are subsets and are skipped.

UNITS / TIME
  TMAX is KELVIN → °F = K·9/5 − 459.67.  Windows are FORECAST HOURS from the
  00Z init (UTC). A US station's local calendar day is offset from the UTC
  forecast day; the collector stores the forecast window, and the consumer
  aligns local day (see docs/plans/GEFSV12-REFORECAST-COLLECTOR-2026-10-13.md).

READ-ONLY on the live book. Writes ONLY data/gefsv12_reforecast.db (+ /tmp cache).

Usage
  python3 scripts/gefsv12_reforecast_collector.py probe
  python3 scripts/gefsv12_reforecast_collector.py collect --start 2010-01-01 \\
      --end 2010-01-31 [--members c00] [--days 3] [--stations KATL,KSEA]
  python3 scripts/gefsv12_reforecast_collector.py status
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "gefsv12_reforecast.db"
CACHE = Path("/tmp/gefsv12_cache")
BUCKET = "https://noaa-gefs-retrospective.s3.amazonaws.com"
PREFIX = "GEFSv12/reforecast"
MEMBERS = ["c00", "p01", "p02", "p03", "p04"]
REQ_DELAY_S = 2.0                    # polite spacing (>= 2 s between any request)
UA = "weather-engine-research/1.0 (GEFSv12 reforecast collector; internal)"

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]
# canonical Kalshi settlement-site coordinates
COORDS = {
    "KATL": (33.6407, -84.4277), "KAUS": (30.1945, -97.6699), "KBOS": (42.3631, -71.0064),
    "KDCA": (38.8512, -77.0402), "KDEN": (39.8561, -104.6737), "KDFW": (32.8998, -97.0403),
    "KHOU": (29.6454, -95.2789), "KLAS": (36.0840, -115.1537), "KLAX": (33.9425, -118.4081),
    "KMDW": (41.7868, -87.7522), "KMIA": (25.7932, -80.2906), "KMSP": (44.8848, -93.2223),
    "KMSY": (29.9934, -90.2580), "KNYC": (40.6413, -73.7781), "KOKC": (35.3931, -97.6007),
    "KPHL": (39.8719, -75.2411), "KPHX": (33.4342, -112.0116), "KSAT": (29.5337, -98.4698),
    "KSEA": (47.4502, -122.3088), "KSFO": (37.6188, -122.3754),
}

DDL = """
CREATE TABLE IF NOT EXISTS gefsv12_reforecast (
    station        TEXT NOT NULL,
    init_date      TEXT NOT NULL,   -- YYYY-MM-DD (00Z init)
    member         TEXT NOT NULL,   -- c00 | p01..p04
    forecast_day   INTEGER NOT NULL,-- 1-based, 24h window from init
    tmax_f         REAL,            -- daily max 2m temp, °F
    window_start_h INTEGER,         -- first forecast hour contributing
    window_end_h   INTEGER,         -- last forecast hour contributing
    n_records      INTEGER,         -- 6h records folded into the daily max
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (station, init_date, member, forecast_day)
);
CREATE TABLE IF NOT EXISTS gefsv12_collect_log (
    init_date  TEXT NOT NULL,
    member     TEXT NOT NULL,
    status     TEXT NOT NULL,       -- ok | missing | error
    n_rows     INTEGER,
    note       TEXT,
    ts_utc     TEXT NOT NULL,
    PRIMARY KEY (init_date, member)
);
"""


def _get(url, rng=None, timeout=90, retries=3):
    hdr = {"User-Agent": UA}
    if rng:
        hdr["Range"] = f"bytes={rng[0]}-{rng[1] - 1}"
    last = None
    for a in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                          timeout=timeout).read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e
            time.sleep(2.0 * (a + 1))
    raise last


def idx_url(d: date, member: str, period="Days:1-10") -> str:
    ymd = d.strftime("%Y%m%d")
    return (f"{BUCKET}/{PREFIX}/{d.year}/{ymd}00/{member}/{period}/"
            f"tmax_2m_{ymd}00_{member}.grib2.idx")


def grib_url(d: date, member: str, period="Days:1-10") -> str:
    ymd = d.strftime("%Y%m%d")
    return (f"{BUCKET}/{PREFIX}/{d.year}/{ymd}00/{member}/{period}/"
            f"tmax_2m_{ymd}00_{member}.grib2")


def parse_idx(text: str):
    """Parse a wgrib .idx → (all_records, tmax6_records).

    `all_records` = [(rec_no, byte)] for EVERY message in file order, so a record's
    exact byte range is [byte, next_byte) — one GRIB message per range. `tmax6_records`
    = the TMAX 6-hour-window subset we actually fold into a daily max.
    """
    all_recs = []
    tmax6 = []
    for line in text.splitlines():
        m = re.match(r"^(\d+):(\d+):d=(\d+):([^:]*):([^:]*):([^:]*):(.*)$", line.strip())
        if not m:
            continue
        rec, byte, dtg, var, lev, span, tail = m.groups()
        all_recs.append((int(rec), int(byte)))
        wm = re.match(r"(\d+)-(\d+)\s*hour max fcst", span)
        if var != "TMAX" or not wm:
            continue
        w0, w1 = int(wm.group(1)), int(wm.group(2))
        if (w1 - w0) != 6:              # keep only the 6-hour windows
            continue
        tmax6.append({"rec": int(rec), "byte": int(byte),
                      "w0": w0, "w1": w1, "dtg": dtg, "desc": span})
    return all_recs, tmax6


def record_range(all_recs, rec_no, file_end):
    """Exact byte range of message `rec_no`: [byte, next_message_byte)."""
    for i, (rn, byte) in enumerate(all_recs):
        if rn == rec_no:
            hi = all_recs[i + 1][1] if i + 1 < len(all_recs) else file_end
            return byte, hi
    return None, None


def sample_stations(grib_bytes: bytes, stations) -> dict:
    """Decode one GRIB2 record → {station: tmax_F} by nearest grid cell.

    Handles both grid layouts seen in the two feeds:
      * regular 1-D lat/lon (GEFSv12 reforecast: 721 x 1440, lat 90..-90,
        lon 0..359.75) — index each axis independently.
      * 2-D curvilinear lat/lon (RTMA 2.5 km) — flatten and argmin.
    """
    import numpy as np
    import xarray as xr
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".grb2", delete=False) as tf:
        tf.write(grib_bytes)
        p = tf.name
    ds = xr.open_dataset(p, engine="cfgrib", backend_kwargs={"indexpath": ""})
    lat = ds.latitude.values
    lon = ds.longitude.values
    val = ds[list(ds.data_vars)[0]].values
    out = {}
    regular = (lat.ndim == 1 and lon.ndim == 1)
    if regular:
        # normalize longitudes to 0..360 for the grid
        lon360 = np.where(lon < 0, lon + 360.0, lon)
        order_l = np.argsort(lon360)
        lon_sorted = lon360[order_l]
        for st in stations:
            slat, slon = COORDS[st]
            slon360 = slon % 360
            iy = int(np.argmin(np.abs(lat - slat)))
            j = int(np.argmin(np.abs(lon_sorted - slon360)))
            ix = int(order_l[j])
            out[st] = float(val[iy, ix]) * 9.0 / 5.0 - 459.67
    else:
        flat_lat = lat.ravel(); flat_lon = lon.ravel(); flat_val = val.ravel()
        for st in stations:
            slat, slon = COORDS[st]
            lon360 = slon % 360
            d2 = (flat_lat - slat) ** 2 + (((flat_lon - lon360 + 180) % 360) - 180) ** 2
            k = int(np.argmin(d2))
            out[st] = float(flat_val[k]) * 9.0 / 5.0 - 459.67
    ds.close()
    Path(p).unlink(missing_ok=True)
    return out


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(DDL)
    return c


# ---------------------------------------------------------------------------
def cmd_probe(args):
    """Fetch the first init's idx, print parsed records. No DB write."""
    d = date.fromisoformat(args.start)
    txt = _get(idx_url(d, "c00")).decode()
    all_recs, recs = parse_idx(txt)
    print(f"init {d} c00  Days:1-10  → {len(recs)} TMAX 6h-window records "
          f"({len(all_recs)} total messages)")
    for r in recs[:12]:
        print(f"  rec{r['rec']:>3} byte={r['byte']:>10} {r['w0']:>3}-{r['w1']:<3}h")
    print(f"  ... daily-max days available: "
          f"{sorted({math.ceil(r['w1']/24) for r in recs})}")


def _existing_keys(c):
    return set(c.execute(
        "SELECT station, init_date, member, forecast_day FROM gefsv12_reforecast"))


def collect_init(c, d: date, member: str, stations, max_day: int, force=False):
    """Collect one (init_date, member). Idempotent: skips rows already present."""
    have = _existing_keys(c) if not force else set()
    try:
        txt = _get(idx_url(d, member)).decode()
    except Exception as e:
        c.execute("INSERT OR REPLACE INTO gefsv12_collect_log VALUES (?,?,?,?,?,?)",
                  (d.isoformat(), member, "missing", 0, str(e)[:200],
                   datetime.now(timezone.utc).isoformat()))
        c.commit()
        return 0, f"idx missing: {e}"
    all_recs, recs = parse_idx(txt)
    if not recs:
        return 0, "no 6h TMAX records"
    file_end = all_recs[-1][1] + 2_000_000 if all_recs else None
    # group records by forecast day (ceil(w1/24)), keep days <= max_day
    by_day = {}
    for r in recs:
        day = math.ceil(r["w1"] / 24.0)
        if day < 1 or day > max_day:
            continue
        by_day.setdefault(day, []).append(r)

    n_written = 0
    for day, items in sorted(by_day.items()):
        # skip if every station already has this (init, member, day)
        todo = [s for s in stations if (s, d.isoformat(), member, day) not in have]
        if not todo:
            continue
        day_max = {s: None for s in todo}
        w0s, w1s, nrec = [], [], 0
        for r in items:
            lo, hi = record_range(all_recs, r["rec"], file_end)
            if lo is None:
                continue
            try:
                raw = _get(grib_url(d, member), rng=(lo, hi))
            except Exception:
                continue
            time.sleep(REQ_DELAY_S)
            try:
                vals = sample_stations(raw, todo)
            except Exception as e:
                sys.stderr.write(f"    decode fail rec{r['rec']}: {e!r}\n")
                continue
            nrec += 1
            w0s.append(r["w0"]); w1s.append(r["w1"])
            for s, v in vals.items():
                if day_max[s] is None or v > day_max[s]:
                    day_max[s] = v
        now = datetime.now(timezone.utc).isoformat()
        for s in todo:
            c.execute(
                "INSERT OR REPLACE INTO gefsv12_reforecast VALUES (?,?,?,?,?,?,?,?,?)",
                (s, d.isoformat(), member, day,
                 None if day_max[s] is None else round(day_max[s], 3),
                 min(w0s) if w0s else None, max(w1s) if w1s else None, nrec, now))
            n_written += 1
        c.commit()
    c.execute("INSERT OR REPLACE INTO gefsv12_collect_log VALUES (?,?,?,?,?,?)",
              (d.isoformat(), member, "ok", n_written, "", datetime.now(timezone.utc).isoformat()))
    c.commit()
    return n_written, "ok"


def _gefsv12_log_insert(c, row):
    c.execute("INSERT OR REPLACE INTO gefsv12_collect_log VALUES (?,?,?,?,?,?)", row)


def cmd_collect(args):
    c = db()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    members = [m.strip() for m in args.members.split(",")] if args.members else MEMBERS
    stations = [s.strip() for s in args.stations.split(",")] if args.stations else STATIONS
    total = 0
    d = start
    while d <= end:
        for m in members:
            n, note = collect_init(c, d, m, stations, args.days)
            total += n
            print(f"  {d} {m}: +{n} rows ({note})", flush=True)
        d += timedelta(days=1)
    print(f"done: {total} rows -> {DB}")
    c.close()


def cmd_status(args):
    if not DB.exists():
        print("no DB yet:", DB)
        return
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    n = c.execute("SELECT COUNT(*) FROM gefsv12_reforecast").fetchone()[0]
    print("rows:", n)
    for r in c.execute("""SELECT init_date, member, COUNT(*), MIN(forecast_day), MAX(forecast_day)
                          FROM gefsv12_reforecast GROUP BY init_date, member
                          ORDER BY init_date DESC LIMIT 20"""):
        print("  ", r)
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="GEFSv12 reforecast Tmax collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe"); p.add_argument("--start", default="2010-01-01")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("collect")
    p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.add_argument("--members", default=None); p.add_argument("--stations", default=None)
    p.add_argument("--days", type=int, default=10)
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    a = ap.parse_args()
    a.func(a)
