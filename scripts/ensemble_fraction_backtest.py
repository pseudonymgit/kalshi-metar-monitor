#!/usr/bin/env python3
"""
Ensemble Fraction Backtest — GEFS 31-member temperature exceedance probability.

Methodology (suislanchez/trading-bot proven approach):
  1. Pull 31 GEFS ensemble members from Open-Meteo ensemble API (free, no key)
  2. For each Kalshi temperature threshold, count fraction of members above/below → model probability
  3. Score against actual Kalshi settlement temperature
  4. Report: accuracy, Brier score, calibration, edge analysis

Usage:
    python3 scripts/ensemble_fraction_backtest.py

Output:
    - data/ensemble_fraction_results.json
    - Console summary with per-station and overall metrics
    - Comparison with existing signal methods from signal_accuracy_report.json

Rate limiting:
    - 1.0s delay between Open-Meteo ensemble API requests
    - Samples every 5th day per station to stay within ~10K/day free limit
    - ~500-600 total requests across all stations
"""

import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

# ─── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, DATA_DIR)

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
logger = logging.getLogger("ensemble_fraction")

# ─── Constants ─────────────────────────────────────────────────────────────────

ENSEMBLE_API_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
REQUEST_TIMEOUT = 30
RATE_LIMIT_SLEEP = 1.0  # seconds between API requests
USER_AGENT = "WeatherEngine-EnsembleFraction/1.0"

# Kalshi HIGH temperature thresholds are at 2°F intervals.
# Standard thresholds: 70, 72, ..., 110°F (21 thresholds)
TEMP_THRESHOLDS = list(range(70, 112, 2))

# Fee model from sweep config
ROUND_TRIP_FEE = 0.0205

# The Open-Meteo GEFS ensemble API only stores the last ~5 days of historical forecasts
# (plus ~16 days of future forecasts). For historical GEFS data, we'd need the Copernicus CDS
# or NOAA NOMADS archive. Until then, only run on dates the API has data for.
GEFS_START_DATE = "2026-07-24"  # earliest date with non-null ensemble member data

# Sampling: every N-th day per station to stay within rate limits
# With only ~4 days of data, no sampling needed
SAMPLE_EVERY_N = 1

# Station mapping path
STATION_MAPPING_PATH = os.path.join(DATA_DIR, "station_mapping.json")


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════


def load_station_mapping() -> dict:
    """Load station → lat/lon mapping."""
    with open(STATION_MAPPING_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict) and "stations" in data:
        return data["stations"]
    return data


def load_kalshi_data() -> List[dict]:
    """Load Kalshi settlement data from the backfill file."""
    path = os.path.join(DATA_DIR, "kalshi_backfill_complete.json")
    with open(path) as f:
        return json.load(f)


def load_existing_signal_accuracy() -> dict:
    """Load existing signal accuracy report for comparison."""
    path = os.path.join(DATA_DIR, "signal_accuracy_report.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def query_ensemble_forecast(
    lat: float, lon: float, date_str: str
) -> Optional[Dict]:
    """
    Query the Open-Meteo GEFS ensemble API for a single station+date.

    The GEFS ensemble has 31 members (1 control + 30 perturbations).

    Args:
        lat: Latitude
        lon: Longitude
        date_str: Date in YYYY-MM-DD format

    Returns:
        Dict with 'members_f' (list of 31 floats in °F), 'mean_f', 'std_f',
        or None on failure.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "gfs_seamless",
        "timezone": "America/New_York",
        "start_date": date_str,
        "end_date": date_str,
    }

    try:
        resp = requests.get(
            ENSEMBLE_API_BASE,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        time.sleep(RATE_LIMIT_SLEEP)

        if resp.status_code != 200:
            logger.warning(
                "Open-Meteo API error %d for lat=%.4f lon=%.4f date=%s: %s",
                resp.status_code, lat, lon, date_str, resp.text[:200],
            )
            return None

        data = resp.json()
        daily = data.get("daily", {})
        if not daily:
            logger.warning("No daily data for lat=%.4f lon=%.4f date=%s", lat, lon, date_str)
            return None

        # Collect all 31 member temperatures in °C
        members_c = []

        # Main (control) member
        main_temps = daily.get("temperature_2m_max", [])
        if main_temps and len(main_temps) > 0 and main_temps[0] is not None:
            members_c.append(main_temps[0])

        # Perturbation members (member01 through member30)
        for i in range(1, 31):
            key = f"temperature_2m_max_member{i:02d}"
            temps = daily.get(key, [])
            if temps and len(temps) > 0 and temps[0] is not None:
                members_c.append(temps[0])

        if len(members_c) < 2:
            logger.warning(
                "Too few ensemble members (%d) for lat=%.4f lon=%.4f date=%s",
                len(members_c), lat, lon, date_str,
            )
            return None

        # Convert to Fahrenheit
        members_f = [celsius_to_fahrenheit(c) for c in members_c]
        mean_f = sum(members_f) / len(members_f)
        variance = sum((m - mean_f) ** 2 for m in members_f) / len(members_f)
        std_f = math.sqrt(variance)

        return {
            "members_f": members_f,
            "mean_f": round(mean_f, 2),
            "std_f": round(std_f, 2),
            "n_members": len(members_f),
        }

    except requests.exceptions.Timeout:
        logger.warning("Timeout for lat=%.4f lon=%.4f date=%s", lat, lon, date_str)
        return None
    except requests.exceptions.ConnectionError as e:
        logger.warning("Connection error for lat=%.4f lon=%.4f date=%s: %s", lat, lon, date_str, e)
        return None
    except Exception as e:
        logger.error("Unexpected error for lat=%.4f lon=%.4f date=%s: %s", lat, lon, date_str, e)
        return None


def compute_ensemble_fraction(
    members_f: List[float], threshold_f: int
) -> float:
    """Compute fraction of ensemble members exceeding a temperature threshold."""
    if not members_f:
        return 0.5
    count_above = sum(1 for m in members_f if m > threshold_f)
    return count_above / len(members_f)


def compute_exceedance_probabilities(
    members_f: List[float],
) -> Dict[int, float]:
    """Compute exceedance probability for each Kalshi threshold."""
    result = {}
    for threshold in TEMP_THRESHOLDS:
        frac = compute_ensemble_fraction(members_f, threshold)
        result[threshold] = round(frac, 4)
    return result


def score_probability_forecast(
    prob_above: float, actually_above: bool
) -> Dict:
    """Score a single probability forecast (Brier, log loss, directional)."""
    outcome = 1.0 if actually_above else 0.0
    eps = 1e-15
    p = max(eps, min(1 - eps, prob_above))
    brier = (p - outcome) ** 2
    log_loss = -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))
    direction_correct = (p > 0.5) == actually_above
    edge = p - 0.5
    return {
        "brier": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "directional_correct": direction_correct,
        "edge_vs_50pct": round(edge, 4),
    }


def edge_after_fees(prob_above: float, market_price: float = 0.5) -> float:
    """Compute edge = model_prob - market_price - fee."""
    return prob_above - market_price - ROUND_TRIP_FEE


# ═════════════════════════════════════════════════════════════════════════════
# Core Backtest Logic
# ═════════════════════════════════════════════════════════════════════════════


def run_backtest(
    kalshi_data: List[dict],
    station_mapping: dict,
    sample_every: int = SAMPLE_EVERY_N,
    max_requests: Optional[int] = None,
) -> dict:
    """
    Run the ensemble fraction backtest.

    For each station+date pair (sampled), query the GEFS ensemble, compute
    exceedance probabilities for each threshold, and score against actual
    settlement temperature.

    Args:
        kalshi_data: List of Kalshi settlement records
        station_mapping: Dict of station -> {lat, lon}
        sample_every: Sample every N-th day per station
        max_requests: Max total API requests (for testing)

    Returns:
        Dict with results
    """
    # Group by station, filtering to dates within GEFS forecast range
    by_station = defaultdict(list)
    for rec in kalshi_data:
        station = rec.get("station")
        target_date = rec.get("target_date")
        kalshi_temp = rec.get("kalshi_temp")
        if station and target_date and kalshi_temp is not None:
            if target_date >= GEFS_START_DATE:
                by_station[station].append({
                    "date": target_date,
                    "kalshi_temp": float(kalshi_temp),
                    "event_ticker": rec.get("event_ticker", ""),
                })

    # Sort each station's records by date and sample every N-th day
    sampled = {}
    for station in by_station:
        by_station[station].sort(key=lambda r: r["date"])
        sampled[station] = by_station[station][::sample_every]

    total_available = sum(len(v) for v in by_station.values())
    total_sampled = sum(len(v) for v in sampled.values())
    logger.info(
        "Total available: %d, sampled (every %dth): %d",
        total_available, sample_every, total_sampled,
    )

    all_results = []
    total_requests = 0
    total_errors = 0
    station_results = {}

    # Process stations in order of data volume (descending)
    station_order = sorted(
        sampled.keys(),
        key=lambda s: -len(sampled.get(s, []))
    )

    for station in station_order:
        records = sampled[station]
        station_info = station_mapping.get(station)
        if not station_info:
            logger.warning("No mapping for station %s, skipping", station)
            continue

        lat = station_info.get("lat")
        lon = station_info.get("lon")
        if lat is None or lon is None:
            logger.warning("No lat/lon for station %s, skipping", station)
            continue

        logger.info("Processing station %s (%d sampled dates)", station, len(records))

        station_records = []
        station_errors = 0
        station_requests = 0

        for rec in records:
            if max_requests is not None and total_requests >= max_requests:
                logger.info("Reached max_requests=%d, stopping", max_requests)
                break

            date_str = rec["date"]
            kalshi_temp = rec["kalshi_temp"]

            # Query ensemble
            ensemble = query_ensemble_forecast(lat, lon, date_str)
            total_requests += 1
            station_requests += 1

            if ensemble is None:
                station_errors += 1
                total_errors += 1
                all_results.append({
                    "station": station,
                    "date": date_str,
                    "kalshi_temp": kalshi_temp,
                    "ensemble_mean_f": None,
                    "ensemble_std_f": None,
                    "n_members": 0,
                    "error": "ensemble_fetch_failed",
                    "threshold_results": [],
                })
                continue

            members_f = ensemble["members_f"]

            # Compute exceedance probabilities for all thresholds
            exceedance_probs = compute_exceedance_probabilities(members_f)

            # Score each threshold
            threshold_results = []
            for threshold, prob_above in sorted(exceedance_probs.items()):
                actually_above = kalshi_temp >= threshold
                score = score_probability_forecast(prob_above, actually_above)
                edge = edge_after_fees(prob_above, 0.5)

                threshold_results.append({
                    "threshold_f": threshold,
                    "prob_above": prob_above,
                    "actually_above": actually_above,
                    "brier": score["brier"],
                    "log_loss": score["log_loss"],
                    "directional_correct": score["directional_correct"],
                    "edge_vs_50pct": score["edge_vs_50pct"],
                    "edge_after_fees": round(edge, 4),
                })

            # Find the "best" threshold (closest to actual temp)
            best_threshold = None
            best_dist = float("inf")
            for tr in threshold_results:
                dist = abs(tr["threshold_f"] - kalshi_temp)
                if dist < best_dist:
                    best_dist = dist
                    best_threshold = tr

            record_result = {
                "station": station,
                "date": date_str,
                "kalshi_temp": kalshi_temp,
                "ensemble_mean_f": ensemble["mean_f"],
                "ensemble_std_f": ensemble["std_f"],
                "n_members": ensemble["n_members"],
                "error": None,
                "best_threshold": best_threshold,
                "threshold_results": threshold_results,
                "ensemble_members_f": [round(m, 1) for m in members_f],
            }
            all_results.append(record_result)
            station_records.append(record_result)

            if station_requests % 10 == 0:
                logger.info(
                    "  %s: %d/%d requests, %d errors",
                    station, station_requests, len(records), station_errors,
                )

        station_results[station] = compute_station_metrics(station, station_records)

        logger.info(
            "Station %s done: %d requests, %d errors",
            station, station_requests, station_errors,
        )

        if max_requests is not None and total_requests >= max_requests:
            break

    overall = compute_overall_metrics(all_results, station_results)

    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": "1.0",
            "method": "GEFS 31-member ensemble fraction exceedance probability",
            "api_source": ENSEMBLE_API_BASE,
            "sample_every_n_days": sample_every,
            "total_available": total_available,
            "total_sampled": total_sampled,
            "total_requests": total_requests,
            "total_errors": total_errors,
            "fee_model": f"{ROUND_TRIP_FEE*100:.2f}% round-trip",
            "thresholds": TEMP_THRESHOLDS,
        },
        "overall": overall,
        "per_station": station_results,
        "all_results": all_results,
    }


def compute_station_metrics(station: str, records: List[dict]) -> dict:
    """Compute per-station metrics from ensemble fraction results."""
    if not records:
        return {"station": station, "n_events": 0, "error": "no_data"}

    successful = [r for r in records if r["error"] is None]
    if not successful:
        return {
            "station": station, "n_events": len(records),
            "n_successful": 0, "error": "all_failed"
        }

    n = len(successful)

    # Temperature prediction errors
    errors = []
    actual_temps = []
    predicted_temps = []
    for r in successful:
        if r["ensemble_mean_f"] is not None:
            err = r["ensemble_mean_f"] - r["kalshi_temp"]
            errors.append(err)
            predicted_temps.append(r["ensemble_mean_f"])
            actual_temps.append(r["kalshi_temp"])

    if not errors:
        return {
            "station": station, "n_events": n,
            "n_scored": 0, "error": "no_predictions"
        }

    mae = sum(abs(e) for e in errors) / len(errors)
    bias = sum(errors) / len(errors)
    rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))

    # Correlation
    n_corr = len(actual_temps)
    if n_corr > 1:
        mean_a = sum(actual_temps) / n_corr
        mean_p = sum(predicted_temps) / n_corr
        num = sum((a - mean_a) * (p - mean_p) for a, p in zip(actual_temps, predicted_temps))
        denom_a = math.sqrt(sum((a - mean_a) ** 2 for a in actual_temps))
        denom_p = math.sqrt(sum((p - mean_p) ** 2 for p in predicted_temps))
        correlation = num / (denom_a * denom_p) if denom_a * denom_p > 0 else 0.0
    else:
        correlation = 0.0

    # Collect all threshold scores
    all_threshold_scores = []
    brier_scores = []
    log_losses = []
    edges = []

    for r in successful:
        for tr in r.get("threshold_results", []):
            all_threshold_scores.append(tr)
            brier_scores.append(tr["brier"])
            log_losses.append(tr["log_loss"])
            edges.append(tr["edge_after_fees"])

    # Best threshold metrics (closest to actual temp)
    best_threshold_results = []
    for r in successful:
        bt = r.get("best_threshold")
        if bt:
            best_threshold_results.append(bt)

    best_brier = (
        sum(bt["brier"] for bt in best_threshold_results) / len(best_threshold_results)
        if best_threshold_results else 0
    )
    best_log_loss = (
        sum(bt["log_loss"] for bt in best_threshold_results) / len(best_threshold_results)
        if best_threshold_results else 0
    )
    best_da = (
        sum(1 for bt in best_threshold_results if bt["directional_correct"])
        / len(best_threshold_results) if best_threshold_results else 0
    )
    best_avg_edge = (
        sum(bt["edge_after_fees"] for bt in best_threshold_results)
        / len(best_threshold_results) if best_threshold_results else 0
    )

    # Calibration analysis (binned by predicted probability)
    calibration_bins = {}
    for i in range(10):
        lo = i / 10.0
        hi = (i + 1) / 10.0
        calibration_bins[f"{lo:.1f}-{hi:.1f}"] = {"predicted": [], "actual": []}

    for tr in all_threshold_scores:
        p = tr["prob_above"]
        bin_idx = min(int(p * 10), 9)
        key = f"{bin_idx/10:.1f}-{(bin_idx+1)/10:.1f}"
        if key in calibration_bins:
            calibration_bins[key]["predicted"].append(p)
            calibration_bins[key]["actual"].append(1.0 if tr["actually_above"] else 0.0)

    calibration_entries = []
    ece = 0.0
    total_weight = 0
    for bin_key, bin_data in calibration_bins.items():
        n_in_bin = len(bin_data["predicted"])
        if n_in_bin == 0:
            continue
        avg_predicted = sum(bin_data["predicted"]) / n_in_bin
        freq_actual = sum(bin_data["actual"]) / n_in_bin
        cal_error = abs(avg_predicted - freq_actual)
        ece += cal_error * n_in_bin
        total_weight += n_in_bin
        calibration_entries.append({
            "bin": bin_key,
            "n": n_in_bin,
            "avg_predicted": round(avg_predicted, 4),
            "freq_actual": round(freq_actual, 4),
            "calibration_error": round(cal_error, 4),
        })
    if total_weight > 0:
        ece /= total_weight

    # Aggregate all-threshold metrics
    avg_brier = sum(brier_scores) / len(brier_scores) if brier_scores else 0.0
    avg_log_loss = sum(log_losses) / len(log_losses) if log_losses else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0
    positive_edge_count = sum(1 for e in edges if e > 0)
    positive_edge_pct = positive_edge_count / len(edges) if edges else 0.0

    # Directional accuracy across all thresholds
    all_da = sum(1 for tr in all_threshold_scores if tr["directional_correct"])
    all_da = all_da / len(all_threshold_scores) if all_threshold_scores else 0.0

    # Edge Sharpe
    if len(edges) > 1:
        mean_edge = sum(edges) / len(edges)
        var_edge = sum((e - mean_edge) ** 2 for e in edges) / (len(edges) - 1)
        std_edge = math.sqrt(var_edge) if var_edge > 0 else 1.0
        sharpe = mean_edge / std_edge
    else:
        sharpe = 0.0

    return {
        "station": station,
        "n_events": n,
        "n_threshold_evaluations": len(all_threshold_scores),
        "temperature_prediction": {
            "mae_f": round(mae, 2),
            "bias_f": round(bias, 2),
            "rmse_f": round(rmse, 2),
            "correlation": round(correlation, 4),
        },
        "best_threshold_metrics": {
            "n": len(best_threshold_results),
            "avg_brier": round(best_brier, 4),
            "avg_log_loss": round(best_log_loss, 4),
            "directional_accuracy": round(best_da, 4),
            "avg_edge_after_fees": round(best_avg_edge, 4),
        },
        "all_threshold_metrics": {
            "n": len(all_threshold_scores),
            "avg_brier": round(avg_brier, 4),
            "avg_log_loss": round(avg_log_loss, 4),
            "directional_accuracy": round(all_da, 4),
            "avg_edge_after_fees": round(avg_edge, 4),
            "positive_edge_fraction": round(positive_edge_pct, 4),
            "edge_sharpe": round(sharpe, 4),
            "ece": round(ece, 4),
        },
        "calibration": calibration_entries,
    }


def compute_overall_metrics(
    all_results: List[dict],
    station_results: Dict[str, dict],
) -> dict:
    """Compute overall metrics across all stations."""
    stations_with_data = {
        s: m for s, m in station_results.items()
        if m.get("n_events", 0) > 0 and "error" not in m
    }

    if not stations_with_data:
        return {"error": "no_data"}

    n_stations = len(stations_with_data)
    n_events = sum(m["n_events"] for m in stations_with_data.values())
    n_threshold_eval = sum(
        m.get("all_threshold_metrics", {}).get("n", 0)
        for m in stations_with_data.values()
    )
    total_weight = sum(m["n_events"] for m in stations_with_data.values())

    def wavg(key, subkey="temperature_prediction"):
        num = 0.0
        den = 0.0
        for m in stations_with_data.values():
            val = m.get(subkey, {}).get(key, 0)
            w = m["n_events"]
            num += val * w
            den += w
        return round(num / den, 4) if den > 0 else 0.0

    def wavg_best(key):
        return wavg(key, "best_threshold_metrics")

    def wavg_all(key):
        return wavg(key, "all_threshold_metrics")

    overall_temp = {
        "mae_f": wavg("mae_f"),
        "bias_f": wavg("bias_f"),
        "rmse_f": wavg("rmse_f"),
        "correlation": wavg("correlation"),
    }
    overall_best = {
        "avg_brier": wavg_best("avg_brier"),
        "avg_log_loss": wavg_best("avg_log_loss"),
        "directional_accuracy": wavg_best("directional_accuracy"),
        "avg_edge_after_fees": wavg_best("avg_edge_after_fees"),
    }
    overall_all = {
        "avg_brier": wavg_all("avg_brier"),
        "avg_log_loss": wavg_all("avg_log_loss"),
        "directional_accuracy": wavg_all("directional_accuracy"),
        "avg_edge_after_fees": wavg_all("avg_edge_after_fees"),
        "positive_edge_fraction": wavg_all("positive_edge_fraction"),
        "edge_sharpe": wavg_all("edge_sharpe"),
        "ece": wavg_all("ece"),
    }

    # Rank stations by directional accuracy
    by_da = sorted(
        stations_with_data.items(),
        key=lambda x: x[1].get("best_threshold_metrics", {}).get("directional_accuracy", 0),
        reverse=True,
    )
    top_stations = [
        {"station": s, "directional_accuracy": m["best_threshold_metrics"]["directional_accuracy"],
         "n_events": m["n_events"], "mae": m["temperature_prediction"]["mae_f"]}
        for s, m in by_da[:5]
    ]
    bottom_stations = [
        {"station": s, "directional_accuracy": m["best_threshold_metrics"]["directional_accuracy"],
         "n_events": m["n_events"], "mae": m["temperature_prediction"]["mae_f"]}
        for s, m in by_da[-5:]
    ]

    return {
        "n_stations": n_stations,
        "n_events": n_events,
        "n_threshold_evaluations": n_threshold_eval,
        "temperature_prediction": overall_temp,
        "best_threshold_metrics": overall_best,
        "all_threshold_metrics": overall_all,
        "top_stations": top_stations,
        "bottom_stations": bottom_stations,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Comparison with Existing Signal Methods
# ═════════════════════════════════════════════════════════════════════════════


def compare_with_existing_signals(ensemble_results: dict) -> dict:
    """
    Compare ensemble fraction results against existing signal methods.

    Loads the existing signal_accuracy_report.json and compares:
    - Directional accuracy per station
    - Which signals add value on top of ensemble fraction
    """
    existing = load_existing_signal_accuracy()
    if not existing:
        return {"error": "No existing signal accuracy report found"}

    comparison = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ensemble_method": "GEFS 31-member ensemble fraction exceedance probability",
        "existing_signal_method": "5-signal ensemble (gaussian, gaussian_v2, pressure_delta, forecast_disagreement, calendar_climatology)",
    }

    # Extract per-station per-signal accuracy
    signal_station_perf = existing.get("signal_station_performance", {})

    existing_by_station = {}
    for key, perf in signal_station_perf.items():
        station = perf.get("station")
        if not station:
            continue
        acc = perf.get("accuracy", 0.5)
        if station not in existing_by_station:
            existing_by_station[station] = {"accuracies": [], "signals": []}
        existing_by_station[station]["accuracies"].append(acc)
        existing_by_station[station]["signals"].append(perf.get("signal", "unknown"))

    existing_signal_avg = {}
    for station, data in existing_by_station.items():
        accs = data["accuracies"]
        existing_signal_avg[station] = {
            "avg_accuracy": sum(accs) / len(accs),
            "n_signals": len(accs),
            "best_accuracy": max(accs),
            "worst_accuracy": min(accs),
        }

    ensemble_per_station = ensemble_results.get("per_station", {})
    station_comparison = {}
    ensemble_better_count = 0
    ensemble_worse_count = 0
    comparable_count = 0
    accuracy_deltas = []

    for station, existing_data in existing_signal_avg.items():
        ensemble_data = ensemble_per_station.get(station, {})
        if "error" in ensemble_data:
            continue

        ensemble_acc = ensemble_data.get("best_threshold_metrics", {}).get("directional_accuracy", 0)
        existing_acc = existing_data["avg_accuracy"]
        best_existing = existing_data["best_accuracy"]

        delta_vs_avg = ensemble_acc - existing_acc
        delta_vs_best = ensemble_acc - best_existing
        accuracy_deltas.append(delta_vs_avg)

        if ensemble_acc > best_existing:
            ensemble_better_count += 1
        else:
            ensemble_worse_count += 1
        comparable_count += 1

        station_comparison[station] = {
            "ensemble_directional_accuracy": round(ensemble_acc, 4),
            "existing_signal_avg_accuracy": round(existing_acc, 4),
            "existing_signal_best_accuracy": round(best_existing, 4),
            "delta_vs_avg": round(delta_vs_avg, 4),
            "delta_vs_best": round(delta_vs_best, 4),
            "n_existing_signals": existing_data["n_signals"],
        }

    mean_delta = sum(accuracy_deltas) / len(accuracy_deltas) if accuracy_deltas else 0

    comparison["station_comparison"] = station_comparison
    comparison["summary"] = {
        "stations_comparable": comparable_count,
        "ensemble_better_than_best_signal": ensemble_better_count,
        "ensemble_worse_than_best_signal": ensemble_worse_count,
        "mean_accuracy_delta_vs_existing_avg": round(mean_delta, 4),
        "ensemble_win_rate": round(ensemble_better_count / comparable_count, 4) if comparable_count > 0 else 0,
    }

    return comparison


# ═════════════════════════════════════════════════════════════════════════════
# Report Generation
# ═════════════════════════════════════════════════════════════════════════════


def generate_report(results: dict, comparison: dict) -> str:
    """Generate a human-readable report from the backtest results."""
    meta = results.get("metadata", {})
    overall = results.get("overall", {})
    per_station = results.get("per_station", {})

    lines = []
    lines.append("=" * 70)
    lines.append("ENSEMBLE FRACTION BACKTEST REPORT")
    lines.append("=" * 70)
    lines.append(f"Generated: {meta.get('generated_at', 'unknown')}")
    lines.append(f"Method: {meta.get('method', 'unknown')}")
    lines.append(f"Source: {meta.get('api_source', 'unknown')}")
    lines.append(f"Fee model: {meta.get('fee_model', 'unknown')}")
    lines.append(f"Sample: every {meta.get('sample_every_n_days', 'N/A')}th day")
    lines.append(f"Total requests: {meta.get('total_requests', 0)}")
    lines.append(f"Total errors: {meta.get('total_errors', 0)}")
    lines.append("")

    if "error" in overall:
        lines.append(f"ERROR: {overall['error']}")
        return "\n".join(lines)

    # Temperature Prediction
    lines.append("-" * 70)
    lines.append("OVERALL TEMPERATURE PREDICTION (Ensemble Mean vs Actual Temp)")
    lines.append("-" * 70)
    temp = overall.get("temperature_prediction", {})
    lines.append(f"  MAE:  {temp.get('mae_f', 0):>6.2f} °F")
    lines.append(f"  Bias: {temp.get('bias_f', 0):>+6.2f} °F")
    lines.append(f"  RMSE: {temp.get('rmse_f', 0):>6.2f} °F")
    lines.append(f"  Corr: {temp.get('correlation', 0):>6.4f}")
    lines.append(f"  Stations: {overall.get('n_stations', 0)}")
    lines.append(f"  Events:   {overall.get('n_events', 0)}")
    lines.append("")

    # Best Threshold Metrics
    lines.append("-" * 70)
    lines.append("BEST THRESHOLD METRICS (closest threshold to actual temp)")
    lines.append("-" * 70)
    best = overall.get("best_threshold_metrics", {})
    lines.append(f"  Directional Accuracy: {best.get('directional_accuracy', 0)*100:>6.1f}%")
    lines.append(f"  Avg Brier:            {best.get('avg_brier', 0):>8.4f}")
    lines.append(f"  Avg Log Loss:         {best.get('avg_log_loss', 0):>8.4f}")
    lines.append(f"  Avg Edge (fees):      {best.get('avg_edge_after_fees', 0):>+8.4f}")
    lines.append("")

    # All Threshold Metrics
    lines.append("-" * 70)
    lines.append("ALL THRESHOLD METRICS (21 thresholds x events)")
    lines.append("-" * 70)
    all_m = overall.get("all_threshold_metrics", {})
    lines.append(f"  Threshold Eval: {overall.get('n_threshold_evaluations', 0):>8}")
    lines.append(f"  Directional:    {all_m.get('directional_accuracy', 0)*100:>8.1f}%")
    lines.append(f"  Avg Brier:      {all_m.get('avg_brier', 0):>8.4f}")
    lines.append(f"  Avg Log Loss:   {all_m.get('avg_log_loss', 0):>8.4f}")
    lines.append(f"  Avg Edge (fees):{all_m.get('avg_edge_after_fees', 0):>+8.4f}")
    lines.append(f"  Pos Edge Frac:  {all_m.get('positive_edge_fraction', 0)*100:>8.1f}%")
    lines.append(f"  Edge Sharpe:    {all_m.get('edge_sharpe', 0):>8.3f}")
    lines.append(f"  ECE:            {all_m.get('ece', 0):>8.4f}")
    lines.append("")

    # Edge Analysis
    lines.append("-" * 70)
    lines.append("EDGE ANALYSIS")
    lines.append("-" * 70)
    lines.append(f"  Fee: {ROUND_TRIP_FEE*100:.2f}% round-trip")
    lines.append(f"  Positive edge after fees: {all_m.get('positive_edge_fraction', 0)*100:.1f}% of threshold-events")
    lines.append(f"  Avg edge after fees: {all_m.get('avg_edge_after_fees', 0):+.4f}")
    lines.append(f"  Edge Sharpe: {all_m.get('edge_sharpe', 0):.3f}")
    lines.append("  (Edge vs 0.5 baseline — no market prices available historically)")
    lines.append("")

    # Calibration
    lines.append("-" * 70)
    lines.append("CALIBRATION (first station with data)")
    lines.append("-" * 70)
    for station, data in per_station.items():
        cal = data.get("calibration", [])
        if cal:
            lines.append(f"  Station: {station}")
            for entry in cal[:10]:
                if entry["n"] > 0:
                    lines.append(f"    {entry['bin']:>8}: n={entry['n']:>4}  pred={entry['avg_predicted']:.3f}  actual={entry['freq_actual']:.3f}  err={entry['calibration_error']:.3f}")
            break
    lines.append("")

    # Per-Station Summary Table
    lines.append("-" * 70)
    lines.append("PER-STATION SUMMARY")
    lines.append("-" * 70)
    lines.append(f"  {'Station':<8} {'Events':>6} {'MAE(F)':>7} {'Bias':>6} {'DirAcc':>7} {'Brier':>8} {'Edge':>8}")
    lines.append(f"  {'-'*8} {'-'*6} {'-'*7} {'-'*6} {'-'*7} {'-'*8} {'-'*8}")
    for station in sorted(per_station.keys()):
        data = per_station[station]
        if "error" in data:
            lines.append(f"  {station:<8} {'ERR':>6} {str(data.get('error', '')):>20}")
            continue
        n = data.get("n_events", 0)
        mae = data.get("temperature_prediction", {}).get("mae_f", 0)
        bias = data.get("temperature_prediction", {}).get("bias_f", 0)
        da = data.get("best_threshold_metrics", {}).get("directional_accuracy", 0)
        brier = data.get("best_threshold_metrics", {}).get("avg_brier", 0)
        edge = data.get("best_threshold_metrics", {}).get("avg_edge_after_fees", 0)
        lines.append(f"  {station:<8} {n:>6} {mae:>6.1f}F {bias:>+5.1f}F {da*100:>6.1f}% {brier:>8.4f} {edge:>+7.4f}")
    lines.append("")

    # Top/Bottom
    lines.append("-" * 70)
    lines.append("TOP 5 STATIONS (by best-threshold directional accuracy)")
    lines.append("-" * 70)
    for s in overall.get("top_stations", []):
        lines.append(f"  {s['station']:<8} acc={s['directional_accuracy']*100:.1f}%  n={s['n_events']}  MAE={s['mae']:.1f}F")
    lines.append("")
    lines.append("BOTTOM 5 STATIONS")
    for s in overall.get("bottom_stations", []):
        lines.append(f"  {s['station']:<8} acc={s['directional_accuracy']*100:.1f}%  n={s['n_events']}  MAE={s['mae']:.1f}F")
    lines.append("")

    # Comparison with Existing Signals
    if comparison and "error" not in comparison:
        lines.append("=" * 70)
        lines.append("COMPARISON WITH EXISTING SIGNAL METHODS")
        lines.append("=" * 70)
        comp_summary = comparison.get("summary", {})
        lines.append(f"  Stations compared: {comp_summary.get('stations_comparable', 0)}")
        lines.append(f"  Ensemble beats best existing signal: {comp_summary.get('ensemble_better_than_best_signal', 0)} stations")
        lines.append(f"  Ensemble loses to best existing signal: {comp_summary.get('ensemble_worse_than_best_signal', 0)} stations")
        lines.append(f"  Mean accuracy delta vs existing avg: {comp_summary.get('mean_accuracy_delta_vs_existing_avg', 0):+.4f}")
        lines.append(f"  Ensemble win rate: {comp_summary.get('ensemble_win_rate', 0)*100:.1f}%")
        lines.append("")
        lines.append("  Per-station:")
        lines.append(f"  {'Station':<8} {'Ensemble':>9} {'Existing':>9} {'Best':>9} {'Delta':>9}")
        lines.append(f"  {'-'*8} {'-'*9} {'-'*9} {'-'*9} {'-'*9}")
        for station in sorted(comparison.get("station_comparison", {}).keys()):
            sc = comparison["station_comparison"][station]
            lines.append(f"  {station:<8} {sc['ensemble_directional_accuracy']*100:>7.1f}% {sc['existing_signal_avg_accuracy']*100:>7.1f}% {sc['existing_signal_best_accuracy']*100:>7.1f}% {sc['delta_vs_best']*100:+>7.1f}%")
        lines.append("")

    lines.append("=" * 70)
    lines.append("END REPORT")
    lines.append("=" * 70)

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════


def main():
    """Run the ensemble fraction backtest and save results."""
    print("=" * 70)
    print("ENSEMBLE FRACTION BACKTEST")
    print("GEFS 31-member ensemble -> Kalshi temperature threshold exceedance")
    print("=" * 70)
    print()

    # Load data
    print("Loading Kalshi settlement data...")
    kalshi_data = load_kalshi_data()
    print(f"  Loaded {len(kalshi_data)} Kalshi settlement records")

    print("Loading station mapping...")
    station_mapping = load_station_mapping()
    print(f"  Loaded {len(station_mapping)} stations")

    # Run backtest
    print()
    print("=" * 70)
    print("RUNNING ENSEMBLE FRACTION BACKTEST")
    print("=" * 70)
    print()
    print(f"  Sampling every {SAMPLE_EVERY_N}th day per station")
    print(f"  Rate limit: {RATE_LIMIT_SLEEP}s between requests")
    print(f"  Thresholds: {TEMP_THRESHOLDS[0]}F to {TEMP_THRESHOLDS[-1]}F (step 2F)")
    print()

    results = run_backtest(kalshi_data, station_mapping)

    # Comparison with existing signals
    print()
    print("Loading existing signal accuracy data for comparison...")
    comparison = compare_with_existing_signals(results)
    if "error" in comparison:
        print(f"  Warning: {comparison['error']}")
    else:
        print(f"  Compared {comparison.get('summary', {}).get('stations_comparable', 0)} stations")

    # Generate report
    print()
    report = generate_report(results, comparison)
    print(report)

    # Save results
    output_path = os.path.join(DATA_DIR, "ensemble_fraction_results.json")
    output = {
        "metadata": results["metadata"],
        "overall": results["overall"],
        "per_station": results["per_station"],
        "comparison_with_existing_signals": comparison,
        "report": report,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print()
    print(f"Results saved to: {output_path}")
    print(f"  Overall directional accuracy: {results['overall'].get('best_threshold_metrics', {}).get('directional_accuracy', 'N/A')}")
    print(f"  Total requests: {results['metadata']['total_requests']}")
    print(f"  Total errors: {results['metadata']['total_errors']}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
