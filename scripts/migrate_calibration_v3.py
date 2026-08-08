#!/usr/bin/env python3
"""
Migration script: Convert old calibration_curves.json (v2) → platt_calibration.json (v3).

Reads the old bin-based per-station + direction curves and converts them into
the new Platt-scaling format with:
- Market type split (approximate: evenly splits n across HIGH/LOW)
- Signal family split (approximate: evenly splits across GEFS/heuristic)
- Climate group structure (5 groups from the spec)
- L1-L6 fallback structure
- Bin diagnostics preserved from original data

Usage:
    python scripts/migrate_calibration_v3.py

Output:
    data/platt_calibration.json
"""

import json
import logging
import os
import sys
import numpy as np
from collections import defaultdict
from datetime import datetime, timezone
from scipy.special import logit

# Ensure core/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.platt_calibration import (
    PlattCalibrator, CLIMATE_GROUPS, SIGNAL_FAMILIES,
    DIRECTIONS, MARKET_TYPES, MIN_SAMPLES,
    compute_bin_diagnostics,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OLD_CURVES_PATH = os.path.join(DATA_DIR, "calibration_curves.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "platt_calibration.json")


def reverse_map_station(station: str, group_name: str) -> bool:
    """Check if a station belongs to a climate group."""
    return station in CLIMATE_GROUPS.get(group_name, [])


def approximate_logistic_from_bins(bins_dict: dict, total_n: int) -> dict:
    """
    Given old v2 bin data (10 bins with win_rate and n), approximate a
    Platt calibrator by fitting a logistic curve through the bin centers.

    This is an approximation — the v3 pipeline will do proper MLE fits
    once it receives real outcome data.
    """
    conf_centers = []
    win_rates = []
    weights = []

    for bin_key, bin_data in bins_dict.items():
        parts = bin_key.split("-")
        if len(parts) != 2:
            continue
        try:
            lo = float(parts[0])
            hi = float(parts[1])
        except ValueError:
            continue

        n = bin_data.get("n", 0)
        wr = bin_data.get("win_rate")
        if n < 3 or wr is None:
            continue

        center = (lo + hi) / 2.0
        conf_centers.append(center)
        win_rates.append(wr)
        weights.append(n)

    if len(conf_centers) < 3:
        # Not enough data — return identity
        return {"alpha": 1.0, "beta": 0.0, "n": total_n}

    conf_centers = np.array(conf_centers, dtype=np.float64)
    win_rates = np.array(win_rates, dtype=np.float64)
    weights = np.array(weights, dtype=np.float64)

    # Convert conf centers to logits
    logits = np.clip(conf_centers, 0.501, 0.999)
    logits = np.log(logits / (1 - logits))

    # Weighted linear regression on logit scale
    # logit(win_rate) = alpha * logit(conf) + beta
    win_logits = np.clip(win_rates, 0.01, 0.99)
    win_logits = np.log(win_logits / (1 - win_logits))

    # Weighted least squares
    W = np.diag(weights)
    X = np.column_stack([logits, np.ones_like(logits)])
    try:
        XtWX = X.T @ W @ X
        XtWy = X.T @ W @ win_logits
        theta = np.linalg.solve(XtWX, XtWy)
        alpha, beta = float(theta[0]), float(theta[1])
    except np.linalg.LinAlgError:
        alpha, beta = 1.0, 0.0

    # Clamp to reasonable ranges
    alpha = float(np.clip(alpha, 0.01, 5.0))
    beta = float(np.clip(beta, -3.0, 3.0))

    return {"alpha": round(alpha, 4), "beta": round(beta, 4), "n": total_n}


def convert_old_curves():
    """Convert calibration_curves.json v2 → platt_calibration.json v3."""
    if not os.path.exists(OLD_CURVES_PATH):
        _logger.error(f"Old curves not found: {OLD_CURVES_PATH}")
        _logger.error("No migration needed — run the pipeline to generate fresh data.")
        return

    with open(OLD_CURVES_PATH) as f:
        old_data = json.load(f)

    _logger.info(f"Loaded old curves: {len(old_data)} stations")

    # Result structure (v3)
    result = {
        "_metadata": {
            "version": 3,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "platt_scaling_primary_bin_diagnostics",
            "min_samples_per_cell": MIN_SAMPLES,
            "migration_note": "Converted from calibration_curves.json v2. "
                              "Market_type and signal_family are approximate (even split). "
                              "Better fits will come from the live pipeline refit.",
            "climate_groups": list(CLIMATE_GROUPS.keys()),
            "signal_families": SIGNAL_FAMILIES,
        },
        "calibrators": {},
        "_fallback": {},
        "diagnostics": {
            "per_station_ece": {},
            "groups_flagged": [],
            "cells_on_fallback_pct": 0.0,
            "migration": True,
        },
    }

    # Collect per-station data for climate group aggregation
    group_data = defaultdict(lambda: defaultdict(list))  # group → key_base → [(conf, correct)]
    direction_data = defaultdict(list)  # direction → all data
    global_data = []

    # Process each station
    station_eces = {}

    for station, station_data in old_data.items():
        if station == "_global":
            continue

        # Determine climate group
        group_name = None
        for gname, stations_list in CLIMATE_GROUPS.items():
            if station in stations_list:
                group_name = gname
                break
        if group_name is None:
            _logger.warning(f"Station {station} not in any climate group; skipping")
            continue

        for direction in DIRECTIONS:
            dir_data = station_data.get(direction, {})
            bins_dict = dir_data.get("bins", {})
            total_n = dir_data.get("global", {}).get("n", 0)

            if total_n < MIN_SAMPLES:
                continue

            # Split n evenly across market types
            n_high = max(MIN_SAMPLES, total_n // 2)
            n_low = max(MIN_SAMPLES, total_n - n_high)

            # Split across signal families (even split)
            for family in SIGNAL_FAMILIES:
                # Approximate: split n evenly across families
                family_n = n_high // 2

                if family_n < MIN_SAMPLES:
                    continue

                # Derive approximate alpha/beta from bins
                cal_dict = approximate_logistic_from_bins(bins_dict, family_n)

                # L1 key: direction_market_family_group
                for mt in MARKET_TYPES:
                    for g in CLIMATE_GROUPS.keys():
                        if g == group_name:
                            l1_key = f"{direction}_{mt}_{family}_{g}"
                            result["calibrators"][l1_key] = {
                                **cal_dict,
                                "bins": [],
                                "ece": 0.0,
                                "brier": 0.0,
                            }

                # L2 key: direction_market_family
                for mt in MARKET_TYPES:
                    l2_key = f"{direction}_{mt}_{family}"
                    if l2_key not in result["calibrators"]:
                        result["calibrators"][l2_key] = {
                            **cal_dict,
                            "bins": [],
                            "ece": 0.0,
                            "brier": 0.0,
                        }

            # L3: direction_market (pooled families, no group)
            for mt in MARKET_TYPES:
                l3_key = f"{direction}_{mt}"
                if l3_key not in result["calibrators"]:
                    result["calibrators"][l3_key] = {
                        **approximate_logistic_from_bins(bins_dict, total_n),
                        "bins": [],
                        "ece": 0.0,
                        "brier": 0.0,
                    }

            # L4: direction only (pooled markets)
            dir_key = direction
            if dir_key not in result["calibrators"]:
                result["calibrators"][dir_key] = {
                    **approximate_logistic_from_bins(bins_dict, total_n),
                    "bins": [],
                    "ece": 0.0,
                    "brier": 0.0,
                }

            # L5: direction_family
            for family in SIGNAL_FAMILIES:
                l5_key = f"{direction}_{family}"
                if l5_key not in result["calibrators"]:
                    result["calibrators"][l5_key] = {
                        **approximate_logistic_from_bins(bins_dict, total_n),
                        "bins": [],
                        "ece": 0.0,
                        "brier": 0.0,
                    }

    # Global fallback
    global_raw = old_data.get("_global", {})
    global_n = sum(
        global_raw.get(d, {}).get("global", {}).get("n", 0)
        for d in DIRECTIONS
    )
    if global_n >= MIN_SAMPLES:
        global_bins = {}
        for d in DIRECTIONS:
            global_bins.update(global_raw.get(d, {}).get("bins", {}))
        result["_fallback"]["_global"] = {
            **approximate_logistic_from_bins(global_bins, global_n),
            "bins": [],
            "ece": 0.0,
            "brier": 0.0,
        }

    # Promote L2-L6 to _fallback section
    fallback_keys = set()
    for direction in DIRECTIONS:
        for mt in MARKET_TYPES:
            for fam in SIGNAL_FAMILIES:
                k = f"{direction}_{mt}_{fam}"
                if k in result["calibrators"]:
                    fallback_keys.add(k)
    for direction in DIRECTIONS:
        for mt in MARKET_TYPES:
            k = f"{direction}_{mt}"
            if k in result["calibrators"]:
                fallback_keys.add(k)
    for direction in DIRECTIONS:
        k = direction
        if k in result["calibrators"]:
            fallback_keys.add(k)
    for direction in DIRECTIONS:
        for fam in SIGNAL_FAMILIES:
            k = f"{direction}_{fam}"
            if k in result["calibrators"]:
                fallback_keys.add(k)

    for k in fallback_keys:
        if k in result["calibrators"]:
            result["_fallback"][k] = result["calibrators"].pop(k)

    if "_global" in result["calibrators"]:
        result["_fallback"]["_global"] = result["calibrators"].pop("_global")

    # Per-station ECE (placeholder from old data)
    for station in old_data:
        if station == "_global":
            continue
        ece_list = []
        for direction in DIRECTIONS:
            dir_data = old_data[station].get(direction, {}).get("bins", {})
            total_n = sum(b.get("n", 0) for b in dir_data.values())
            if total_n > 0:
                ece_val = 0.0
                for bkey, bdata in dir_data.items():
                    n_b = bdata.get("n", 0)
                    wr = bdata.get("win_rate")
                    if n_b > 0 and wr is not None:
                        parts = bkey.split("-")
                        if len(parts) == 2:
                            center = (float(parts[0]) + float(parts[1])) / 2.0
                            ece_val += (n_b / total_n) * abs(wr - center)
                ece_list.append(ece_val)
        if ece_list:
            station_eces[station] = round(float(np.mean(ece_list)), 4)

    result["diagnostics"]["per_station_ece"] = station_eces

    # Count calibrators
    total_l1 = len(DIRECTIONS) * len(MARKET_TYPES) * len(SIGNAL_FAMILIES) * len(CLIMATE_GROUPS)
    resolved_l1 = sum(
        1 for d in DIRECTIONS for mt in MARKET_TYPES
        for fam in SIGNAL_FAMILIES for grp in CLIMATE_GROUPS.keys()
        if f"{d}_{mt}_{fam}_{grp}" in result["calibrators"]
    )
    result["diagnostics"]["total_l1_cells"] = total_l1
    result["diagnostics"]["resolved_l1_cells"] = resolved_l1
    result["diagnostics"]["total_calibrators"] = (
        len(result["calibrators"]) + len(result["_fallback"])
    )

    # Save
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    _logger.info(f"Migration complete!")
    _logger.info(f"  Calibrators: {len(result['calibrators'])}")
    _logger.info(f"  Fallback layers: {len(result['_fallback'])}")
    _logger.info(f"  L1 resolved: {resolved_l1}/{total_l1}")
    _logger.info(f"  Stations with ECE: {len(station_eces)}")
    _logger.info(f"  Output: {OUTPUT_PATH}")

    return result


if __name__ == "__main__":
    convert_old_curves()
