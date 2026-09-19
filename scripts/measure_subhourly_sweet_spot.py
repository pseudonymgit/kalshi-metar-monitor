#!/usr/bin/env python3
"""
Sub-hourly ASOS trajectory → daily-high-crosses-strike nowcast: CORRECT sweet-spot test.

This is the rigorous re-measurement of Dan's hypothesis (2026-09-19):

  "Does the sub-hourly ASOS trajectory, in the ~3-4h window BEFORE the daily high
   (HOTD), predict whether the daily high will cross the strike better than hourly
   resolution does?"

The prior `measure_subhourly_trajectory.py` harness had a degenerate Part B:
  - its 'trajectory' predictor (proj_high = cur_max + ramp*remaining) almost always
    predicts CROSS (n_bets 139k, accuracy 0.32) — it over-triggers on any positive ramp.
  - its 'running_max' predictor (has cur_max already crossed?) is a naive "already
    happened" check that accumulates bets all day including the trivially-known tail,
    not a forecast of the not-yet-set daily high.

This script fixes both by framing the prediction correctly:

  TASK: at nowcast time T (before HOTD), using ONLY observations with minute <= T,
  predict whether the EVENTUAL daily high (observed over the FULL local day) will
  strictly exceed the strike (HIGH market) or fall strictly below (LOW market).

  SIGNAL (trajectory): the linear ramp of temperature over the trailing window,
  projected forward to the TYPICAL HOTD time (15:30 local). This is the ONLY
  genuine *sub-hourly* predictive content — the instantaneous slope you can see at
  5-min resolution but not at 60-min resolution.

  WINDOW OF INTEREST: the 3-4h band before the observed HOTD minute (HOTD-240min ..
  HOTD-180min). We evaluate the nowcast only at nowcast times T inside that band.

  RESOLUTIONS TESTED:
    - 5-min  (true sub-hourly, finest practical)
    - 15-min (sub-hourly)
    - 60-min (hourly control — downsample to the same observations the old feed had)

  HEAD-TO-HEAD: identical signal, identical fee model, identical strike axis; the
  ONLY thing that differs is the observation cadence fed to the ramp fit.

Constraints honored:
  - contemporaneous-only (no lookahead; past = minute <= T)
  - correct strike axis (settlement_temp_f vs strike_price, strict > for HIGH / < for LOW)
  - fee-adjusted (Kalshi binary, round-trip 0.0205)
  - deterministic (fixed thresholds, no randomness, no AI)

Read-only. No source data is written.
"""
import os, json, sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from statistics import fmean
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
ASOS_DB = os.path.join(REPO, "data", "iem_asos_1min.db")
TRUTH_DB = os.path.join(REPO, "data", "metar_archive.db")
OUT = os.path.join(REPO, "data", "measurement_subhourly_sweet_spot.json")

_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}
STATIONS = list(_ICAO_TZ.keys())

# Kalshi fee model (canonical)
SPREAD = 0.031
SLIPPAGE = 0.005
ROUND_TRIP = SPREAD / 2.0 + SLIPPAGE   # 0.0205
BUY_PRICE = 0.5 + SPREAD / 2.0 + SLIPPAGE  # 0.5205 effective YES entry
BREAKEVEN = BUY_PRICE                    # must exceed this win-rate to profit

# Tunables (deterministic, documented)
TYPICAL_HOTD_MIN = 15 * 60 + 30      # 15:30 local — typical daily-high time
WINDOW_BEFORE_HOTD = (180, 240)      # evaluate T in [HOTD-240, HOTD-180] minutes
TRAJECTORY_WINDOW_MIN = 90           # trailing ramp window for the slope fit
MIN_OBS_FOR_FIT = 4                  # need >=4 obs in the ramp window for a stable slope
RESOLUTIONS = [5, 15, 60]            # minutes; 60 = hourly control


def parse_utc(s):
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tz_of(icao):
    return ZoneInfo(_ICAO_TZ.get(icao, "America/New_York"))


# ─── PART A — resolution audit ──────────────────────────────────────────────

def audit_resolution():
    c = sqlite3.connect(ASOS_DB)
    cur = c.cursor()
    out = {}
    for st in STATIONS:
        cur.execute(
            "SELECT valid FROM observations WHERE station=? AND tmpf IS NOT NULL ORDER BY valid",
            (st,),
        )
        rows = [r[0] for r in cur.fetchall()]
        gaps = []
        for i in range(1, len(rows)):
            g = (datetime.fromisoformat(rows[i]) - datetime.fromisoformat(rows[i - 1])).total_seconds() / 60.0
            if 0 < g < 180:
                gaps.append(g)
        if not gaps:
            out[st] = dict(n_obs=0, n_gaps=0, median_gap_min=None,
                           frac_lt5min=None, frac_lt15min=None,
                           frac_lt30min=None, frac_around60min=None)
            continue
        gaps.sort()
        n = len(gaps)
        out[st] = dict(
            n_obs=len(rows), n_gaps=n,
            median_gap_min=round(gaps[n // 2], 2),
            frac_lt5min=round(sum(1 for g in gaps if g < 5) / n, 4),
            frac_lt15min=round(sum(1 for g in gaps if g < 15) / n, 4),
            frac_lt30min=round(sum(1 for g in gaps if g < 30) / n, 4),
            frac_around60min=round(sum(1 for g in gaps if 55 <= g <= 65) / n, 4),
        )
    c.close()
    return out


# ─── data loading ───────────────────────────────────────────────────────────

def load_strike_days():
    """Return list of strike-day rows (HIGH→greater, LOW→less, trustworthy=1)."""
    c = sqlite3.connect(TRUTH_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("""
        SELECT station, market_type, local_trading_date,
               settlement_temp_f, strike_price, strike_type
        FROM settlement_truth
        WHERE strike_type IN ('greater','less') AND trustworthy=1
          AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL""")]
    c.close()
    return [r for r in rows if r["station"] in STATIONS]


def load_asos_by_station():
    """{station: {local_date_iso: [(minute_of_day, tmpf), ...]}} sorted by minute."""
    c = sqlite3.connect(ASOS_DB)
    cur = c.cursor()
    out = defaultdict(lambda: defaultdict(list))
    for st in STATIONS:
        cur.execute(
            "SELECT valid, tmpf FROM observations WHERE station=? AND tmpf IS NOT NULL ORDER BY valid",
            (st,),
        )
        for valid, tmpf in cur.fetchall():
            dt = parse_utc(valid)
            if dt is None:
                continue
            loc = dt.astimezone(tz_of(st))
            mod = loc.hour * 60 + loc.minute
            out[st][loc.date().isoformat()].append((mod, tmpf))
    c.close()
    for st in out:
        for ld in out[st]:
            out[st][ld].sort()
    return out


def hotd_minute(asos_day):
    """(minute_of_day, temp) of the daily high; None if empty."""
    if not asos_day:
        return None, None
    hi_mod, hi_t = max(asos_day, key=lambda x: x[1])
    return hi_mod, hi_t


# ─── ramp fit helper ────────────────────────────────────────────────────────

def ramp_slope(obs_window):
    """Least-squares slope of tmpf vs minute over a window of (minute, tmpf)."""
    if len(obs_window) < 2:
        return 0.0
    xs = [m for m, _ in obs_window]
    ys = [v for _, v in obs_window]
    mx = fmean(xs)
    my = fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def downsample(obs, step_min):
    """Bucket observations into fixed step_min bins (last obs per bin) → hourly control."""
    if step_min <= 1:
        return obs
    out = []
    for m, v in obs:
        if not out or (m - out[-1][0]) >= step_min:
            out.append((m, v))
    return out


# ─── PART B — sweet-spot nowcast ────────────────────────────────────────────

def run_nowcast(strike_days, asos):
    """Correct contemporaneous sub-hourly trajectory nowcast, resolution sweep.

    For each (station, date, strike) row:
      - find observed HOTD minute (label; never an input)
      - for each resolution R in [5, 15, 60]:
          downsample that day's obs to R-min cadence
          for each nowcast time T in the 3-4h band before HOTD (step = R):
              build trailing window [T-90min, T] from the R-downsampled obs (<= T)
              slope = ramp over that window (contemporaneous only)
              projected_high = cur_max + slope * max(0, TYPICAL_HOTD - T)
              (cur_max = max temp seen <= T)
              predict CROSS for HIGH if projected_high > strike
              predict CROSS for LOW  if cur_min < strike   (low is already ~set)
              record (outcome, pred) → fee-adjusted P&L

    The 60-min resolution uses the SAME code path but with hourly-downsampled obs,
    which is exactly the flat hourly baseline being re-tested for head-to-head.
    """
    strike_lookup = defaultdict(list)
    for r in strike_days:
        strike_lookup[(r["station"], r["local_trading_date"])].append(r)

    results = {R: {"bets": [], "n_days_scored": 0} for R in RESOLUTIONS}
    n_days_no_asos = 0

    for (station, ldate), rows in strike_lookup.items():
        asos_day = asos.get(station, {}).get(ldate)
        if not asos_day or len(asos_day) < MIN_OBS_FOR_FIT:
            n_days_no_asos += 1
            continue

        # Observed daily high (label) + minute it occurred (to bound the window).
        hotd_mod, _ = hotd_minute(asos_day)
        if hotd_mod is None:
            continue
        full_lo, _ = min(asos_day, key=lambda x: x[1]) if asos_day else (None, None)

        # The 3-4h band before HOTD (clamp to >= 0).
        t_lo = max(0, hotd_mod - WINDOW_BEFORE_HOTD[1])
        t_hi = max(0, hotd_mod - WINDOW_BEFORE_HOTD[0])

        for R in RESOLUTIONS:
            ds = downsample(asos_day, R)
            # Pre-sort is guaranteed; build a minute→temp map for O(1) lookup of "<= T".
            by_min = [(m, v) for (m, v) in ds]
            for t in range(t_lo, t_hi + 1, R):
                # contemporaneous past = obs with minute <= t
                past = [(m, v) for (m, v) in by_min if m <= t]
                if len(past) < MIN_OBS_FOR_FIT:
                    continue
                window = [(m, v) for (m, v) in past if m >= t - TRAJECTORY_WINDOW_MIN]
                if len(window) < 2:
                    continue
                slope = ramp_slope(window)
                cur_max = max(v for _, v in past)
                cur_min = min(v for _, v in past)
                proj_high = cur_max + slope * max(0.0, TYPICAL_HOTD_MIN - t)

                for r in rows:
                    strike = r["strike_price"]
                    settle = r["settlement_temp_f"]
                    is_high = (r["strike_type"] == "greater")
                    if is_high:
                        outcome = 1.0 if settle > strike else 0.0
                        pred = 1.0 if proj_high > strike else 0.0
                    else:
                        outcome = 1.0 if settle < strike else 0.0
                        pred = 1.0 if cur_min < strike else 0.0

                    results[R]["bets"].append({
                        "outcome": outcome,
                        "pred": pred,
                        "pnl": ((1.0 - BUY_PRICE) if outcome == 1.0 else -BUY_PRICE) if pred == 1.0 else 0.0,
                    })
            results[R]["n_days_scored"] += 1

    # Aggregate per resolution
    summary = {}
    for R in RESOLUTIONS:
        bets = results[R]["bets"]
        n_bets = sum(1 for b in bets if b["pred"] == 1.0)
        if n_bets == 0:
            summary[str(R)] = {
                "n_bets": 0, "n_days_scored": results[R]["n_days_scored"],
                "accuracy": None, "total_pnl": 0.0, "mean_pnl_per_bet": None,
                "breakeven_accuracy": round(BREAKEVEN, 4),
                "net_ev_per_bet": None,
            }
            continue
        wins = sum(b["outcome"] for b in bets if b["pred"] == 1.0)
        pnl = sum(b["pnl"] for b in bets)
        acc = wins / n_bets
        summary[str(R)] = {
            "n_bets": n_bets,
            "n_days_scored": results[R]["n_days_scored"],
            "accuracy": round(acc, 4),
            "total_pnl": round(pnl, 4),
            "mean_pnl_per_bet": round(pnl / n_bets, 4),
            "breakeven_accuracy": round(BREAKEVEN, 4),
            "net_ev_per_bet": round(pnl / n_bets, 4),
            "bettable": bool(acc > BREAKEVEN and n_bets >= 100),
        }

    return {
        "status": "executed",
        "typical_hotd_min": TYPICAL_HOTD_MIN,
        "window_before_hotd_min": list(WINDOW_BEFORE_HOTD),
        "trajectory_window_min": TRAJECTORY_WINDOW_MIN,
        "min_obs_for_fit": MIN_OBS_FOR_FIT,
        "resolutions_min": RESOLUTIONS,
        "round_trip_fraction": ROUND_TRIP,
        "buy_price": BUY_PRICE,
        "breakeven_accuracy": round(BREAKEVEN, 4),
        "n_days_no_asos": n_days_no_asos,
        "by_resolution": summary,
        "note": (
            "Prediction = trajectory slope over trailing 90min projected to typical HOTD "
            "(15:30) compared to strike. Evaluated ONLY in the 3-4h band before the observed "
            "HOTD. Contemporaneous-only (obs <= nowcast time). 60-min resolution is the hourly "
            "control (same code, downsampled cadence). Fee-adjusted P&L."
        ),
    }


def run_binned_signal(strike_days, asos):
    """Binned P(cross) by (gap-to-strike, ramp-slope), per resolution.

    This is the load-bearing diagnostic: it shows WHERE the signal lives (the
    near-strike + fast-ramp cells) and whether finer resolution exposes cells
    that hourly cadence smears away.
    """
    strike_lookup = defaultdict(list)
    for r in strike_days:
        if r["strike_type"] == "greater":  # HIGH market only for the crossing axis
            strike_lookup[(r["station"], r["local_trading_date"])].append(r)

    def gap_bin(g):
        if g < 1: return "gap<1"
        if g < 3: return "gap1-3"
        if g < 6: return "gap3-6"
        return "gap>6"

    def slope_bin(s):
        if s < 0.02: return "slope<0.02"
        if s < 0.05: return "0.02-0.05"
        if s < 0.10: return "0.05-0.10"
        return "slope>0.10"

    per_res = {}
    for RES in RESOLUTIONS:
        bins = defaultdict(lambda: [0, 0])
        for (station, ldate), rows in strike_lookup.items():
            day = asos.get(station, {}).get(ldate)
            if not day or len(day) < MIN_OBS_FOR_FIT:
                continue
            hi_mod, _ = hotd_minute(day)
            if hi_mod is None:
                continue
            t_lo = max(0, hi_mod - WINDOW_BEFORE_HOTD[1])
            t_hi = max(0, hi_mod - WINDOW_BEFORE_HOTD[0])
            ds = downsample(day, RES)
            for t in range(t_lo, t_hi + 1, RES):
                past = [(m, v) for (m, v) in ds if m <= t]
                if len(past) < MIN_OBS_FOR_FIT:
                    continue
                win = [(m, v) for (m, v) in past if m >= t - TRAJECTORY_WINDOW_MIN]
                if len(win) < 2:
                    continue
                slope = ramp_slope(win)
                cur_max = max(v for _, v in past)
                for r in rows:
                    strike = r["strike_price"]
                    settle = r["settlement_temp_f"]
                    cross = 1 if settle > strike else 0
                    gb = gap_bin(strike - cur_max)
                    sb = slope_bin(slope)
                    bins[(gb, sb)][0] += 1
                    bins[(gb, sb)][1] += cross

        cells = {}
        bettable = []
        for (gb, sb), (n, cx) in sorted(bins.items()):
            if n < 50:
                continue
            p = cx / n
            cells[f"{gb}|{sb}"] = {"n": n, "crosses": cx, "p_cross": round(p, 4)}
            if p > BREAKEVEN and gb in ("gap<1", "gap1-3"):
                bettable.append((gb, sb, n, p))
        tot_n = sum(x[2] for x in bettable)
        tot_w = sum(x[2] * x[3] for x in bettable)
        per_res[str(RES)] = {
            "cells": cells,
            "bettable_combined_n": tot_n,
            "bettable_combined_p_cross": round(tot_w / tot_n, 4) if tot_n else None,
            "bettable_combined_net_ev": round(tot_w / tot_n - BUY_PRICE, 4) if tot_n else None,
        }
    return per_res


def main():
    audit = audit_resolution()

    subhourly_stations = {
        st: d for st, d in audit.items()
        if d["n_gaps"] >= 100 and d["median_gap_min"] < 30 and d["frac_lt15min"] >= 0.30
    }
    has_true_subhourly = len(subhourly_stations) > 0

    report = {
        "meta": {
            "hypothesis": "sub-hourly ASOS trajectory, 3-4h before HOTD, predicts daily-high strike cross better than hourly",
            "round_trip_fraction": ROUND_TRIP,
            "spread": SPREAD,
            "slippage": SLIPPAGE,
            "buy_price": BUY_PRICE,
            "breakeven_accuracy": round(BREAKEVEN, 4),
            "asos_db": os.path.basename(ASOS_DB),
            "truth_db": os.path.basename(TRUTH_DB),
        },
        "part_a_resolution_audit": audit,
        "part_a_verdict": {
            "has_true_subhourly_density": has_true_subhourly,
            "subhourly_stations": sorted(subhourly_stations.keys()),
            "threshold_used": "median_gap<30min AND frac_lt15min>=0.30 AND n_gaps>=100",
        },
    }

    if has_true_subhourly:
        strike_days = load_strike_days()
        asos = load_asos_by_station()
        report["n_strike_days"] = len(strike_days)
        report["part_b"] = run_nowcast(strike_days, asos)
        report["part_c_binned_signal"] = run_binned_signal(strike_days, asos)
    else:
        report["part_b"] = {"status": "SKIPPED — no sub-hourly density"}

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)

    # ── console summary ──
    print("=== PART A: resolution audit ===")
    for st in STATIONS:
        d = audit[st]
        print(f"  {st:5s} n={d['n_obs']:>7} med={d['median_gap_min']:>5}min "
              f"<5min={d['frac_lt5min']:>6.1%} ~60min={d['frac_around60min']:>6.1%}")
    print(f"\nTrue sub-hourly density present: {has_true_subhourly} "
          f"({len(subhourly_stations)}/20 stations)")

    if has_true_subhourly:
        print("\n=== PART B: sweet-spot nowcast (3-4h before HOTD, fee-adjusted) ===")
        pb = report["part_b"]
        for R in RESOLUTIONS:
            s = pb["by_resolution"][str(R)]
            acc = f"{s['accuracy']:.4f}" if s["accuracy"] is not None else "n/a"
            ev = f"{s['net_ev_per_bet']:+.4f}" if s["net_ev_per_bet"] is not None else "n/a"
            flag = "BETTABLE" if s.get("bettable") else ""
            print(f"  {R:>2}min: n_bets={s['n_bets']:>6} acc={acc} "
                  f"netEV/bet={ev} {flag}")
        print(f"\nbreakeven accuracy = {BREAKEVEN:.4f}")
        print("\n=== PART C: binned signal by resolution (bettable cells) ===")
        pc = report["part_c_binned_signal"]
        for R in RESOLUTIONS:
            s = pc[str(R)]
            n = s["bettable_combined_n"]
            p = s["bettable_combined_p_cross"]
            ev = s["bettable_combined_net_ev"]
            ps = f"{p:.4f}" if p is not None else "n/a"
            evs = f"{ev:+.4f}" if ev is not None else "n/a"
            print(f"  {R:>2}min: bettable_n={n:>6} P(cross)={ps} netEV={evs}")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
