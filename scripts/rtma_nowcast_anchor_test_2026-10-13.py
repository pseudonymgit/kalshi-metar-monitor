#!/usr/bin/env python3
"""
rtma_nowcast_anchor_test_2026-10-13.py — Item 1 of the Brier improvement sprint.

QUESTION
  RTMA/URMA is NOAA's hourly 2.5 km 2-m-temperature *analysis* (an assimilated
  "now state", not a forecast). Does the RTMA 2-m temperature at the 13:30
  decision pin beat our current `rate_1h` nowcast as the "now state" input?

DATA
  AWS Open Data, keyless, pre-indexed byte ranges (no GRIB whole-file pull):
    https://noaa-rtma-pds.s3.amazonaws.com/rtma2p5.<YYYYMMDD>/
        rtma2p5.t<HH>z.2dvaranl_ndfd.grb2_wexp(+.idx)
  The `.idx` line `3:<byte>:...:TMP:2 m above ground:anl:` gives the exact byte
  range of the 2-m temperature record. We fetch ONLY that record (~6 MB/cycle,
  vs the ~85 MB whole file). The offsets are IDENTICAL across cycles (verified
  03Z/13Z/17Z/23Z), so the range is a constant.
  t2m units: KELVIN -> °F.

SCOPE
  Probe window 2026-10-01 .. 2026-10-07 (7 days), all 20 canonical stations,
  cycles 10Z-23Z. This is a scoping test, not the full 2026-04-21..10-09 pull.

AXIS
  Same strict strike-crossing axis as the diversity study / rate_1h sweep:
    T = floor(ASOS high-so-far at the 13:30 station-local pin) + 1
    y = 1 if canonical settled max > T else 0   ("==" is a LOSS)
  Predictors scored with Brier + P(cross)=Phi((mu-T)/sigma):
    rate_1h   mu = hsf(13:30) + deflated rate_1h            (current best, sigma=1.5)
    rtma_pin  mu = RTMA 2-m temp at the cycle nearest pin   (sigma=4, analysis)
    rtma_hsf  mu = max RTMA 2-m temp over cycles <= pin     (sigma=4)

READ-ONLY on the live book. Writes ONLY:
  artifacts/rtma-scope/rtma_probe_2026-10-13.json
  /tmp cache of the fetched t2m records (re-fetchable, never committed)

Usage:
  python3 scripts/rtma_nowcast_anchor_test_2026-10-13.py fetch   # download probe
  python3 scripts/rtma_nowcast_anchor_test_2026-10-13.py score   # compute
  python3 scripts/rtma_nowcast_anchor_test_2026-10-13.py all
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.station_time import station_timezone_name  # noqa: E402

# Default probe window: a week with DENSE ASOS tmpf (>=100 obs/station-day) so
# rate_1h is computable. The archive's 1-min tmpf is sparse in early Oct 2026
# (0/140 dense station-days 10-01..10-07) but 140/140 dense 08-01..08-07.
PROBE_START = date(2026, 8, 1)
PROBE_END = date(2026, 8, 7)
CYCLE_HOURS = list(range(10, 24))          # 10Z..23Z
BUCKET_MIN = 15
DECISION_PIN_MOD = 13 * 60 + 30            # 13:30 station-local
SIGMA_RATE = 1.5
SIGMA_RTMA = 4.0                           # analysis treated as a deterministic "forecast"
ROLL_FACTOR = 2.5
ROLL_FLOOR = 1.0
RATE_CAP = 8.0
REQ_DELAY_S = 1.2                          # polite spacing (pre-indexed endpoint)

TMP_REC_LO, TMP_REC_HI = 14980236, 21065993   # constant byte range of TMP:2m
BUCKET = "https://noaa-rtma-pds.s3.amazonaws.com"
CACHE = Path("/tmp/rtma_probe_cache")
OUT = REPO / "artifacts" / "rtma-scope" / "rtma_probe_2026-10-13.json"
ASOS = REPO / "data" / "iem_asos_1min.db"
METAR = REPO / "data" / "metar_backfill.db"

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]
# canonical station coords (Kalshi settlement sites); KNYC is Central Park.
COORDS = {
    "KATL": (33.6407, -84.4277), "KAUS": (30.1945, -97.6699), "KBOS": (42.3631, -71.0064),
    "KDCA": (38.8512, -77.0402), "KDEN": (39.8561, -104.6737), "KDFW": (32.8998, -97.0403),
    "KHOU": (29.6454, -95.2789), "KLAS": (36.0840, -115.1537), "KLAX": (33.9425, -118.4081),
    "KMDW": (41.7868, -87.7522), "KMIA": (25.7932, -80.2906), "KMSP": (44.8848, -93.2223),
    "KMSY": (29.9934, -90.2580), "KNYC": (40.6413, -73.7781), "KOKC": (35.3931, -97.6007),
    "KPHL": (39.8719, -75.2411), "KPHX": (33.4342, -112.0116), "KSAT": (29.5337, -98.4698),
    "KSEA": (47.4502, -122.3088), "KSFO": (37.6188, -122.3754),
}


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ge(mu, sigma, x):
    if mu is None:
        return None
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


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
def _get(url, rng=None, timeout=60):
    hdr = {"Range": f"bytes={rng[0]}-{rng[1]-1}"} if rng else {}
    req = urllib.request.Request(url, headers=hdr)
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch_one(d: date, hh: int) -> bool:
    """Fetch the TMP:2m record for (d, hh) -> cache file. Returns cache hit state."""
    dstr = d.strftime("%Y%m%d")
    fname = CACHE / f"rtma2p5.{dstr}.t{hh:02d}z.t2m.grb2"
    if fname.exists() and fname.stat().st_size > 1_000_000:
        return True
    url = f"{BUCKET}/rtma2p5.{dstr}/rtma2p5.t{hh:02d}z.2dvaranl_ndfd.grb2_wexp"
    try:
        data = _get(url, rng=(TMP_REC_LO, TMP_REC_HI))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"  MISS {dstr} {hh:02d}Z HTTP {e.code}\n")
        return False
    CACHE.mkdir(parents=True, exist_ok=True)
    fname.write_bytes(data)
    return True


def cmd_fetch():
    CACHE.mkdir(parents=True, exist_ok=True)
    d = PROBE_START
    n = ok = 0
    while d <= PROBE_END:
        for hh in CYCLE_HOURS:
            n += 1
            if fetch_one(d, hh):
                ok += 1
            time.sleep(REQ_DELAY_S)
        print(f"  {d} done ({ok}/{n})", flush=True)
        d += timedelta(days=1)
    print(f"fetched {ok}/{n} cycles -> {CACHE}")


# ---------------------------------------------------------------------------
# decode + score
# ---------------------------------------------------------------------------
def sample_grid(grib_path: Path) -> dict:
    """Decode one t2m record -> {station: t2m_F}. Import inside fn (heavy dep)."""
    cachef = grib_path.with_suffix(grib_path.suffix + ".samples.json")
    if cachef.exists():
        try:
            return json.loads(cachef.read_text())
        except Exception:
            pass
    import numpy as np
    import xarray as xr
    ds = xr.open_dataset(str(grib_path), engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    lat = ds.latitude.values
    lon = ds.longitude.values
    t2m = ds.t2m.values
    out = {}
    for st, (slat, slon) in COORDS.items():
        lon360 = slon % 360
        d2 = (lat - slat) ** 2 + (((lon - lon360 + 180) % 360) - 180) ** 2
        iy, ix = np.unravel_index(int(np.argmin(d2)), d2.shape)
        v = float(t2m[iy, ix])
        out[st] = round(v * 9.0 / 5.0 - 459.67, 4)
    ds.close()
    cachef.write_text(json.dumps(out))
    return out


def load_asos_probe():
    """{(station, local_date): {'dmax':F, 'buckets':{local_mod:mean_F}}} for the probe week."""
    tz = {st: ZoneInfo(station_timezone_name(st)) for st in STATIONS}
    lo = (PROBE_START - timedelta(days=1)).isoformat() + " 00:00"
    hi = (PROBE_END + timedelta(days=1)).isoformat() + " 23:59"
    offcache = {}
    bs = {}
    dmax = {}
    c = ro(ASOS)
    cur = c.execute(
        "SELECT station, valid, tmpf FROM observations "
        "WHERE tmpf IS NOT NULL AND valid>=? AND valid<=?", (lo, hi))
    for st, valid, tmpf in cur:
        tzs = tz.get(st)
        if tzs is None:
            continue
        try:
            ds = valid[0:10]
            hh = int(valid[11:13])
            mm = int(valid[14:16])
        except (ValueError, TypeError):
            continue
        key = (st, ds, hh)
        off = offcache.get(key)
        if off is None:
            try:
                rep = datetime.strptime(ds + f" {hh:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                off = int(rep.astimezone(tzs).utcoffset().total_seconds())
            except Exception:
                off = 0
            offcache[key] = off
        tot = hh * 60 + mm + off // 60
        ldate = (date.fromisoformat(ds) + timedelta(days=tot // 1440)).isoformat()
        if ldate < PROBE_START.isoformat() or ldate > PROBE_END.isoformat():
            continue
        mod = ((tot % 1440) // BUCKET_MIN) * BUCKET_MIN
        k = (st, ldate, mod)
        a = bs.get(k)
        if a is None:
            bs[k] = [tmpf, 1]
        else:
            a[0] += tmpf
            a[1] += 1
        mk = (st, ldate)
        v = dmax.get(mk)
        if v is None or tmpf > v:
            dmax[mk] = tmpf
    c.close()
    buckets = {}
    for (st, ldate, mod), (s, cnt) in bs.items():
        buckets.setdefault((st, ldate), {})[mod] = s / cnt
    out = {}
    for k in set(list(dmax.keys()) + list(buckets.keys())):
        out[k] = {"dmax": dmax.get(k), "buckets": buckets.get(k, {})}
    return out


def load_truth():
    c = ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL "
            "AND local_trading_date>=? AND local_trading_date<=?",
            (PROBE_START.isoformat(), PROBE_END.isoformat())):
        out[(st, ds)] = float(v)
    c.close()
    return out


def _rate_and_roll(buckets, mod):
    need = [mod - 60, mod - 45, mod - 30, mod - 15, mod]
    if any(b not in buckets for b in need):
        return None, None
    rate = buckets[mod] - sum(buckets[b] for b in need[:-1]) / 4.0
    need_roll = [mod - 30, mod - 15, mod, mod + 15]
    roll = (sum(buckets[b] for b in need_roll) / 4.0
            if all(b in buckets for b in need_roll) else None)
    return rate, roll


def deflate(rate, roll):
    if rate is None:
        return None
    if roll is not None and roll > 0.0 and rate > 0.0:
        return min(rate / max(ROLL_FLOOR, ROLL_FACTOR * roll), RATE_CAP)
    return min(rate, RATE_CAP)


def rate_nowcast(rec, strike):
    if rec is None or strike is None:
        return None
    buckets = rec["buckets"]
    if not buckets:
        return None
    mods = sorted(buckets)
    best = None
    for mod in range(13 * 60, 16 * 60 + 45 + 1, BUCKET_MIN):
        if mod not in buckets:
            continue
        hsf = max(buckets[b] for b in mods if b <= mod)
        if hsf <= 0:
            continue
        rate, roll = _rate_and_roll(buckets, mod)
        d = deflate(rate, roll)
        if d is None:
            continue
        p = p_ge(hsf + d, SIGMA_RATE, strike)
        if best is None or p > best:
            best = p
    return best


def load_rtma():
    """{('YYYY-MM-DD', hh): {station: t2m_F}} from the cache."""
    out = {}
    for f in sorted(CACHE.glob("rtma2p5.*.t*.t2m.grb2")):
        m = re.match(r"rtma2p5\.(\d{8})\.t(\d{2})z\.t2m\.grb2", f.name)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%Y%m%d").date().isoformat()
        if d < PROBE_START.isoformat() or d > PROBE_END.isoformat():
            continue
        out[(d, int(m.group(2)))] = sample_grid(f)
    return out


def cmd_score():
    truth = load_truth()
    asos = load_asos_probe()
    rtma = load_rtma()
    print(f"rtma cycles decoded: {len(rtma)}", flush=True)

    # tz offsets for pin cycle selection
    pins = {}   # (station, date) -> UTC pin epoch
    for st in STATIONS:
        tz = ZoneInfo(station_timezone_name(st))
        d = PROBE_START
        while d <= PROBE_END:
            loc = datetime(d.year, d.month, d.day, 13, 30, tzinfo=tz)
            pins[(st, d.isoformat())] = loc.astimezone(timezone.utc)
            d += timedelta(days=1)

    rows = []
    for st in STATIONS:
        d = PROBE_START
        while d <= PROBE_END:
            ds = d.isoformat()
            rec = asos.get((st, ds))
            if not rec or not rec["buckets"]:
                d += timedelta(days=1)
                continue
            mods = sorted(rec["buckets"])
            hsf_vals = [rec["buckets"][b] for b in mods if b <= DECISION_PIN_MOD]
            hsf = max(hsf_vals) if hsf_vals else None
            settled = truth.get((st, ds))
            if hsf is None or settled is None:
                d += timedelta(days=1)
                continue
            T = float(math.floor(hsf) + 1.0)
            y = 1 if settled > T else 0

            # RTMA: cycle nearest the pin, and max over cycles <= pin
            pin_utc = pins[(st, ds)]
            avail = sorted([h for (dd, h) in rtma if dd == ds and st in rtma[(dd, h)]])
            rtma_pin = rtma_hsf = None
            pin_hh = None
            if avail:
                def epoch(h):
                    return datetime(d.year, d.month, d.day, h, 0, tzinfo=timezone.utc).timestamp()
                pin_hh = min(avail, key=lambda h: abs(epoch(h) - pin_utc.timestamp()))
                rtma_pin = rtma[(ds, pin_hh)][st]
                upto = [h for h in avail if epoch(h) <= pin_utc.timestamp() + 1800]
                if upto:
                    rtma_hsf = max(rtma[(ds, h)][st] for h in upto)

            # rate_1h at the 13:30 pin specifically (for the RTMA-anchor test)
            rate_at_pin = None
            if DECISION_PIN_MOD in rec["buckets"]:
                _r, _roll = _rate_and_roll(rec["buckets"], DECISION_PIN_MOD)
                rate_at_pin = deflate(_r, _roll)
            rtma_rate_mu = (rtma_pin + rate_at_pin
                            if (rtma_pin is not None and rate_at_pin is not None) else None)
            rows.append({
                "station": st, "date": ds, "strike": T, "settled": settled, "y": y,
                "hsf_1330": round(hsf, 3),
                "rate_1h_p": rate_nowcast(rec, T),
                "hsf_p4": p_ge(hsf, SIGMA_RTMA, T),
                "hsf_p1": p_ge(hsf, SIGMA_RATE, T),
                "rate_at_pin": rate_at_pin,
                "rtma_pin_F": None if rtma_pin is None else round(rtma_pin, 3),
                "rtma_pin_hh": pin_hh,
                "rtma_hsf_F": None if rtma_hsf is None else round(rtma_hsf, 3),
                "rtma_pin_p": p_ge(rtma_pin, SIGMA_RTMA, T),
                "rtma_hsf_p": p_ge(rtma_hsf, SIGMA_RTMA, T),
                "rtma_rate_p": p_ge(rtma_rate_mu, SIGMA_RATE, T),
            })
            d += timedelta(days=1)

    # score
    def score(key):
        sub = [r for r in rows if r.get(key) is not None]
        if not sub:
            return {"n": 0}
        ps = [r[key] for r in sub]
        ys = [r["y"] for r in sub]
        return {"n": len(sub), "brier": round(brier(ps, ys), 4),
                "dir_acc": round(dir_acc(ps, ys), 4),
                "base_rate": round(sum(ys) / len(ys), 4)}

    res = {
        "window": [PROBE_START.isoformat(), PROBE_END.isoformat()],
        "n_station_days": len(rows),
        "rtma_cycles": len(rtma),
        "scores": {
            "rate_1h": score("rate_1h_p"),
            "hsf_only_s4": score("hsf_p4"),
            "hsf_only_s1.5": score("hsf_p1"),
            "rtma_pin": score("rtma_pin_p"),
            "rtma_hsf": score("rtma_hsf_p"),
            "rtma_plus_rate": score("rtma_rate_p"),
        },
        "per_station": {},
    }

    # correlation of forecast errors on the overlapping subset (independence axis)
    ov = [r for r in rows if r["rate_1h_p"] is not None and r["rtma_pin_p"] is not None]
    if len(ov) >= 3:
        e_rate = [r["rate_1h_p"] - r["y"] for r in ov]
        e_rtma = [r["rtma_pin_p"] - r["y"] for r in ov]
        res["fcerr_rate_vs_rtma_pin"] = round(pearson(e_rate, e_rtma), 4)
        res["level_corr_rate_mu_vs_rtma"] = round(
            pearson([r["hsf_1330"] for r in ov], [r["rtma_pin_F"] for r in ov]), 4)
        res["n_overlap"] = len(ov)
        res["rtma_pin_bias_F"] = round(
            sum(r["rtma_pin_F"] - r["settled"] for r in ov) / len(ov), 3)
        res["hsf_bias_F"] = round(
            sum(r["hsf_1330"] - r["settled"] for r in ov) / len(ov), 3)
        # within-station demeaned fcerr (removes cross-station level differences,
        # the metric the om_gfs study used to call out collinearity)
        by_st = {}
        for r in ov:
            by_st.setdefault(r["station"], []).append(r)
        de_r, de_t = [], []
        for st, rs in by_st.items():
            mr = sum(x["rate_1h_p"] - x["y"] for x in rs) / len(rs)
            mt = sum(x["rtma_pin_p"] - x["y"] for x in rs) / len(rs)
            de_r += [x["rate_1h_p"] - x["y"] - mr for x in rs]
            de_t += [x["rtma_pin_p"] - x["y"] - mt for x in rs]
        res["fcerr_rate_vs_rtma_demeaned"] = round(pearson(de_r, de_t), 4)
        # level correlation of the two "now-state" levels, demeaned by station
        dl_r, dl_t = [], []
        for st, rs in by_st.items():
            mh = sum(x["hsf_1330"] for x in rs) / len(rs)
            mt = sum(x["rtma_pin_F"] for x in rs) / len(rs)
            dl_r += [x["hsf_1330"] - mh for x in rs]
            dl_t += [x["rtma_pin_F"] - mt for x in rs]
        res["level_corr_demeaned"] = round(pearson(dl_r, dl_t), 4)

    # per-station breakdown
    for st in STATIONS:
        sub = [r for r in rows if r["station"] == st]
        if not sub:
            continue
        res["per_station"][st] = {
            "n": len(sub),
            "rate_1h_brier": round(brier([r["rate_1h_p"] for r in sub if r["rate_1h_p"] is not None],
                                         [r["y"] for r in sub if r["rate_1h_p"] is not None]), 4)
            if any(r["rate_1h_p"] is not None for r in sub) else None,
            "rtma_pin_brier": round(brier([r["rtma_pin_p"] for r in sub if r["rtma_pin_p"] is not None],
                                          [r["y"] for r in sub if r["rtma_pin_p"] is not None]), 4)
            if any(r["rtma_pin_p"] is not None for r in sub) else None,
        }

    res["rows"] = rows
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out_name = f"rtma_probe_{PROBE_START.isoformat()}_{PROBE_END.isoformat()}.json"
    res["out_file"] = str(OUT.parent / out_name)
    (OUT.parent / out_name).write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k not in ("rows", "per_station")}, indent=2))
    print("wrote", OUT.parent / out_name)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if "--start" in sys.argv:
        PROBE_START = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    if "--end" in sys.argv:
        PROBE_END = date.fromisoformat(sys.argv[sys.argv.index("--end") + 1])
    if "--cycles" in sys.argv:
        CYCLE_HOURS = [int(x) for x in sys.argv[sys.argv.index("--cycles") + 1].split(",")]
    if cmd in ("fetch", "all"):
        cmd_fetch()
    if cmd in ("score", "all"):
        cmd_score()
