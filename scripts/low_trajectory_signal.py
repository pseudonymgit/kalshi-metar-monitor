#!/usr/bin/env python3
"""low_trajectory_signal.py — hourly COOLING-rate nowcast for LOW markets (design).

Mirror of the HIGH `rate_1h` heating nowcast (`scripts/predictor_diversity_study_
2026-10-13.py`), but for FALLING temperatures. LOW ("or below") markets settle
YES iff the day's MINIMUM is strictly below the strike, so the analog of
`high_so_far + deflated_rise` is `running_low − deflated_drop`.

Signal
------
At station-local pin `p` (minutes-of-day), over the 15-min ASOS bucket grid
(already shifted to station-local time; see predictor_diversity_study.load_asos):

  rlow(p)  = min(buckets[m] for m <= p)            # running low so far
  drop(p)  = mean(buckets[p-60..p-15]) - buckets[p]  # 1h DROP, >0 when falling
  roll(p)  = mean(buckets[p-30..p+15])             # 45-min centred roll
  d(p)     = deflate the drop (cap RATE_CAP; divide by the roll when it is a
             same-signed falling roll)              # conservative projection
  mu_low(p)= rlow(p) - d(p)                         # projected daily minimum
  p_low_le(X; p) = Phi((X - mu_low(p)) / SIGMA)     # P(daily min <= X)

The mirror of PDS.p_ge(mu, sigma, X) = Phi((mu-X)/sigma) is
p_low_le(X) = 1 - p_ge(mu_low, sigma, X) = Phi((X - mu_low)/sigma) — increasing
in X, so a higher "or below" strike is more likely to be crossed. Correct.

Honest LOW axis (mirror of HIGH `T = floor(hsf)+1, y = settled_max > T`):
  T(p) = floor(rlow(p)) - 0.5      # the "just below what we've seen" tail
  y    = 1 if settled_min <= T(p) else 0

READ-ONLY on every book. No trades writes. Writes ONLY:
  data/low_trajectory_pin_sweep_2026-10-14.json
  artifacts/low-market-lane/low_trajectory_pin_sweep_2026-10-14.json

usage: low_trajectory_signal.py [sweep|signal]
  sweep  — pin sweep 18:00..23:00 (+ 00:00/06:00/12:00/13:30 context) over the
           178 LOW-backfill station-days and the broader ASOS∩GHCN-low panel.
  signal — print the signal for one station-day-pin (debug).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import pickle
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402

ASOS_RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")   # local-mod bucket grid
OM_LOW_CACHE = Path("/tmp/we_low_omgfs_tmin_2026-10-14.json")
GHCN_DB = REPO / "data" / "ghcn_settlements.db"
METAR_DB = REPO / "data" / "metar_backfill.db"
LOW_CSV = REPO / "artifacts" / "low-market-backfill" / "low_entry_price_series_2026-10-13.csv"
ART_DIR = REPO / "artifacts" / "low-market-lane"
OUT_JSON = REPO / "data" / "low_trajectory_pin_sweep_2026-10-14.json"
OUT_ART_JSON = ART_DIR / "low_trajectory_pin_sweep_2026-10-14.json"

BUCKET_MIN = 15
SIGMA = 1.5              # nowcast sigma (°F) — same as HIGH SIGMA_RATE
UA = "weather-engine-research/1.0 (low-trajectory-signal; internal)"
OM_LOW_START, OM_LOW_END = "2026-09-07", "2026-09-23"
COORDS = {
    "KATL": (33.64, -84.43), "KAUS": (30.20, -97.67), "KBOS": (42.37, -71.01),
    "KDCA": (38.85, -77.04), "KDEN": (39.86, -104.67), "KDFW": (32.90, -97.04),
    "KHOU": (29.98, -95.36), "KLAS": (36.08, -115.16), "KLAX": (33.94, -118.41),
    "KMDW": (41.79, -87.75), "KMIA": (25.80, -80.29), "KMSP": (44.88, -93.22),
    "KMSY": (29.99, -90.26), "KNYC": (40.71, -74.01), "KOKC": (35.39, -97.60),
    "KPHL": (39.87, -75.24), "KPHX": (33.43, -112.01), "KSAT": (29.53, -98.47),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.38),
}
RATE_CAP = 8.0           # max projected drop (°F) — same cap as HIGH rise
ROLL_FLOOR = 1.0
ROLL_FACTOR = 2.5

# candidate LOW pins (station-local minutes-of-day)
SWEEP_PINS = [1080, 1110, 1140, 1170, 1200, 1230, 1260, 1290, 1320, 1350, 1380]
CONTEXT_PINS = [0, 180, 360, 540, 720, 810, 900]
PIN_LABEL = {0: "00:00", 180: "03:00", 360: "06:00", 540: "09:00", 720: "12:00",
             810: "13:30", 900: "15:00", 1080: "18:00", 1110: "18:30",
             1140: "19:00", 1170: "19:30", 1200: "20:00", 1230: "20:30",
             1260: "21:00", 1290: "21:30", 1320: "22:00", 1350: "22:30",
             1380: "23:00"}


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def log(m):
    sys.stderr.write(f"[low] {m}\n")
    sys.stderr.flush()


# ── signal ───────────────────────────────────────────────────────────────────
def deflate_drop(drop, roll):
    """Conservative projection of a 1h DROP, mirroring PDS.deflate for rises."""
    if drop is None:
        return None
    if roll is not None and roll < 0.0:
        return min(drop / max(ROLL_FLOOR, ROLL_FACTOR * abs(roll)), RATE_CAP)
    return min(drop, RATE_CAP)


def cooling_nowcast(buckets, pin_mod):
    """1h cooling nowcast at `pin_mod`. Returns a dict or None if no data."""
    if not buckets or pin_mod not in buckets:
        return None
    vals = [buckets[b] for b in buckets if b <= pin_mod]
    if not vals:
        return None
    rlow = min(vals)
    need = [pin_mod - 60, pin_mod - 45, pin_mod - 30, pin_mod - 15, pin_mod]
    if any(b not in buckets for b in need):
        return None
    drop = sum(buckets[b] for b in need[:-1]) / 4.0 - buckets[pin_mod]
    roll_b = [pin_mod - 30, pin_mod - 15, pin_mod, pin_mod + 15]
    roll = (sum(buckets[b] for b in roll_b) / 4.0
            if all(b in buckets for b in roll_b) else None)
    d = deflate_drop(drop, roll)
    if d is None:
        return None
    mu = rlow - d
    return {"rlow": round(rlow, 4), "drop": round(drop, 4),
            "deflated_drop": round(d, 4), "mu_low": round(mu, 4)}


def p_low_le(mu_low, sigma, x):
    """P(daily min <= X) = Phi((X - mu_low)/sigma)."""
    if sigma <= 0:
        return 1.0 if mu_low <= x else 0.0
    return 0.5 * (1.0 + math.erf(((x - mu_low) / sigma) / math.sqrt(2.0)))


def target_strike(rlow, offset=1.0):
    """Mirror of HIGH T=floor(hsf)+1: T = floor(rlow) - 0.5 (offset=1 -> just below)."""
    return math.floor(rlow) - (offset - 0.5)


# ── data ─────────────────────────────────────────────────────────────────────
def load_recs():
    if not ASOS_RECS.exists():
        raise SystemExit(f"missing {ASOS_RECS} — run the honest benchmark's ASOS load first")
    with gzip.open(ASOS_RECS, "rb") as f:
        return pickle.load(f)


def fetch_omgfs_tmin(force=False):
    """{(station, iso_date): om_gfs Tmin_F} over the LOW window (keyless API).

    The HIGH recipe generalized to the daily MINIMUM: same Open-Meteo
    historical-forecast endpoint, model gfs_seamless, °C->°F via *9/5+32.
    """
    if OM_LOW_CACHE.exists() and not force:
        flat = json.loads(OM_LOW_CACHE.read_text())
        return {(k.split("|")[0], k.split("|")[1]): v for k, v in flat.items()}
    from urllib.parse import quote
    out = {}
    for st, (lat, lon) in COORDS.items():
        tz = station_timezone_name(st)
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&daily=temperature_2m_min"
               f"&start_date={OM_LOW_START}&end_date={OM_LOW_END}"
               f"&models=gfs_seamless&timezone={quote(tz)}")
        for attempt in range(3):
            if attempt:
                time.sleep(1.2 * (2 ** attempt))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=45) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                for t, v in zip(d["daily"]["time"], d["daily"].get("temperature_2m_min", [])):
                    if v is not None:
                        out[(st, t)] = v * 9.0 / 5.0 + 32.0
                break
            except Exception as e:  # noqa: BLE001
                log(f"  om_gfs Tmin {st} attempt {attempt+1} FAILED: {e}")
        time.sleep(0.4)
    OM_LOW_CACHE.write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in out.items()}))
    return out


def om_tmin_arm(recs, truth, keys, om_t, *, bias=0.0, sigma=3.27, pin_mod=810):
    """Score the om_gfs Tmin level forecast as a P(low<=T) signal at a pin."""
    ps, ys = [], []
    for (st, ds) in keys:
        rec = recs.get((st, ds))
        tmin = truth.get((st, ds))
        mu = om_t.get((st, ds))
        if rec is None or tmin is None or mu is None:
            continue
        b = rec.get("buckets", {})
        vals = [b[x] for x in b if x <= pin_mod]
        if not vals:
            continue
        T = math.floor(min(vals)) - 0.5
        ps.append(p_low_le(mu - bias, sigma, T))
        ys.append(1 if tmin <= T else 0)
    return _score(ps, ys) if ps else {"n": 0}


def load_ghcn_lows():
    c = ro(GHCN_DB)
    try:
        out = {}
        for st, dt, lo in c.execute(
                "SELECT station, date, low_f FROM ghcn_settlements WHERE low_f IS NOT NULL"):
            out[(st, dt[:10])] = float(lo)
        return out
    finally:
        c.close()


def load_low_backfill_truth():
    """{(station, date): truth_min} for the 178 LOW-backfill station-days."""
    c = ro(METAR_DB)
    try:
        truth = {(r[0], r[1]): float(r[2]) for r in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='LOW' AND settlement_temp_f IS NOT NULL "
            "AND (trustworthy IS NULL OR trustworthy=1)")}
    finally:
        c.close()
    sds = set()
    with open(LOW_CSV) as f:
        for r in csv.DictReader(f):
            sds.add((r["station"], r["target_date"]))
    return {k: truth[k] for k in sds if k in truth}, sds


def _score(ps, ys):
    n = len(ps)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum(ys) / n
    # 10-bin ECE
    bins = [0.0] * 10
    cnt = [0] * 10
    conf = [0.0] * 10
    for p, y in zip(ps, ys):
        b = min(9, int(p * 10))
        bins[b] += y
        conf[b] += p
        cnt[b] += 1
    ece = sum(abs(bins[i] / cnt[i] - conf[i] / cnt[i]) * cnt[i] / n
              for i in range(10) if cnt[i])
    dir_acc = sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / n
    return {"n": n, "brier": round(brier, 4), "base_rate": round(base, 4),
            "mean_p": round(sum(ps) / n, 4), "ece": round(ece, 4),
            "dir_acc": round(dir_acc, 4)}


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    n = len(xs)
    if n <= 1:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def sweep_pin(recs, truth_by_key, keys, pin_mod, offset=1.0, use_cooling=True):
    """Score the cooling signal at `pin_mod` over `keys` (a (station,date) set).

    ``use_cooling=False`` uses the running-low LEVEL only (mu = rlow, no
    projected drop) — the level-only arm that isolates whether the cooling
    term adds discrimination over simply reading the running low.
    """
    ps, ys = [], []
    for (st, ds) in keys:
        rec = recs.get((st, ds))
        tmin = truth_by_key.get((st, ds))
        if rec is None or tmin is None:
            continue
        nc = cooling_nowcast(rec.get("buckets", {}), pin_mod)
        if nc is None:
            continue
        T = target_strike(nc["rlow"], offset)
        y = 1 if tmin <= T else 0
        mu = nc["mu_low"] if use_cooling else nc["rlow"]
        ps.append(p_low_le(mu, SIGMA, T))
        ys.append(y)
    return _score(ps, ys) if ps else {"n": 0}


def level_metrics(recs, truth_by_key, keys, pin_mod):
    """Level accuracy of the running-low NOWCAST vs the settled daily min.

    This is the decisive LOW pin diagnostic: MAE(running_low@pin, settled_min)
    and the fraction of days whose running low at the pin already equals the
    final min ("low already set"). If MAE flattens and the "already set"
    fraction saturates by dawn, a later pin buys no predictive information.
    """
    ae, got, set_already, resid = [], 0, 0, []
    for (st, ds) in keys:
        rec = recs.get((st, ds))
        tmin = truth_by_key.get((st, ds))
        if rec is None or tmin is None:
            continue
        b = rec.get("buckets", {})
        vals = [b[x] for x in b if x <= pin_mod]
        if not vals:
            continue
        rlow = min(vals)
        got += 1
        ae.append(abs(rlow - tmin))
        resid.append(tmin - rlow)          # <=0 when the min drops further later
        if abs(rlow - tmin) < 1e-9:
            set_already += 1
    if not got:
        return {"n": 0}
    return {"n": got,
            "level_mae": round(sum(ae) / got, 4),
            "pct_low_already_set": round(set_already / got, 4),
            "mean_further_drop": round(sum(resid) / got, 4),
            "p90_further_drop": round(sorted(resid)[int(0.10 * got)], 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="sweep", choices=["sweep", "signal"])
    ap.add_argument("--station", default="KATL")
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    log("loading ASOS bucket recs ...")
    recs = load_recs()
    log("loading GHCN lows ...")
    ghcn = load_ghcn_lows()
    low_truth, low_sds = load_low_backfill_truth()

    if args.mode == "signal":
        ds = args.date or sorted(d for s, d in low_sds if s == args.station)[-1]
        rec = recs[(args.station, ds)]
        print(json.dumps({PIN_LABEL.get(p, p): cooling_nowcast(rec["buckets"], p)
                          for p in CONTEXT_PINS + SWEEP_PINS}, indent=2))
        return

    # --- cohorts -------------------------------------------------------------
    # (A) 178 LOW-backfill station-days with canonical LOW truth
    keys_A = sorted(k for k in low_sds if k in recs and k in low_truth)
    truth_A = low_truth
    # (B) broader ASOS∩GHCN-low panel, full-coverage days only
    keys_B = []
    for k in recs:
        if k in ghcn and recs[k].get("buckets"):
            b = recs[k]["buckets"]
            if b and min(b) == 0 and max(b) >= 1350:
                keys_B.append(k)
    keys_B = sorted(keys_B)
    truth_B = ghcn
    log(f"cohort A (LOW backfill) = {len(keys_A)}; cohort B (ASOS∩GHCN full) = {len(keys_B)}")

    result = {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "signal": "1h cooling-rate nowcast (mirror of rate_1h)",
        "axis": "T=floor(rlow@pin)-0.5; y = settled_min <= T; p = Phi((T-mu_low)/sigma)",
        "sigma": SIGMA,
        "note": ("rlow(p) is the station-local running low; the US daily min is set "
                 "near dawn in this cohort (median ~05:45 local), so an evening pin "
                 "is largely past the min — see the doc."),
        "cohorts": {},
    }

    for name, keys, truth in (("A_low_backfill_178", keys_A, truth_A),
                              ("B_asos_ghcn_full", keys_B, truth_B)):
        per_pin = {}
        for p in CONTEXT_PINS + SWEEP_PINS:
            per_pin[PIN_LABEL.get(p, str(p))] = {
                "pin_mod": p,
                "offset_1": sweep_pin(recs, truth, keys, p, offset=1.0),
                "offset_0": sweep_pin(recs, truth, keys, p, offset=0.5),
                "offset_2": sweep_pin(recs, truth, keys, p, offset=2.0),
                "level_only": sweep_pin(recs, truth, keys, p, offset=1.0, use_cooling=False),
                "level": level_metrics(recs, truth, keys, p),
            }
        result["cohorts"][name] = {"n_keys": len(keys), "per_pin": per_pin}
        log(f"  cohort {name}: scored {len(CONTEXT_PINS + SWEEP_PINS)} pins")

    # --- time-of-min diagnostic on cohort B ----------------------------------
    from collections import Counter
    mins = Counter()
    for k in keys_B:
        b = recs[k]["buckets"]
        mm = min(b, key=lambda x: b[x])
        mins[PIN_LABEL.get((mm // 60) * 60, f"{(mm//60):02d}:00")] += 1
    result["time_of_min_histogram_B"] = dict(sorted(mins.items()))
    late = sum(1 for k in keys_B
               if min((recs[k]["buckets"][x] for x in recs[k]["buckets"] if x <= 810),
                      default=9e9)
               > min((recs[k]["buckets"][x] for x in recs[k]["buckets"] if x <= 1200),
                     default=9e9) + 1e-9)
    result["station_days_min_below_after_1330_B"] = late

    # --- om_gfs Tmin arm (HIGH recipe generalized to the daily min) ----------
    log("om_gfs Tmin arm ...")
    om_t = fetch_omgfs_tmin(force=False)
    om_pairs = [(om_t[k] - truth_A[k]) for k in keys_A if k in om_t]
    om_bias = _mean(om_pairs) if om_pairs else 0.0
    om_sigma = _std(om_pairs) if len(om_pairs) > 1 else 3.27
    result["om_gfs_tmin"] = {
        "n": len(om_pairs), "bias": round(om_bias, 4), "sigma": round(om_sigma, 4),
        "level_mae": round(sum(abs(om_t[k] - (truth_A[k] - om_bias)) for k in keys_A if k in om_t)
                           / max(1, len(om_pairs)), 4),
        "level_mae_vs_raw": round(sum(abs(om_t[k] - truth_A[k]) for k in keys_A if k in om_t)
                                  / max(1, len(om_pairs)), 4),
        "per_pin_bc": {}, "per_pin_raw": {},
    }
    for p in CONTEXT_PINS + SWEEP_PINS:
        result["om_gfs_tmin"]["per_pin_bc"][PIN_LABEL.get(p, str(p))] = \
            om_tmin_arm(recs, truth_A, keys_A, om_t, bias=om_bias, sigma=om_sigma, pin_mod=p)
        result["om_gfs_tmin"]["per_pin_raw"][PIN_LABEL.get(p, str(p))] = \
            om_tmin_arm(recs, truth_A, keys_A, om_t, bias=0.0, sigma=om_sigma, pin_mod=p)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    log(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes)")

    # console summary
    for name in result["cohorts"]:
        print(f"\n=== cohort {name} (n_keys={result['cohorts'][name]['n_keys']}) ===")
        print(f"{'pin':>6} {'n':>5} {'brier':>7} {'BSS':>6} {'base':>6} {'LvlMAE':>7} {'%set':>6} {'dDrop':>6}")
        for lab, d in result["cohorts"][name]["per_pin"].items():
            s = d["offset_1"]
            lv = d["level"]
            if s.get("n"):
                b = s["base_rate"]; bss = 1 - s["brier"] / (b * (1 - b)) if 0 < b < 1 else float("nan")
                lo = d["level_only"]
                print(f"{lab:>6} {s['n']:5d} {s['brier']:7.4f} {bss:6.3f} {b:6.3f} "
                      f"{lv.get('level_mae',0):7.4f} {lv.get('pct_low_already_set',0):6.3f} "
                      f"{lv.get('mean_further_drop',0):6.3f}  lvlonly={lo.get('brier')}")
    print("\ntime_of_min_histogram_B:", json.dumps(result["time_of_min_histogram_B"]))
    ot = result["om_gfs_tmin"]
    print(f"\nom_gfs Tmin: n={ot['n']} bias={ot['bias']} sigma={ot['sigma']} "
          f"level_mae={ot['level_mae']} (raw {ot['level_mae_vs_raw']})")
    print(f"{'pin':>6} {'bc_brier':>9} {'bc_bss':>7} {'raw_brier':>10}")
    for lab in ot["per_pin_bc"]:
        s = ot["per_pin_bc"][lab]; sr = ot["per_pin_raw"][lab]
        if s.get("n"):
            b = s["base_rate"]; bss = 1 - s["brier"] / (b * (1 - b)) if 0 < b < 1 else float("nan")
            print(f"{lab:>6} {s['brier']:9.4f} {bss:7.3f} {sr['brier']:10.4f}")


if __name__ == "__main__":
    main()
