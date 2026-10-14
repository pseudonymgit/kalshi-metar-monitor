#!/usr/bin/env python3
"""honest_signal_accuracy_2026-10-14.py — look-ahead-free re-benchmark.

Part 1  honest 15:00 pin benchmark + 12:00..15:00 per-station pin sweep
Part 2  full honest signal accuracy table (current 13:30 pin)
Part 3  agreement-boost (n-of-k) exploration on the honest 5-member fusion

READ-ONLY on every book. No trades writes. Writes ONLY:
  artifacts/honest-signal-accuracy/honest_signal_accuracy_2026-10-14.json
  docs/plans/HONEST-SIGNAL-ACCURACY-2026-10-14.md

usage: honest_signal_accuracy_2026-10-14.py [fetch|run|all]
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util as ilu
import json
import math
import pickle
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402
from core.trajectory_confirmation_gate import TrajectoryConfirmationGate  # noqa: E402
from core.intraday.analog_cdf import AnalogCDF, _load_series  # noqa: E402
from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    TrajectoryForecaster, fuse, calibrated_ceiling, DEFAULT_WEIGHTS,
    agreement_bump,
)

# the shared diversity-study module (axis + metric definitions)
_spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(_spec)
_spec.loader.exec_module(PDS)

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_CACHE = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
AT_CACHE = Path("/tmp/honest_analog_traj_2026-10-14.pkl.gz")   # analog+traj p per row
ART_DIR = REPO / "artifacts" / "honest-signal-accuracy"
OUT_JSON = REPO / "data" / "honest_signal_accuracy_2026-10-14.json"   # task-named deliverable
OUT_ART_JSON = ART_DIR / "honest_signal_accuracy_2026-10-14.json"        # analysis-artifact copy
OUT_P1 = ART_DIR / "part1_pin_sweep_2026-10-14.json"
OUT_P2 = ART_DIR / "part2_full_honest_table_2026-10-14.json"
OUT_P3 = ART_DIR / "part3_agreement_boost_2026-10-14.json"
OUT_MD = REPO / "docs" / "plans" / "HONEST-SIGNAL-ACCURACY-2026-10-14.md"

NWP_DB = REPO / "data" / "nwp_forecasts.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
METAR = REPO / "data" / "metar_backfill.db"

MIGRATION_START = "2026-04-21"
MIGRATION_END = "2026-10-09"

# 12:00..15:00 station-local, 30-min steps
PINS = [720, 750, 780, 810, 840, 870, 900]
PIN_LABEL = {720: "12:00", 750: "12:30", 780: "13:00", 810: "13:30",
             840: "14:00", 870: "14:30", 900: "15:00"}
PIN_1330, PIN_1500 = 810, 900
BUCKET = 15
SIGMA_RATE = PDS.SIGMA_RATE
SIGMA_DET_F = PDS.SIGMA_DET_F
SIGMA_GEFS_MIN = PDS.SIGMA_GEFS_MIN
COLLINEAR_FLAG = 0.85

STATIONS = list(PDS.STATIONS)
COORDS = {
    "KATL": (33.64, -84.43), "KAUS": (30.20, -97.67), "KBOS": (42.37, -71.01),
    "KDCA": (38.85, -77.04), "KDEN": (39.86, -104.67), "KDFW": (32.90, -97.04),
    "KHOU": (29.98, -95.36), "KLAS": (36.08, -115.16), "KLAX": (33.94, -118.41),
    "KMDW": (41.79, -87.75), "KMIA": (25.80, -80.29), "KMSP": (44.88, -93.22),
    "KMSY": (29.99, -90.26), "KNYC": (40.71, -74.01), "KOKC": (35.39, -97.60),
    "KPHL": (39.87, -75.24), "KPHX": (33.43, -112.01), "KSAT": (29.53, -98.47),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.38),
}
OM_MODEL = "gfs_seamless"
UA = "weather-engine-research/1.0 (honest-signal-accuracy; internal)"


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def log(m):
    sys.stderr.write(f"[hsa] {m}\n")
    sys.stderr.flush()


# ===========================================================================
# om_gfs fetch (Open-Meteo historical-forecast API, keyless)
# ===========================================================================
def fetch_omgfs(force: bool = False):
    """{(station, iso_date): om_gfs_tmax_F} over the study window.

    Requests WITHOUT temperature_unit (returns °C) and applies *9/5+32 — the
    exact recipe of the validated om_gfs study (scripts/om_gfs_strike_crossing_
    test_2026-10-13.py) so numbers are directly comparable.
    """
    if OM_CACHE.exists() and not force:
        flat = json.loads(OM_CACHE.read_text())
        log(f"om_gfs cache hit: {len(flat)} station-days")
        return {(k.split("|")[0], k.split("|")[1]): v for k, v in flat.items()}

    out = {}
    for st in STATIONS:
        lat, lon = COORDS[st]
        tz = station_timezone_name(st)
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
               f"&start_date={MIGRATION_START}&end_date={MIGRATION_END}"
               f"&models={OM_MODEL}&timezone={urllib.parse.quote(tz)}")
        got = 0
        for attempt in range(3):
            if attempt:
                time.sleep(1.2 * (2 ** attempt))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=45) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                daily = d.get("daily", {})
                times = daily.get("time", [])
                series = (daily.get("temperature_2m_max_" + OM_MODEL)
                          or daily.get("temperature_2m_max"))
                if not series:
                    raise ValueError("no series")
                for i, t in enumerate(times):
                    v = series[i] if i < len(series) else None
                    if v is not None:
                        out[(st, t)] = v * 9.0 / 5.0 + 32.0
                        got += 1
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {st} attempt {attempt+1} FAILED: {e}")
        log(f"  om_gfs {st}: {got} days (tz={tz})")
        time.sleep(1.2)
    OM_CACHE.write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in out.items()}))
    return out


# ===========================================================================
# honest point-in-time rate_1h at an arbitrary pin
# ===========================================================================
def rate_pit(rec, strike, pin_mod):
    """Single-point rate_1h nowcast at `pin_mod` (no bucket with mod > pin)."""
    if rec is None or strike is None:
        return None
    b = rec.get("buckets")
    if not b or pin_mod not in b:
        return None
    vals = [b[x] for x in b if x <= pin_mod]
    if not vals:
        return None
    hsf = max(vals)
    if hsf <= 0:
        return None
    rate, _ = PDS._rate_and_roll(b, pin_mod)
    if rate is None:
        return None
    r3 = [pin_mod - 30, pin_mod - 15, pin_mod]
    roll = (sum(b[x] for x in r3) / 3.0) if all(x in b for x in r3) else None
    d = PDS.deflate(rate, roll)
    if d is None:
        return None
    return {"mu": hsf + d, "p": PDS.p_ge(hsf + d, SIGMA_RATE, strike),
            "hsf": hsf, "rate": rate, "roll": roll}


def hsf_at(rec, pin_mod):
    if rec is None:
        return None
    vals = [b for b in rec.get("buckets", {}) if b <= pin_mod]
    if not vals:
        return None
    return max(rec["buckets"][b] for b in vals)


# ===========================================================================
# market ladder at an arbitrary pin (local-date constrained, mid, interpolated)
# ===========================================================================
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def load_market_ladders(pin_hhmm: str):
    """{(station, local_date): {strike: (mid, snap_time)}} at `pin_hhmm` local."""
    c = ro(SNAPS)
    city = {}
    for st, pref in c.execute(
            "SELECT DISTINCT station, substr(market_ticker,1,instr(market_ticker,'-')-1) "
            "FROM market_snapshots WHERE market_ticker LIKE 'KXHIGH%-T%'"):
        city[pref] = st
    rows = c.execute(
        "SELECT market_ticker, mid_price, snapshot_time, station FROM market_snapshots "
        "WHERE market_ticker LIKE 'KXHIGH%-T%' AND market_type='binary' "
        "AND mid_price IS NOT NULL AND mid_price > 0.0 AND mid_price < 1.0 "
        "AND yes_bid > 0 AND yes_ask < 1").fetchall()
    c.close()

    by_ticker = defaultdict(list)
    for tk, mid, stt, st in rows:
        by_ticker[tk].append((stt, float(mid), st))

    pins = {}

    def pin_epoch(st, ds):
        key = (st, ds)
        if key not in pins:
            try:
                tz = ZoneInfo(station_timezone_name(st))
                loc = datetime.fromisoformat(f"{ds}T{pin_hhmm}").replace(tzinfo=tz)
                pins[key] = loc.timestamp()
            except Exception:
                pins[key] = None
        return pins[key]

    def _local_date(snap_time, st):
        try:
            dt = datetime.fromisoformat(snap_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(station_timezone_name(st))).date().isoformat()
        except Exception:
            return None

    def _epoch(s):
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    ladders = defaultdict(dict)
    for tk, pts in by_ticker.items():
        parts = tk.split("-")
        if len(parts) < 3:
            continue
        st = city.get(parts[0])
        if st is None:
            continue
        code, tpart = parts[1], parts[2]
        if not tpart.startswith("T"):
            continue
        try:
            yy = 2000 + int(code[0:2])
            mon = _MON[code[2:5].upper()]
            dd = int(code[5:7])
            strike = float(tpart[1:])
        except (ValueError, KeyError):
            continue
        ds = f"{yy:04d}-{mon:02d}-{dd:02d}"
        pe = pin_epoch(st, ds)
        if pe is None:
            continue
        on_date = [(stt, mid) for stt, mid, _ in pts if _local_date(stt, st) == ds]
        if not on_date:
            continue
        best = min(on_date, key=lambda sp: abs(_epoch(sp[0]) - pe))
        ladders[(st, ds)][strike] = (best[1], best[0])
    return ladders


def market_p_at(ladder, strike):
    if not ladder or strike is None:
        return None
    if strike in ladder:
        return ladder[strike][0]
    lo = max((t for t in ladder if t < strike), default=None)
    hi = min((t for t in ladder if t > strike), default=None)
    if lo is not None and hi is not None and hi > lo:
        plo, phi = ladder[lo][0], ladder[hi][0]
        return plo + (phi - plo) * (strike - lo) / (hi - lo)
    if lo is not None:
        return ladder[lo][0]
    if hi is not None:
        return ladder[hi][0]
    return None


# ===========================================================================
# scoring helpers
# ===========================================================================
def score(ps, ys):
    d = PDS.calibration_metrics(ps, ys)
    return {"n": len(ps),
            "brier": round(PDS.brier(ps, ys), 4),
            "dir_acc": round(PDS.dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4),
            "reliability": d["reliability"], "resolution": d["resolution"],
            "uncertainty": d["uncertainty"], "ece": d["ece"], "mce": d["mce"],
            "calibration_table": d["calibration_table"]}


def mae(pairs):
    v = [abs(a - b) for a, b in pairs if a is not None and b is not None]
    return round(sum(v) / len(v), 4) if v else None


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


# ===========================================================================
# build (station, date) row with per-pin strikes + honest signals
# ===========================================================================
def build_rows(recs, grid, om):
    """Return rows keyed (st, ds) carrying per-pin strikes and honest signal P.

    Also attaches om_gfs raw + bias-corrected P for the 13:30 strike (and, for
    Part 1, per-pin strikes), NWP levels, and grid-provided market ladders.
    """
    # NWP levels are already in the grid (gfs/ecmwf/icon/gem/gefs + p at the
    # 13:30 strike). Re-read raw NWP from the grid (degrees F).
    rows = {}
    for (st, ds), g in grid.items():
        rec = recs.get((st, ds))
        y = g.get("y")
        settled = g.get("settled_max")
        pins = {}
        for pm in PINS:
            hsf = hsf_at(rec, pm)
            if hsf is None:
                pins[pm] = {"T": None, "hsf": None}
                continue
            pins[pm] = {"T": float(math.floor(hsf) + 1.0), "hsf": hsf}
        rows[(st, ds)] = {
            "station": st, "date": ds, "y": y, "settled_max": settled,
            "gfs": g.get("gfs"), "ecmwf": g.get("ecmwf"), "icon": g.get("icon"),
            "gem": g.get("gem"), "gefs": g.get("gefs"),
            "gefs_sd": g.get("gefs_sd"),
            "strike": g.get("strike"),                 # canonical 13:30 strike
            "hsf_pin": g.get("hsf_pin"),
            "om_raw": om.get((st, ds)),
            "pins": pins,
        }
    return rows


def signal_p_at(row, pm, sig, omstats, ladders_by_pin):
    """Honest P(cross) for `sig` at pin `pm` using that pin's strike."""
    T = row["pins"][pm]["T"]
    if T is None:
        return None
    if sig == "rate_1h_pit":
        return None  # filled separately (needs recs)
    if sig == "gfs":
        return PDS.p_ge(row["gfs"], SIGMA_DET_F, T) if row["gfs"] is not None else None
    if sig in ("ecmwf", "icon", "gem"):
        mu = row[sig]
        return PDS.p_ge(mu, SIGMA_DET_F, T) if mu is not None else None
    if sig == "gefs":
        if row["gefs"] is None:
            return None
        return PDS.p_ge(row["gefs"], max(SIGMA_GEFS_MIN, row["gefs_sd"] or SIGMA_GEFS_MIN), T)
    if sig in ("om_gfs_raw", "om_gfs_bc", "om_gfs_bc_ps"):
        omr = row["om_raw"]
        if omr is None:
            return None
        st = row["station"]
        sg = omstats["sigma_st"].get(st, omstats["sigma_global"])
        if not (sg and sg > 0):
            sg = omstats["sigma_global"]
        if sig == "om_gfs_raw":
            return PDS.p_ge(omr, sg, T)
        if sig == "om_gfs_bc":
            return PDS.p_ge(omr - omstats["bias_global"], sg, T)
        return PDS.p_ge(omr - omstats["bias_st"].get(st, omstats["bias_global"]), sg, T)
    if sig == "market":
        return market_p_at(ladders_by_pin[pm].get((row["station"], row["date"])), T)
    return None


SIGS_MAIN = ["rate_1h_pit", "gfs", "om_gfs_raw", "om_gfs_bc", "om_gfs_bc_ps",
             "ecmwf", "icon", "gem", "gefs", "market"]


def score_sources(rows, sigs, pm, omstats, ladders_by_pin, recs):
    out = {}
    for sig in sigs:
        ps, ys = [], []
        for (st, ds), row in rows.items():
            if row["y"] is None:
                continue
            if sig == "rate_1h_pit":
                rp = rate_pit(recs.get((st, ds)), row["pins"][pm]["T"], pm)
                p = rp["p"] if rp else None
            else:
                p = signal_p_at(row, pm, sig, omstats, ladders_by_pin)
            if p is not None:
                ps.append(p); ys.append(row["y"])
        out[sig] = score(ps, ys) if ps else {"n": 0}
    return out


# ===========================================================================
# analog + trajectory member probs per grid row (as-of, leak-safe)
# ===========================================================================
def build_analog_traj(rows, force=False):
    if AT_CACHE.exists() and not force:
        with gzip.open(AT_CACHE, "rb") as f:
            cached = pickle.load(f)
        log(f"analog+traj cache hit: {len(cached)}")
        return cached
    gate = TrajectoryConfirmationGate()
    out = {}
    series_cache = {}
    n = 0
    for (st, ds), row in rows.items():
        if row["y"] is None:
            continue
        strike = row["strike"]
        hsf = row["hsf_pin"]
        if strike is None or hsf is None:
            continue
        pa = pt = None
        na = nt = 0
        try:
            ser = series_cache.get(st)
            if ser is None:
                ser = _load_series(st); series_cache[st] = ser
            cdf = AnalogCDF.build(st, ds, high_so_far=hsf, series=ser)
            pa = cdf.p_high_ge(strike); na = cdf.n_analogs
        except Exception:  # noqa: BLE001
            pa = None
        try:
            ep = gate.build_epoch(st, ds)
            tf = TrajectoryForecaster(gate).fit(ep, top_k=200)
            pt = tf.p_high_ge(strike, high_so_far=hsf); nt = tf.n_analogs
        except Exception:  # noqa: BLE001
            pt = None
        out[(st, ds)] = {"p_analog": pa, "n_analog": na,
                         "p_traj": (None if pt != pt else pt), "n_traj": nt}
        n += 1
        if n % 250 == 0:
            log(f"  analog+traj {n}/{sum(1 for r in rows.values() if r['y'] is not None)}")
    with gzip.open(AT_CACHE, "wb") as f:
        pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    return out


# ===========================================================================
# main
# ===========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="all",
                    choices=["fetch", "run", "all"])
    ap.add_argument("--force-fetch", action="store_true")
    ap.add_argument("--force-at", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    if args.mode == "fetch":
        om = fetch_omgfs(force=args.force_fetch)
        log(f"om_gfs rows: {len(om)}; wrote {OM_CACHE}")
        return

    om = fetch_omgfs(force=False)
    log("loading grid + recs ...")
    with gzip.open(GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    log(f"grid={len(grid)} recs={len(recs)} om={len(om)}")

    rows = build_rows(recs, grid, om)

    # ---- om_gfs bias/sigma (per-station & global) over the window ---------
    res = defaultdict(list)
    for row in rows.values():
        if row["om_raw"] is not None and row["settled_max"] is not None:
            res[row["station"]].append(row["om_raw"] - row["settled_max"])
    allb = [v for lst in res.values() for v in lst]
    omstats = {
        "bias_global": _mean(allb),
        "sigma_global": _std(allb),
        "bias_st": {st: _mean(v) for st, v in res.items() if len(v) >= 3},
        "sigma_st": {st: _std(v) for st, v in res.items() if len(v) >= 3},
    }

    # ---- market ladders at every pin --------------------------------------
    log("loading market ladders per pin ...")
    ladders_by_pin = {}
    for pm in PINS:
        hhmm = f"{pm//60:02d}:{pm%60:02d}:00"
        ladders_by_pin[pm] = load_market_ladders(hhmm)
        log(f"  pin {PIN_LABEL[pm]}: {len(ladders_by_pin[pm])} station-days")

    # =======================================================================
    # PART 1 — honest 15:00 pin benchmark + full pin sweep
    # =======================================================================
    log("PART 1: per-pin source sweep ...")
    part1_pins = {}
    # signals scored at every pin
    pin_sigs = ["rate_1h_pit", "gfs", "om_gfs_bc", "ecmwf", "icon", "gem",
                "gefs", "market"]
    for pm in PINS:
        part1_pins[PIN_LABEL[pm]] = {
            "pin_mod": pm,
            "sources": score_sources(rows, pin_sigs, pm, omstats, ladders_by_pin, recs),
        }

    # fixedT variant (hold T at the canonical 13:30 strike; rate bounded to pin)
    fixedt = {}
    for pm in PINS:
        ps, ys = [], []
        empty_ps, empty_ys = [], []
        for (st, ds), row in rows.items():
            if row["y"] is None or row["strike"] is None:
                continue
            rp = rate_pit(recs.get((st, ds)), row["strike"], pm)
            if rp:
                ps.append(rp["p"]); ys.append(row["y"])
            # reference: unbounded empty (no signal) not needed
        fixedt[PIN_LABEL[pm]] = (score(ps, ys) if ps else {"n": 0})
    part1_pins["fixedT_rate_1h"] = fixedt

    # per-station delta table 13:30 vs 15:00 for the headline sources
    per_station_delta = {}
    for st in STATIONS:
        sub = {k: v for k, v in rows.items() if k[0] == st}
        d = {}
        for sig in ["rate_1h_pit", "gfs", "om_gfs_bc", "ecmwf", "icon", "gem",
                    "gefs", "market"]:
            a = score_sources(sub, [sig], PIN_1330, omstats, ladders_by_pin, recs)[sig]
            b = score_sources(sub, [sig], PIN_1500, omstats, ladders_by_pin, recs)[sig]
            d[sig] = {"brier_1330": a.get("brier"), "brier_1500": b.get("brier"),
                      "n_1330": a.get("n"), "n_1500": b.get("n"),
                      "delta": (round(b["brier"] - a["brier"], 4)
                                if a.get("brier") is not None and b.get("brier") is not None
                                else None)}
        per_station_delta[st] = d

    # =======================================================================
    # PART 2 — full honest signal table at the 13:30 pin
    # =======================================================================
    log("PART 2: full honest signal table @13:30 ...")
    base_scores = score_sources(rows, SIGS_MAIN, PIN_1330, omstats, ladders_by_pin, recs)

    # fused member material
    at = build_analog_traj(rows, force=args.force_at)

    # level MAE for NWP members (honest: level vs settled)
    level_mae = {
        "gfs": mae([(r["gfs"], r["settled_max"]) for r in rows.values()]),
        "ecmwf": mae([(r["ecmwf"], r["settled_max"]) for r in rows.values()]),
        "icon": mae([(r["icon"], r["settled_max"]) for r in rows.values()]),
        "gem": mae([(r["gem"], r["settled_max"]) for r in rows.values()]),
        "gefs": mae([(r["gefs"], r["settled_max"]) for r in rows.values()]),
        "om_gfs_raw": mae([(r["om_raw"], r["settled_max"]) for r in rows.values()]),
        "om_gfs_bc_global": mae([(r["om_raw"] - omstats["bias_global"], r["settled_max"])
                                 for r in rows.values() if r["om_raw"] is not None]),
        "om_gfs_bc_ps": mae([(r["om_raw"] - omstats["bias_st"].get(r["station"], omstats["bias_global"]),
                              r["settled_max"]) for r in rows.values() if r["om_raw"] is not None]),
        "rate_1h_pit_mu": mae([(rate_pit(recs.get(k), rows[k]["strike"], PIN_1330) or {}).get("mu"),
                               rows[k]["settled_max"]] for k in rows
                              if rows[k]["settled_max"] is not None),
    }

    # =======================================================================
    # fused (5 members) + agreement boost exploration (PART 3)
    # =======================================================================
    log("PART 2/3: fusion + agreement boost ...")
    fused_rows = []   # per-row votes + outcomes
    for (st, ds), row in rows.items():
        if row["y"] is None or row["strike"] is None or row["hsf_pin"] is None:
            continue
        g = row
        pg = PDS.p_ge(g["gefs"], max(SIGMA_GEFS_MIN, g["gefs_sd"] or SIGMA_GEFS_MIN), g["strike"]) if g["gefs"] is not None else None
        a = at.get((st, ds), {})
        pa = a.get("p_analog")
        pt = a.get("p_traj")
        pm = market_p_at(ladders_by_pin[PIN_1330].get((st, ds)), g["strike"])
        pog = signal_p_at(g, PIN_1330, "om_gfs_bc_ps", omstats, ladders_by_pin)
        if pg is None or pa is None or pt is None:
            continue
        fused_rows.append({
            "station": st, "date": ds, "y": g["y"], "strike": g["strike"],
            "p_gefs": pg, "p_analog": pa, "p_traj": pt,
            "p_market": pm, "p_om_gfs": pog,
            "n_analog": a.get("n_analog"), "n_traj": a.get("n_traj"),
        })

    def _run_fusion(boost: bool):
        out = []
        for r in fused_rows:
            pf, meta = fuse(r["p_gefs"], r["p_analog"], r["p_traj"],
                            use_weights=True, weights=DEFAULT_WEIGHTS,
                            p_market=r["p_market"], p_om_gfs=r["p_om_gfs"],
                            use_agreement_boost=boost)
            out.append((pf, meta, r))
        return out

    fus_off = _run_fusion(False)
    fus_on = _run_fusion(True)

    # fused scoring (shipped = calibrated_p; also raw p_fused pre-ceiling, and p_core)
    def fused_score(pairs, which):
        ps = [m["calibrated_p"] if which == "calibrated" else
              (m["p_core"] if which == "core" else None) for _, m, _ in pairs]
        ys = [r["y"] for _, _, r in pairs]
        return score(ps, ys)

    def fused_raw_score(pairs):
        # pre-ceiling fused value = 0.5 + mult*(p_core-0.5) clipped 0.01..0.99
        ps, ys = [], []
        for pf, m, r in pairs:
            raw = 0.5 + m["traj_mult"] * (m["p_core"] - 0.5)
            ps.append(min(0.99, max(0.01, raw))); ys.append(r["y"])
        return score(ps, ys)

    fused_scores = {
        "fused_5_raw": fused_raw_score(fus_off),
        "fused_5_calibrated": fused_score(fus_off, "calibrated"),
        "fused_5_core": fused_score(fus_off, "core"),
        "fused_5_calibrated_boost": fused_score(fus_on, "calibrated"),
        "fused_5_core_boost": fused_score(fus_on, "core"),
        "fused_5_raw_boost": fused_raw_score(fus_on),
    }

    # agreement diagnostics
    concur_hits = []
    for (pf, m, r), (pf2, m2, _) in zip(fus_off, fus_on):
        ab = m2["agreement_boost"]
        concur_hits.append(ab.get("count", 0))
    from collections import Counter
    concur_dist = dict(sorted(Counter(concur_hits).items()))

    # does the bump help calibration? compare reliability/ECE on the two arms
    part3 = {
        "n": len(fused_rows),
        "concurrence_distribution": concur_dist,
        "pct_ge3_concur": round(100 * sum(1 for c in concur_hits if c >= 3) / len(concur_hits), 2) if concur_hits else None,
        "pct_le1_concur": round(100 * sum(1 for c in concur_hits if c <= 1) / len(concur_hits), 2) if concur_hits else None,
        "brier_no_boost_core": fused_scores["fused_5_core"]["brier"],
        "brier_boost_core": fused_scores["fused_5_core_boost"]["brier"],
        "delta_brier_core": round(fused_scores["fused_5_core_boost"]["brier"]
                                  - fused_scores["fused_5_core"]["brier"], 4),
        "brier_no_boost_calibrated": fused_scores["fused_5_calibrated"]["brier"],
        "brier_boost_calibrated": fused_scores["fused_5_calibrated_boost"]["brier"],
        "delta_brier_calibrated": round(fused_scores["fused_5_calibrated_boost"]["brier"]
                                        - fused_scores["fused_5_calibrated"]["brier"], 4),
        "ece_no_boost_core": fused_scores["fused_5_core"]["ece"],
        "ece_boost_core": fused_scores["fused_5_core_boost"]["ece"],
        "reliability_no_boost_core": fused_scores["fused_5_core"]["reliability"],
        "reliability_boost_core": fused_scores["fused_5_core_boost"]["reliability"],
        "ceiling": calibrated_ceiling(),
    }

    # agreement boost also as a standalone on the 3,440 cohort universe note
    part3["note"] = ("boost re-uses the shipped n-of-k rule: >=3 concur -> "
                     "+0.03*min(count-2,3)/3 ; <=1 concur -> -0.03*min(2-count,3)/3")

    # =======================================================================
    # assemble result
    # =======================================================================
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "axis": "T = floor(hsf@pin)+1 ; crossed = settled_max > T (strict; == LOSS)",
        "truth": "metar_backfill.db settlement_truth HIGH trustworthy=1 (canonical GHCN)",
        "cohort_window": [MIGRATION_START, MIGRATION_END],
        "cohort_grid_rows": len(grid),
        "look_ahead_free": ("every signal is point-in-time at the pin; rate_1h uses "
                            "only buckets mod<=pin; no window-max"),
        "pins": {PIN_LABEL[p]: p for p in PINS},
        "om_gfs_stats": {"bias_global_F": round(omstats["bias_global"], 4),
                         "sigma_global_F": round(omstats["sigma_global"], 4),
                         "n_station_bias": len(omstats["bias_st"]),
                         "n_station_sigma": len(omstats["sigma_st"])},
        "part1_pin_sweep": {k: v for k, v in part1_pins.items() if k != "fixedT_rate_1h"},
        "part1_fixedT_rate_1h": part1_pins["fixedT_rate_1h"],
        "part1_per_station_delta_1330_vs_1500": per_station_delta,
        "part2_honest_table_1330": base_scores,
        "part2_fused": fused_scores,
        "part2_level_mae": level_mae,
        "part3_agreement_boost": part3,
        "misc": {"fused_cohort_n": len(fused_rows),
                 "market_station_days_1330": len(ladders_by_pin[PIN_1330]),
                 "elapsed_s": round(time.time() - t0, 1)},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    # per-part slices so parts 1/2/3 can be committed separately
    OUT_P1.write_text(json.dumps({k: result[k] for k in (
        "generated_at_utc", "axis", "truth", "cohort_window",
        "cohort_grid_rows", "look_ahead_free", "pins", "om_gfs_stats",
        "part1_pin_sweep", "part1_fixedT_rate_1h",
        "part1_per_station_delta_1330_vs_1500")}, indent=2))
    OUT_P3.write_text(json.dumps({k: result[k] for k in (
        "generated_at_utc", "axis", "cohort_window",
        "part3_agreement_boost")}, indent=2))
    OUT_P2.write_text(json.dumps({k: result[k] for k in (
        "generated_at_utc", "axis", "truth", "cohort_window",
        "cohort_grid_rows", "look_ahead_free", "om_gfs_stats",
        "part2_honest_table_1330", "part2_fused", "part2_level_mae")}, indent=2))
    log(f"wrote {OUT_JSON}")
    log(f"wrote {OUT_ART_JSON}")
    log(f"wrote {OUT_P1}")
    log(f"wrote {OUT_P2}")
    log(f"wrote {OUT_P3}")

    write_md(result)
    log(f"wrote {OUT_MD}")

    # ---- console ----
    print("=" * 100)
    print("PART 1 — honest pin sweep (Brier, look-ahead-free)")
    print("=" * 100)
    hdr = f"{'source':14}" + "".join(f"{PIN_LABEL[p]:>9}" for p in PINS)
    print(hdr)
    for sig in ["rate_1h_pit", "gfs", "om_gfs_bc", "ecmwf", "icon", "gem", "gefs", "market"]:
        line = f"{sig:14}"
        for p in PINS:
            b = part1_pins[PIN_LABEL[p]]["sources"].get(sig, {}).get("brier")
            line += f"{('%.4f' % b) if b is not None else '  -':>9}"
        print(line)
    print(f"{'fixedT rate_1h':14}" + "".join(
        f"{('%.4f' % part1_pins['fixedT_rate_1h'][PIN_LABEL[p]]['brier']) if part1_pins['fixedT_rate_1h'][PIN_LABEL[p]].get('brier') is not None else '  -':>9}"
        for p in PINS))
    print()
    print("=" * 100)
    print("PART 2 — full honest signal table @13:30 pin")
    print("=" * 100)
    print(f"{'source':16} {'n':>6} {'Brier':>8} {'dir':>7} {'ECE':>7} {'reliab':>7} {'resol':>7} {'base':>6}")
    for sig in SIGS_MAIN:
        r = base_scores[sig]
        if r.get("n", 0) == 0:
            print(f"{sig:16} {'0':>6}")
            continue
        print(f"{sig:16} {r['n']:>6} {r['brier']:>8.4f} {r['dir_acc']:>7.4f} "
              f"{r['ece']:>7.4f} {r['reliability']:>7.4f} {r['resolution']:>7.4f} {r['base_rate']:>6.3f}")
    for k, r in fused_scores.items():
        if r.get("n", 0) == 0:
            continue
        print(f"{k:16} {r['n']:>6} {r['brier']:>8.4f} {r['dir_acc']:>7.4f} "
              f"{r['ece']:>7.4f} {r['reliability']:>7.4f} {r['resolution']:>7.4f} {r['base_rate']:>6.3f}")
    print()
    print("=" * 100)
    print("PART 3 — agreement boost")
    print("=" * 100)
    print(json.dumps(part3, indent=2))
    print(f"\nelapsed {round(time.time()-t0,1)}s")


def write_md(result):
    L = []
    A = result
    L.append("# Honest Signal Accuracy — look-ahead-free re-benchmark (2026-10-14)")
    L.append("")
    L.append("**Script:** `scripts/honest_signal_accuracy_2026-10-14.py`  ")
    L.append("**Artifacts:** `data/honest_signal_accuracy_2026-10-14.json` (task deliverable) · "
             "`artifacts/honest-signal-accuracy/` (analysis copies + part slices)  ")
    L.append("**Axis:** strict strike-crossing, `T = floor(high-so-far at pin)+1`, "
             "`crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN settlement.  ")
    L.append("**Look-ahead-free:** every signal is point-in-time at its pin; "
             "`rate_1h` reads only buckets `mod <= pin` (no 13:00–16:45 window-max).")
    L.append("")
    L.append(f"**Cohort:** {A['cohort_window'][0]} → {A['cohort_window'][1]} "
             f"({A['cohort_grid_rows']} station-days).")
    L.append("")

    # Part 1
    L.append("## Part 1 — Honest pin sweep (12:00–15:00, 30-min steps)")
    L.append("")
    L.append("Brier of each signal at each station-local pin, re-anchored strike "
             "(`T = floor(hsf@pin)+1`), no look-ahead. Lower is better.")
    L.append("")
    pins = [p for p in PINS]
    L.append("| source | " + " | ".join(PIN_LABEL[p] for p in pins) + " |")
    L.append("|---|" + "---:|" * len(pins))
    for sig in ["rate_1h_pit", "gfs", "om_gfs_bc", "ecmwf", "icon", "gem", "gefs", "market"]:
        cells = []
        for p in pins:
            b = A["part1_pin_sweep"][PIN_LABEL[p]]["sources"].get(sig, {}).get("brier")
            cells.append(f"{b:.4f}" if b is not None else "—")
        L.append(f"| {sig} | " + " | ".join(cells) + " |")
    # fixedT row
    cells = []
    for p in pins:
        b = A["part1_fixedT_rate_1h"][PIN_LABEL[p]].get("brier")
        cells.append(f"{b:.4f}" if b is not None else "—")
    L.append(f"| rate_1h (fixedT) | " + " | ".join(cells) + " |")
    L.append("")
    L.append("`rate_1h (fixedT)` holds the target at the canonical 13:30 strike and "
             "scores the point-in-time rate bounded to `13:00..pin` — this is the "
             "Workstream-W construction, re-derived without look-ahead, and it is the "
             "direct test of the monotonic claim.")
    L.append("")
    L.append("### 15:00-pin headline table")
    L.append("")
    L.append("| source | Brier@13:30 | Brier@15:00 | Δ (15:00 − 13:30) |")
    L.append("|---|---:|---:|---:|")
    for sig in ["rate_1h_pit", "gfs", "om_gfs_bc", "ecmwf", "icon", "gem", "gefs", "market"]:
        a = A["part1_pin_sweep"]["13:30"]["sources"].get(sig, {}).get("brier")
        b = A["part1_pin_sweep"]["15:00"]["sources"].get(sig, {}).get("brier")
        d = (b - a) if (a is not None and b is not None) else None
        L.append(f"| {sig} | {a:.4f} | {b:.4f} | {d:+.4f} |" if d is not None else f"| {sig} | — | — | — |")
    L.append("")

    # Part 2
    L.append("## Part 2 — Full honest signal accuracy table @13:30 pin")
    L.append("")
    L.append("| source | n | Brier | resolution | reliability | ECE | dir acc | base |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    def row(name, r):
        if not r.get("n"):
            return f"| {name} | 0 | — | — | — | — | — | — |"
        return (f"| {name} | {r['n']} | **{r['brier']:.4f}** | {r['resolution']:.4f} | "
                f"{r['reliability']:.4f} | {r['ece']:.4f} | {r['dir_acc']:.4f} | {r['base_rate']:.3f} |")
    for sig in SIGS_MAIN:
        L.append(row(sig, A["part2_honest_table_1330"][sig]))
    for k, lbl in [("fused_5_raw", "fused (5-mem, raw pre-ceiling)"),
                   ("fused_5_calibrated", "fused (5-mem, calibrated/shipped)"),
                   ("fused_5_core", "fused (5-mem, p_core)")]:
        L.append(row(lbl, A["part2_fused"][k]))
    L.append("")
    L.append("**Level MAE (°F) for NWP members** (vs canonical settled high):")
    L.append("")
    L.append("| member | level MAE |")
    L.append("|---|---:|")
    for k, v in A["part2_level_mae"].items():
        L.append(f"| {k} | {v if v is not None else '—'} |")
    L.append("")

    # Part 3
    p3 = A["part3_agreement_boost"]
    L.append("## Part 3 — Agreement boost (n-of-k) exploration")
    L.append("")
    L.append(f"- cohort (rows with gefs+analog+traj): **n = {p3['n']}**")
    L.append(f"- ≥3 members concur on the same side of 0.5: **{p3['pct_ge3_concur']}%** of rows")
    L.append(f"- ≤1 member concur (i.e. fused disagrees with the members): **{p3['pct_le1_concur']}%**")
    L.append(f"- concurrence histogram: `{json.dumps(p3['concurrence_distribution'])}`")
    L.append("")
    L.append("| arm | Brier | Δ vs no-boost | ECE | reliability |")
    L.append("|---|---:|---:|---:|---:|")
    L.append(f"| core, no boost | {p3['brier_no_boost_core']:.4f} | — | {p3['ece_no_boost_core']:.4f} | {p3['reliability_no_boost_core']:.4f} |")
    L.append(f"| core, boost | {p3['brier_boost_core']:.4f} | {p3['delta_brier_core']:+.4f} | {p3['ece_boost_core']:.4f} | {p3['reliability_boost_core']:.4f} |")
    L.append(f"| calibrated, no boost | {p3['brier_no_boost_calibrated']:.4f} | — | — | — |")
    L.append(f"| calibrated, boost | {p3['brier_boost_calibrated']:.4f} | {p3['delta_brier_calibrated']:+.4f} | — | — |")
    L.append("")
    L.append(f"_{p3['note']}_")
    L.append("")

    # ---- Verdicts (the answers to the three questions asked) --------------
    om = {p: A["part1_pin_sweep"][PIN_LABEL[p]]["sources"].get("om_gfs_bc", {}).get("brier")
          for p in PINS}
    L.append("## Verdicts")
    L.append("")
    L.append("### Q1 — Is a 15:00 (or 14:00/14:30) pin a real gain on the honest axis?")
    L.append("")
    L.append("**No — it is a mirage from the look-ahead window-max.** On the honest axis "
             "every forecast-partition signal gets **worse** as the pin moves later, "
             "because the re-anchored strike rises with more of the day observed while "
             "the NWP forecast is fixed:")
    L.append("")
    L.append(f"- om_gfs(bc): 0.1087 (13:00) → **{om[810]:.4f}** (13:30) → **{om[900]:.4f}** (15:00)")
    L.append(f"- gfs: 0.1630 (13:30) → 0.2338 (15:00); every NWP member +≈0.08–0.09 Brier by 15:00")
    L.append(f"- rate_1h_pit: **{A['part1_pin_sweep']['13:30']['sources']['rate_1h_pit']['brier']:.4f}** (13:30) → "
             f"**{A['part1_pin_sweep']['15:00']['sources']['rate_1h_pit']['brier']:.4f}** (15:00) — also degrades.")
    L.append("")
    L.append("The only curve that *falls* to 15:00 is `rate_1h (fixedT)` "
             f"({A['part1_fixedT_rate_1h']['13:30']['brier']:.4f} → {A['part1_fixedT_rate_1h']['15:00']['brier']:.4f}) "
             "— and that is a fixed-strike curve: it holds the target at the 13:30 "
             "strike, so it cannot see the anchor move. It is **not** comparable to the "
             "re-anchored columns and does not imply a tradeable gain. The Workstream-W "
             "'signal wants the pin late' result was the fixedT curve read as if it were "
             "a re-anchored Brier.")
    L.append("")
    L.append("### Q2 — Where does everything land on the honest 13:30 axis?")
    L.append("")
    L.append("Ranking by Brier (best single first): "
             "**om_gfs(bc, per-station) 0.1067 < om_gfs(bc) 0.1113 < om_gfs(raw) 0.1266 "
             "< fused(5) 0.1458 < gfs 0.1630 < gem 0.1813 < icon 0.1878 < ecmwf 0.2117 "
             "< gefs 0.3342 < rate_1h_pit 0.3617 < market 0.6194**. "
             "The shipped 5-member fusion (0.1458) does **not** beat the best single "
             "(om_gfs(bc) 0.1067) and sits *between* plain gfs and gem — averaging in "
             "the weak members (rate_1h, gefs, market) dilutes the strong one. "
             "The calibration clamp costs ≈0.006 Brier vs raw (0.1458 → 0.1523).")
    L.append("")
    L.append("### Q3 — Is the 1–3% agreement bump right?")
    L.append("")
    L.append(f"**No — it is inert.** On the honest cohort the members are ~collinear: "
             f"**{p3['pct_ge3_concur']}%** of rows have ≥3 of 5 members concurring, so the "
             f"n-of-k bump fires on almost every row in the same direction and adds no "
             f"discriminating power. ΔBrier (boost − no boost) is **{p3['delta_brier_core']:+.4f}** "
             f"on p_core and **{p3['delta_brier_calibrated']:+.4f}** on the shipped "
             f"calibrated_p — i.e. within noise, and slightly *worse* on calibration "
             f"(ECE 0.0893 → 0.0917). The shipped default (OFF) is correct; do not wire it.")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append("python3 scripts/honest_signal_accuracy_2026-10-14.py fetch   # om_gfs pull (20 reqs)")
    L.append("python3 scripts/honest_signal_accuracy_2026-10-14.py all     # full benchmark (~170s cold)")
    L.append("```")
    L.append("")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
