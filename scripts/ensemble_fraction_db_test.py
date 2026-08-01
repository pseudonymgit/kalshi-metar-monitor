#!/usr/bin/env python3
"""
Ensemble Fraction DB Test — GEFS 6-member from NWP DB vs Kalshi ground truth.

Reads the GEFS 6-member ensemble from the local NWP database (nwp_forecasts.db),
computes exceedance probabilities for each Kalshi 2°F threshold (70°F to 110°F),
and scores against actual Kalshi settlement temperature.

Usage:
    python3 scripts/ensemble_fraction_db_test.py

Output:
    - data/ensemble_fraction_db_results.json
    - Full console report
"""

import json
import logging
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ─── Bias Correction Path ──────────────────────────────────────────────────
BIAS_CORRECTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ensemble_fraction_bias_corrections.json")

# ─── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "nwp_forecasts.db")
KALSHI_PATH = os.path.join(DATA_DIR, "kalshi_backfill_complete.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "ensemble_fraction_db_results.json")

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
logger = logging.getLogger("ensemble_fraction_db")


# ─── Bias Correction Helpers ──────────────────────────────────────────────────

_BIAS_CACHE = None


def get_season(date_str: str) -> str:
    """Return season key (DJF, MAM, JJA, SON) for a YYYY-MM-DD date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    m = dt.month
    if m == 12 or m <= 2:
        return "DJF"
    elif m <= 5:
        return "MAM"
    elif m <= 8:
        return "JJA"
    else:
        return "SON"


def load_bias_corrections() -> Optional[dict]:
    """Load bias table from JSON. Returns {station: {season: bias_f}} or None."""
    global _BIAS_CACHE
    if _BIAS_CACHE is not None:
        return _BIAS_CACHE
    try:
        with open(BIAS_CORRECTIONS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Bias corrections not found at %s: %s", BIAS_CORRECTIONS_PATH, e)
        return None
    bias_table = data.get("bias_table", {})
    seasons = data.get("season_order", ["DJF", "MAM", "JJA", "SON"])
    result = {}
    for station, biases in bias_table.items():
        result[station] = {s: biases[i] for i, s in enumerate(seasons) if i < len(biases)}
    _BIAS_CACHE = result
    logger.info("Loaded bias corrections: %d stations x 4 seasons", len(result))
    return result


def apply_bias_correction(
    members_f: List[float],
    station: str,
    date_str: str,
    bias_table: Optional[dict],
) -> List[float]:
    """
    Apply bias correction to ensemble member values before fraction computation.
    
    corrected_value = member_value - bias_table[station][season]
    Bias defined as (GEFS_mean - actual). Positive bias = GEFS overpredicts.
    Subtracting bias from members removes systematic error.
    For KDEN DJF bias = -3.75, member - (-3.75) = member + 3.75""F.
    """
    if bias_table is None:
        return members_f
    bs = bias_table.get(station, {}).get(get_season(date_str))
    if bs is None:
        return members_f
    return [m - bs for m in members_f]


# ─── Constants ─────────────────────────────────────────────────────────────────

# Kalshi HIGH temperature thresholds: 70, 72, ..., 110°F (21 thresholds)
TEMP_THRESHOLDS = list(range(70, 112, 2))

# Fee model from sweep config
ROUND_TRIP_FEE = 0.0205

# 6 GEFS ensemble members
MEMBER_VARS = [
    "temperature_2m_max_membergec00",
    "temperature_2m_max_membergep01",
    "temperature_2m_max_membergep02",
    "temperature_2m_max_membergep03",
    "temperature_2m_max_membergep04",
    "temperature_2m_max_membergep05",
]


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def load_kalshi_data() -> List[dict]:
    """Load Kalshi settlement data from the backfill file."""
    with open(KALSHI_PATH) as f:
        return json.load(f)


def get_db_connection() -> sqlite3.Connection:
    """Get a read-only connection to the NWP database."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def compute_ensemble_fraction(members_f: List[float], threshold_f: int) -> float:
    """Compute fraction of ensemble members exceeding a temperature threshold."""
    if not members_f:
        return 0.5
    count_above = sum(1 for m in members_f if m > threshold_f)
    return count_above / len(members_f)


def compute_exceedance_probabilities(members_f: List[float]) -> Dict[int, float]:
    """Compute exceedance probability for each Kalshi threshold."""
    result = {}
    for threshold in TEMP_THRESHOLDS:
        frac = compute_ensemble_fraction(members_f, threshold)
        result[threshold] = round(frac, 4)
    return result


def score_probability_forecast(prob_above: float, actually_above: bool) -> Dict:
    """Score a single probability forecast (Brier, log loss, directional)."""
    outcome = 1.0 if actually_above else 0.0
    eps = 1e-15
    p = max(eps, min(1 - eps, prob_above))
    brier = (p - outcome) ** 2
    log_loss = -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))

    # Directional accuracy: predicted above/below 50% matches actual
    # For rare events (prob_above < 0.5), correct prediction is "below"
    # For common events (prob_above > 0.5), correct prediction is "above"
    direction_correct = (p > 0.5) == actually_above

    # For ties (exactly 0.5), it's always "no edge"
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
# Database Access
# ═════════════════════════════════════════════════════════════════════════════


def fetch_ensemble_data(station: str, target_date: str, conn: sqlite3.Connection) -> Optional[List[float]]:
    """
    Fetch all available GEFS ensemble member values for a station+date.
    
    Returns list of temperatures in Fahrenheit, or None if no data.
    """
    placeholders = ",".join("?" for _ in MEMBER_VARS)
    query = f"""
        SELECT variable, value
        FROM nwp_forecasts
        WHERE model = 'gefs_ens'
          AND station = ?
          AND target_date = ?
          AND variable IN ({placeholders})
        ORDER BY variable
    """
    params = [station, target_date] + MEMBER_VARS
    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        return None

    # Convert Celsius to Fahrenheit
    members_f = [celsius_to_fahrenheit(row["value"]) for row in rows]
    return members_f


def load_all_ensemble_data(conn: sqlite3.Connection) -> Dict:
    """
    Load all GEFS ensemble data into a lookup dict for faster processing.
    
    Returns {station: {target_date: [member1_f, member2_f, ...]}}
    """
    query = """
        SELECT station, target_date, variable, value
        FROM nwp_forecasts
        WHERE model = 'gefs_ens'
          AND variable LIKE 'temperature_2m_max_member%'
        ORDER BY station, target_date, variable
    """
    cursor = conn.execute(query)

    data = defaultdict(lambda: defaultdict(list))
    for row in cursor:
        station = row["station"]
        target_date = row["target_date"]
        value_f = celsius_to_fahrenheit(row["value"])
        data[station][target_date].append(value_f)

    return dict(data)


# ═════════════════════════════════════════════════════════════════════════════
# Core Analysis
# ═════════════════════════════════════════════════════════════════════════════


def analyze_single_record(
    station: str,
    target_date: str,
    kalshi_temp: float,
    members_f: List[float],
) -> Optional[Dict]:
    """
    Analyze a single station+date record.
    
    Returns None if no ensemble data available.
    """
    if not members_f:
        return None

    # Apply bias correction (Item A2 from Gray Room Round 12)
    bias_table = load_bias_corrections()
    corrected_f = apply_bias_correction(members_f, station, target_date, bias_table)

    n_members = len(members_f)

    # Compute exceedance probabilities from CORRECTED values
    exceedance_probs = compute_exceedance_probabilities(corrected_f)

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

    # Find the "best threshold" (closest to actual temp)
    best_threshold = None
    best_dist = float("inf")
    for tr in threshold_results:
        dist = abs(tr["threshold_f"] - kalshi_temp)
        if dist < best_dist:
            best_dist = dist
            best_threshold = tr

    # Ensemble mean (from corrected values)
    ensemble_mean = sum(corrected_f) / len(corrected_f)
    variance = sum((m - ensemble_mean) ** 2 for m in corrected_f) / len(corrected_f)
    ensemble_std = math.sqrt(variance)

    return {
        "station": station,
        "date": target_date,
        "kalshi_temp": kalshi_temp,
        "ensemble_mean_f": round(ensemble_mean, 2),
        "ensemble_std_f": round(ensemble_std, 2),
        "n_members": n_members,
        "ensemble_members_f": [round(m, 1) for m in sorted(members_f)],
        "best_threshold": best_threshold,
        "threshold_results": threshold_results,
    }


def compute_station_metrics(station: str, records: List[Dict]) -> Dict:
    """Compute per-station metrics from ensemble fraction results."""
    if not records:
        return {"station": station, "n_events": 0, "error": "no_data"}

    n = len(records)

    # Temperature prediction errors (ensemble mean vs actual)
    errors = []
    actual_temps = []
    predicted_temps = []
    for r in records:
        if r.get("ensemble_mean_f") is not None:
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

    for r in records:
        for tr in r.get("threshold_results", []):
            all_threshold_scores.append(tr)
            brier_scores.append(tr["brier"])
            log_losses.append(tr["log_loss"])
            edges.append(tr["edge_after_fees"])

    # Best threshold metrics (closest to actual temp)
    best_threshold_results = []
    for r in records:
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
    all_results: List[Dict],
    station_results: Dict[str, Dict],
) -> Dict:
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

    # Rank stations by directional accuracy (best threshold)
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
# Report Generation
# ═════════════════════════════════════════════════════════════════════════════


def generate_report(results: Dict) -> str:
    """Generate a human-readable report from the backtest results."""
    meta = results.get("metadata", {})
    overall = results.get("overall", {})
    per_station = results.get("per_station", {})

    lines = []
    lines.append("=" * 73)
    lines.append("  ENSEMBLE FRACTION DB TEST — 6-Member GEFS vs Kalshi Ground Truth")
    lines.append("=" * 73)
    lines.append(f"  Generated:        {meta.get('generated_at', 'unknown')}")
    lines.append(f"  Data source:      NWP DB ({DB_PATH})")
    lines.append(f"  Kalshi records:   {meta.get('total_kalshi_records', 0)}")
    lines.append(f"  GEFS members:     6 ({', '.join(MEMBER_VARS[:3])}...)")
    lines.append(f"  Fee model:        {ROUND_TRIP_FEE*100:.3f}% round-trip")
    lines.append(f"  Thresholds:       {TEMP_THRESHOLDS[0]}°F to {TEMP_THRESHOLDS[-1]}°F (step 2°F, {len(TEMP_THRESHOLDS)} thresholds)")
    lines.append(f"  Naive benchmark:  Brier = 0.2500 (always predict 50%)")
    lines.append("")

    if "error" in overall:
        lines.append(f"  ERROR: {overall['error']}")
        return "\n".join(lines)

    # Data Coverage
    lines.append("-" * 73)
    lines.append("  DATA COVERAGE")
    lines.append("-" * 73)
    lines.append(f"  Kalshi records total:                       {meta.get('total_kalshi_records', 0):>7}")
    lines.append(f"  With GEFS ensemble data:                   {meta.get('records_with_gefs', 0):>7}")
    lines.append(f"  Coverage fraction:                         {meta.get('coverage_fraction', 0)*100:>6.1f}%")
    lines.append(f"  Unique dates with GEFS:                    {meta.get('unique_dates_with_gefs', 0):>7}")
    lines.append(f"  Stations with GEFS data:                   {meta.get('stations_with_gefs', 0):>7}")
    lines.append(f"  Records with all 6 members:                {meta.get('full_6_member_records', 0):>7}")
    lines.append(f"  Records with 3-5 members:                  {meta.get('partial_records', 0):>7}")
    lines.append(f"  Records with 1-2 members:                  {meta.get('sparse_records', 0):>7}")
    lines.append(f"  Records with no GEFS data (out of range):  {meta.get('no_gefs_records', 0):>7}")
    lines.append(f"  Date range of overlap:                     {meta.get('gefs_date_min', 'N/A')} to {meta.get('gefs_date_max', 'N/A')}")
    lines.append("")

    # Temperature Prediction
    lines.append("-" * 73)
    lines.append("  OVERALL TEMPERATURE PREDICTION (Ensemble Mean vs Actual)")
    lines.append("-" * 73)
    temp = overall.get("temperature_prediction", {})
    lines.append(f"    MAE:     {temp.get('mae_f', 0):>7.2f} °F")
    lines.append(f"    Bias:    {temp.get('bias_f', 0):>+7.2f} °F")
    lines.append(f"    RMSE:    {temp.get('rmse_f', 0):>7.2f} °F")
    lines.append(f"    Corr:    {temp.get('correlation', 0):>7.4f}")
    lines.append(f"    Events:  {overall.get('n_events', 0):>7}")
    lines.append(f"    Stations:{overall.get('n_stations', 0):>7}")
    lines.append("")

    # Best Threshold Metrics
    lines.append("-" * 73)
    lines.append("  BEST-THRESHOLD METRICS (closest threshold to actual temp)")
    lines.append("-" * 73)
    best = overall.get("best_threshold_metrics", {})
    lines.append(f"    Directional Accuracy:  {best.get('directional_accuracy', 0)*100:>7.1f}%")
    lines.append(f"    Avg Brier:             {best.get('avg_brier', 0):>9.4f}")
    lines.append(f"    Avg Log Loss:          {best.get('avg_log_loss', 0):>9.4f}")
    lines.append(f"    Avg Edge (after fees): {best.get('avg_edge_after_fees', 0):>+9.4f}")
    lines.append("")

    # All Threshold Metrics
    lines.append("-" * 73)
    lines.append("  ALL-THRESHOLD METRICS (21 thresholds × events)")
    lines.append("-" * 73)
    all_m = overall.get("all_threshold_metrics", {})
    lines.append(f"    Threshold Evaluations: {overall.get('n_threshold_evaluations', 0):>8}")
    lines.append(f"    Directional Accuracy:  {all_m.get('directional_accuracy', 0)*100:>7.1f}%")
    lines.append(f"    Avg Brier:             {all_m.get('avg_brier', 0):>9.4f}")
    lines.append(f"    Avg Log Loss:          {all_m.get('avg_log_loss', 0):>9.4f}")
    lines.append(f"    Avg Edge (after fees): {all_m.get('avg_edge_after_fees', 0):>+9.4f}")
    lines.append(f"    Positive Edge Frac:    {all_m.get('positive_edge_fraction', 0)*100:>7.1f}%")
    lines.append(f"    Edge Sharpe:           {all_m.get('edge_sharpe', 0):>9.3f}")
    lines.append(f"    ECE (calibration):     {all_m.get('ece', 0):>9.4f}")
    lines.append("")

    # Naive benchmark comparison
    naive_brier = 0.25
    actual_avg_brier = all_m.get("avg_brier", 0)
    brier_improvement = (naive_brier - actual_avg_brier) / naive_brier * 100
    lines.append("-" * 73)
    lines.append("  BENCHMARK COMPARISON")
    lines.append("-" * 73)
    lines.append(f"    Naive benchmark (always 50%):     Brier = {naive_brier:.4f}")
    lines.append(f"    Ensemble fraction (all thresholds): Brier = {actual_avg_brier:.4f}")
    lines.append(f"    Brier skill score (vs naive):      {brier_improvement:>+6.1f}%")
    lines.append(f"    Best-threshold Brier:              {best.get('avg_brier', 0):.4f}")
    lines.append(f"    Best-threshold Brier skill score:  {(naive_brier - best.get('avg_brier', 0)) / naive_brier * 100:>+6.1f}%")
    lines.append("")

    # Edge Analysis
    lines.append("-" * 73)
    lines.append("  EDGE ANALYSIS")
    lines.append("-" * 73)
    lines.append(f"    Round-trip fee:        {ROUND_TRIP_FEE*100:.3f}%")
    lines.append(f"    Positive edge events:  {all_m.get('positive_edge_fraction', 0)*100:.1f}% of threshold-events")
    lines.append(f"    Avg edge after fees:   {all_m.get('avg_edge_after_fees', 0):+.4f}")
    lines.append(f"    Edge Sharpe:           {all_m.get('edge_sharpe', 0):.3f}")
    lines.append(f"    (Edge vs 0.5 baseline — no historical market prices available)")
    lines.append("")

    # Per-Station Summary Table
    lines.append("-" * 73)
    lines.append("  PER-STATION SUMMARY (sorted by directional accuracy)")
    lines.append("-" * 73)
    lines.append(f"  {'Station':<8} {'Events':>7} {'MAE(°F)':>8} {'Bias(°F)':>8} {'DA%':>7} {'Brier':>8} {'Edge':>8}")
    lines.append(f"  {'-'*8} {'-'*7} {'-'*8} {'-'*8} {'-'*7} {'-'*8} {'-'*8}")

    # Sort stations by best-threshold directional accuracy
    sorted_stations = sorted(
        per_station.items(),
        key=lambda x: x[1].get("best_threshold_metrics", {}).get("directional_accuracy", 0),
        reverse=True,
    )
    for station, data in sorted_stations:
        if "error" in data:
            lines.append(f"  {station:<8} {'ERR':>7}  {str(data.get('error', ''))}")
            continue
        n = data.get("n_events", 0)
        mae = data.get("temperature_prediction", {}).get("mae_f", 0)
        bias = data.get("temperature_prediction", {}).get("bias_f", 0)
        da = data.get("best_threshold_metrics", {}).get("directional_accuracy", 0)
        brier = data.get("best_threshold_metrics", {}).get("avg_brier", 0)
        edge = data.get("best_threshold_metrics", {}).get("avg_edge_after_fees", 0)
        lines.append(f"  {station:<8} {n:>7} {mae:>7.1f}°F {bias:>+6.1f}°F {da*100:>6.1f}% {brier:>8.4f} {edge:>+8.4f}")
    lines.append("")

    # Top/Bottom
    lines.append("-" * 73)
    lines.append("  TOP 5 STATIONS (by best-threshold directional accuracy)")
    lines.append("-" * 73)
    for s in overall.get("top_stations", []):
        lines.append(f"    {s['station']:<8}  acc={s['directional_accuracy']*100:.1f}%  n={s['n_events']}  MAE={s['mae']:.1f}°F")
    lines.append("")
    lines.append("  BOTTOM 5 STATIONS")
    for s in overall.get("bottom_stations", []):
        lines.append(f"    {s['station']:<8}  acc={s['directional_accuracy']*100:.1f}%  n={s['n_events']}  MAE={s['mae']:.1f}°F")
    lines.append("")

    # Calibration Summary
    lines.append("-" * 73)
    lines.append("  CALIBRATION SUMMARY (all-threshold, first 3 stations)")
    lines.append("-" * 73)
    stations_shown = 0
    for station, data in per_station.items():
        if stations_shown >= 3:
            break
        cal = data.get("calibration", [])
        if not cal:
            continue
        lines.append(f"  Station: {station}")
        header = f"    {'Bin':<8} {'N':>5} {'Pred':>7} {'Actual':>7} {'Err':>7}"
        lines.append(header)
        for entry in cal:
            if entry["n"] > 0:
                lines.append(f"    {entry['bin']:<8} {entry['n']:>5} {entry['avg_predicted']:.4f} {entry['freq_actual']:.3f}  {entry['calibration_error']:.3f}")
        stations_shown += 1
    lines.append("")

    # Summary
    lines.append("=" * 73)
    lines.append("  SUMMARY")
    lines.append("=" * 73)
    signal_found = best.get("directional_accuracy", 0) > 0.55
    edge_exists = all_m.get("edge_sharpe", 0) > 0.5
    brier_beats_naive = actual_avg_brier < 0.24

    lines.append(f"  Ensemble fraction directional accuracy (best threshold): {best.get('directional_accuracy', 0)*100:.1f}%")
    lines.append(f"  vs 50% random baseline:                                {'✓ SIGNAL DETECTED' if signal_found else '✗ NO SIGNAL'}")
    lines.append(f"  Brier skill score vs naive (always 50%):                {brier_improvement:+.1f}% {'✓ BETTER' if brier_improvement > 0 else '✗ WORSE'}")
    lines.append(f"  Edge Sharpe:                                            {all_m.get('edge_sharpe', 0):.3f} {'✓ POSITIVE' if edge_exists else '✗ NEUTRAL/NEGATIVE'}")
    lines.append("")
    lines.append(f"  Key finding: The 6-member GEFS ensemble fraction {'DOES' if signal_found else 'DOES NOT'} show directional signal")
    lines.append(f"  against Kalshi settlement ground truth for temperature thresholds.")
    lines.append(f"  Edge after fees: {'YES' if all_m.get('avg_edge_after_fees', 0) > 0 else 'NO'}")
    lines.append(f"  Edge Sharpe: {all_m.get('edge_sharpe', 0):.3f}")
    lines.append("")
    lines.append("  Recommendation: This should inform whether to proceed with ensemble-based")
    lines.append("  probability modeling or investigate other approaches.")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════


def main():
    """Run the ensemble fraction test against the NWP database."""
    print("=" * 73)
    print("  ENSEMBLE FRACTION DB TEST — 6-Member GEFS from NWP DB")
    print("=" * 73)
    print()

    # Load Kalshi data
    print("  Loading Kalshi settlement data...")
    kalshi_data = load_kalshi_data()
    total_kalshi = len(kalshi_data)
    print(f"    Loaded {total_kalshi} Kalshi settlement records")
    print()

    # Connect to database
    print("  Connecting to NWP database...")
    conn = get_db_connection()
    print(f"    Connected to {DB_PATH}")

    # Verify GEFS data exists
    cursor = conn.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE model='gefs_ens'")
    gefs_total = cursor.fetchone()[0]
    print(f"    {gefs_total} GEFS records in database")
    print()

    # Process all Kalshi records against GEFS data
    print("  Processing records...")
    print(f"    {total_kalshi} Kalshi records to check against GEFS")
    print()

    # Build a lookup: {station: {target_date: [members_f]}}
    all_gefs = load_all_ensemble_data(conn)
    conn.close()

    stations_with_gefs = len(all_gefs)
    print(f"    Loaded GEFS data for {stations_with_gefs} stations")

    # Track coverage stats
    full_6 = 0
    partial = 0
    sparse = 0
    no_gefs = 0
    total_checked = 0

    # Process each Kalshi record
    analyzed_results = []
    station_raw = defaultdict(list)  # {station: [analyzed_record]}

    for rec in kalshi_data:
        station = rec.get("station")
        target_date = rec.get("target_date")
        kalshi_temp = rec.get("kalshi_temp")

        if not station or not target_date or kalshi_temp is None:
            continue

        total_checked += 1

        # Look up in the ensemble data
        station_data = all_gefs.get(station, {})
        members_f = station_data.get(target_date, [])

        if not members_f:
            no_gefs += 1
            continue

        n_members = len(members_f)
        if n_members == 6:
            full_6 += 1
        elif n_members >= 3:
            partial += 1
        else:
            sparse += 1

        # Analyze this record
        result = analyze_single_record(station, target_date, kalshi_temp, members_f)
        if result:
            analyzed_results.append(result)
            station_raw[station].append(result)

    # Get date range
    dates_with_gefs = set()
    for station_data in all_gefs.values():
        dates_with_gefs.update(station_data.keys())
    gefs_dates = sorted(dates_with_gefs)
    gefs_date_min = gefs_dates[0] if gefs_dates else "N/A"
    gefs_date_max = gefs_dates[-1] if gefs_dates else "N/A"

    coverage_fraction = len(analyzed_results) / total_checked if total_checked > 0 else 0.0

    print(f"    Records with all 6 members:              {full_6:>7}")
    print(f"    Records with 3-5 members:                {partial:>7}")
    print(f"    Records with 1-2 members:                {sparse:>7}")
    print(f"    Records with no GEFS data (out of range):{no_gefs:>7}")
    print(f"    Total analyzed:                          {len(analyzed_results):>7}")
    print(f"    Coverage fraction:                       {coverage_fraction*100:>6.1f}%")
    print()

    # Compute per-station metrics
    print("  Computing per-station metrics...")
    station_results = {}
    for station, records in station_raw.items():
        station_results[station] = compute_station_metrics(station, records)
    print(f"    Computed for {len(station_results)} stations")
    print()

    # Compute overall metrics
    print("  Computing overall metrics...")
    overall = compute_overall_metrics(analyzed_results, station_results)
    print(f"    Overall: {overall.get('n_events', 0)} events across {overall.get('n_stations', 0)} stations")
    print()

    # Build metadata
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "method": "GEFS 6-member ensemble fraction exceedance probability (local DB)",
        "db_path": DB_PATH,
        "kalshi_path": KALSHI_PATH,
        "total_kalshi_records": total_kalshi,
        "records_with_gefs": len(analyzed_results),
        "coverage_fraction": round(coverage_fraction, 4),
        "unique_dates_with_gefs": len(gefs_dates),
        "stations_with_gefs": stations_with_gefs,
        "full_6_member_records": full_6,
        "partial_records": partial,
        "sparse_records": sparse,
        "no_gefs_records": no_gefs,
        "gefs_date_min": gefs_date_min,
        "gefs_date_max": gefs_date_max,
        "fee_model": f"{ROUND_TRIP_FEE*100:.3f}% round-trip",
        "thresholds": TEMP_THRESHOLDS,
        "member_variables": MEMBER_VARS,
    }

    results = {
        "metadata": metadata,
        "overall": overall,
        "per_station": station_results,
    }

    # Generate report
    print("  Generating report...")
    report = generate_report(results)
    print(report)

    # Save results
    print(f"  Saving results to: {OUTPUT_PATH}")
    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"    Done.")

    # Final summary
    print()
    print("=" * 73)
    print("  COMPLETE")
    print("=" * 73)
    print(f"  Results saved to: {OUTPUT_PATH}")
    da = overall.get("best_threshold_metrics", {}).get("directional_accuracy", "N/A")
    print(f"  Overall directional accuracy (best threshold): {da}")
    brier = overall.get("best_threshold_metrics", {}).get("avg_brier", "N/A")
    print(f"  Overall Brier (best threshold): {brier}")
    print()


if __name__ == "__main__":
    main()
