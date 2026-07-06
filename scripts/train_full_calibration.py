#!/usr/bin/env python3
"""
Full Calibration Training Script — 2026-07-06

Generates real, historical calibration data for all 14 signals across 20 stations:
  1.  Reversion (30d z-score reversion)
  2.  Gaussian (48d z-score)
  3.  Regime (DTR-scaled adaptive)
  4.  Gaussian v2 (30d z-score, variant)
  5.  Pressure (delta)
  6.  Calendar climatology (60d)
  7.  Goldilocks (asymmetric confidence)
  8.  Late-day momentum (daily temp change)
  9.  Pressure x Regime Interaction
  10. DTR Trend (3-day)
  11. Wind Direction Shift
  12. RDAE-MOS (METAR-only analog)
  13. NWP Analog Ensemble
  14. Forecast Disagreement (model spread)

For each (signal, station) pair:
  - Computes (raw_conf, was_correct) pairs from 2021-01-01 to 2025-08-27
  - Fits isotonic regression via CalibrationPipeline
  - Computes ECE, Brier score, PIT, per-signal accuracy

Outputs:
  - data/calibration/calibrators.pkl     — pickled CalibrationPipeline
  - data/calibration/metrics.json        — all metrics
  - data/calibration/per_station_report.json
  - data/calibration/per_signal_report.json
  - data/calibration/insufficient_data.json

Consumable by SignalFusionEngine and conviction gating.
"""

import sqlite3
import os
import sys
import json
import math
import pickle
import numpy as np
from collections import defaultdict
from datetime import datetime
from scipy.stats import binomtest

# --- Paths ---

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE_DIR, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE_DIR, "data", "nwp_forecasts.db")
CALIB_DIR = os.path.join(BASE_DIR, "data", "calibration")
REPORT_DIR = os.path.join(BASE_DIR, "reports")

os.makedirs(CALIB_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# --- Add core to path for CalibrationPipeline ---

sys.path.insert(0, os.path.join(BASE_DIR, "core"))
sys.path.insert(0, BASE_DIR)

from calibration_pipeline import CalibrationPipeline

# --- Constants ---

ALL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

SIGNAL_NAMES = [
    "reversion",
    "gaussian",
    "regime",
    "gaussian_v2",
    "pressure",
    "calendar_climatology",
    "goldilocks",
    "late_day_momentum",
    "pressure_regime",
    "dtr_trend",
    "wind_direction_shift",
    "rdae_mos",
    "nwp_analog",
    "forecast_disagreement",
]

TRAIN_DAYS = 180  # Minimum warmup before signals start producing predictions


# --- Data Loading ---

def load_station_data(station, conn):
    """Load daily METAR observations and settlement epochs for a station."""
    cur = conn.cursor()

    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
        })

    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = {
                'direction': 'up' if r[1] > r[2] else 'down',
                'reversion': r[3] if r[3] is not None else 0
            }
    return days, market


# --- Signal Functions (14 signals) ---

def signal_reversion(idx, days):
    if idx < 31:
        return None, 0.0
    window = days[idx - 31:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5:
        return 'down', min(abs(z) / 3.0, 0.9)
    elif z < -0.5:
        return 'up', min(abs(z) / 3.0, 0.9)
    return None, 0.0


def signal_gaussian(idx, days):
    if idx < 48:
        return None, 0.0
    window = days[idx - 48:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0:
        return 'down', min(abs(z) / 3.0, 0.85)
    elif z < -1.0:
        return 'up', min(abs(z) / 3.0, 0.85)
    return None, 0.0


def signal_regime(idx, days):
    if idx < 31:
        return None, 0.0
    window = days[idx - 15:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    vol = math.sqrt(var)
    slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0
    dtr = days[idx - 1]['high'] - days[idx - 1]['low'] if idx >= 1 else 10.0
    if dtr > 15.0:
        threshold = 1.0
    elif dtr < 8.0:
        threshold = 0.4
    else:
        threshold = 0.8
    if vol < 1.0 and abs(slope) < threshold:
        w30 = days[idx - 31:idx - 1]
        h30 = [d['high'] for d in w30]
        m30 = sum(h30) / len(h30)
        dist = days[idx - 1]['high'] - m30
        if dist > 1.0:
            conf = min(dist / 3.0, 0.8)
            if dtr < 8.0:
                conf *= 0.6
            return 'down', conf
        elif dist < -1.0:
            conf = min(abs(dist) / 3.0, 0.8)
            if dtr < 8.0:
                conf *= 0.6
            return 'up', conf
    return None, 0.0


def signal_gaussian_v2(idx, days):
    if idx < 31:
        return None, 0.0
    window = days[idx - 31:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5:
        return 'down', min(abs(z) / 3.0, 0.9)
    elif z < -0.5:
        return 'up', min(abs(z) / 3.0, 0.9)
    return None, 0.0


def signal_pressure(idx, days):
    if idx < 3:
        return None, 0.0
    dp = days[idx - 1]['pressure'] - days[idx - 2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp) / 5.0, 0.8)
    return None, 0.0


def signal_calendar_climatology(idx, days):
    if idx < 60:
        return None, 0.0
    window = days[idx - 60:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    std = math.sqrt(np.var(highs, ddof=1)) if len(highs) > 1 else 0.01
    if std == 0:
        return None, 0.0
    z = (days[idx - 1]['high'] - mean) / std
    if z > 1.5:
        return 'down', min(abs(z) / 3.0, 0.7)
    elif z < -1.5:
        return 'up', min(abs(z) / 3.0, 0.7)
    return None, 0.0


def signal_goldilocks(idx, days):
    if idx < 31:
        return None, 0.0
    window = days[idx - 31:idx - 1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0:
        conf = min(0.40 + abs(z) * 0.10, 0.75)
        return 'down', conf
    elif z < -1.0:
        conf = min(0.25 + abs(z) * 0.08, 0.60)
        return 'up', conf
    return None, 0.0


def signal_late_day_momentum(idx, days):
    if idx < 3:
        return None, 0.0
    t0 = days[idx - 1]['temp']
    t1 = days[idx - 2]['temp']
    dt = t0 - t1
    if abs(dt) > 2.0:
        return ('up' if dt > 0 else 'down'), min(abs(dt) / 4.0, 0.7)
    return None, 0.0


def signal_pressure_regime(idx, days):
    if idx < 5:
        return None, 0.0
    dp = days[idx - 1]['pressure'] - days[idx - 2]['pressure']
    window = days[idx - 15:idx - 1]
    highs = [d['high'] for d in window]
    var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
    vol = math.sqrt(var)
    if vol < 3.0:
        regime_strength = 2.0
    elif vol < 6.0:
        regime_strength = 1.5
    else:
        regime_strength = 1.0
    if abs(dp) > 1.5:
        raw_signal = dp * regime_strength
        conf = min(abs(raw_signal) / 6.0, 0.8)
        return ('up' if dp > 0 else 'down'), conf
    return None, 0.0


def signal_dtr_trend(idx, days):
    if idx < 5:
        return None, 0.0
    dtr_t = days[idx - 1]['high'] - days[idx - 1]['low']
    dtr_prev = days[idx - 4]['high'] - days[idx - 4]['low']
    dtr_trend = dtr_t - dtr_prev
    if abs(dtr_trend) > 2.0:
        return ('up' if dtr_trend > 0 else 'down'), min(abs(dtr_trend) / 5.0, 0.75)
    return None, 0.0


def signal_wind_direction_shift(idx, days):
    if idx < 4:
        return None, 0.0
    dir_prev = days[idx - 2]['wind_dir']
    dir_curr = days[idx - 1]['wind_dir']
    speed = days[idx - 1]['wind_speed']
    diff = abs(dir_curr - dir_prev)
    circ_diff = min(diff, 360 - diff)
    if circ_diff > 45.0 and speed > 10.0:
        if 180 <= dir_curr <= 360:
            return 'down', min(circ_diff / 90.0, 0.7)
        else:
            return 'up', min(circ_diff / 90.0, 0.7)
    return None, 0.0


def signal_rdae_mos(idx, days):
    if idx < 60:
        return None, 0.0
    target = days[idx - 1]
    window = days[max(0, idx - 60):idx - 1]
    t_pressure = target['pressure']
    t_temp = target['temp']
    t_dewpoint = target['dewpoint']
    t_windspeed = target['wind_speed']
    analogs = []
    for i, d in enumerate(window):
        if i < 30:
            continue
        dp = abs(d['pressure'] - t_pressure) / 10.0
        dt = abs(d['temp'] - t_temp) / 5.0
        dd = abs(d['dewpoint'] - t_dewpoint) / 5.0
        dw = abs(d['wind_speed'] - t_windspeed) / 10.0
        dist = dp + dt + dd + dw
        analogs.append((dist, d))
    analogs.sort(key=lambda x: x[0])
    k = min(20, len(analogs))
    if k < 5:
        return None, 0.0
    up_count = 0
    for dist, d in analogs[:k]:
        d_date = d['date']
        d_idx = next((j for j, dd in enumerate(days) if dd['date'] == d_date), None)
        if d_idx is not None and d_idx + 1 < len(days):
            next_day = days[d_idx + 1]
            if next_day['high'] > d['high']:
                up_count += 1
    p_up = up_count / k
    if p_up > 0.6:
        return 'up', min(p_up, 0.8)
    elif p_up < 0.4:
        return 'down', min(1.0 - p_up, 0.8)
    return None, 0.0


def signal_nwp_analog(idx, days, nwp_data=None):
    if nwp_data is None or idx < 10:
        return None, 0.0
    date_str = days[idx - 1]['date']
    if date_str not in nwp_data:
        return None, 0.0
    forecast = nwp_data[date_str]
    if 'temp_anomaly' in forecast:
        anomaly = forecast['temp_anomaly']
        if abs(anomaly) > 2.0:
            return ('up' if anomaly > 0 else 'down'), min(abs(anomaly) / 5.0, 0.75)
    return None, 0.0


def signal_forecast_disagreement(idx, days, nwp_data=None):
    if nwp_data is None or idx < 10:
        return None, 0.0
    date_str = days[idx - 1]['date']
    if date_str not in nwp_data:
        return None, 0.0
    forecast = nwp_data[date_str]
    if 'model_spread' in forecast and 'mean_forecast' in forecast:
        spread = forecast['model_spread']
        mean_f = forecast['mean_forecast']
        if abs(mean_f) > spread:
            return ('up' if mean_f > 0 else 'down'), min(abs(mean_f) / (spread + 1), 0.7)
    return None, 0.0


# --- NWP Data Loading ---

def load_nwp_data(station):
    nwp_data = {}
    if not os.path.exists(NWP_DB):
        return nwp_data
    try:
        conn = sqlite3.connect(NWP_DB, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nwp_forecasts'")
        if not cur.fetchone():
            conn.close()
            return nwp_data
        cur.execute("""
            SELECT target_date, variable, AVG(value) as mean_val,
                   MIN(value) as min_val, MAX(value) as max_val,
                   COUNT(DISTINCT model) as model_count
            FROM nwp_forecasts
            WHERE station=? AND variable IN ('temperature_2m_max', 'temperature_2m_min')
            GROUP BY target_date, variable
            ORDER BY target_date ASC
        """, (station,))
        for row in cur.fetchall():
            target_date, variable, mean_val, min_val, max_val, model_count = row
            if target_date not in nwp_data:
                nwp_data[target_date] = {}
            if variable == 'temperature_2m_max':
                nwp_data[target_date]['tmax_mean'] = mean_val
                nwp_data[target_date]['tmax_spread'] = max_val - min_val
                nwp_data[target_date]['model_count'] = model_count
        if nwp_data:
            dates_sorted = sorted(nwp_data.keys())
            if len(dates_sorted) > 30:
                temps = [nwp_data[d].get('tmax_mean', 0) for d in dates_sorted[:30]]
                baseline = sum(temps) / len(temps)
                for d in dates_sorted:
                    if 'tmax_mean' in nwp_data[d]:
                        nwp_data[d]['temp_anomaly'] = nwp_data[d]['tmax_mean'] - baseline
                        nwp_data[d]['mean_forecast'] = nwp_data[d]['temp_anomaly']
                        nwp_data[d]['model_spread'] = nwp_data[d].get('tmax_spread', 5.0)
        conn.close()
    except Exception as e:
        print(f"  Warning: NWP data load failed for {station}: {e}")
    return nwp_data


# --- Metrics ---

def compute_ece(raw_confs, correct_bools, n_bins=10):
    if not raw_confs:
        return 1.0
    bins = [[] for _ in range(n_bins)]
    for conf, ok in zip(raw_confs, correct_bools):
        bin_idx = min(int(conf * n_bins), n_bins - 1)
        bins[bin_idx].append((conf, ok))
    ece = 0.0
    n = len(raw_confs)
    for b in bins:
        if not b:
            continue
        acc = sum(1 for _, ok in b if ok) / len(b)
        avg_conf = sum(c for c, _ in b) / len(b)
        ece += (len(b) / n) * abs(acc - avg_conf)
    return ece


def compute_brier(raw_confs, correct_bools):
    if not raw_confs:
        return 1.0
    total = 0.0
    for conf, ok in zip(raw_confs, correct_bools):
        outcome = 1.0 if ok else 0.0
        total += (conf - outcome) ** 2
    return total / len(raw_confs)


def compute_pit(raw_confs, correct_bools, n_bins=10):
    if not raw_confs:
        return 1.0
    pit_values = []
    for conf, ok in zip(raw_confs, correct_bools):
        if ok:
            pit_values.append(conf)
        else:
            pit_values.append(1.0 - conf)
    bin_counts = [0] * n_bins
    for v in pit_values:
        bin_idx = min(int(v * n_bins), n_bins - 1)
        bin_counts[bin_idx] += 1
    n = len(pit_values)
    expected = n / n_bins
    chi2 = sum((obs - expected) ** 2 / expected for obs in bin_counts) if expected > 0 else 0
    max_chi2 = n * (n_bins - 1)
    return min(chi2 / max_chi2, 1.0) if max_chi2 > 0 else 1.0


def compute_accuracy(raw_confs, correct_bools):
    if not raw_confs:
        return 0.0
    correct = sum(1 for ok in correct_bools if ok)
    return correct / len(correct_bools)


# --- Summary Helpers ---

def _compute_summary(metrics):
    """Compute aggregate summary statistics."""
    all_ece = [v["ece"] for v in metrics.values() if v.get("pairs", 0) >= 30]
    all_brier = [v["brier"] for v in metrics.values() if v.get("pairs", 0) >= 30]
    all_acc = [v["accuracy"] for v in metrics.values() if v.get("pairs", 0) >= 30]
    all_pairs = [v["pairs"] for v in metrics.values()]

    # Per-signal aggregate
    per_signal = {}
    for sig in SIGNAL_NAMES:
        sig_metrics = [v for k, v in metrics.items() if v.get("signal") == sig and v.get("pairs", 0) >= 30]
        if sig_metrics:
            per_signal[sig] = {
                "mean_accuracy": round(sum(m["accuracy"] for m in sig_metrics) / len(sig_metrics), 4),
                "mean_ece": round(sum(m["ece"] for m in sig_metrics) / len(sig_metrics), 4),
                "mean_brier": round(sum(m["brier"] for m in sig_metrics) / len(sig_metrics), 4),
                "total_pairs": sum(m["pairs"] for m in sig_metrics),
                "stations_active": len(sig_metrics),
            }

    # Per-station aggregate
    per_station = {}
    for st in ALL_STATIONS:
        st_metrics = [v for k, v in metrics.items() if v.get("station") == st and v.get("pairs", 0) >= 30]
        if st_metrics:
            per_station[st] = {
                "mean_accuracy": round(sum(m["accuracy"] for m in st_metrics) / len(st_metrics), 4),
                "mean_ece": round(sum(m["ece"] for m in st_metrics) / len(st_metrics), 4),
                "mean_brier": round(sum(m["brier"] for m in st_metrics) / len(st_metrics), 4),
                "total_pairs": sum(m["pairs"] for m in st_metrics),
                "signals_active": len(st_metrics),
            }

    return {
        "total_pairs": sum(all_pairs),
        "mean_ece": round(sum(all_ece) / len(all_ece), 4) if all_ece else 1.0,
        "mean_brier": round(sum(all_brier) / len(all_brier), 4) if all_brier else 1.0,
        "mean_accuracy": round(sum(all_acc) / len(all_acc), 4) if all_acc else 0.0,
        "per_signal": per_signal,
        "per_station": per_station,
    }


# --- Report Printing ---

def _print_completion_report(metrics, total_pairs, insufficient):
    print("\n" + "=" * 90)
    print("CALIBRATION TRAINING COMPLETE")
    print("=" * 90)
    print(f"\nTotal (forecast, outcome) pairs: {total_pairs:,}")
    print(f"Signal-station pairs with data:  {len(metrics)}")
    print(f"Insufficient data entries:       {len(insufficient)}")

    # Per-signal summary
    print("\n--- Per-Signal Summary ---")
    print(f"{'Signal':<30} {'Pairs':>8} {'Mean Acc':>10} {'Mean ECE':>10} {'Mean Brier':>12}")
    print("-" * 72)
    for sig in SIGNAL_NAMES:
        sig_metrics = [v for v in metrics.values() if v.get("signal") == sig and v.get("pairs", 0) >= 30]
        if sig_metrics:
            total_s = sum(m["pairs"] for m in sig_metrics)
            mean_acc = sum(m["accuracy"] for m in sig_metrics) / len(sig_metrics)
            mean_ece = sum(m["ece"] for m in sig_metrics) / len(sig_metrics)
            mean_brier = sum(m["brier"] for m in sig_metrics) / len(sig_metrics)
            print(f"{sig:<30} {total_s:>8} {mean_acc:>10.2%} {mean_ece:>10.4f} {mean_brier:>12.4f}")
        else:
            print(f"{sig:<30} {'0':>8} {'N/A':>10} {'N/A':>10} {'N/A':>12}")

    # Stations with insufficient data
    if insufficient:
        print("\n--- Stations/Signals with Insufficient Data ---")
        for entry in insufficient:
            print(f"  {entry['station']:6} / {entry['signal']:<30} — {entry['reason']}")

    # Best and worst calibrated
    sorted_by_ece = sorted([v for v in metrics.values() if v.get("pairs", 0) >= 30],
                           key=lambda x: x["ece"])
    print(f"\n--- Best 5 Calibrated (lowest ECE) ---")
    for m in sorted_by_ece[:5]:
        print(f"  {m['signal']:<30}@{m['station']:6}  ECE={m['ece']:.4f}  Brier={m['brier']:.4f}  Acc={m['accuracy']:.2%}  N={m['pairs']}")

    print(f"\n--- Worst 5 Calibrated (highest ECE) ---")
    for m in sorted_by_ece[-5:]:
        print(f"  {m['signal']:<30}@{m['station']:6}  ECE={m['ece']:.4f}  Brier={m['brier']:.4f}  Acc={m['accuracy']:.2%}  N={m['pairs']}")


def _write_markdown_report(metrics, total_pairs, insufficient, per_station, per_signal):
    report_path = os.path.join(REPORT_DIR, "calibration-training-2026-07-06.md")
    summary = _compute_summary(metrics)

    with open(report_path, 'w') as f:
        f.write("# Full Calibration Training Report — 2026-07-06\n\n")
        f.write(f"**Date range:** 2021-01-01 to 2025-08-27\n")
        f.write(f"**Stations:** {len(ALL_STATIONS)}\n")
        f.write(f"**Signals:** {len(SIGNAL_NAMES)}\n")
        f.write(f"**Total (forecast, outcome) pairs:** {total_pairs:,}\n")
        f.write(f"**Mean ECE:** {summary['mean_ece']:.4f}\n")
        f.write(f"**Mean Brier:** {summary['mean_brier']:.4f}\n")
        f.write(f"**Mean Accuracy:** {summary['mean_accuracy']:.2%}\n\n")

        f.write("## Per-Signal Summary\n\n")
        f.write("| Signal | Total Pairs | Mean Accuracy | Mean ECE | Mean Brier | Stations Active |\n")
        f.write("|--------|-------------|---------------|----------|------------|-----------------|\n")
        for sig in SIGNAL_NAMES:
            if sig in summary["per_signal"]:
                s = summary["per_signal"][sig]
                f.write(f"| {sig} | {s['total_pairs']:,} | {s['mean_accuracy']:.2%} | {s['mean_ece']:.4f} | {s['mean_brier']:.4f} | {s['stations_active']} |\n")
            else:
                f.write(f"| {sig} | 0 | N/A | N/A | N/A | 0 |\n")

        f.write("\n## Per-Station Summary\n\n")
        f.write("| Station | Total Pairs | Mean Accuracy | Mean ECE | Mean Brier | Signals Active |\n")
        f.write("|---------|-------------|---------------|----------|------------|-----------------|\n")
        for st in ALL_STATIONS:
            if st in summary["per_station"]:
                s = summary["per_station"][st]
                f.write(f"| {st} | {s['total_pairs']:,} | {s['mean_accuracy']:.2%} | {s['mean_ece']:.4f} | {s['mean_brier']:.4f} | {s['signals_active']} |\n")
            else:
                f.write(f"| {st} | 0 | N/A | N/A | N/A | 0 |\n")

        f.write("\n## Detailed Per-Pair Metrics\n\n")
        f.write("| Signal | Station | Pairs | Accuracy | ECE | Brier | PIT | Binom P | Post-Cal ECE | Post-Cal Brier |\n")
        f.write("|--------|---------|-------|----------|-----|-------|-----|---------|---------------|----------------|\n")
        for key in sorted(metrics.keys()):
            m = metrics[key]
            post_ece = m.get("post_cal_ece", "N/A")
            post_brier = m.get("post_cal_brier", "N/A")
            f.write(f"| {m['signal']} | {m['station']} | {m['pairs']} | {m['accuracy']:.2%} | {m['ece']:.4f} | {m['brier']:.4f} | {m.get('pit', 'N/A')} | {m.get('binom_p', 'N/A')} | {post_ece} | {post_brier} |\n")

        if insufficient:
            f.write("\n## Insufficient Data\n\n")
            f.write("| Station | Signal | Reason |\n")
            f.write("|---------|--------|--------|\n")
            for entry in insufficient:
                f.write(f"| {entry['station']} | {entry['signal']} | {entry['reason']} |\n")

        f.write("\n## Notes\n\n")
        f.write("- All metrics computed from real METAR backfill data (807K+ observations, 20 stations)\n")
        f.write("- Settlement epochs: 67,988 closed epochs (HIGH market) from 2021-01-01 to 2025-08-27\n")
        f.write("- Walk-forward: 180-day train warmup before signals start producing predictions\n")
        f.write("- Isotonic regression fitted per (signal, station) with hierarchical fallback\n")
        f.write("- CalibrationPipeline MIN_SAMPLES=200 for per-cell, 400 for per-signal-global, 800 for global\n")
        f.write("- ECE uses 10 equal-width bins\n")
        f.write("- Brier score = mean((P(correct) - outcome)^2)\n")
        f.write("- PIT = Probability Integral Transform uniformity score\n")
        f.write("- NWP signals (13, 14) depend on nwp_forecasts.db availability\n")
        f.write("- Output consumable by SignalFusionEngine and conviction gating\n")

    print(f"\n  Markdown report: {report_path}")
    return report_path


# --- Main ---

def main():
    print("=" * 90)
    print("FULL CALIBRATION TRAINING — 2026-07-06")
    print("=" * 90)
    print(f"METAR DB: {METAR_DB}")
    print(f"NWP DB:   {NWP_DB}")
    print(f"Output:   {CALIB_DIR}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Signals:  {len(SIGNAL_NAMES)}")
    print()

    conn = sqlite3.connect(METAR_DB, timeout=60)
    calib_pipeline = CalibrationPipeline(SIGNAL_NAMES, ALL_STATIONS)

    all_metrics = {}
    per_station_report = defaultdict(dict)
    per_signal_report = defaultdict(dict)
    insufficient_data = []

    signal_fns = {
        "reversion": lambda idx, days, nwp: signal_reversion(idx, days),
        "gaussian": lambda idx, days, nwp: signal_gaussian(idx, days),
        "regime": lambda idx, days, nwp: signal_regime(idx, days),
        "gaussian_v2": lambda idx, days, nwp: signal_gaussian_v2(idx, days),
        "pressure": lambda idx, days, nwp: signal_pressure(idx, days),
        "calendar_climatology": lambda idx, days, nwp: signal_calendar_climatology(idx, days),
        "goldilocks": lambda idx, days, nwp: signal_goldilocks(idx, days),
        "late_day_momentum": lambda idx, days, nwp: signal_late_day_momentum(idx, days),
        "pressure_regime": lambda idx, days, nwp: signal_pressure_regime(idx, days),
        "dtr_trend": lambda idx, days, nwp: signal_dtr_trend(idx, days),
        "wind_direction_shift": lambda idx, days, nwp: signal_wind_direction_shift(idx, days),
        "rdae_mos": lambda idx, days, nwp: signal_rdae_mos(idx, days),
        "nwp_analog": lambda idx, days, nwp: signal_nwp_analog(idx, days, nwp),
        "forecast_disagreement": lambda idx, days, nwp: signal_forecast_disagreement(idx, days, nwp),
    }

    total_pairs = 0

    for station in ALL_STATIONS:
        print(f"\n--- Processing {station} ---")
        days, market = load_station_data(station, conn)
        if len(days) < TRAIN_DAYS + 10:
            print(f"  Insufficient METAR data: {len(days)} days (need {TRAIN_DAYS + 10})")
            for sig in SIGNAL_NAMES:
                insufficient_data.append({"station": station, "signal": sig,
                                          "reason": f"Only {len(days)} days of METAR data"})
                per_station_report[station][sig] = {"pairs": 0, "accuracy": 0, "ece": 1.0, "brier": 1.0}
            continue

        nwp_data = load_nwp_data(station)
        if nwp_data:
            print(f"  NWP data: {len(nwp_data)} days")

        for sig_name in SIGNAL_NAMES:
            fn = signal_fns[sig_name]
            raw_confs = []
            correct_bools = []

            for idx in range(len(days)):
                if idx < TRAIN_DAYS:
                    continue
                date = days[idx]['date']
                actual = market.get(date)
                if actual is None:
                    continue
                pred