#!/usr/bin/env python3
"""
Trajectory / magnitude PREDICTION — Kalman nowcast of settlement_temp_f.

This is the counterpart to `measure_intraday_timing_edge_v2.py`, which tested
DIRECTION (does the high cross the strike?). This module tests MAGNITUDE: can we
predict the *final settlement temperature value* (the daily high for HIGH markets,
daily low for LOW markets) as a function of hour H, and does fusing contemporaneous
METAR observations via a Kalman filter beat the static h-24 NWP forecast?

Model
-----
State: x = true settlement_temp_f (a scalar, constant over the day — it *is* the
realized daily max/min, revealed progressively, not physically changing).

  prior      : x ~ N(mu_nwp, sigma_prior^2)        # NWP h-24 forecast + its error var
  measurement: z_H = running observed extremum      # max(HIGH) / min(LOW) up to hour H
               z_H ~ N(x, R_H)                      # R_H shrinks as H -> close

A constant-state Kalman filter fuses these. As observations accumulate, the running
extremum approaches the truth from below (HIGH) / above (LOW), and the Kalman nowcast
converges to the realized settlement temp. This is exactly the "constraining" effect
that the intraday-timing hypothesis predicts — but here measured on the MAGNITUDE axis
(degrees F of error), not the direction axis.

Calibration (global, not per-day lookahead)
-------------------------------------------
  sigma_prior^2 = mean over days of (nwp_forecast - settlement_temp_f)^2
  R_H           = mean over days of (settlement_temp_f - running_extremum_H)^2

These are single global scalars estimated from the full historical corpus (identical
to how any forecasting system calibrates its error bars). No single day's prediction
uses its own settlement temp as an input. Contemporaneous-only per-day.

Scoring
-------
For each hour H in [h-24 ... close]:
  1. Kalman nowcast xhat_H, and NWP-only xhat = mu_nwp (constant).
  2. MAE_H = mean |xhat - settlement_temp_f|  for Kalman vs NWP-only.
  3. Predicted cross-probability p = P(x > strike) from the nowcast distribution,
     converted to a fee-adjusted bet, net EV computed.

Verdict: does the magnitude branch have real edge (Kalman MAE < NWP MAE, shrinking
with H, and net EV turning positive), or is it also dead like the direction branch?

Read-only, deterministic, no AI, no signal modification, no DROP/backfill.
"""
import os, json, sqlite3, math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import scipy.stats as _st

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
METAR_DB = os.path.join(REPO, "data", "metar_archive.db")
NWP_DB = os.path.join(REPO, "data", "nwp_forecasts.db")
OUT = os.path.join(REPO, "data", "measurement_trajectory_magnitude.json")

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
NWP_MODELS = ["gfs", "ecmwf", "gefs_ens", "gem", "icon", "era5"]
# gefs_ens stores its 31 members as temperature_2m_{max,min}_membergec00/gep01..gep30
# (no plain "temperature_2m_max"), so we compute its ensemble mean from members.
GEFS_ENSEMBLE_VAR = "temperature_2m_{kind}_member"  # prefix; members = gec00, gep01..gep30
GEFS_MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]


def c_to_f(v):
    """NWP temperature_2m_{max,min} are stored in Celsius; settlement/METAR are F."""
    return v * 9.0 / 5.0 + 32.0

# Round-trip cost: spread 3.1c (half = 1.55c) + slippage 0.5c = 2.05c, plus Kalshi
# taker fee 0.07 * P * (1-P). Same cost model as the direction measurement.
SPREAD = 0.031
SLIPPAGE = 0.005
ROUND_TRIP = SPREAD / 2.0 + SLIPPAGE


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


def load_strike_days():
    c = sqlite3.connect(METAR_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        """SELECT station, market_type, local_trading_date, settlement_temp_f,
                  strike_price, strike_type
           FROM settlement_truth
           WHERE strike_type IN ('greater','less') AND trustworthy=1
             AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL""")]
    c.close()
    return [r for r in rows if r["station"] in STATIONS]


def load_metar():
    """station -> day -> sorted list of (local_hour, temp_f).

    Filters physically-plausible range [-60, 140] F. A small fraction (~0.05%) of
    ingested temp_f rows are corrupt (a C value stored as F, e.g. 64893 F); these
    would poison the running extremum and the Kalman measurement."""
    c = sqlite3.connect(METAR_DB)
    c.row_factory = sqlite3.Row
    out = defaultdict(lambda: defaultdict(list))
    for st in STATIONS:
        for r in c.execute(
            """SELECT timestamp_utc, temp_f FROM metar_observations
               WHERE station=? AND temp_f IS NOT NULL
                 AND temp_f BETWEEN -60 AND 140 ORDER BY timestamp_utc""", (st,)):
            dt = parse_utc(r["timestamp_utc"])
            if dt is None:
                continue
            loc = dt.astimezone(tz_of(st))
            out[st][loc.date().isoformat()].append((loc.hour, r["temp_f"]))
    c.close()
    for st in out:
        for day in out[st]:
            out[st][day].sort()
    return out


def load_nwp():
    """Return station -> target_date -> {'max'|'min': lead_days -> [values in F]}.

    Contemporaneous-only: fetch_date <= target_date (no lookahead). Each (model, lead)
    contributes one ensemble value (gefs_ens = mean of its 31 members). Values are
    converted C -> F.
    """
    c = sqlite3.connect(NWP_DB)
    c.row_factory = sqlite3.Row
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in c.execute(
        """SELECT station, target_date, fetch_date, model, variable, value
           FROM nwp_forecasts
           WHERE variable IN ('temperature_2m_max','temperature_2m_min')
             AND model IN (%s) AND value IS NOT NULL
             AND fetch_date <= target_date""" % ",".join("?" * len(NWP_MODELS)),
        NWP_MODELS):
        st = r["station"]
        td = r["target_date"]
        fd = r["fetch_date"]
        lead = (datetime.strptime(td, "%Y-%m-%d") - datetime.strptime(fd, "%Y-%m-%d")).days
        kind = "max" if r["variable"].endswith("_max") else "min"
        out[st][td][(kind, lead)].append(c_to_f(r["value"]))
    # gefs_ens members
    for r in c.execute(
        """SELECT station, target_date, fetch_date, variable, value
           FROM nwp_forecasts
           WHERE model='gefs_ens'
             AND (variable LIKE 'temperature_2m_max_member%'
                  OR variable LIKE 'temperature_2m_min_member%')
             AND value IS NOT NULL AND fetch_date <= target_date"""):
        st = r["station"]
        td = r["target_date"]
        fd = r["fetch_date"]
        lead = (datetime.strptime(td, "%Y-%m-%d") - datetime.strptime(fd, "%Y-%m-%d")).days
        kind = "max" if "_max_member" in r["variable"] else "min"
        out[st][td][(kind, lead)].append(c_to_f(r["value"]))
    c.close()
    return out


def nwp_forecast(nwp, st, td, kind, lead=1):
    """Ensemble-mean forecast (F) for a station/day/kind at a given lead (days).
    Falls back to the latest available lead <= `lead` if the requested lead is missing.
    kind is 'max' (HIGH) or 'min' (LOW)."""
    per_lead = nwp.get(st, {}).get(td, {})
    if not per_lead:
        return None
    # choose lead: exact, else nearest available lead <= requested
    leads = sorted({l for (k, l) in per_lead.keys() if k == kind})
    if not leads:
        return None
    pick = None
    for l in sorted(leads, reverse=True):
        if l <= lead:
            pick = l
            break
    if pick is None:
        pick = min(leads)  # only longer leads available; take shortest
    vals = per_lead[(kind, pick)]
    return (sum(vals) / len(vals)) if vals else None


def nowcast(mu, sigma, bound, kind):
    """One-sided (truncated-normal) nowcast of the final settlement temp.

    The final settlement temp M ~ N(mu, sigma^2) (the NWP prior). Observing temps
    up to hour H reveals the running extremum, which is a ONE-SIDED bound on M:
      HIGH: M >= running_max  (the daily max is at least what we've seen)
      LOW : M <= running_min
    The correct posterior mean is the truncated-normal conditional mean.
    This is the mathematically-correct fusion of a forecast with a realized bound
    (the symmetric constant-state Kalman treats the bound as an unbiased sample,
    which is wrong for a monotone revealed quantity and biases the nowcast low/high).
    """
    if sigma <= 0:
        return mu
    z = (bound - mu) / sigma
    if kind == "max":
        # E[M | M >= bound]
        cdf = _st.norm.cdf(z)
        if cdf >= 1.0 - 1e-15:
            return bound
        return mu + sigma * _st.norm.pdf(z) / (1.0 - cdf)
    else:
        # E[M | M <= bound]
        cdf = _st.norm.cdf(z)
        if cdf <= 1e-15:
            return bound
        return mu - sigma * _st.norm.pdf(z) / cdf


def calibrate(strike_days, metar, nwp):
    """Estimate global sigma_prior^2 (NWP forecast error variance, in F^2).
    Single scalar over the full corpus — a global calibration parameter, not
    per-day lookahead."""
    nwp_err2 = []
    for d in strike_days:
        st = d["station"]
        mt = (d["market_type"] or "").upper()
        td = d["local_trading_date"]
        truth = d["settlement_temp_f"]
        kind = "max" if mt == "HIGH" else "min"
        fc = nwp_forecast(nwp, st, td, kind)
        if fc is not None:
            nwp_err2.append((fc - truth) ** 2)
    sigma_prior2 = (sum(nwp_err2) / len(nwp_err2)) if nwp_err2 else 1.0
    return sigma_prior2


def main():
    strike_days = load_strike_days()
    metar = load_metar()
    nwp = load_nwp()

    sigma_prior2 = calibrate(strike_days, metar, nwp)
    sigma_prior = math.sqrt(sigma_prior2)

    hours = list(range(24))
    # Per-hour accumulators for nowcast vs NWP-only magnitude error and EV.
    now = {h: dict(mae_sum=0.0, n=0, sq=0.0, correct=0, total=0,
                   pred_cross=0, actual_cross=0, tp=0, fp=0, tn=0, fn=0,
                   und_mae_sum=0.0, und_n=0, und_tp=0, und_fp=0, und_total=0,
                   und_actual_cross=0) for h in hours}
    nwp_acc = {h: dict(mae_sum=0.0, n=0, sq=0.0, tp=0, fp=0, tn=0, fn=0,
                       total=0, actual_cross=0, pred_cross=0) for h in hours}
    now_mean = {h: 0.0 for h in hours}
    now_mean_n = {h: 0 for h in hours}

    for d in strike_days:
        st = d["station"]
        mt = (d["market_type"] or "").upper()
        td = d["local_trading_date"]
        strike = d["strike_price"]
        truth = d["settlement_temp_f"]
        kind = "max" if mt == "HIGH" else "min"
        better = max if mt == "HIGH" else min
        actual_cross = truth > strike if mt == "HIGH" else truth < strike

        fc = nwp_forecast(nwp, st, td, kind)
        obs = metar[st].get(td, [])

        run = None
        oi = 0
        for h in hours:
            # ingest observations up to hour H (contemporaneous, no lookahead)
            while oi < len(obs) and obs[oi][0] <= h:
                t = obs[oi][1]
                run = t if run is None else better(run, t)
                oi += 1

            # --- magnitude nowcast (truncated-normal, one-sided bound) ---
            if fc is not None:
                nwp_acc[h]["n"] += 1
                nwp_acc[h]["mae_sum"] += abs(fc - truth)
                nwp_acc[h]["sq"] += (fc - truth) ** 2
                # NWP-only direction prediction (raw threshold)
                nwp_pred = fc > strike if mt == "HIGH" else fc < strike
                nwp_acc[h]["total"] += 1
                nwp_acc[h]["actual_cross"] += int(actual_cross)
                nwp_acc[h]["pred_cross"] += int(nwp_pred)
                if nwp_pred and actual_cross: nwp_acc[h]["tp"] += 1
                elif nwp_pred and not actual_cross: nwp_acc[h]["fp"] += 1
                elif (not nwp_pred) and (not actual_cross): nwp_acc[h]["tn"] += 1
                else: nwp_acc[h]["fn"] += 1

            if fc is not None and run is not None:
                xhat = nowcast(fc, sigma_prior, run, kind)
                now[h]["n"] += 1
                now[h]["mae_sum"] += abs(xhat - truth)
                now[h]["sq"] += (xhat - truth) ** 2
                now_mean[h] += xhat
                now_mean_n[h] += 1

                # --- cross probability under the truncated posterior ---
                if kind == "max":
                    lo = max(strike, run)
                    p_num = 1.0 - _st.norm.cdf((lo - fc) / sigma_prior)
                    p_den = 1.0 - _st.norm.cdf((run - fc) / sigma_prior)
                    p_cross = (p_num / p_den) if p_den > 1e-15 else 1.0
                else:
                    hi = min(strike, run)
                    p_num = _st.norm.cdf((hi - fc) / sigma_prior)
                    p_den = _st.norm.cdf((run - fc) / sigma_prior)
                    p_cross = (p_num / p_den) if p_den > 1e-15 else 1.0
                p_cross = max(0.0, min(1.0, p_cross))
                pred_cross = p_cross >= 0.5
                now[h]["total"] += 1
                now[h]["pred_cross"] += int(pred_cross)
                now[h]["actual_cross"] += int(actual_cross)
                if pred_cross and actual_cross: now[h]["tp"] += 1
                elif pred_cross and not actual_cross: now[h]["fp"] += 1
                elif (not pred_cross) and (not actual_cross): now[h]["tn"] += 1
                else: now[h]["fn"] += 1
                now[h]["correct"] += int(pred_cross == actual_cross)

                # --- undecided-only magnitude (predictive, not mechanical) ---
                # already realized if the running extremum has already crossed the strike
                already = (run > strike) if mt == "HIGH" else (run < strike)
                if not already:
                    now[h]["und_n"] += 1
                    now[h]["und_mae_sum"] += abs(xhat - truth)
                    now[h]["und_total"] += 1
                    now[h]["und_actual_cross"] += int(actual_cross)
                    if pred_cross and actual_cross: now[h]["und_tp"] += 1
                    elif pred_cross and not actual_cross: now[h]["und_fp"] += 1

    # Assemble report
    report = {
        "meta": {
            "stations": STATIONS, "n_strike_days": len(strike_days),
            "round_trip_fraction": ROUND_TRIP, "spread": SPREAD, "slippage": SLIPPAGE,
            "nwp_models": NWP_MODELS, "sigma_prior2": sigma_prior2,
            "sigma_prior_degF": sigma_prior,
            "nwp_lead_days": 1,
            "description": "Truncated-normal nowcast of settlement_temp_f magnitude "
                           "(one-sided bound from running METAR extremum fused with NWP h-24 prior)",
        },
        "nowcast": [], "nwp_only": [],
    }

    def realized_net_ev(tp, fp, total, actual_cross):
        """Realized net EV of the cross bet = precision - base - round_trip - taker.
        Matches the direction measurement's net_ev definition exactly."""
        if total == 0:
            return None, None
        prec = tp / (tp + fp) if (tp + fp) else None
        q = actual_cross / total
        if prec is None:
            return prec, None
        taker = 0.07 * q * (1.0 - q)
        return prec, prec - q - ROUND_TRIP - taker

    for h in hours:
        k = now[h]; w = nwp_acc[h]
        nprec, nev = realized_net_ev(k["tp"], k["fp"], k["total"], k["actual_cross"])
        uprec, uev = realized_net_ev(k["und_tp"], k["und_fp"], k["und_total"], k["und_actual_cross"])
        wprec, wev = realized_net_ev(w["tp"], w["fp"], w["total"], w["actual_cross"])
        report["nowcast"].append({
            "hour_local": h,
            "mae": (k["mae_sum"] / k["n"]) if k["n"] else None,
            "rmse": math.sqrt(k["sq"] / k["n"]) if k["n"] else None,
            "n": k["n"],
            "mean_nowcast": (now_mean[h] / now_mean_n[h]) if now_mean_n[h] else None,
            "direction_accuracy": (k["correct"] / k["total"]) if k["total"] else None,
            "precision_cross": nprec,
            "net_ev_cross": nev,
            "undecided_n": k["und_n"],
            "undecided_mae": (k["und_mae_sum"] / k["und_n"]) if k["und_n"] else None,
            "undecided_precision_cross": uprec,
            "undecided_net_ev": uev,
        })
        report["nwp_only"].append({
            "hour_local": h,
            "mae": (w["mae_sum"] / w["n"]) if w["n"] else None,
            "rmse": math.sqrt(w["sq"] / w["n"]) if w["n"] else None,
            "n": w["n"],
            "precision_cross": wprec,
            "net_ev_cross": wev,
        })

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Wrote {OUT}\n")

    # ---- human-readable table ----
    print(f"sigma_prior (NWP error std) = {sigma_prior:.2f} F")
    print()
    print(f"{'H':>3} | {'Now MAE':>8} {'NWP MAE':>8} {'dMAE':>8} | {'Now RMSE':>8} | "
          f"{'NWP ev':>8} {'Now ev':>8} | {'Und n':>7} {'UndMAE':>8} {'Und ev':>8}")
    print("-" * 96)
    for h in hours:
        k = report["nowcast"][h]; w = report["nwp_only"][h]
        km = k["mae"]; wm = w["mae"]
        dmae = (km - wm) if (km is not None and wm is not None) else None
        kr = k["rmse"]
        nev = k["net_ev_cross"]; wev = w["net_ev_cross"]
        un = k["undecided_n"]; uma = k["undecided_mae"]; uev = k["undecided_net_ev"]
        def f(x): return f"{x:8.3f}" if x is not None else "     nan"
        def f2(x): return f"{x:8.4f}" if x is not None else "     nan"
        print(f"{h:>3} | {f(km)} {f(wm)} {f(dmae)} | {f(kr)} | "
              f"{f2(wev)} {f2(nev)} | {un:>7} {f(uma)} {f2(uev)}")


if __name__ == "__main__":
    main()
