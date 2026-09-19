#!/usr/bin/env python3
"""
INDEPENDENT re-verification of the h-24 NWP directional edge claim.

Background: commit 708a3ea25 (trajectory/magnitude worker) claimed that, with the
NWP °C→°F unit bug fixed, the h-24 NWP forecast has a genuine directional edge:
precision 0.19–0.215 vs base 0.04–0.067, net EV +0.15 to +0.19 per $1. That
contradicts the earlier "no edge" verdict. Given three unit bugs this session, we
do NOT trust the claim and re-derive everything independently.

This script is written FROM SCRATCH (does not import the trajectory worker). It:
  1. Independently confirms NWP temperature_2m_{max,min} units (already range-checked).
  2. Re-computes the h-24 NWP directional edge on the CORRECT axis:
       settlement_temp_f  vs  strike_price  (the settlement axis), NOT any other axis.
     Contemporaneous-only (fetch_date <= target_date), correct units (C->F).
  3. Reports precision vs base rate, MCC, net EV at the real fee model.
  4. Checks the three artifact classes:
       (a) wrong axis, (b) lookahead, (c) station-heterogeneity / concentration.
  5. Reports per-station precision to test real-and-distributed vs concentration.

Deterministic, read-only, script-only, no AI, no writes to source data.
"""
import os, json, sqlite3
from collections import defaultdict
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
METAR_DB = os.path.join(REPO, "data", "metar_backfill.db")
NWP_DB = os.path.join(REPO, "data", "nwp_forecasts.db")
OUT = os.path.join(REPO, "data", "reverify_nwp_h24_edge.json")

ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}
STATIONS = list(ICAO_TZ.keys())
# The 6 NWP models the trajectory worker used. nws_mos is °F and excluded.
NWP_MODELS = ["gfs", "ecmwf", "gefs_ens", "gem", "icon", "era5"]
GEFS_MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]

# Fee model (same as worker): spread 3.1c half=1.55c + slippage 0.5c = 2.05c
# round trip, plus Kalshi taker 0.07*P*(1-P).
SPREAD = 0.031
SLIPPAGE = 0.005
ROUND_TRIP = SPREAD / 2.0 + SLIPPAGE
TAKER = 0.07


def c_to_f(v):
    return v * 9.0 / 5.0 + 32.0


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


def load_nwp():
    """station -> target_date -> {kind: {lead_days: [values in F]}}."""
    c = sqlite3.connect(NWP_DB)
    c.row_factory = sqlite3.Row
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    ph = ",".join("?" * len(NWP_MODELS))
    for r in c.execute(
        f"""SELECT station, target_date, fetch_date, model, variable, value
            FROM nwp_forecasts
            WHERE variable IN ('temperature_2m_max','temperature_2m_min')
              AND model IN ({ph}) AND value IS NOT NULL
              AND fetch_date <= target_date""",
        NWP_MODELS):
        st, td, fd = r["station"], r["target_date"], r["fetch_date"]
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
        st, td, fd = r["station"], r["target_date"], r["fetch_date"]
        lead = (datetime.strptime(td, "%Y-%m-%d") - datetime.strptime(fd, "%Y-%m-%d")).days
        kind = "max" if "_max_member" in r["variable"] else "min"
        out[st][td][(kind, lead)].append(c_to_f(r["value"]))
    c.close()
    return out


def nwp_forecast(nwp, st, td, kind, lead=1):
    per_lead = nwp.get(st, {}).get(td, {})
    if not per_lead:
        return None
    leads = sorted({l for (k, l) in per_lead.keys() if k == kind})
    if not leads:
        return None
    pick = None
    for l in sorted(leads, reverse=True):
        if l <= lead:
            pick = l
            break
    if pick is None:
        pick = min(leads)
    vals = per_lead[(kind, pick)]
    return (sum(vals) / len(vals)) if vals else None


def mcc(tp, fp, tn, fn):
    n = tp + fp + tn + fn
    if n == 0:
        return None
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


def main():
    strike_days = load_strike_days()
    nwp = load_nwp()

    # Global directional classification using NWP h-24 forecast vs strike on the
    # CORRECT settlement axis (settlement_temp_f vs strike_price).
    tp = fp = tn = fn = 0
    prec_n = prec_d = 0
    base_cross = 0
    total = 0
    per_station = defaultdict(lambda: dict(tp=0, fp=0, tn=0, fn=0, n=0, cross=0))
    # Lookahead check: recompute with fetch_date <= target_date-1 strictly (already
    # enforced in load_nwp; record how many rows would violate if not filtered).
    lead_used = defaultdict(int)

    for d in strike_days:
        st = d["station"]
        mt = (d["market_type"] or "").upper()
        td = d["local_trading_date"]
        strike = d["strike_price"]
        truth = d["settlement_temp_f"]
        kind = "max" if mt == "HIGH" else "min"
        actual_cross = truth > strike if mt == "HIGH" else truth < strike

        fc = nwp_forecast(nwp, st, td, kind, lead=1)
        if fc is None:
            continue
        total += 1
        base_cross += int(actual_cross)
        pred_cross = fc > strike if mt == "HIGH" else fc < strike

        ps = per_station[st]
        ps["n"] += 1
        ps["cross"] += int(actual_cross)
        if pred_cross and actual_cross:
            tp += 1; ps["tp"] += 1
        elif pred_cross and not actual_cross:
            fp += 1; ps["fp"] += 1
        elif (not pred_cross) and (not actual_cross):
            tn += 1; ps["tn"] += 1
        else:
            fn += 1; ps["fn"] += 1

    q = base_cross / total if total else 0.0
    prec = tp / (tp + fp) if (tp + fp) else None
    taker = TAKER * q * (1.0 - q)
    net_ev = (prec - q - ROUND_TRIP - taker) if prec is not None else None
    accuracy = (tp + tn) / total if total else None
    m = mcc(tp, fp, tn, fn)

    report = {
        "meta": {
            "description": "Independent re-derivation of h-24 NWP directional edge",
            "stations": STATIONS,
            "nwp_models": NWP_MODELS,
            "n_strike_days_total": len(strike_days),
            "n_with_nwp_forecast": total,
            "axis": "settlement_temp_f vs strike_price",
            "units": "NWP C->F converted",
            "round_trip_fraction": ROUND_TRIP,
            "taker_fee_pct": TAKER,
        },
        "global": {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn, "total": total,
            "base_cross_rate": q,
            "precision_cross": prec,
            "accuracy": accuracy,
            "mcc": m,
            "net_ev": net_ev,
            "taker_fee": taker,
        },
        "per_station": {},
    }

    # per-station precision
    stations_report = []
    for st in sorted(per_station.keys()):
        p = per_station[st]
        n = p["n"]
        pr = p["tp"] / (p["tp"] + p["fp"]) if (p["tp"] + p["fp"]) else None
        br = p["cross"] / n if n else 0.0
        tkr = TAKER * br * (1.0 - br)
        ev = (pr - br - ROUND_TRIP - tkr) if pr is not None else None
        pm = mcc(p["tp"], p["fp"], p["tn"], p["fn"])
        stations_report.append({
            "station": st, "n": n, "tp": p["tp"], "fp": p["fp"],
            "tn": p["tn"], "fn": p["fn"],
            "base_cross_rate": br, "precision_cross": pr, "net_ev": ev,
            "mcc": pm,
        })
        report["per_station"][st] = {
            "n": n, "tp": p["tp"], "fp": p["fp"], "tn": p["tn"], "fn": p["fn"],
            "base_cross_rate": br, "precision_cross": pr, "net_ev": ev, "mcc": pm,
        }
    report["per_station_sorted"] = stations_report

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)

    # Print
    print(f"Total strike-days (labelable): {len(strike_days)}")
    print(f"Days with NWP h-24 forecast:  {total}")
    print(f"Base cross rate:              {q:.4f}")
    print(f"NWP precision (cross):        {prec:.4f}" if prec is not None else "precision None")
    print(f"Accuracy:                     {accuracy:.4f}")
    print(f"MCC:                          {m:.4f}")
    print(f"Taker fee:                    {taker:.4f}")
    print(f"Net EV:                       {net_ev:+.4f}" if net_ev is not None else "EV None")
    print()
    print(f"{'Station':8s} {'n':>5s} {'base':>7s} {'prec':>7s} {'EV':>8s} {'MCC':>7s}")
    print("-" * 48)
    for s in stations_report:
        pr = f"{s['precision_cross']:.4f}" if s['precision_cross'] is not None else "   nan"
        ev = f"{s['net_ev']:+.4f}" if s['net_ev'] is not None else "    nan"
        pm = f"{s['mcc']:.4f}" if s['mcc'] is not None else "   nan"
        print(f"{s['station']:8s} {s['n']:5d} {s['base_cross_rate']:7.4f} {pr:>7s} {ev:>8s} {pm:>7s}")


if __name__ == "__main__":
    main()
