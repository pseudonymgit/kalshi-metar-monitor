#!/usr/bin/env python3
"""
ensemble_fraction.py — Bias correction and ensemble fraction computation.

Loadable module for the weather engine:
  - load_bias_corrections(path) -> dict from data/ensemble_fraction_bias_corrections.json
  - apply_bias_correction(member_values, station, target_date) -> corrected array
  - compute_ensemble_fraction(corrected_members, threshold) -> float

Usage:
    from core.ensemble_fraction import load_bias_corrections, apply_bias_correction, compute_ensemble_fraction

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default bias correction file
_DEFAULT_BIAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "ensemble_fraction_bias_corrections.json",
)

# Season mapping: month -> season index (0=DJF, 1=MAM, 2=JJA, 3=SON)
_SEASON_MAP = {12: 0, 1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3}


def _get_season_idx(target_date: str) -> int:
    """Get season index (0-3) from a date string 'YYYY-MM-DD'."""
    try:
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        return _SEASON_MAP.get(dt.month, 0)
    except (ValueError, TypeError):
        return 0


def load_bias_corrections(path: Optional[str] = None) -> dict:
    """
    Load ensemble fraction bias corrections from JSON file.

    Args:
        path: Path to bias corrections JSON. Defaults to
              data/ensemble_fraction_bias_corrections.json

    Returns:
        dict with bias_table, season_order, sample_counts, and metadata.
        Returns empty dict if file not found.
    """
    if path is None:
        path = _DEFAULT_BIAS_PATH

    if not os.path.exists(path):
        logger.warning("Bias corrections file not found: %s", path)
        return {}

    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Error loading bias corrections: %s", e)
        return {}


def get_bias_for_station(
    bias_data: dict,
    station: str,
    target_date: str,
) -> float:
    """
    Get the bias correction value for a station and date.

    Bias is defined as: bias = GEFS_mean_TMAX_F - actual_HIGH_temp_F
    Positive bias = GEFS overpredicts (correct by subtracting)
    Negative bias = GEFS underpredicts (correct by adding)

    Args:
        bias_data: Loaded bias corrections dict
        station: ICAO station code (e.g., 'KDEN')
        target_date: Date string 'YYYY-MM-DD'

    Returns:
        Float bias correction value to SUBTRACT from GEFS values.
        Returns 0.0 if station or date not found in bias data.
    """
    bias_table = bias_data.get("bias_table", {})
    season_order = bias_data.get("season_order", ["DJF", "MAM", "JJA", "SON"])
    sample_counts = bias_data.get("sample_counts", {})

    # Get station bias values
    station_biases = bias_table.get(station, [])
    if not station_biases:
        logger.debug("No bias data for station %s", station)
        return 0.0

    # Get season index
    season_idx = _get_season_idx(target_date)

    # Validate the season index is within range
    if season_idx >= len(station_biases):
        logger.debug(
            "Season index %d out of range for station %s (%d values)",
            season_idx, station, len(station_biases),
        )
        return 0.0

    # Get sample count for this station-season
    station_samples = sample_counts.get(station, [])
    min_samples = bias_data.get("min_samples", 10)

    if season_idx < len(station_samples) and station_samples[season_idx] < min_samples:
        logger.debug(
            "Insufficient samples for %s season %s: %d < %d",
            station, season_order[season_idx],
            station_samples[season_idx], min_samples,
        )
        return 0.0

    return station_biases[season_idx]


def apply_bias_correction(
    member_values: np.ndarray,
    station: str,
    target_date: str,
    bias_data: Optional[dict] = None,
) -> np.ndarray:
    """
    Apply bias correction to an array of ensemble member values.

    Bias formula: corrected = member - bias
    Where bias = GEFS_mean_TMAX_F - actual_HIGH_temp_F
    (Positive bias means GEFS overpredicts, so we subtract)

    Args:
        member_values: numpy array of ensemble member temperature values (°F)
        station: ICAO station code
        target_date: Date string 'YYYY-MM-DD'
        bias_data: Pre-loaded bias corrections dict. Loaded if None.

    Returns:
        numpy array of corrected member values
    """
    if bias_data is None:
        bias_data = load_bias_corrections()

    if not bias_data:
        return member_values.copy()

    bias = get_bias_for_station(bias_data, station, target_date)
    if bias == 0.0:
        return member_values.copy()

    return member_values - bias


def compute_ensemble_fraction(
    corrected_members: np.ndarray,
    threshold: float,
) -> float:
    """
    Compute the fraction of ensemble members exceeding a temperature threshold.

    Args:
        corrected_members: numpy array of bias-corrected ensemble member values (°F)
        threshold: Temperature threshold (°F) to test against

    Returns:
        Float fraction [0.0, 1.0] of members exceeding the threshold.
        Returns 0.5 if no valid members (neutral baseline).
    """
    if corrected_members is None or len(corrected_members) == 0:
        logger.warning("No valid ensemble members to compute fraction")
        return 0.5

    # Filter out NaN values
    valid = corrected_members[~np.isnan(corrected_members)]

    if len(valid) == 0:
        logger.warning("All ensemble member values are NaN")
        return 0.5

    fraction = float(np.mean(valid > threshold))
    return fraction


def compute_ensemble_fraction_both(
    gefs_members: np.ndarray,
    ecmwf_members: np.ndarray,
    threshold: float,
    station: str,
    target_date: str,
    bias_data: Optional[dict] = None,
    combination: str = "per_model_avg",
) -> Tuple[float, dict]:
    """
    Compute ensemble fraction from both GEFS and ECMWF members,
    with bias correction and model combination.

    Args:
        gefs_members: numpy array of GEFS member values
        ecmwf_members: numpy array of ECMWF member values
        threshold: Temperature threshold (°F)
        station: ICAO station code
        target_date: Date string
        bias_data: Pre-loaded bias corrections
        combination: 'raw_pool' or 'per_model_avg'
            raw_pool = count all 82 members equally (ECMWF gets 62% weight)
            per_model_avg = fraction per model, then average (50/50)

    Returns:
        (combined_fraction, metadata_dict)
    """
    # Bias correct
    gefs_corrected = apply_bias_correction(gefs_members, station, target_date, bias_data)
    ecmwf_corrected = apply_bias_correction(ecmwf_members, station, target_date, bias_data)

    # Compute per-model fractions
    gefs_frac = compute_ensemble_fraction(gefs_corrected, threshold)
    ecmwf_frac = compute_ensemble_fraction(ecmwf_corrected, threshold)

    if combination == "per_model_avg":
        combined = (gefs_frac + ecmwf_frac) / 2.0
    else:  # raw_pool
        all_members = np.concatenate([gefs_corrected, ecmwf_corrected])
        combined = compute_ensemble_fraction(all_members, threshold)

    metadata = {
        "gefs_fraction": round(gefs_frac, 4),
        "ecmwf_fraction": round(ecmwf_frac, 4),
        "combined_fraction": round(combined, 4),
        "combination_method": combination,
        "n_gefs": len(gefs_corrected),
        "n_ecmwf": len(ecmwf_corrected),
        "intermodel_disagreement": round(abs(gefs_frac - ecmwf_frac), 4),
    }

    return combined, metadata


# ── Standalone Test ─────────────────────────────────────────────────────

def test_bias_correction():
    """Test the bias correction module with real data."""
    print("=" * 72)
    print("  Ensemble Fraction Module — Self-Test")
    print("=" * 72)

    # Load bias corrections
    bias_data = load_bias_corrections()
    if not bias_data:
        print("  ⚠️  Bias corrections not found — skipping test")
        return False

    print(f"  Version: {bias_data.get('version', 'unknown')}")
    print(f"  Matched pairs: {bias_data.get('matched_pairs', 0)}")
    print(f"  Stations in bias table: {len(bias_data.get('bias_table', {}))}")
    print(f"  Min samples: {bias_data.get('min_samples', 10)}")

    # Test for KDEN in summer
    station = "KDEN"
    date = "2024-07-15"
    bias = get_bias_for_station(bias_data, station, date)
    print(f"\n  KDEN bias for {date} (JJA): {bias:.2f}°F")

    # Test applying bias correction
    members = np.array([88.0, 90.0, 92.0, 85.0, 87.0, 91.0, 86.0, 89.0])
    corrected = apply_bias_correction(members, station, date, bias_data)
    bias_val = get_bias_for_station(bias_data, station, date)
    print(f"  Original members:  {members}")
    print(f"  Bias:              {bias_val:.2f}°F")
    print(f"  Corrected members: {corrected}")

    # Test ensemble fraction
    threshold = 90.0
    frac = compute_ensemble_fraction(corrected, threshold)
    print(f"\n  Fraction > {threshold}°F: {frac:.4f} ({frac*100:.1f}%)")

    # Test compute_ensemble_fraction_both
    gefs_members = np.array([88.0, 90.0, 92.0, 85.0, 87.0, 91.0, 86.0, 89.0,
                             93.0, 84.0, 91.0, 87.0, 90.0, 88.0, 86.0, 92.0,
                             85.0, 89.0, 91.0, 87.0, 90.0, 88.0, 86.0, 92.0,
                             85.0, 89.0, 91.0, 87.0, 90.0, 88.0, 86.0])
    ecmwf_members = np.array([87.0, 89.0, 91.0, 84.0, 86.0, 90.0, 85.0, 88.0,
                              92.0, 83.0, 90.0, 86.0, 89.0, 87.0, 85.0, 91.0,
                              84.0, 88.0, 90.0, 86.0, 89.0, 87.0, 85.0, 91.0,
                              84.0, 88.0, 90.0, 86.0, 89.0, 87.0, 85.0, 91.0,
                              84.0, 88.0, 90.0, 86.0, 89.0, 87.0, 85.0, 91.0,
                              84.0, 88.0, 90.0, 86.0, 89.0, 87.0, 85.0, 91.0,
                              84.0, 88.0, 90.0])

    combined_pooled, meta_pooled = compute_ensemble_fraction_both(
        gefs_members, ecmwf_members, threshold, station, date,
        bias_data, combination="raw_pool",
    )

    combined_avg, meta_avg = compute_ensemble_fraction_both(
        gefs_members, ecmwf_members, threshold, station, date,
        bias_data, combination="per_model_avg",
    )

    print(f"\n  Combined (raw_pool):     {combined_pooled:.4f}")
    print(f"  Combined (per_model_avg): {combined_avg:.4f}")
    print(f"  Disagreement:            {meta_avg['intermodel_disagreement']:.4f}")

    print("\n  ✅ Self-test complete")
    return True


if __name__ == "__main__":
    test_bias_correction()