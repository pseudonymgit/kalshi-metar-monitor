#!/usr/bin/env python3
"""
Rigorous version — separates PREDICTIVE edge from MECHANICAL realization.

Key refinement: at hour H, a day is "already decided" if the running observed
extremum has already crossed the strike (then predicting 'cross' is trivially correct,
no predictive value). We measure predictive skill ONLY on the still-undecided subset:
days where the cross has NOT yet been observed by hour H.

Three signals compared at each local hour H:
  1. NWP-only (h-24 daily forecast, no intraday) — the baseline. Should be ~flat.
  2. Trajectory (NWP + running observed extremum) on ALL days — monotone, contaminated.
  3. Trajectory on UNDECIDED days only — the pure predictive edge of intraday timing.

Also computes fee-adjusted net EV for each. Read-only, deterministic, no AI.
"""
import os, json, sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
METAR_DB = os.path.join(REPO, "data", "metar_archive.db")
NWP_DB = os.path.join(REPO, "data", "nwp_forecasts.db")
OUT = os.path.join(REPO, "data", "measurement_intraday_timing_edge_v2.json")

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


def parse_utc(s):
    if not s: return None
    s = str(s).strip()
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tz_of(icao): return ZoneInfo(_ICAO_TZ.get(icao, "America/New_York"))


def load_strike_days():
    c = sqlite3.connect(METAR_DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f, strike_price, strike_type
        FROM settlement_truth
        WHERE strike_type IN ('greater','less') AND trustworthy=1
          AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL""")]
    c.close()
    return [r for r in rows if r["station"] in STATIONS]


def load_metar():
    c = sqlite3.connect(METAR_DB); c.row_factory = sqlite3.Row
    out = defaultdict(list)
    for st in STATIONS:
        for r in c.execute("""SELECT timestamp_utc, temp_f FROM metar_observations
            WHERE station=? AND temp_f IS NOT NULL ORDER BY timestamp_utc""", (st,)):
            dt = parse_utc(r["timestamp_utc"])
            if dt is None: continue
            loc = dt.astimezone(tz_of(st))
            out[st].append((loc.hour, r["temp_f"], loc.date().isoformat()))
    c.close()
    return out


def load_nwp():
    c = sqlite3.connect(NWP_DB); c.row_factory = sqlite3.Row
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in c.execute("""SELECT station, target_date, model, variable, value FROM nwp_forecasts
        WHERE variable IN ('temperature_2m_max','temperature_2m_min')
          AND model IN (%s) AND value IS NOT NULL""" % ",".join("?" * len(NWP_MODELS)), NWP_MODELS):
        out[r["station"]][r["target_date"]][r["variable"]].append(r["value"])
    c.close()
    return out


def main():
    strike_days = load_strike_days()
    metar = load_metar()
    nwp = load_nwp()

    metar_by_day = defaultdict(lambda: defaultdict(list))
    for st in STATIONS:
        for lh, tf, ld in metar[st]:
            metar_by_day[st][ld].append((lh, tf))
    for st in metar_by_day:
        for ld in metar_by_day[st]: metar_by_day[st][ld].sort()

    def nwp_forecast(st, td, var):
        v = nwp[st][td][var]
        return (sum(v) / len(v)) if v else None

    hours = list(range(24))
    # metrics: {signal: {hour: {tp,fp,tn,fn, correct,total, pred_cross, actual_cross}}}
    signals = ["nwp_only", "trajectory_all", "trajectory_undecided"]
    S = {s: {h: dict(tp=0, fp=0, tn=0, fn=0, correct=0, total=0, pred_cross=0, actual_cross=0)
             for h in hours} for s in signals}
    # per-station undecided metrics for timing-edge check
    per_station = defaultdict(lambda: {h: dict(tp=0, fp=0, tn=0, fn=0, correct=0, total=0, pred_cross=0, actual_cross=0) for h in hours})

    for d in strike_days:
        st, mt = d["station"], (d["market_type"] or "").upper()
        td, strike, truth = d["local_trading_date"], d["strike_price"], d["settlement_temp_f"]
        if mt == "HIGH":
            actual_cross = truth > strike; var = "temperature_2m_max"; better = max
        elif mt == "LOW":
            actual_cross = truth < strike; var = "temperature_2m_min"; better = min
        else:
            continue
        fc = nwp_forecast(st, td, var)
        obs = metar_by_day[st].get(td, [])

        run = None; oi = 0
        for h in hours:
            while oi < len(obs) and obs[oi][0] <= h:
                t = obs[oi][1]
                run = t if run is None else better(run, t)
                oi += 1

            # Signal 1: NWP-only
            if fc is not None:
                nwp_pred = fc > strike if mt == "HIGH" else fc < strike
                m = S["nwp_only"][h]; m["total"] += 1
                m["actual_cross"] += int(actual_cross); m["pred_cross"] += int(nwp_pred)
                if nwp_pred == actual_cross: m["correct"] += 1
                if nwp_pred and actual_cross: m["tp"] += 1
                elif nwp_pred and not actual_cross: m["fp"] += 1
                elif (not nwp_pred) and (not actual_cross): m["tn"] += 1
                else: m["fn"] += 1

            # Signal 2: trajectory-all (NWP + running observed)
            if fc is not None or run is not None:
                pv = run
                if fc is not None: pv = fc if pv is None else better(fc, pv)
                if pv is not None:
                    pred_cross = pv > strike if mt == "HIGH" else pv < strike
                    m = S["trajectory_all"][h]; m["total"] += 1
                    m["actual_cross"] += int(actual_cross); m["pred_cross"] += int(pred_cross)
                    if pred_cross == actual_cross: m["correct"] += 1
                    if pred_cross and actual_cross: m["tp"] += 1
                    elif pred_cross and not actual_cross: m["fp"] += 1
                    elif (not pred_cross) and (not actual_cross): m["tn"] += 1
                    else: m["fn"] += 1

                # Signal 3: trajectory on UNDECIDED days (cross not yet observed)
                already_crossed = run is not None and ((run > strike) if mt == "HIGH" else (run < strike))
                if not already_crossed:
                    # use forecast + running (running hasn't crossed, so effectively forecast for the cross question)
                    if fc is not None:
                        pred_cross = fc > strike if mt == "HIGH" else fc < strike
                        m = S["trajectory_undecided"][h]; m["total"] += 1
                        m["actual_cross"] += int(actual_cross); m["pred_cross"] += int(pred_cross)
                        if pred_cross == actual_cross: m["correct"] += 1
                        if pred_cross and actual_cross: m["tp"] += 1
                        elif pred_cross and not actual_cross: m["fp"] += 1
                        elif (not pred_cross) and (not actual_cross): m["tn"] += 1
                        else: m["fn"] += 1
                        pm = per_station[st][h]; pm["total"] += 1
                        pm["actual_cross"] += int(actual_cross); pm["pred_cross"] += int(pred_cross)
                        if pred_cross == actual_cross: pm["correct"] += 1
                        if pred_cross and actual_cross: pm["tp"] += 1
                        elif pred_cross and not actual_cross: pm["fp"] += 1
                        elif (not pred_cross) and (not actual_cross): pm["tn"] += 1
                        else: pm["fn"] += 1

    SPREAD, SLIPPAGE = 0.031, 0.005
    round_trip = SPREAD / 2.0 + SLIPPAGE

    def metrics(m):
        tot = m["total"]
        if tot == 0: return None
        tp, fp, tn, fn = m["tp"], m["fp"], m["tn"], m["fn"]
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / (tp + fn) if (tp + fn) else None
        return dict(total=tot, accuracy=m["correct"] / tot, base_cross=m["actual_cross"] / tot,
                    pred_cross_rate=m["pred_cross"] / tot, precision_cross=prec,
                    recall_cross=rec, tp=tp, fp=fp, tn=tn, fn=fn)

    def net_ev(m):
        if m is None or m["precision_cross"] is None: return None
        q = m["base_cross"]
        taker = 0.07 * q * (1 - q)
        return m["precision_cross"] - q - round_trip - taker

    report = {"meta": {"stations": STATIONS, "n_strike_days": len(strike_days),
                       "round_trip_fraction": round_trip, "spread": SPREAD, "slippage": SLIPPAGE,
                       "nwp_models": NWP_MODELS},
              "signals": {}}
    for s in signals:
        rows = []
        for h in hours:
            m = metrics(S[s][h])
            rows.append({"hour_local": h, "metrics": m, "net_ev_cross_side": net_ev(m)})
        report["signals"][s] = rows

    report["per_station_undecided"] = {}
    for st in STATIONS:
        rows = []
        for h in hours:
            m = metrics(per_station[st][h])
            rows.append({"hour_local": h, "metrics": m, "net_ev_cross_side": net_ev(m)})
        report["per_station_undecided"][st] = rows

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Wrote {OUT}\n")

    # Print comparison table
    for s in signals:
        print(f"=== {s} ===")
        print(f"{'H':>3} {'n':>6} {'acc':>7} {'base':>7} {'prec':>7} {'recall':>7} {'netEV':>8}")
        for r in report["signals"][s]:
            m = r["metrics"]
            if not m: print(f"{r['hour_local']:>3} {'-':>6}"); continue
            p = m["precision_cross"] if m["precision_cross"] is not None else float("nan")
            rc = m["recall_cross"] if m["recall_cross"] is not None else float("nan")
            ev = r["net_ev_cross_side"] if r["net_ev_cross_side"] is not None else float("nan")
            print(f"{r['hour_local']:>3} {m['total']:>6} {m['accuracy']:>7.3f} {m['base_cross']:>7.3f} "
                  f"{p:>7.3f} {rc:>7.3f} {ev:>8.4f}")
        print()


if __name__ == "__main__":
    main()
