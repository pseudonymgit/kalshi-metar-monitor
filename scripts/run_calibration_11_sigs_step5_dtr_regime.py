#!/usr/bin/env python3
"""
Calibration v3 — Clean 9-signal set with wind_gust_kt lever
All previous real signals + gust anomaly.
200 random calibration trials, risk guardrails active (limit=8).
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

print("=== CLEAN 9-SIGNAL CALIBRATION (wind_gust lever added) ===")
print("Stations:", STATIONS)
print("Trials: 200 | Risk limit: 8 consecutive losses")

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

# METAR + settlement (now includes wind_gust_kt)
metar_conn = sqlite3.connect(METAR_DB)
settlements = {}
for station, date, bucket, prior in metar_conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    settlements[(date, station)] = (bucket, prior)

metar_daily = defaultdict(lambda: defaultdict(list))
for row in metar_conn.execute("""
    SELECT station, date_utc, temp_c, pressure_mb, sea_level_pressure_mb,
           wind_speed_kt, wind_gust_kt, dewpoint_c, wind_direction_deg, ceiling_ft, visibility_mi
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    station, date, temp_c, pressure_mb, slp_mb, wind_kt, gust_kt, dewpoint_c, wind_dir, ceiling_ft, visibility_mi = row
    metar_daily[date][station].append({
        "temp_c": temp_c,
        "pressure_mb": pressure_mb,
        "sea_level_pressure_mb": slp_mb,
        "wind_kt": wind_kt or 0,
        "wind_gust_kt": gust_kt,
        "dewpoint_c": dewpoint_c,
        "wind_dir": wind_dir or 0,
        "ceiling_ft": ceiling_ft,
        "visibility_mi": visibility_mi
    })

# Collapse + prior-day (including additional lags for analysis)
metar_features = {}
all_dates = sorted(metar_daily.keys())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

for date_utc, stations in metar_daily.items():
    for station, obs_list in stations.items():
        if not obs_list: continue
        temps = [o["temp_c"] for o in obs_list if o["temp_c"] is not None]
        press = [o["pressure_mb"] for o in obs_list if o["pressure_mb"] is not None]
        slps = [o["sea_level_pressure_mb"] for o in obs_list if o.get("sea_level_pressure_mb") is not None]
        dews = [o["dewpoint_c"] for o in obs_list if o["dewpoint_c"] is not None]
        winds = [o["wind_kt"] for o in obs_list if o.get("wind_kt")]
        gusts = [o["wind_gust_kt"] for o in obs_list if o.get("wind_gust_kt") is not None]
        wind_dirs = [o["wind_dir"] for o in obs_list if o.get("wind_dir")]
        ceilings = [o["ceiling_ft"] for o in obs_list if o.get("ceiling_ft")]
        visibs = [o["visibility_mi"] for o in obs_list if o.get("visibility_mi")]

        feat = {
            "temp_c": sum(temps)/len(temps) if temps else None,
            "pressure_mb": sum(press)/len(press) if press else None,
            "sea_level_pressure_mb": sum(slps)/len(slps) if slps else None,
            "dewpoint_c": sum(dews)/len(dews) if dews else None,
            "wind_kt": sum(winds)/len(winds) if winds else None,
            "wind_gust_kt": sum(gusts)/len(gusts) if gusts else None,
            "wind_dir": sum(wind_dirs)/len(wind_dirs) if wind_dirs else None,
            "ceiling_ft": sum(ceilings)/len(ceilings) if ceilings else None,
            "visibility_mi": sum(visibs)/len(visibs) if visibs else None,
        }

        idx = date_to_idx.get(date_utc, -1)
        prev_date = all_dates[idx-1] if idx > 0 else None
        prev2_date = all_dates[idx-2] if idx > 1 else None  # Extra lag for 2-day tendency
        
        if prev_date and station in metar_daily.get(prev_date, {}):
            prev_obs = metar_daily[prev_date][station]
            if prev_obs:
                prev_temps = [o["temp_c"] for o in prev_obs if o["temp_c"] is not None]
                prev_press = [o["pressure_mb"] for o in prev_obs if o["pressure_mb"] is not None]
                prev_slps = [o["sea_level_pressure_mb"] for o in prev_obs if o.get("sea_level_pressure_mb") is not None]
                prev_winds = [o["wind_kt"] for o in prev_obs if o.get("wind_kt")]
                prev_gusts = [o["wind_gust_kt"] for o in prev_obs if o.get("wind_gust_kt") is not None]
                prev_dirs = [o["wind_dir"] for o in prev_obs if o.get("wind_dir")]
                prev_ceil = [o["ceiling_ft"] for o in prev_obs if o.get("ceiling_ft")]
                prev_vis = [o["visibility_mi"] for o in prev_obs if o.get("visibility_mi")]
                feat["prev_temp_c"] = sum(prev_temps)/len(prev_temps) if prev_temps else None
                feat["prev_pressure_mb"] = sum(prev_press)/len(prev_press) if prev_press else None
                feat["prev_sea_level_pressure_mb"] = sum(prev_slps)/len(prev_slps) if prev_slps else None
                feat["prev_wind_kt"] = sum(prev_winds)/len(prev_winds) if prev_winds else None
                feat["prev_wind_gust_kt"] = sum(prev_gusts)/len(prev_gusts) if prev_gusts else None
                feat["prev_wind_dir"] = sum(prev_dirs)/len(prev_dirs) if prev_dirs else None
                feat["prev_ceiling_ft"] = sum(prev_ceil)/len(prev_ceil) if prev_ceil else None
                feat["prev_visibility_mi"] = sum(prev_vis)/len(prev_vis) if prev_vis else None
            else:
                feat["prev_temp_c"] = feat["prev_pressure_mb"] = feat["prev_sea_level_pressure_mb"] = None
                feat["prev_wind_kt"] = feat["prev_wind_gust_kt"] = feat["prev_wind_dir"] = None
                feat["prev_ceiling_ft"] = feat["prev_visibility_mi"] = None
        else:
            feat["prev_temp_c"] = feat["prev_pressure_mb"] = feat["prev_sea_level_pressure_mb"] = None
            feat["prev_wind_kt"] = feat["prev_wind_gust_kt"] = feat["prev_wind_dir"] = None
            feat["prev_ceiling_ft"] = feat["prev_visibility_mi"] = None
            
        # Add two-day ago information for pressure tendency
        if prev2_date and station in metar_daily.get(prev2_date, {}):
            prev2_obs = metar_daily[prev2_date][station]
            if prev2_obs:
                prev2_press = [o["pressure_mb"] for o in prev2_obs if o["pressure_mb"] is not None]
                feat["prev2_pressure_mb"] = sum(prev2_press)/len(prev2_press) if prev2_press else None
            else:
                feat["prev2_pressure_mb"] = None
        else:
            feat["prev2_pressure_mb"] = None

        metar_features[(date_utc, station)] = feat

metar_conn.close()
print(f"Data ready: {len(settlements)} settlement epochs, {len(metar_features)} METAR days")

def compute_signals(feat, date_str, station, nwp_data):
    temp_c = feat["temp_c"]
    pressure_mb = feat["pressure_mb"]
    slp_mb = feat.get("sea_level_pressure_mb")
    prev_slp = feat.get("prev_sea_level_pressure_mb")
    prev2_pressure_mb = feat.get("prev2_pressure_mb")  # Extra lag for 2-day pressure tendency
    dewpoint_c = feat.get("dewpoint_c")
    prev_temp = feat.get("prev_temp_c")
    prev_pressure = feat.get("prev_pressure_mb")
    wind_kt = feat.get("wind_kt")
    gust_kt = feat.get("wind_gust_kt")
    prev_wind_kt = feat.get("prev_wind_kt")
    prev_gust = feat.get("prev_wind_gust_kt")
    wind_dir = feat.get("wind_dir")
    prev_wind_dir = feat.get("prev_wind_dir")
    ceiling_ft = feat.get("ceiling_ft")
    visibility_mi = feat.get("visibility_mi")
    prev_ceiling = feat.get("prev_ceiling_ft")
    prev_visibility = feat.get("prev_visibility_mi")

    signals = {}

    # 1. pressure (station)
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0))

    # 2. dtr_regime_filter (NEW SIGNAL for step 5 - binary high/low DTR regime)
    # Calculate current day DTR (based on NWP forecasts if possible, or estimates) 
    # Since we want to implement a binary regime, check day-to-day changes
    
    # To calculate DTR we'd typically need: max_temp - min_temp for the day
    # Using METAR day to approximate from temp data if available
    current_dtr = None
    if temp_c is not None and prev_temp is not None:
        # Estimate using temperature change or if we have min/max from NWP
        nwp_key = (date_str, station)
        if nwp_key in nwp_data:
            nwp_temps = nwp_data[nwp_key]
            tmax_vals = nwp_temps.get('temperature_2m_max', [])
            tmin_vals = nwp_temps.get('temperature_2m_min', [])
            if tmax_vals and tmin_vals:
                avg_tmax = sum(tmax_vals) / len(tmax_vals) 
                avg_tmin = sum(tmin_vals) / len(tmin_vals)
                current_dtr = avg_tmax - avg_tmin
    
    # Implement binary regime signal
    if current_dtr is not None:
        # Define thresholds for high/low DTR binary split
        # Based on typical continental US DTR (avg ~10-20°C in summer, less in winter)
        if current_dtr > 14.0:  # High DTR regime
            signals["dtr_regime_filter"] = 0.8   # High DTR case
        elif current_dtr < 8.0:  # Low DTR regime 
            signals["dtr_regime_filter"] = -0.8  # Low DTR case
        else:
            signals["dtr_regime_filter"] = 0.2   # Neutral range
    else:
        # Default if no DTR data
        signals["dtr_regime_filter"] = 0.0

    # 3. gaussian_v2
    if prev_temp is not None and temp_c is not None:
        dt = temp_c - prev_temp
        signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.25))
    else:
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
    wind_sig = 0.0
    if (wind_kt is not None and prev_wind_kt is not None and wind_dir is not None and prev_wind_dir is not None):
        speed_delta = wind_kt - prev_wind_kt
        dir_delta = abs(wind_dir - prev_wind_dir)
        if dir_delta > 180: dir_delta = 360 - dir_delta
        if speed_delta > 4 and dir_delta > 45:
            wind_sig = 0.6
        elif speed_delta < -4 and dir_delta > 45:
            wind_sig = -0.4
        else:
            wind_sig = speed_delta / 12.0
    signals["wind_advection"] = max(-1.0, min(1.0, wind_sig))

    # 6. cloud_cover_modulation
    cloud_sig = 0.0
    if ceiling_ft is not None and visibility_mi is not None:
        if ceiling_ft >= 10000 and visibility_mi >= 8:
            cloud_sig = 0.5
        elif ceiling_ft < 3000 or visibility_mi < 3:
            cloud_sig = -0.5
        else:
            cloud_sig = 0.1
    elif prev_ceiling is not None and prev_visibility is not None:
        if prev_ceiling >= 10000 and prev_visibility >= 8:
            cloud_sig = 0.3
        elif prev_ceiling < 3000 or prev_visibility < 3:
            cloud_sig = -0.3
    signals["cloud_cover_modulation"] = max(-1.0, min(1.0, cloud_sig))

    # 7. forecast_disagreement (multi-model)
    nwp_key = (date_str, station)
    if nwp_key in nwp_data:
        vars_at_key = nwp_data[nwp_key]
        spreads = []
        for var_name in ('temperature_2m_max', 'temperature_2m_min', 'pressure_mb'):
            vals = vars_at_key.get(var_name, [])
            if len(vals) >= 2:
                spreads.append(max(vals) - min(vals))
        if spreads:
            avg_spread = sum(spreads) / len(spreads)
            signals["forecast_disagreement"] = max(-1.0, min(1.0, avg_spread / 3.0))
        else:
            signals["forecast_disagreement"] = 0.15
    else:
        signals["forecast_disagreement"] = 0.0

    # 8. slp_anomaly
    if slp_mb is not None:
        signals["slp_anomaly"] = max(-1.0, min(1.0, (slp_mb - 1013.25) / 20.0))
    else:
        signals["slp_anomaly"] = 0.0

    # 9. gust_anomaly (NEW lever - wind_gust_kt strength + delta)
    gust_sig = 0.0
    if gust_kt is not None:
        # normalize gust (typical 0-40 kt range)
        gust_norm = min(1.0, gust_kt / 35.0)
        if prev_gust is not None:
            gust_delta = gust_kt - prev_gust
            # stronger positive delta = more mixing / warmer bias
            if gust_delta > 8:
                gust_sig = 0.6
            elif gust_delta < -8:
                gust_sig = -0.5
            else:
                gust_sig = gust_norm * 0.4 + (gust_delta / 20.0)
        else:
            gust_sig = gust_norm * 0.4
    signals["gust_anomaly"] = max(-1.0, min(1.0, gust_sig))

    return signals

signals_list = ["dtr_regime_filter","pressure","gaussian_v2","calendar_climatology","goldilocks","wind_advection","cloud_cover_modulation","forecast_disagreement","slp_anomaly","gust_anomaly"]
# --- Baseline with 10 signals ---
print("\n=== BASELINE (10 signals, equal weights, threshold 0.08) ===")
weights = {s: 1.0/10.0 for s in signals_list}
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

# --- 200-round calibration ---
print("\n=== 500-ROUND RANDOM SEARCH (10 signals with dtr_regime_filter) ===")
best = {"acc": 0, "pnl": 0, "weights": None, "threshold": None, "trades": 0}

for trial in range(500):  # Increase trials to 500 as required
    raw = [random.uniform(0.35, 2.3) for _ in range(10)]
    s = sum(raw)
    w = [x/s for x in raw]
    weights = dict(zip(["dtr_regime_filter","pressure","gaussian_v2","calendar_climatology","goldilocks","wind_advection","cloud_cover_modulation","forecast_disagreement","slp_anomaly","gust_anomaly"], w))
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

    acc = (correct / total_trades * 100) if total_trades > 80 else 0
    if acc > best["acc"] and total_trades > 80:
        best = {"acc": acc, "pnl": pnl_total, "weights": weights.copy(), "threshold": threshold, "trades": total_trades}

print(f"\n=== BEST 10-SIGNAL CONFIGURATION (500 trials with dtr_regime_filter) ===")
print(f"Accuracy: {round(best['acc'],2)}% | Trades: {best['trades']} | P&L: ${best['pnl']:,.0f}")
print(f"Threshold: {round(best['threshold'],4)}")
print("Weights:")
for s, w in sorted(best['weights'].items()):
    print(f"  {s}: {round(w,3)}")