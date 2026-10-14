#!/usr/bin/env python3
"""LOW pin sweep — MARKET TIMING (spread/depth) vs WEATHER SKILL (Brier).

Dan (2026-10-14): "Now that LOW is built, do another pin sweep. Unless the pin
is based strictly on the market and weather has no actual bearing."

The prior LOW pin sweep (`scripts/low_trajectory_signal.py sweep`) scored pins
by om_gfs Tmin Brier ALONE and found a flat 06:00-21:00 skill plateau -> keep
13:30. Dan questions that: if the pin should be chosen by MARKET conditions
(spread, depth, liquidity), the weather-skill plateau is the wrong objective.

This sweep scores candidate LOW pins 18:00 (day D) .. 06:00 (day D+1), hourly,
on THREE axes:

  1. MARKET SPREAD  — median yes_ask-yes_bid for LOW tickers (KXLOWT*) at that
     station-local hour, from data/kalshi_market_snapshots.db (READ-ONLY).
  2. WEATHER SKILL  — om_gfs Tmin Brier at that pin: P(daily_min <= T) from a
     bias-corrected Tmin Gaussian + running-low truncation, T=floor(rlow)-0.5,
     y = settled_min <= T (the honest LOW axis, mirror of HIGH).
  3. PIN SCORE      — weighted combo, spread weighted MORE (Dan's thesis: the
     market timing matters more than weather skill).

READ-ONLY on every DB. Writes ONLY the plan doc + its JSON artifact.
No book writes. No network (reuses the /tmp caches the prior sweep produced).

usage: python3 scripts/low_pin_sweep_market_timing_2026-10-14.py
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
METAR_DB = REPO / "data" / "metar_backfill.db"
LOW_CSV = REPO / "artifacts" / "low-market-backfill" / "low_entry_price_series_2026-10-13.csv"
ASOS_RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_LOW_CACHE = Path("/tmp/we_low_omgfs_tmin_2026-10-14.json")

# Pin range 18:00 day D .. 06:00 day D+1, hourly. 18:00 day D = mod 1080; the
# overnight clock wraps at midnight (mod 0) and runs to 06:00 (mod 360).
PINS = [810, 1080, 1140, 1200, 1260, 1320, 1380, 0, 60, 120, 180, 240, 300, 360]
PIN_LABEL = {
    810: "13:30*", 1080: "18:00", 1140: "19:00", 1200: "20:00", 1260: "21:00",
    1320: "22:00", 1380: "23:00", 0: "00:00", 60: "01:00", 120: "02:00",
    180: "03:00", 240: "04:00", 300: "05:00", 360: "06:00",
}

SIGMA_TMIN = 3.2691  # om_gfs Tmin sigma of the bias (°F), prior LOW sweep


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# ── 1. market timing: LOW spread by station-local hour ───────────────────────
def low_spread_by_local_hour() -> Dict[int, Dict]:
    """Median/mean yes_ask-yes_bid for LOW tickers by STATION-LOCAL hour.

    Only rows with a genuine two-sided quote (0 < bid < ask < 1) count; the
    bulk of KXLOWT* snapshot rows are synthetic 0/1 books (mid 0.5) and would
    poison the spread. Depth = mean open_interest of the quoted rows.
    """
    c = ro(SNAPS)
    try:
        rows = c.execute(
            "SELECT snapshot_time, station, market_ticker, yes_bid, yes_ask, "
            "open_interest, volume_24h FROM market_snapshots "
            "WHERE market_ticker LIKE 'KXLOWT%' AND market_type='binary' "
            "AND yes_bid > 0 AND yes_ask < 1 AND yes_ask > yes_bid").fetchall()
    finally:
        c.close()
    by_hour: Dict[int, List] = defaultdict(lambda: {"spread": [], "oi": [], "vol": []})
    for snap, st, tk, bid, ask, oi, vol in rows:
        try:
            dt = datetime.fromisoformat(snap)
        except (TypeError, ValueError):
            continue
        if ZoneInfo is not None:
            try:
                dt = dt.astimezone(ZoneInfo(station_timezone_name(st)))
            except Exception:
                pass
        h = dt.hour
        by_hour[h]["spread"].append(float(ask) - float(bid))
        if oi is not None:
            by_hour[h]["oi"].append(float(oi))
        if vol is not None:
            by_hour[h]["vol"].append(float(vol))
    out: Dict[int, Dict] = {}
    for h, d in by_hour.items():
        sp = sorted(d["spread"])
        n = len(sp)
        out[h] = {
            "n": n,
            "median_spread": round(sp[n // 2] if n % 2 else
                                   (sp[n // 2 - 1] + sp[n // 2]) / 2, 4) if n else None,
            "mean_spread": round(sum(sp) / n, 4) if n else None,
            "mean_oi": round(sum(d["oi"]) / len(d["oi"]), 1) if d["oi"] else None,
            "mean_vol": round(sum(d["vol"]) / len(d["vol"]), 1) if d["vol"] else None,
        }
    return out


# ── 2. weather skill: om_gfs Tmin Brier at a pin (honest LOW axis) ───────────
def _p_low_le(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu <= x else 0.0
    return 0.5 * (1.0 + math.erf(((x - mu) / sigma) / math.sqrt(2.0)))


def _score(ps, ys):
    n = len(ps)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum(ys) / n
    dir_acc = sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / n
    # Brier skill score vs the base-rate climatology (higher = more skilful)
    brier_base = sum((base - y) ** 2 for y in ys) / n
    bss = 1.0 - brier / brier_base if brier_base > 0 else 0.0
    return {"n": n, "brier": round(brier, 4), "bss": round(bss, 4),
            "base_rate": round(base, 4), "dir_acc": round(dir_acc, 4)}


def load_recs():
    with gzip.open(ASOS_RECS, "rb") as f:
        return pickle.load(f)


def om_tmin_cache() -> Dict[Tuple[str, str], float]:
    flat = json.loads(OM_LOW_CACHE.read_text())
    return {(k.split("|")[0], k.split("|")[1]): v for k, v in flat.items()}


def low_backfill_truth() -> Dict[Tuple[str, str], float]:
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
            if r.get("has_low_truth") == "1":
                sds.add((r["station"], r["target_date"]))
    return {k: truth[k] for k in sds if k in truth}


def om_tmin_brier_at_pin(recs, truth, keys, om_t, pin_mod, bias):
    """om_gfs Tmin P(min<=T) Brier at `pin_mod` on the honest LOW axis."""
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
        T = math.floor(min(vals)) - 0.5           # running-low truncation, LOW axis
        ps.append(_p_low_le(mu - bias, SIGMA_TMIN, T))
        ys.append(1 if tmin <= T else 0)
    return _score(ps, ys) if ps else {"n": 0}


def om_tmin_bias(recs, truth, keys, om_t):
    """om_gfs Tmin bias vs settled low over the panel (mean(tmin_for - settled))."""
    resid = []
    for (st, ds) in keys:
        tmin = truth.get((st, ds))
        mu = om_t.get((st, ds))
        if tmin is None or mu is None:
            continue
        resid.append(mu - tmin)
    return sum(resid) / len(resid) if resid else 0.0


def main():
    recs = load_recs()
    truth = low_backfill_truth()
    om_t = om_tmin_cache()
    keys = sorted(truth.keys())

    spread = low_spread_by_local_hour()
    bias = om_tmin_bias(recs, truth, keys, om_t)

    # spread score = smaller median spread is better; normalise across the pins
    # present so the weighting is on a comparable 0-1 scale. The spread axis is
    # keyed by STATION-LOCAL HOUR (0-23); map each pin_mod to its hour.
    pin_spreads = {p: spread.get((p // 60) % 24, {}) for p in PINS}
    med = [v["median_spread"] for v in pin_spreads.values() if v.get("median_spread") is not None]
    smin, smax = (min(med), max(med)) if med else (0.0, 1.0)

    W_SPREAD, W_SKILL = 0.7, 0.3        # Dan's thesis: market timing weighted more

    rows = []
    for p in PINS:
        tr = om_tmin_brier_at_pin(recs, truth, keys, om_t, p, bias)
        sp = pin_spreads.get(p, {})
        ms = sp.get("median_spread")
        spread_score = (1.0 - (ms - smin) / (smax - smin)) if (ms is not None and smax > smin) else 0.0
        # skill score: BSS clamped at >=0 (a negative-BSS pin carries no skill)
        skill_score = max(0.0, tr.get("bss", 0.0))
        pin_score = W_SPREAD * spread_score + W_SKILL * skill_score
        rows.append({
            "pin_local": PIN_LABEL[p], "pin_mod": p,
            "median_spread": ms, "mean_spread": sp.get("mean_spread"),
            "spread_n": sp.get("n"), "mean_oi": sp.get("mean_oi"),
            "mean_vol": sp.get("mean_vol"),
            "tmin_n": tr.get("n"), "tmin_brier": tr.get("brier"),
            "tmin_bss": tr.get("bss"), "tmin_dir_acc": tr.get("dir_acc"),
            "spread_score": round(spread_score, 4),
            "skill_score": round(skill_score, 4),
            "pin_score": round(pin_score, 4),
        })

    best = max(rows, key=lambda r: r["pin_score"])
    best_spread = min((r for r in rows if r["median_spread"] is not None),
                      key=lambda r: r["median_spread"])
    best_skill = max((r for r in rows if r["tmin_bss"] is not None),
                     key=lambda r: r["tmin_bss"])

    # per-station spread at each pin (station-local) for the recommendation table
    per_station = {}
    c = ro(SNAPS)
    try:
        srows = c.execute(
            "SELECT snapshot_time, station, yes_bid, yes_ask FROM market_snapshots "
            "WHERE market_ticker LIKE 'KXLOWT%' AND market_type='binary' "
            "AND yes_bid > 0 AND yes_ask < 1 AND yes_ask > yes_bid").fetchall()
    finally:
        c.close()
    acc: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    for snap, st, bid, ask in srows:
        try:
            dt = datetime.fromisoformat(snap)
        except (TypeError, ValueError):
            continue
        if ZoneInfo is not None:
            try:
                dt = dt.astimezone(ZoneInfo(station_timezone_name(st)))
            except Exception:
                pass
        acc[(st, dt.hour)].append(float(ask) - float(bid))
    for (st, h), vals in acc.items():
        per_station.setdefault(st, {})[h] = round(sorted(vals)[len(vals) // 2], 4)

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_pins_local": [PIN_LABEL[p] for p in PINS],
        "weights": {"spread": W_SPREAD, "skill": W_SKILL},
        "spread_source": "kalshi_market_snapshots.db market_snapshots KXLOWT* "
                         "market_type='binary', 0<bid<ask<1, station-local hour",
        "skill_source": "om_gfs Tmin (Open-Meteo gfs_seamless) bias-corrected; "
                        "T=floor(rlow@pin)-0.5; y=settled_min<=T; sigma=3.2691",
        "om_tmin_bias_f": round(bias, 4),
        "panel_n": len(keys),
        "rows": rows,
        "best_overall": best,
        "best_spread": best_spread,
        "best_skill": best_skill,
        "per_station_median_spread_by_hour": per_station,
    }
    out = REPO / "artifacts" / "low-market-lane" / "low_pin_sweep_market_timing_2026-10-14.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
