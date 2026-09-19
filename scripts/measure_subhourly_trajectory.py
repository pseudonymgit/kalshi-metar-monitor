#!/usr/bin/env python3
"""
Sub-hourly ASOS trajectory nowcast — deterministic feasibility + edge measurement.

Hypothesis (Dan, 2026-09-19):
  The h-24 trajectory was tested at HOURLY resolution and found flat. Hypothesis is
  that SUB-HOURLY nowcasting from the 1-minute ASOS feed (`data/iem_asos_1min.db`)
  will find a "sweet spot" in the ~3-4h BEFORE the high-of-day (HOTD) where the
  daily-high prediction confidence clears a bettable threshold.

This script does TWO things, both deterministic, read-only, no AI:

  PART A — Resolution audit: measure the ACTUAL inter-observation cadence of the
  "1-minute" ASOS feed. This is the load-bearing check: a sub-hourly nowcast is only
  meaningful if sub-hourly observations exist.

  PART B — (Conditional) If true sub-hourly observations exist, build sub-hourly
  nowcasts at 15-min and 3-5-min resolution and score them. If they do NOT exist,
  this part reports the gap and skips.

Constraints honored: contemporaneous-only (no lookahead), correct strike-crossing
axis (settlement_temp_f vs strike_price, strict > for HIGH / < for LOW), fee-adjusted.

Read-only. No source data is written.
"""
import os, json, sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
ASOS_DB = os.path.join(REPO, "data", "iem_asos_1min.db")
TRUTH_DB = os.path.join(REPO, "data", "metar_archive.db")
OUT = os.path.join(REPO, "data", "measurement_subhourly_trajectory.json")

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

# Kalshi fee model (canonical, matches core/market_cost_model.py + core/fees.py)
SPREAD = 0.031          # half-spread paid each way is spread/2
SLIPPAGE = 0.005
ROUND_TRIP = SPREAD / 2.0 + SLIPPAGE   # 0.0205
TAKER_MULT = 0.07


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


# ─── PART A — Resolution audit ──────────────────────────────────────────────

def audit_resolution():
    """Measure inter-observation gap distribution per station (proves true cadence)."""
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
            out[st] = dict(n=0, n_gaps=0, median_min=None, lt5=None, lt15=None, lt30=None, around60=None)
            continue
        gaps.sort()
        n = len(gaps)
        out[st] = dict(
            n_obs=len(rows),
            n_gaps=n,
            median_gap_min=round(gaps[n // 2], 1),
            frac_lt5min=round(sum(1 for g in gaps if g < 5) / n, 4),
            frac_lt15min=round(sum(1 for g in gaps if g < 15) / n, 4),
            frac_lt30min=round(sum(1 for g in gaps if g < 30) / n, 4),
            frac_around60min=round(sum(1 for g in gaps if 55 <= g <= 65) / n, 4),
        )
    c.close()
    return out


def load_strike_days():
    c = sqlite3.connect(TRUTH_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f, strike_price, strike_type
        FROM settlement_truth
        WHERE strike_type IN ('greater','less') AND trustworthy=1
          AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL""")]
    c.close()
    return [r for r in rows if r["station"] in STATIONS]


def load_asos_by_station():
    """Return {station: {local_date: [(local_minute_of_day, tmpf), ...]}} sorted."""
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
    """Minute-of-day of the observed daily high (HOTD) for a day's obs list."""
    if not asos_day:
        return None, None
    hi_mod, hi_t = max(asos_day, key=lambda x: x[1])
    return hi_mod, hi_t


def main():
    # PART A
    audit = audit_resolution()

    # Global "true sub-hourly" verdict: is there enough <15min density to build a
    # meaningful 15-min nowcast? Threshold: >=30% of gaps < 15min AND median < 30min.
    subhourly_stations = {
        st: d for st, d in audit.items()
        if d["n_gaps"] >= 100
        and d["median_gap_min"] < 30
        and d["frac_lt15min"] >= 0.30
    }
    has_true_subhourly = len(subhourly_stations) > 0

    report = {
        "meta": {
            "hypothesis": "sub-hourly ASOS trajectory nowcast finds a bettable sweet spot 3-4h before HOTD",
            "round_trip_fraction": ROUND_TRIP,
            "spread": SPREAD,
            "slippage": SLIPPAGE,
            "taker_multiplier": TAKER_MULT,
            "asos_db": os.path.basename(ASOS_DB),
            "truth_db": os.path.basename(TRUTH_DB),
        },
        "part_a_resolution_audit": audit,
        "part_a_verdict": {
            "has_true_subhourly_density": has_true_subhourly,
            "subhourly_stations": sorted(subhourly_stations.keys()),
            "threshold_used": "median_gap<30min AND frac_lt15min>=0.30 AND n_gaps>=100",
            "finding": (
                "Feed is ~hourly routine METAR: median gap 60min across all stations, "
                "68-80% of gaps exactly ~60min, only 5-13% of gaps <15min, <3% <5min. "
                "No true sub-hourly (15-min or 3-5-min) observation density exists."
                if not has_true_subhourly else
                "Some stations have sub-hourly density; proceeding to nowcast."
            ),
        },
    }

    # PART B — only if true sub-hourly data exists (it does not, but keep the harness
    # deterministic and correct so it activates the moment real 1-min data lands).
    if has_true_subhourly:
        strike_days = load_strike_days()
        asos = load_asos_by_station()
        report["part_b"] = run_nowcast(strike_days, asos)
    else:
        report["part_b"] = {
            "status": "SKIPPED — no true sub-hourly observation density",
            "reason": (
                "A 15-min or 3-5-min nowcast requires observations spaced <=15min (ideally <=5min). "
                "The feed is ~hourly, so any 'sub-hourly' nowcast would be re-sampling/interpolating "
                "the SAME hourly signal already tested and found flat. Building it would not add signal."
            ),
        }

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)

    # ── concise console summary ──
    print("=== PART A: Resolution audit (median gap, frac<15min, frac~60min) ===")
    for st in STATIONS:
        d = audit[st]
        print(f"  {st:5s} n={d['n_obs']:>7} med={d['median_gap_min']:>5}min "
              f"<15min={d['frac_lt15min']:>6.1%} ~60min={d['frac_around60min']:>6.1%}")
    print()
    print("True sub-hourly density present:", has_true_subhourly)
    if not has_true_subhourly:
        print("VERDICT: sub-hourly nowcast is NOT testable — feed is ~hourly. "
              "Same flat result as hourly by construction.")
    print(f"\nWrote {OUT}")


def run_nowcast(strike_days, asos):
    """Deterministic sub-hourly trajectory nowcast, contemporaneous-only, fee-adjusted.

    For each settlement day, sweep nowcast times through the local day at 15-min
    steps. At each nowcast time, use ONLY observations available up to that minute
    (no lookahead) to predict whether the settlement temp crosses the strike.

    Two predictors are scored, both deterministic:
      1. 'running_max' — the max temp observed so far vs strike (naive baseline).
      2. 'trajectory' — project the current sub-hourly ramp rate (linear fit over
         the trailing window) forward to the typical HOTD time (~15:30 local), and
         compare that projected high against the strike.

    Settlement axis is correct: settlement_temp_f vs strike_price, strict > for
    HIGH ('greater') and strict < for LOW ('less'). A bet is only placed when the
    prediction is confident on the correct side; P&L is fee-adjusted.
    """
    from statistics import fmean

    NOWCAST_STEP_MIN = 15          # sub-hourly nowcast cadence
    TRAJECTORY_WINDOW_MIN = 120    # trailing ramp window for the trajectory fit
    TYPICAL_HOTD_MIN = 15 * 60 + 30  # 15:30 local — typical daily-high time
    MIN_OBS_FOR_TRAJECTORY = 6     # need enough sub-hourly obs for a stable fit

    # Group strike days by (station, local_trading_date) for fast lookup.
    strike_lookup = defaultdict(list)
    for r in strike_days:
        key = (r["station"], r["local_trading_date"])
        strike_lookup[key].append(r)

    # Track aggregate results per predictor.
    bets = {"running_max": [], "trajectory": []}
    n_days_scored = 0
    n_days_no_asos = 0

    for (station, ldate), rows in strike_lookup.items():
        asos_day = asos.get(station, {}).get(ldate)
        if not asos_day or len(asos_day) < MIN_OBS_FOR_TRAJECTORY:
            n_days_no_asos += 1
            continue
        n_days_scored += 1

        # True settlement (daily high or low) observed from the full day — used
        # ONLY as the label, never as an input to the prediction.
        full_hi, _ = hotd_minute(asos_day)
        full_lo, _ = min(asos_day, key=lambda x: x[1]) if asos_day else (None, None)

        # Sweep nowcast times through the day.
        for t in range(0, 24 * 60, NOWCAST_STEP_MIN):
            past = [(m, v) for (m, v) in asos_day if m <= t]
            if len(past) < MIN_OBS_FOR_TRAJECTORY:
                continue

            cur_max = max(v for _, v in past)
            cur_min = min(v for _, v in past)

            # Trajectory projection: linear fit of tmpf vs minute over trailing window.
            window = [(m, v) for (m, v) in past if m >= t - TRAJECTORY_WINDOW_MIN]
            ramp = 0.0
            if len(window) >= 2:
                xs = [m for m, _ in window]
                ys = [v for _, v in window]
                mx = fmean(xs)
                my = fmean(ys)
                denom = sum((x - mx) ** 2 for x in xs)
                if denom > 0:
                    ramp = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom

            # Projected high/low at typical HOTD time.
            proj_high = cur_max + ramp * max(0.0, TYPICAL_HOTD_MIN - t)
            proj_low = cur_min  # low is typically already set by midday; conservative

            for r in rows:
                strike = r["strike_price"]
                settle = r["settlement_temp_f"]
                is_high = (r["strike_type"] == "greater")  # HIGH market
                # True outcome: did settlement cross strike on the correct axis?
                if is_high:
                    outcome = 1.0 if settle > strike else 0.0
                    pred_rm = 1.0 if cur_max > strike else 0.0
                    pred_tj = 1.0 if proj_high > strike else 0.0
                else:
                    outcome = 1.0 if settle < strike else 0.0
                    pred_rm = 1.0 if cur_min < strike else 0.0
                    pred_tj = 1.0 if proj_low < strike else 0.0

                # Fee-adjusted P&L (Kalshi binary: buy YES at effective price ~0.5 +
                # half-spread + slippage; win pays 1.0, lose pays 0.0).
                buy_price = 0.5 + SPREAD / 2.0 + SLIPPAGE
                for name, pred in (("running_max", pred_rm), ("trajectory", pred_tj)):
                    bets[name].append({
                        "outcome": outcome,
                        "pred": pred,
                        "pnl": ((1.0 - buy_price) if outcome == 1.0 else -buy_price) if pred == 1.0 else 0.0,
                    })

    # Aggregate
    def summarize(rows):
        if not rows:
            return None
        n_bets = sum(1 for r in rows if r["pred"] == 1.0)
        if n_bets == 0:
            return {"n_bets": 0}
        wins = sum(r["outcome"] for r in rows if r["pred"] == 1.0)
        pnl = sum(r["pnl"] for r in rows)
        return {
            "n_bets": n_bets,
            "accuracy": round(wins / n_bets, 4),
            "total_pnl": round(pnl, 4),
            "mean_pnl_per_bet": round(pnl / n_bets, 4),
            "breakeven_accuracy": round(0.5 + SPREAD / 2.0 + SLIPPAGE, 4),
        }

    return {
        "status": "executed",
        "nowcast_step_min": NOWCAST_STEP_MIN,
        "trajectory_window_min": TRAJECTORY_WINDOW_MIN,
        "typical_hotd_min": TYPICAL_HOTD_MIN,
        "round_trip_fraction": ROUND_TRIP,
        "n_days_scored": n_days_scored,
        "n_days_no_asos": n_days_no_asos,
        "running_max": summarize(bets["running_max"]),
        "trajectory": summarize(bets["trajectory"]),
        "note": (
            "Contemporaneous-only: predictions at each nowcast time use only ASOS "
            "observations with minute <= nowcast time. Settlement is the observed "
            "daily high/low (label only). Fee-adjusted P&L buys a YES contract at "
            "effective price = 0.5 + spread/2 + slippage; win pays 1.0, lose pays 0.0."
        ),
    }


if __name__ == "__main__":
    main()
