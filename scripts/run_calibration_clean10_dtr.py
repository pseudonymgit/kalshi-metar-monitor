#!/usr/bin/env python3
"""
Calibration v4 — Clean 10-signal set with DTR (Diurnal Temperature Range) anomaly lever
All previous real signals + DTR anomaly computed from same-day MAX/MIN.
500 random calibration trials, risk guardrails active (limit=8).
"""

import sys
sys.path.insert(0, ".")
import sqlite3
import random
from collections import defaultdict
from core.risk_controls import risk_manager

STATIONS = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
risk_manager.config.skill_gated_stations = STATIONS
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 8

METAR_DB = "data/metar_backfill.db"
NWP_DB = "data/nwp_forecasts.db"

print("=== CLEAN 10-SIGNAL CALIBRATION (DTR anomaly lever) ===")
print("Stations:", STATIONS)
print("Trials: 500 | Risk limit: 8 consecutive losses")

# NWP multi-model
nwp_conn = sqlite3.connect(NWP_DB)
nwp_data = defaultdict(lambda: defaultdict(list))
for fetch_date, target_date, station, model, variable, value in nwp_conn.execute("""
    SELECT fetch_date, target_date, station, model, variable, value 
    FROM nwp_forecasts 
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND variable IN ('temperature_2m_max','temperature_2m_min','pressure_mb')
"""):
    nwp_data[(target_date, station)][variable].append(value)
nwp_conn.close()

# METAR + settlement (raw rows for true DTR)
metar_conn = sqlite3.connect(METAR_DB)
settlements = {}
for station, date, bucket, prior in metar_conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    settlements[(date, station)] = (bucket, prior)

# Compute true DTR per day (MAX - MIN temp_f) + all other daily aggregates
dtr_daily = defaultdict(lambda: defaultdict(dict))
for station, date, temp_f, pressure_mb, slp_mb, wind_kt, gust_kt, dewpoint_c, wind_dir, ceiling_ft, visibility_mi in metar_conn.execute("""
    SELECT station, date_utc, temp_f, pressure_mb, sea_level_pressure_mb,
           wind_speed_kt, wind_gust_kt, dewpoint_c, wind_direction_deg, ceiling_ft, visibility_mi
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    key = (date, station)
    if key not in dtr_daily:
        dtr_daily[key] = {
            "temps": [], "press": [], "slps": [], "dews": [],
            "winds": [], "gusts": [], "wind_dirs": [],
            "ceilings": [], "visibs": []
        }
    dtr_daily[key]["temps"].append(temp_f)
    if pressure_mb: dtr_daily[key]["press"].append(pressure_mb)
    if slp_mb: dtr_daily[key]["slps"].append(slp_mb)
    if dewpoint_c: dtr_daily[key]["dews"].append(dewpoint_c)
    if wind_kt: dtr_daily[key]["winds"].append(wind_kt)
    if gust_kt: dtr_daily[key]["gusts"].append(gust_kt)
    if wind_dir: dtr_daily[key]["wind_dirs"].append(wind_dir)
    if ceiling_ft: dtr_daily[key]["ceilings"].append(ceiling_ft)
    if visibility_mi: dtr_daily[key]["visibs"].append(visibility_mi)

# Collapse to daily features + prior-day DTR
metar_features = {}
all_dates = sorted({k[0] for k in dtr_daily.keys()})
date_to_idx = {d: i for i, d in enumerate(all_dates)}

for (date_utc, station), data in dtr_daily.items():
    if not data["temps"]: continue
    dtr = max(data["temps"]) - min(data["temps"])
    feat = {
        "temp_c": (sum(data["temps"])/len(data["temps"]) - 32) * 5/9 if data["temps"] else None,
        "pressure_mb": sum(data["press"])/len(data["press"]) if data["press"] else None,
        "sea_level_pressure_mb": sum(data["slps"])/len(data["slps"]) if data["slps"] else None,
        "dewpoint_c": (sum(data["dews"])/len(data["dews"]) - 32) * 5/9 if data["dews"] else None,
        "wind_kt": sum(data["winds"])/len(data["winds"]) if data["winds"] else None,
        "wind_gust_kt": sum(data["gusts"])/len(data["gusts"]) if data["gusts"] else None,
        "wind_dir": sum(data["wind_dirs"])/len(data["wind_dirs"]) if data["wind_dirs"] else None,
        "ceiling_ft": sum(data["ceilings"])/len(data["ceilings"]) if data["ceilings"] else None,
        "visibility_mi": sum(data["visibs"])/len(data["visibs"]) if data["visibs"] else None,
        "dtr_f": dtr,
    }

    # prior-day lookup for DTR delta
    idx = date_to_idx.get(date_utc, -1)
    prev_date = all_dates[idx-1] if idx > 0 else None
    prev_key = (prev_date, station) if prev_date else None
    if prev_key and prev_key in dtr_daily and dtr_daily[prev_key]["temps"]:
        prev_dtr = max(dtr_daily[prev_key]["temps"]) - min(dtr_daily[prev_key]["temps"])
        feat["prev_dtr_f"] = prev_dtr
    else:
        feat["prev_dtr_f"] = None

    metar_features[(date_utc, station)] = feat

metar_conn.close()
print(f"Data ready: {len(settlements)} settlement epochs, {len(metar_features)} METAR days (DTR computed)")

def compute_signals(feat, date_str, station, nwp_data):
    temp_c = feat["temp_c"]
    pressure_mb = feat["pressure_mb"]
    slp_mb = feat.get("sea_level_pressure_mb")
    dewpoint_c = feat.get("dewpoint_c")
    wind_kt = feat.get("wind_kt")
    gust_kt = feat.get("wind_gust_kt")
    wind_dir = feat.get("wind_dir")
    ceiling_ft = feat.get("ceiling_ft")
    visibility_mi = feat.get("visibility_mi")
    dtr_f = feat.get("dtr_f")
    prev_dtr = feat.get("prev_dtr_f")

    signals = {}

    # 1. pressure
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0)) if pressure_mb else 0.0

    # 2. gaussian_v2 (temp delta)
    # (we don't have prior temp in this structure; use 0 as neutral for this pass)
    signals["gaussian_v2"] = 0.0

    # 3. calendar_climatology
    month = int(date_str[5:7]) if len(date_str) >= 7 else 6
    signals["calendar_climatology"] = 0.6 if month in (6, 7, 8) else 0.1

    # 4. goldilocks
    if dewpoint_c is not None and temp_c is not None:
        dd = temp_c - dewpoint_c
        if 10 <= dd <= 18 and 14 <= temp_c <= 28:
            signals["goldilocks"] = 0.9
        elif dd < 5 or dd > 22:
            signals["goldilocks"] = -0.4
        else:
            signals["goldilocks"] = 0.2
    else:
        signals["goldilocks"] = 0.1

    # 5. wind_advection
    signals["wind_advection"] = 0.0  # (prior-day wind fields dropped for simplicity in this script)

    # 6. cloud_cover_modulation
    cloud_sig = 0.0
    if ceiling_ft is not None and visibility_mi is not None:
        if ceiling_ft >= 10000 and visibility_mi >= 8: cloud_sig = 0.5
        elif ceiling_ft < 3000 or visibility_mi < 3: cloud_sig = -0.5
        else: cloud_sig = 0.1
    signals["cloud_cover_modulation"] = max(-1.0, min(1.0, cloud_sig))

    # 7. forecast_disagreement (multi-model)
    nwp_key = (date_str, station)
    if nwp_key in nwp_data:
        vars_at_key = nwp_data[nwp_key]
        spreads = []
        for var_name in ('temperature_2m_max', 'temperature_2m_min', 'pressure_mb'):
            vals = vars_at_key.get(var_name, [])
            if len(vals) >= 2: spreads.append(max(vals) - min(vals))
        if spreads:
            avg_spread = sum(spreads) / len(spreads)
            signals["forecast_disagreement"] = max(-1.0, min(1.0, avg_spread / 3.0))
        else:
            signals["forecast_disagreement"] = 0.15
    else:
        signals["forecast_disagreement"] = 0.0

    # 8. slp_anomaly
    signals["slp_anomaly"] = max(-1.0, min(1.0, (slp_mb - 1013.25) / 20.0)) if slp_mb else 0.0

    # 9. gust_anomaly
    gust_sig = 0.0
    if gust_kt is not None:
        gust_norm = min(1.0, gust_kt / 35.0)
        gust_sig = gust_norm * 0.4
    signals["gust_anomaly"] = max(-1.0, min(1.0, gust_sig))

    # 10. dtr_anomaly (NEW — Diurnal Temperature Range delta)
    dtr_sig = 0.0
    if dtr_f is not None:
        # normalize DTR (typical 5-25°F)
        dtr_norm = max(-1.0, min(1.0, (dtr_f - 15.0) / 12.0))
        if prev_dtr is not None:
            dtr_delta = dtr_f - prev_dtr
            # DTR collapse (negative delta) often signals warming overnight / front
            if dtr_delta < -6:
                dtr_sig = 0.7
            elif dtr_delta > 6:
                dtr_sig = -0.6
            else:
                dtr_sig = dtr_norm * 0.5 + (dtr_delta / 15.0)
        else:
            dtr_sig = dtr_norm * 0.4
    signals["dtr_anomaly"] = max(-1.0, min(1.0, dtr_sig))

    return signals

# --- Baseline with 10 signals ---
print("\n=== BASELINE (10 signals, equal weights, threshold 0.08) ===")
weights = {s: 1.0/10.0 for s in ["pressure","gaussian_v2","calendar_climatology","goldilocks","wind_advection","cloud_cover_modulation","forecast_disagreement","slp_anomaly","gust_anomaly","dtr_anomaly"]}
threshold = 0.08

risk_manager.risk_state = {"daily_pnl": 0.0, "consecutive_losses": 0, "kill_switch_active": False, "kill_reason": ""}
total_trades = 0
correct = 0
pnl_total = 0.0
for key in sorted(settlements.keys()):
    date_str, station = key
    if not risk_manager.is_station_allowed(station): continue
    if key not in metar_features: continue
    settlement_bucket, prior_bucket = settlements[key]
    actual_up = settlement_bucket > prior_bucket
    feat = metar_features[key]
    signals = compute_signals(feat, date_str, station, nwp_data)
    ensemble = sum(signals[s] * weights[s] for s in signals)
    predicted_up = ensemble > threshold
    pnl = 14.0 if (predicted_up == actual_up) else -11.0
    risk_manager.update_after_trade(pnl)
    total_trades += 1
    if pnl > 0: correct += 1
    pnl_total += pnl
    if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
        break

acc = (correct / total_trades * 100) if total_trades > 0 else 0
print(f"Trades: {total_trades} | Accuracy: {round(acc,2)}% | P&L: ${pnl_total:,.0f}")

# --- 500-round calibration ---
print("\n=== 500-ROUND RANDOM SEARCH (10 signals) ===")
best = {"acc": 0, "pnl": 0, "weights": None, "threshold": None, "trades": 0}

for trial in range(500):
    raw = [random.uniform(0.3, 2.4) for _ in range(10)]
    s = sum(raw)
    w = [x/s for x in raw]
    weights = dict(zip(["pressure","gaussian_v2","calendar_climatology","goldilocks","wind_advection","cloud_cover_modulation","forecast_disagreement","slp_anomaly","gust_anomaly","dtr_anomaly"], w))
    threshold = random.uniform(0.02, 0.13)

    risk_manager.risk_state = {"daily_pnl": 0.0, "consecutive_losses": 0, "kill_switch_active": False, "kill_reason": ""}
    total_trades = 0
    correct = 0
    pnl_total = 0.0

    for key in sorted(settlements.keys()):
        date_str, station = key
        if not risk_manager.is_station_allowed(station): continue
        if key not in metar_features: continue
        settlement_bucket, prior_bucket = settlements[key]
        actual_up = settlement_bucket > prior_bucket
        feat = metar_features[key]
        signals = compute_signals(feat, date_str, station, nwp_data)
        ensemble = sum(signals[s] * weights[s] for s in signals)
        predicted_up = ensemble > threshold
        pnl = 14.0 if (predicted_up == actual_up) else -11.0
        risk_manager.update_after_trade(pnl)
        total_trades += 1
        if pnl > 0: correct += 1
        pnl_total += pnl
        if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
            break

    acc = (correct / total_trades * 100) if total_trades > 100 else 0
    if acc > best["acc"] and total_trades > 100:
        best = {"acc": acc, "pnl": pnl_total, "weights": weights.copy(), "threshold": threshold, "trades": total_trades}

print(f"\n=== BEST 10-SIGNAL CONFIGURATION (500 trials) ===")
print(f"Accuracy: {round(best['acc'],2)}% | Trades: {best['trades']} | P&L: ${best['pnl']:,.0f}")
print(f"Threshold: {round(best['threshold'],4)}")
print("Weights:")
for s, w in sorted(best['weights'].items()):
    print(f"  {s}: {round(w,3)}")