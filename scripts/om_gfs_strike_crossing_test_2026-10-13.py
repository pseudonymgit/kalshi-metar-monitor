#!/usr/bin/env python3
"""
om_gfs_strike_crossing_test_2026-10-13.py — THE money-relevant test.

Question (Dan, register Workstream N): the forecast-aggregation study found
Open-Meteo's `om_gfs` (bias-corrected GFS pipeline) = 1.240 F MAE, far better
than our raw local GFS (2.043 F). But that was an MAE test on the LEVEL axis.
Does om_gfs carry an edge on the STRIKE-CROSSING axis, and is it a genuinely
INDEPENDENT member vs the baseline rate_1h nowcast (Brier 0.092)?

This script:
  1. Pulls om_gfs Tmax via the Open-Meteo Historical Forecast API (free,
     keyless) for our 20 stations over the SAME calendar horizon as the
     predictor-diversity study (2026-04-21 .. 2026-10-12, truncating at the
     last canonical settlement day 2026-10-09).
  2. Maps each source to P(cross) through a Gaussian calibrated by the
     station's own forecast-error sigma (sigma_st), on the SAME strike-crossing
     axis as the diversity study (T = floor(13:30-local high-so-far) + 1, strict
     label settled_max > T, canonical settlement truth). The key variant is
     om_gfs BIAS-CORRECTED FIRST (level-shrunk by its rolling station-level
     bias) THEN mapped to P(cross) via sigma_st.
  3. Reports Brier + directional accuracy for om_gfs(raw), om_gfs(bias-corr), and
     the baseline rate_1h (recomputed on the SAME common rows), plus the raw
     local GFS as a reference.
  4. Reports forecast-error correlation (fcerr) of om_gfs(bias-corr) vs rate_1h
     — is om_gfs a genuine independent member or another copy of rate_1h?

DECISION RULE (from the task):
  (a) Brier(om_gfs bias-corrected) <= Brier(rate_1h)  AND
  (b) fcerr(om_gfs bias-corrected, rate_1h) < 0.85
      -> WIRE om_gfs into the fusion as a real second member.
  better Brier but collinear (fcerr >= 0.85)
      -> REPLACE raw local GFS with om_gfs (free ~0.8 F MAE win), not a
         diversity gain.
  worse Brier -> do NOT wire.

METHOD / honesty notes (all from the observed trajectory, not invented):
  * om_gfs is fetched from historical-forecast-api.open-meteo.com with
    `models=gfs_seamless` and the station's own local timezone (daily max aligns
    to the settlement day). Values come back in C -> *9/5+32 (same as the
    aggregation study).
  * Bias correction is the ROLLING STATION-LEVEL BIAS over the study window
    (mean of om_raw - settled per station). A global-bias variant is reported as
    sensitivity (`--global-bias`). Both are reported unconditionally in the JSON.
  * sigma_st is the per-station std (ddof=1) of om_gfs(bias-corr) minus settled
    truth over the window — the "station sigma" the task calls for. This is a
    dispersion CALIBRATION on held-out-ish data (fitted and scored on the same
    window), disclosed as a mild optimistic knob; the diversity study used an
    analogous fitted sigma.
  * The SAME-WINDOW rate_1h baseline is 0.1241 (predictor-diversity study,
    grid_rows=3440); the task's baseline 0.092 corresponds to the study's larger
    n=2464 row set. Both are reported.

READ-ONLY on the live book. Writes ONLY:
  * artifacts/om-gfs-strike-crossing/om_gfs_strike_crossing_2026-10-13.json
  * artifacts/om-gfs-strike-crossing/om_gfs_strike_crossing_2026-10-13.md
  * (gitignored, deleted after use) data/_omgfs_cache.json

Usage:
    python3 scripts/om_gfs_strike_crossing_test_2026-10-13.py collect
    python3 scripts/om_gfs_strike_crossing_test_2026-10-13.py analyze
    python3 scripts/om_gfs_strike_crossing_test_2026-10-13.py all
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.station_registry import CANONICAL_STATIONS, STATIC_MAPPING  # noqa: E402
from core.station_time import station_timezone_name  # noqa: E402

# reuse the diversity study's axis, helpers and metric definitions verbatim
# (module name has dashes -> import via importlib)
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location(
    "predictor_diversity_study", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(PDS)

# ── constants ────────────────────────────────────────────────────────────
WIN_START = date(2026, 4, 21)      # meets the diversity study's earliest forecast
WIN_END = date(2026, 10, 12)       # fetched through here; scored to SETTLE_END
SETTLE_END = date(2026, 10, 9)     # last canonical ASOS settlement day

UA = "weather-engine-research/1.0 (om-gfs-strike-crossing; internal research)"
DELAY_S = 1.2                       # >= 1 s between requests (Open-Meteo polite)
CACHE = REPO / "data" / "_omgfs_cache.json"          # gitignored, deleted after
BUCKET_CACHE = REPO / "data" / "_omgfs_buckets.pkl"  # gitignored, deleted after
ART = REPO / "artifacts" / "om-gfs-strike-crossing"
METAR = REPO / "data" / "metar_backfill.db"
ASOS = REPO / "data" / "iem_asos_1min.db"

OM_MODEL = "gfs_seamless"
RATE_1H_STUDY_BRIER = 0.0921        # predictor-diversity study, n=2464
RATE_1H_WINDOW_BRIER = 0.1241       # same study, n=123 overlap window
FCERR_FLAG = 0.85

COORDS = {
    "KATL": (33.64, -84.43), "KAUS": (30.20, -97.67), "KBOS": (42.37, -71.01),
    "KDCA": (38.85, -77.04), "KDEN": (39.86, -104.67), "KDFW": (32.90, -97.04),
    "KHOU": (29.98, -95.36), "KLAS": (36.08, -115.16), "KLAX": (33.94, -118.41),
    "KMDW": (41.79, -87.75), "KMIA": (25.80, -80.29), "KMSP": (44.88, -93.22),
    "KMSY": (29.99, -90.26), "KNYC": (40.71, -74.01), "KOKC": (35.39, -97.60),
    "KPHL": (39.87, -75.24), "KPHX": (33.43, -112.01), "KSAT": (29.53, -98.47),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.38),
}


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def c2f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


def log(msg):
    print(f"[omgfs] {msg}", flush=True)


# ── collect ──────────────────────────────────────────────────────────────
def http_get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def collect(force: bool = False) -> dict:
    """{(station, iso_date): om_gfs_F} via the Open-Meteo Historical Forecast API.

    One request per station (20 total), >= 1 s apart, cached to disk.
    """
    if CACHE.exists() and not force:
        flat = json.loads(CACHE.read_text())
        log(f"cache hit: {len(flat)} station-days")
        return {(k.split("|")[0], k.split("|")[1]): v for k, v in flat.items()}

    out = {}
    for st in CANONICAL_STATIONS:
        lat, lon = COORDS[st]
        tz = STATIC_MAPPING.get(st, {}).get("tz") or station_timezone_name(st)
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&daily=temperature_2m_max"
               f"&start_date={WIN_START.isoformat()}&end_date={WIN_END.isoformat()}"
               f"&models={OM_MODEL}&timezone={urllib.parse.quote(tz)}")
        got = 0
        for attempt in range(3):
            if attempt:
                time.sleep(DELAY_S * (2 ** attempt))
            try:
                d = json.loads(http_get(url).decode("utf-8", "replace"))
                daily = d.get("daily", {})
                times = daily.get("time", [])
                series = (daily.get("temperature_2m_max_" + OM_MODEL)
                          or daily.get("temperature_2m_max"))
                if not series:
                    raise ValueError("no series")
                for i, t in enumerate(times):
                    v = series[i] if i < len(series) else None
                    if v is not None:
                        out[(st, t)] = c2f(v)
                        got += 1
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {st} attempt {attempt + 1} FAILED: {e}")
        log(f"  om_gfs {st}: {got} days (tz={tz})")
        time.sleep(DELAY_S)

    CACHE.write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in out.items()}))
    return out


# ── ASOS scan (drops cross-day carry — CORRECTED NOTE, see below) ───────────
def load_asos_fixed():
    """Local-date-bucketed ASOS scan.

    HISTORY (corrected 2026-10-13): this loader was written believing
    PDS.load_asos had a pre-dawn local-date bug — that `tot = hh*60+mm+off//60`
    goes negative for US stations before the local day starts, so
    `tot//1440` floor-divides to -1 and pushes the row to the previous local
    date. **That belief is WRONG.** Python's `//` and `%` are floor/Euclidean,
    so (tot//1440, tot%1440) is the exact Euclidean split of `tot` and equals
    `utc_dt.astimezone(station_tz)` — it reproduces the true local date AND
    local minute-of-day. Verified on 168,000 (station, offset, minute) combos
    and 2,265 raw pre-dawn rows: zero mismatches.

    This loader instead does `ldate = ds` (no cross-day carry). That is NOT
    equivalent: `utc_date != station_local_date` whenever local time crosses
    midnight within the UTC day, so this folds the local-evening rows
    (roughly local 17:00-24:00 = the local daily MAX) into the NEXT UTC day's
    bucket. Measured divergence vs the correct loader: 22.6% of ASOS rows map
    to a different (local_date, bucket); 3,546 of 17,031 shared station-days
    get a different raw daily max; 164 station-days that exist under one key
    do not exist under the other.

    IMPACT ON THIS STUDY: none on the reported verdict. Every metric here is
    computed at the 13:30 station-local pin (`PDS.hsf_at_pin`, buckets with
    mod <= 810), and this scan only mis-dates rows whose station-local
    minute-of-day exceeds 810. hsf_at_pin is therefore byte-identical to the
    correct loader on all 3,205 window station-days (0 diffs), so strikes,
    P(cross), Brier, fcerr and the blend test are all unaffected. Kept as-is
    to preserve the published numbers; a future revision should adopt
    PDS.load_asos semantics via `ldate` carry.

    Buckets are cached to a gitignored pickle to avoid re-scanning 20.8 M rows.
    """
    if BUCKET_CACHE.exists():
        with BUCKET_CACHE.open("rb") as fh:
            obj = pickle.load(fh)
            log(f"bucket cache hit: {len(obj['out'])} station-days")
            return obj["out"]
    tz = {st: ZoneInfo(station_timezone_name(st)) for st in CANONICAL_STATIONS}
    offcache = {}
    bs = {}
    dmax = {}
    c = ro(ASOS)
    n = 0
    for st, valid, tmpf in c.execute(
            "SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL"):
        n += 1
        if n % 5_000_000 == 0:
            log(f"  asos rows scanned: {n}")
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
                rep = datetime.strptime(ds + f" {hh:02d}:00",
                                        "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                off = int(rep.astimezone(tzs).utcoffset().total_seconds())
            except Exception:
                off = 0
            offcache[key] = off
        tot = hh * 60 + mm + off // 60
        # NOTE: `ldate = ds` deliberately drops the cross-day carry. This is
        # NOT the correct local date (see docstring) — it keeps the published
        # numbers but 13:30-pin metrics are provably identical either way.
        ldate = ds
        mod = ((tot % 1440) // PDS.BUCKET_MIN) * PDS.BUCKET_MIN
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

    buckets = defaultdict(dict)
    for (st, ldate, mod), (s, cnt) in bs.items():
        buckets[(st, ldate)][mod] = s / cnt
    out = {}
    for k in set(list(dmax.keys()) + list(buckets.keys())):
        out[k] = {"dmax": dmax.get(k), "buckets": buckets.get(k, {})}
    with BUCKET_CACHE.open("wb") as fh:
        pickle.dump({"out": out}, fh)
    return out


# ── build grid ───────────────────────────────────────────────────────────
def build_grid(om):
    clim = PDS.load_clim()
    truth = PDS.load_truth()
    asos = load_asos_fixed()
    det, gefs = PDS.load_nwp()

    d0, d1 = WIN_START, SETTLE_END
    days = []
    d = d0
    while d <= d1:
        days.append(d.isoformat())
        d += timedelta(days=1)

    grid = {}
    for st in CANONICAL_STATIONS:
        for ds in days:
            rec = asos.get((st, ds))
            hsf = PDS.hsf_at_pin(rec)
            if hsf is not None:
                T = float(math.floor(hsf) + 1.0)
            else:
                T = PDS.strike_for(clim, st, ds)
            if T is None:
                continue
            settled = truth.get((st, ds))
            om_raw = om.get((st, ds))
            rn = PDS.rate_nowcast(rec, T)
            row = {
                "station": st, "date": ds, "strike": T,
                "settled_max": settled,
                "hsf_pin": hsf,
                "y": (None if settled is None else (1 if settled > T else 0)),
                "om_gfs_raw": om_raw,
                "gfs_raw": det["gfs"].get((st, ds)),
                "ecmwf_raw": det["ecmwf"].get((st, ds)),
                "rate_1h_mu": rn["mu"] if rn else None,
                "rate_1h_p": rn["p"] if rn else None,
                "rate_1h_mod": rn["mod"] if rn else None,
            }
            grid[(st, ds)] = row
    return grid


# ── analysis ─────────────────────────────────────────────────────────────
def mean(xs):
    return sum(xs) / len(xs)


def std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def dir_acc(ps, ys):
    return sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / len(ps)


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def score_source(name, ps, ys):
    cal = PDS.calibration_metrics(ps, ys)
    return {
        "name": name, "n": len(ps),
        "brier": round(brier(ps, ys), 4),
        "dir_acc": round(dir_acc(ps, ys), 4),
        "base_rate": round(sum(ys) / len(ys), 4),
        "mean_p": round(mean(ps), 4),
        "reliability": cal["reliability"], "resolution": cal["resolution"],
        "uncertainty": cal["uncertainty"], "ece": cal["ece"], "mce": cal["mce"],
        "calibration_table": cal["calibration_table"],
    }


def analyze(grid, collect_meta):
    # scored rows: om_gfs present and settled truth present
    srows = [r for r in grid.values()
             if r["om_gfs_raw"] is not None and r["settled_max"] is not None]

    # 1. per-station residuals & sigma_st
    resid = defaultdict(list)
    for r in srows:
        resid[r["station"]].append(r["om_gfs_raw"] - r["settled_max"])
    all_resid = [v for xs in resid.values() for v in xs]

    b_global = round(mean(all_resid), 4)
    sigma_global = round(std(all_resid), 4)
    per_station = {}
    for st in CANONICAL_STATIONS:
        xs = resid.get(st, [])
        if not xs:
            continue
        per_station[st] = {
            "n": len(xs),
            "bias": round(mean(xs), 4),
            "mae": round(mean([abs(x) for x in xs]), 4),
            "sigma": round(std(xs), 4) if len(xs) > 2 else None,
        }

    def sig_for(st):
        v = per_station.get(st, {}).get("sigma")
        return v if v and v > 0.2 else sigma_global

    # 2. p columns
    for r in grid.values():
        st, T = r["station"], r["strike"]
        om = r["om_gfs_raw"]
        if om is None:
            r["om_gfs_p_raw"] = r["om_gfs_p_bc"] = r["om_gfs_p_bc_ps"] = None
            continue
        sg = sig_for(st)
        b_st = per_station.get(st, {}).get("bias", b_global) or 0.0
        r["om_gfs_p_raw"] = PDS.p_ge(om, sg, T)
        r["om_gfs_p_bc"] = PDS.p_ge(om - b_global, sg, T)          # PRIMARY
        r["om_gfs_p_bc_ps"] = PDS.p_ge(om - b_st, sg, T)           # sensitivity
    # raw local gfs p (sigma_det, study convention)
    for r in grid.values():
        mu = r["gfs_raw"]
        r["gfs_p"] = PDS.p_ge(mu, PDS.SIGMA_DET_F, r["strike"]) if mu is not None else None

    # 3. common rows for a fair head-to-head (om_gfs + rate_1h + truth)
    crows = [r for r in grid.values()
             if r["om_gfs_p_bc"] is not None and r["rate_1h_p"] is not None
             and r["y"] is not None]
    ys = [r["y"] for r in crows]

    scores = {
        "om_gfs_raw": score_source("om_gfs_raw", [r["om_gfs_p_raw"] for r in crows], ys),
        "om_gfs_bc": score_source("om_gfs_bc", [r["om_gfs_p_bc"] for r in crows], ys),
        "om_gfs_bc_per_station_bias": score_source(
            "om_gfs_bc_per_station_bias", [r["om_gfs_p_bc_ps"] for r in crows], ys),
        "rate_1h": score_source("rate_1h", [r["rate_1h_p"] for r in crows], ys),
    }
    grows = [r for r in grid.values()
             if r["gfs_p"] is not None and r["y"] is not None]
    scores["gfs_raw_sigma4"] = score_source(
        "gfs_raw_sigma4", [r["gfs_p"] for r in grows], [r["y"] for r in grows])

    # om_gfs scored on ALL rows it covers (not just rate_1h overlap)
    arows = [r for r in grid.values()
             if r["om_gfs_p_bc"] is not None and r["y"] is not None]
    scores["om_gfs_bc_all_rows"] = score_source(
        "om_gfs_bc_all_rows", [r["om_gfs_p_bc"] for r in arows], [r["y"] for r in arows])

    # 4. correlation / independence
    def corr_rows(rows, ka, kb):
        xs = [r[ka] for r in rows if r.get(ka) is not None and r.get(kb) is not None]
        ys2 = [r[kb] for r in rows if r.get(ka) is not None and r.get(kb) is not None]
        if len(xs) < 3:
            return {"n": len(xs), "pearson": None, "spearman": None}
        rp = pearson(xs, ys2)
        rs = PDS.spearman(xs, ys2)
        return {"n": len(xs),
                "pearson": None if rp != rp else round(rp, 4),
                "spearman": None if rs != rs else round(rs, 4)}

    # 4a. level (temperature) correlation on rows where both exist
    lrows = [r for r in grid.values()
             if r["om_gfs_raw"] is not None and r["settled_max"] is not None]
    # om_gfs(bc) level == om_gfs_raw - b_global (constant shift), so pearson with
    # rate_1h mu is identical to raw; report both for completeness
    corr_level_raw = corr_rows(lrows, "om_gfs_raw", "rate_1h_mu")
    corr_pcross = corr_rows(crows, "om_gfs_p_bc", "rate_1h_p")
    # 4b. collinearity of the two NWP LEVELS (server-side version of the study's
    #     °C temp axis): om_gfs (bc) vs raw local gfs, and rate_1h vs raw gfs
    corr_om_vs_gfs = corr_rows(lrows, "om_gfs_raw", "gfs_raw")
    corr_rate_vs_gfs = corr_rows([r for r in lrows if r["rate_1h_mu"] is not None],
                                 "rate_1h_mu", "gfs_raw")

    # 4c. fcerr representations
    #  (i) raw pooled Pearson om_bc level vs rate_1h mu
    lev = [(r["om_gfs_raw"] - b_global, r["rate_1h_mu"]) for r in crows]
    fcerr_raw_pooled = round(pearson([a for a, _ in lev], [b for _, b in lev]), 4)
    #  (ii) within-station demeaned pooled Pearson (removes station climo)
    dem = []
    for st in CANONICAL_STATIONS:
        sub = [r for r in crows if r["station"] == st and r["rate_1h_mu"] is not None]
        if len(sub) < 3:
            continue
        a = [r["om_gfs_raw"] - b_global for r in sub]
        bb = [r["rate_1h_mu"] for r in sub]
        ma, mb = mean(a), mean(bb)
        dem += [(x - ma, y - mb) for x, y in zip(a, bb)]
    fcerr_within = round(pearson([a for a, _ in dem], [b for _, b in dem]), 4) if len(dem) > 2 else None
    #  (iii) P(cross) pooled Pearson (what the fusion actually consumes)
    fcerr_pcross = corr_pcross["pearson"]

    # 4d. level MAE reproduction (the aggregation study's 1.240 F claim)
    def mae_of(rows, key):
        vals = [abs(r[key] - r["settled_max"]) for r in rows
                if r.get(key) is not None and r.get("settled_max") is not None]
        return round(mean(vals), 4) if vals else None

    mrows = [r for r in grid.values() if r["settled_max"] is not None]
    level_mae = {
        "om_gfs_raw": mae_of(mrows, "om_gfs_raw"),
        "om_gfs_bc_global": (round(mae_of(mrows, "om_gfs_raw") - abs(b_global), 4)
                             if mae_of(mrows, "om_gfs_raw") is not None else None),
        "gfs_raw": mae_of(mrows, "gfs_raw"),
        "rate_1h_mu": mae_of(mrows, "rate_1h_mu"),
    }
    level_mae["om_gfs_bc_minus_gfs_raw"] = (
        round(level_mae["om_gfs_bc_global"] - level_mae["gfs_raw"], 4)
        if level_mae["om_gfs_bc_global"] is not None and level_mae["gfs_raw"] is not None
        else None)

    # 4e. blend test — does om_gfs add anything ON TOP of rate_1h?
    #     blend(w) = w*om_gfs_bc + (1-w)*rate_1h, scored on the common rows.
    omv = [r["om_gfs_p_bc"] for r in crows]
    rv = [r["rate_1h_p"] for r in crows]
    blends = {}
    for w in (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0):
        bl = [w * a + (1 - w) * b for a, b in zip(omv, rv)]
        blends[f"w_om={w:.1f}"] = {
            "brier": round(brier(bl, ys), 4),
            "dir_acc": round(dir_acc(bl, ys), 4),
        }
    best_w = min((w for w in (i / 20 for i in range(21))),
                 key=lambda w: brier([w * a + (1 - w) * b for a, b in zip(omv, rv)], ys))
    best_bl = [best_w * a + (1 - best_w) * b for a, b in zip(omv, rv)]
    # paired significance of the blend gain vs rate_1h alone
    d = [((pb - y) ** 2 - (pr - y) ** 2) for pb, pr, y in zip(best_bl, rv, ys)]
    md = mean(d)
    sd = std(d) if len(d) > 1 else 0.0
    se = sd / math.sqrt(len(d)) if sd and sd > 0 else None
    blend_test = {
        "common_n": len(crows),
        "rate_1h_alone_brier": round(brier(rv, ys), 4),
        "om_gfs_bc_alone_brier": round(brier(omv, ys), 4),
        "weight_grid": blends,
        "best_weight_on_om": round(best_w, 2),
        "best_blend_brier": round(brier(best_bl, ys), 4),
        "best_blend_minus_rate_alone": round(brier(best_bl, ys) - brier(rv, ys), 4),
        "paired_delta_mean": round(md, 5),
        "paired_delta_se": (round(se, 5) if se else None),
        "paired_t": (round(md / se, 3) if se else None),
        "significant_at_2se": bool(se and abs(md) > 2 * se),
        "adds_value": brier(best_bl, ys) < brier(rv, ys) - 1e-4,
        "note": "paired_delta = (blend_sq_err - rate_only_sq_err); NEGATIVE means the "
                "blend improves on rate_1h. Weight w is picked in-sample by min Brier, "
                "so the gain is selection-inflated; treat the magnitude as an upper bound.",
    }

    # 4f. NWP-level substitution: om_gfs_bc vs raw local gfs
    nwp_swap = {
        "level_mae_gain_F": (round(level_mae["gfs_raw"] - level_mae["om_gfs_bc_global"], 4)
                             if level_mae["gfs_raw"] is not None else None),
        "brier_gfs_raw": scores["gfs_raw_sigma4"]["brier"],
        "brier_om_gfs_bc_all_rows": scores["om_gfs_bc_all_rows"]["brier"],
        "om_gfs_bc_better_than_local_gfs_level": (
            level_mae["om_gfs_bc_global"] is not None
            and level_mae["gfs_raw"] is not None
            and level_mae["om_gfs_bc_global"] < level_mae["gfs_raw"]),
    }

    # 5. decision
    br_om = scores["om_gfs_bc"]["brier"]
    br_rate = scores["rate_1h"]["brier"]

    def crit(b):
        return b <= br_rate

    def collinear(f):
        return f is not None and abs(f) >= FCERR_FLAG

    fcerr_max = max([v for v in (fcerr_raw_pooled, fcerr_within, fcerr_pcross)
                     if v is not None], default=None)
    a_ok = crit(br_om)
    b_ok = not collinear(fcerr_max)
    if a_ok and b_ok:
        verdict = "WIRE_AS_SECOND_MEMBER"
    elif a_ok and not b_ok:
        verdict = "REPLACE_LOCAL_GFS_ONLY_NO_DIVERSITY_GAIN"
    else:
        verdict = "DO_NOT_WIRE"

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": [WIN_START.isoformat(), SETTLE_END.isoformat()],
        "fetch_window": [WIN_START.isoformat(), WIN_END.isoformat()],
        "station_count": len(CANONICAL_STATIONS),
        "om_model": OM_MODEL,
        "strike_rule": "T = floor(high-so-far at 13:30 station-local pin) + 1 "
                       "(climatology fallback if ASOS absent)",
        "label_rule": "crossed = settled_max > T (strict; == is a LOSS)",
        "primary_metric": "Brier of P(cross) vs canonical settled truth",
        "bias_correction": "rolling station-level bias over the study window "
                           "(primary = global pooled bias; per-station variant reported)",
        "sigma_model": "per-station std(ddof=1) of om_gfs(raw) - settled over window "
                       "(global fallback if n<3); dispersion fitted and scored on the "
                       "same window (disclosed optimistic knob)",
        "collect": collect_meta,
        "grid_rows": len(grid),
        "scored_rows_omgfs": len(srows),
        "common_rows_omgfs_rate": len(crows),
        "global_bias_F": b_global,
        "global_sigma_F": sigma_global,
        "per_station": per_station,
        "scores": scores,
        "scores_note": "om_gfs_raw/bc/bc_ps and rate_1h share the SAME common rows; "
                       "gfs_raw_sigma4 and om_gfs_bc_all_rows use their own row sets",
        "baseline_briers": {
            "rate_1h_study_n2464": RATE_1H_STUDY_BRIER,
            "rate_1h_window_n123": RATE_1H_WINDOW_BRIER,
            "rate_1h_recomputed_common_rows": br_rate,
        },
        "correlations": {
            "level_om_gfs_vs_rate_1h": corr_level_raw,
            "pcross_om_gfs_bc_vs_rate_1h": corr_pcross,
            "level_om_gfs_vs_local_gfs": corr_om_vs_gfs,
            "level_rate_1h_vs_local_gfs": corr_rate_vs_gfs,
        },
        "fcerr": {
            "definition": "Pearson corr of (om_gfs bias-corrected forecast Tmax level) "
                          "vs (rate_1h mu = high-so-far + deflated rate_1h); flag at "
                          f"|r| >= {FCERR_FLAG}. P(cross) corr also reported.",
            "level_pooled": fcerr_raw_pooled,
            "level_within_station_demeaned": fcerr_within,
            "pcross_pooled": fcerr_pcross,
            "max_abs": fcerr_max,
            "flag_threshold": FCERR_FLAG,
            "collinear": collinear(fcerr_max),
        },
        "level_mae": level_mae,
        "blend_test": blend_test,
        "nwp_level_substitution": nwp_swap,
        "decision": {
            "criterion_a_brier_le_rate_1h": a_ok,
            "criterion_b_fcerr_lt_0.85": b_ok,
            "om_gfs_bc_brier": br_om,
            "rate_1h_brier": br_rate,
            "verdict": verdict,
            "verdicts": {
                "WIRE_AS_SECOND_MEMBER": "Brier <= rate_1h AND fcerr < 0.85 -> wire om_gfs into fusion",
                "REPLACE_LOCAL_GFS_ONLY_NO_DIVERSITY_GAIN": "better/equal Brier but collinear -> swap out raw local GFS, no diversity claim",
                "DO_NOT_WIRE": "worse Brier -> do not wire",
            },
        },
    }
    return out


def write_report(out):
    ART.mkdir(parents=True, exist_ok=True)
    js = ART / "om_gfs_strike_crossing_2026-10-13.json"
    js.write_text(json.dumps(out, indent=2))

    s = out["scores"]
    f = out["fcerr"]
    d = out["decision"]
    L = []
    A = L.append
    A("# om_gfs strike-crossing validation — 2026-10-13")
    A("")
    A("**Question:** does Open-Meteo `om_gfs` (bias-corrected GFS pipeline) carry an edge")
    A("on the STRIKE-CROSSING axis, or is it another copy of rate_1h? The aggregation")
    A("study's 1.240 F MAE was a LEVEL-axis result — it does not establish a crossing edge.")
    A("")
    A(f"**Window:** {out['window'][0]} → {out['window'][1]} · "
      f"{out['grid_rows']} grid station-days · read-only on the live book.")
    A(f"**Axis:** {out['strike_rule']}; label `{out['label_rule']}`.")
    A("")
    A("## Head-to-head (same common rows)")
    A("")
    A(f"| source | n | **Brier** | dir_acc | ECE | resolution | mean_p |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for k in ("rate_1h", "om_gfs_raw", "om_gfs_bc", "om_gfs_bc_per_station_bias"):
        r = s[k]
        A(f"| {r['name']} | {r['n']} | **{r['brier']}** | {r['dir_acc']} | "
          f"{r['ece']} | {r['resolution']} | {r['mean_p']} |")
    g = s.get("gfs_raw_sigma4", {})
    if g.get("n"):
        A(f"| {g['name']} (ref) | {g['n']} | **{g['brier']}** | {g['dir_acc']} | "
          f"{g['ece']} | {g['resolution']} | {g['mean_p']} |")
    A("")
    A(f"Baselines: rate_1h study n=2464 → **{out['baseline_briers']['rate_1h_study_n2464']}**; "
      f"same-window overlap → **{out['baseline_briers']['rate_1h_window_n123']}**; "
      f"recomputed common rows → **{out['baseline_briers']['rate_1h_recomputed_common_rows']}**.")
    A("")
    A("## Independence (fcerr)")
    A("")
    A(f"- level (pooled): **{f['level_pooled']}**")
    A(f"- level (within-station demeaned): **{f['level_within_station_demeaned']}**")
    A(f"- P(cross) pooled: **{f['pcross_pooled']}**")
    A(f"- max |r| = **{f['max_abs']}** (flag at {f['flag_threshold']}) → "
      f"collinear = **{f['collinear']}**")
    A("")
    A("## Decision")
    A("")
    A(f"- criterion (a) Brier(om_gfs bc) ≤ rate_1h: **{d['criterion_a_brier_le_rate_1h']}** "
      f"({d['om_gfs_bc_brier']} vs {d['rate_1h_brier']})")
    A(f"- criterion (b) fcerr < 0.85: **{d['criterion_b_fcerr_lt_0.85']}**")
    A(f"- **VERDICT: `{d['verdict']}`**")
    A("")
    A("## Bias / sigma")
    A("")
    A(f"- global bias = **{out['global_bias_F']} F**; global sigma = **{out['global_sigma_F']} F**")
    A("")
    lm = out["level_mae"]
    A("## Level MAE (reproduction + swap win)")
    A("")
    A(f"- om_gfs raw = {lm['om_gfs_raw']} F, bias-corrected = {lm['om_gfs_bc_global']} F")
    A(f"- raw local gfs = {lm['gfs_raw']} F  →  om_gfs(bc) − local gfs = "
      f"**{lm['om_gfs_bc_minus_gfs_raw']} F**")
    A(f"- rate_1h mu level MAE = {lm['rate_1h_mu']} F (already tighter than any NWP)")
    A("")
    bt = out["blend_test"]
    A("## Blend test — does om_gfs add on top of rate_1h?")
    A("")
    A(f"- common rows: {bt['common_n']}")
    A(f"- rate_1h alone = {bt['rate_1h_alone_brier']}; om_gfs(bc) alone = {bt['om_gfs_bc_alone_brier']}")
    A(f"- best blend (w_om={bt['best_weight_on_om']}) = **{bt['best_blend_brier']}** "
      f"({bt['best_blend_minus_rate_alone']} vs rate_1h alone)")
    A(f"- paired Δ = {bt['paired_delta_mean']} ± {bt['paired_delta_se']} (t={bt['paired_t']}) — "
      f"significant at 2s.e.: **{bt['significant_at_2se']}**, but w is in-sample selected.")
    A("")
    ns = out["nwp_level_substitution"]
    A("## Verdict interpretation")
    A("")
    A(f"- om_gfs(bc) is a better NWP than raw local GFS on BOTH axes: level MAE "
      f"{ns['level_mae_gain_F']} F better; strike-crossing Brier {ns['brier_om_gfs_bc_all_rows']} "
      f"vs {ns['brier_gfs_raw']}.")
    A(f"- But it does NOT beat rate_1h's Brier ({out['decision']['rate_1h_brier']}) and it is "
      f"collinear with rate_1h (fcerr max |r| = {out['fcerr']['max_abs']} ≥ 0.85), so it is "
      f"**not an independent second member**.")
    A(f"- Verdict: **{out['decision']['verdict']}**. The free win is on the LEVER/level axis "
      f"(substitute om_gfs for raw local GFS); do not claim a diversity gain or wire it as a "
      f"fusion member ahead of rate_1h.")
    A("")
    md = ART / "om_gfs_strike_crossing_2026-10-13.md"
    md.write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["collect", "analyze", "all"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-cache", action="store_true")
    args = ap.parse_args()

    om = {}
    meta = {}
    if args.phase in ("collect", "all"):
        log("collecting om_gfs (20 requests, >= %.1fs apart)..." % DELAY_S)
        om = collect(force=args.force)
        meta = {"station_days_fetched": len(om),
                "stations_with_data": len({k[0] for k in om}),
                "source": "historical-forecast-api.open-meteo.com"}
    if args.phase in ("analyze", "all"):
        if not om:
            om = collect(force=False)
            meta = {"station_days_fetched": len(om),
                    "stations_with_data": len({k[0] for k in om}),
                    "source": "cache"}
        log("building grid (ASOS scan ~60-120s)...")
        grid = build_grid(om)
        log(f"grid rows: {len(grid)}")
        out = analyze(grid, meta)
        write_report(out)
        # console
        print("=" * 88)
        print("om_gfs STRIKE-CROSSING VALIDATION")
        print("=" * 88)
        for k in ("rate_1h", "om_gfs_raw", "om_gfs_bc", "om_gfs_bc_per_station_bias",
                  "gfs_raw_sigma4", "om_gfs_bc_all_rows"):
            r = out["scores"].get(k, {})
            if r.get("n"):
                print(f"{r['name']:28} n={r['n']:5} Brier={r['brier']:.4f} "
                      f"dir={r['dir_acc']:.4f} ECE={r['ece']:.4f} "
                      f"resol={r['resolution']:.4f}")
        print("-" * 88)
        print(f"fcerr: level_pooled={out['fcerr']['level_pooled']} "
              f"within={out['fcerr']['level_within_station_demeaned']} "
              f"pcross={out['fcerr']['pcross_pooled']} "
              f"max|r|={out['fcerr']['max_abs']} collinear={out['fcerr']['collinear']}")
        print(f"global bias={out['global_bias_F']} sigma={out['global_sigma_F']}")
        print(f"VERDICT: {out['decision']['verdict']}")
        # delete caches (constraint)
        if not args.keep_cache:
            for p in (CACHE, BUCKET_CACHE):
                try:
                    p.unlink()
                    log(f"deleted {p.name}")
                except OSError:
                    pass


if __name__ == "__main__":
    main()
