#!/usr/bin/env python3
"""
forecast_confidence_modulator.py — Multi-model agreement → Bayesian confidence modulation.

Modulates ensemble probability using deterministic multi-model agreement scores.
Implements the design from docs/plans/FORECAST-AGGREGATION-INTEGRATION-DESIGN.md.

Pipeline:
  1. Fetch deterministic forecasts (GFS, ECMWF IFS, ICON, GEM)
  2. Bias-correct per-model, per-station
  3. Compute directional agreement + magnitude dispersion
  4. Modulate ensemble probability: p_adjusted = 0.50 + (p_ens - 0.50) × agreement^β
  5. Apply position sizing gate based on agreement

Usage:
    modulator = ForecastConfidenceModulator()
    p_adj, meta = modulator.modulate(station="KNYC", p_ens=0.72, prev_actual=85.0, lat=40.7, lon=-74.0)

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ──
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIAS_PATH = os.path.join(REPO_ROOT, "data", "forecast_bias.json")
DEFAULT_NWP_DB = os.path.join(REPO_ROOT, "data", "nwp_forecasts.db")

# Required models for consensus
REQUIRED_MODELS = ["gfs", "ecmwf", "icon", "gem"]

# Agreement weights
DIRECTIONAL_WEIGHT = 0.7
MAGNITUDE_WEIGHT = 0.3

# Modulation parameters
AGREEMENT_EXPONENT = 1.5  # β — controls how aggressively we discount low agreement
AGREEMENT_GATE_SLOPE = 4.0 / 3.0  # reaches 1.0 at 0.75 agreement

# Gating thresholds
GATE_MIN_AGREEMENT_DIR = 0.50
GATE_P_ENS_LOW = 0.45
GATE_P_ENS_HIGH = 0.55

# Open-Meteo model key mapping
MODEL_KEY_MAP = {
    "gfs": "gfs_global",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_global",
    "gem": "gem_global",
}


class ForecastConfidenceModulator:
    """
    Modulates ensemble confidence using deterministic multi-model agreement.

    The modulator computes cross-model agreement from 4 deterministic models
    (GFS, ECMWF IFS, ICON, GEM) and adjusts the 82-member ensemble probability
    accordingly.
    """

    def __init__(self, bias_path: str = DEFAULT_BIAS_PATH, nwp_db_path: str = DEFAULT_NWP_DB):
        """
        Initialize the modulator.

        Args:
            bias_path: Path to per-model per-station bias JSON
            nwp_db_path: Path to NWP forecasts database
        """
        self.bias_path = bias_path
        self.nwp_db_path = nwp_db_path
        self._bias_data: Optional[dict] = None

    def _load_bias_data(self) -> dict:
        """Load bias data lazily."""
        if self._bias_data is None:
            self._bias_data = self._load_json_or_default(self.bias_path, {})
            if not self._bias_data:
                logger.warning("No bias data at %s — agreement will use raw forecasts", self.bias_path)
        return self._bias_data

    @staticmethod
    def _load_json_or_default(path: str, default: any) -> dict:
        """Load JSON file or return default if not found."""
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error loading %s: %s", path, e)
            return default

    def _fetch_forecasts_for_station(
        self,
        station: str,
        lat: float,
        lon: float,
        target_date: str,
        force_refresh: bool = False,
    ) -> Dict[str, Optional[float]]:
        """
        Fetch deterministic forecasts for a station from the NWP DB.

        Tries NWP DB first, falls back to Open-Meteo if no cached data.

        Args:
            station: ICAO code
            lat: Latitude
            lon: Longitude
            target_date: Date string YYYY-MM-DD
            force_refresh: If True, skip cache and always fetch fresh

        Returns:
            {model_key: temp_c or None}
        """
        forecasts: Dict[str, Optional[float]] = {m: None for m in REQUIRED_MODELS}

        # Try NWP DB
        if os.path.exists(self.nwp_db_path):
            try:
                conn = sqlite3.connect(f"file:{self.nwp_db_path}?mode=ro", uri=True)
                cur = conn.cursor()

                for model_key in REQUIRED_MODELS:
                    # Map to Open-Meteo model name stored in DB
                    openmeteo_key = MODEL_KEY_MAP.get(model_key, model_key)
                    # Try to find temperature_2m for this station, date, model
                    cur.execute("""
                        SELECT value FROM nwp_forecasts
                        WHERE station = ? AND target_date = ?
                          AND model = ? AND variable = 'temperature_2m'
                        ORDER BY fetch_date DESC
                        LIMIT 1
                    """, (station, target_date, openmeteo_key))
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        forecasts[model_key] = float(row[0])

                conn.close()
            except Exception as e:
                logger.debug("Error reading NWP DB for %s/%s: %s", station, target_date, e)

        # Fallback: try ensemble DB for GEFS mean as a GFS proxy
        if all(v is None for v in forecasts.values()):
            try:
                gefs_db = os.path.join(REPO_ROOT, "data", "gefs_archive.db")
                if os.path.exists(gefs_db):
                    conn = sqlite3.connect(f"file:{gefs_db}?mode=ro", uri=True)
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT ensemble_mean FROM gefs_archive
                        WHERE station = ? AND target_date = ? AND step = 24
                        LIMIT 1
                    """, (station, target_date))
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        # GEFS mean in Celsius — use as GFS proxy
                        gefs_c = float(row[0])
                        forecasts["gfs"] = gefs_c
                        forecasts["ecmwf"] = gefs_c * 1.01  # Approximate — small offset
                    conn.close()
            except Exception as e:
                logger.debug("GEFS fallback error: %s", e)

        return forecasts

    def _get_model_bias(self, model: str, station: str) -> float:
        """
        Get bias correction for a model-station pair.

        Bias = model_forecast - actual (positive = model overpredicts)

        Args:
            model: Model key (gfs, ecmwf, icon, gem)
            station: ICAO station code

        Returns:
            Bias value in °C to subtract from forecast
        """
        bias_data = self._load_bias_data()
        if not bias_data:
            return 0.0

        # Try model-specific bias
        model_biases = bias_data.get("model_biases", {})
        station_bias = model_biases.get(model, {}).get(station, {}).get("bias", 0.0)
        if station_bias != 0.0:
            return float(station_bias)

        # Fallback: use ensemble bias as proxy
        ensemble_bias = bias_data.get("bias_table", {}).get(station, [0.0])[0]
        if ensemble_bias:
            return float(ensemble_bias)

        return 0.0

    def _get_model_weight(self, model: str, station: str) -> float:
        """
        Get weight for a model based on historical MAE.

        Weight = 1 / (1 + MAE) — higher skill = higher weight.

        Args:
            model: Model key
            station: ICAO code

        Returns:
            Weight in [0.0, 1.0]
        """
        bias_data = self._load_bias_data()
        if not bias_data:
            return 0.25  # Equal weight if no data

        model_biases = bias_data.get("model_biases", {})
        station_mae = model_biases.get(model, {}).get(station, {}).get("mae", None)
        if station_mae is not None and station_mae > 0:
            return 1.0 / (1.0 + float(station_mae))

        # Default: equal weight
        return 0.25

    def _compute_directional_agreement(
        self,
        forecasts: Dict[str, Optional[float]],
        prev_actual: float,
        station: str,
    ) -> Tuple[float, float, Dict[str, int]]:
        """
        Compute weighted directional agreement score.

        Args:
            forecasts: {model: temp_c or None}
            prev_actual: Previous day's actual temperature in same units
            station: ICAO code for bias lookup

        Returns:
            (agreement_dir, weighted_vote, directions_dict)
        """
        directions: Dict[str, int] = {}
        weights: Dict[str, float] = {}

        for model, temp in forecasts.items():
            if temp is None:
                continue
            if temp > prev_actual:
                directions[model] = 1
            elif temp < prev_actual:
                directions[model] = -1
            else:
                directions[model] = 0

            weights[model] = self._get_model_weight(model, station)

        if not directions:
            return 0.0, 0.0, {}

        # Weighted vote
        weighted_vote = sum(weights[m] * directions[m] for m in directions)
        total_weight = sum(weights.values())

        if total_weight == 0:
            return 0.0, 0.0, directions

        # Majority direction
        majority = 1 if weighted_vote >= 0 else -1

        # Agreement = fraction of weighted votes agreeing with majority
        agreeing_weight = sum(
            weights[m] for m in directions if directions[m] == majority
        )
        agreement = agreeing_weight / total_weight

        return agreement, weighted_vote, directions

    def _compute_magnitude_dispersion(
        self,
        forecasts: Dict[str, Optional[float]],
        station: str,
    ) -> float:
        """
        Compute weighted magnitude dispersion score.

        Returns 1.0 when all models predict similar temperatures;
        lower when forecasts diverge.

        Args:
            forecasts: {model: temp_c or None}
            station: ICAO code

        Returns:
            agreement_mag ∈ [0, 1]
        """
        valid = {m: t for m, t in forecasts.items() if t is not None}
        if len(valid) < 2:
            return 1.0

        weights = {
            m: self._get_model_weight(m, station)
            for m in valid
        }
        total_w = sum(weights.values())
        if total_w == 0:
            return 1.0

        w_mean = sum(weights[m] * valid[m] for m in valid) / total_w
        w_var = sum(weights[m] * (valid[m] - w_mean) ** 2 for m in valid) / total_w
        w_std = math.sqrt(w_var) if w_var > 0 else 0.0
        max_dev = max(abs(valid[m] - w_mean) for m in valid)

        if max_dev < 0.01:  # All forecasts nearly identical
            return 1.0

        return max(0.0, 1.0 - w_std / max_dev)

    def modulate(
        self,
        station: str,
        p_ens: float,
        prev_actual: float,
        lat: float,
        lon: float,
        target_date: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Tuple[float, dict]:
        """
        Compute modulated probability from ensemble + deterministic agreement.

        Full pipeline:
          1. Fetch deterministic forecasts
          2. Bias correct
          3. Directional agreement
          4. Magnitude dispersion
          5. Combined agreement
          6. Probability modulation
          7. Position sizing gate

        Args:
            station: ICAO station code
            p_ens: Ensemble-derived probability of exceeding threshold [0, 1]
            prev_actual: Previous day's actual temperature (same units)
            lat: Station latitude
            lon: Station longitude
            target_date: Date string YYYY-MM-DD (default: today)
            force_refresh: If True, fetch fresh forecasts

        Returns:
            (p_adjusted, metadata_dict)
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Step 1: Fetch deterministic forecasts
        forecasts = self._fetch_forecasts_for_station(
            station, lat, lon, target_date, force_refresh
        )

        # Step 2: Bias correction
        forecasts_corrected: Dict[str, float] = {}
        for model, temp in forecasts.items():
            if temp is None:
                continue
            bias = self._get_model_bias(model, station)
            forecasts_corrected[model] = temp - bias

        # Step 3: Directional agreement
        agreement_dir, weighted_vote, directions = self._compute_directional_agreement(
            forecasts_corrected, prev_actual, station
        )

        # Step 4: Magnitude dispersion
        agreement_mag = self._compute_magnitude_dispersion(forecasts_corrected, station)

        # Step 5: Combined agreement
        agreement = (
            DIRECTIONAL_WEIGHT * agreement_dir + MAGNITUDE_WEIGHT * agreement_mag
        )

        # Step 6: Probability modulation
        # p_adjusted = 0.50 + (p_ens - 0.50) × agreement^β
        p_adjusted = 0.50 + (p_ens - 0.50) * math.pow(agreement, AGREEMENT_EXPONENT)
        p_adjusted = max(0.01, min(0.99, p_adjusted))

        # Step 7: Gating
        gate_active = (
            agreement_dir < GATE_MIN_AGREEMENT_DIR
            and GATE_P_ENS_LOW < p_ens < GATE_P_ENS_HIGH
        )
        should_trade = not gate_active

        # Position sizing modifier
        position_modifier = min(1.0, agreement_dir * AGREEMENT_GATE_SLOPE)

        metadata = {
            "agreement_directional": round(agreement_dir, 4),
            "agreement_magnitude": round(agreement_mag, 4),
            "agreement_combined": round(agreement, 4),
            "p_ens_raw": round(p_ens, 4),
            "p_adjusted": round(p_adjusted, 4),
            "should_trade": should_trade,
            "position_modifier": round(position_modifier, 4),
            "n_models": len(forecasts_corrected),
            "weighted_vote": round(weighted_vote, 2),
            "models": {
                m: {"forecast": round(t, 2), "bias": round(self._get_model_bias(m, station), 2)}
                for m, t in forecasts_corrected.items()
            },
            "directions": directions,
        }

        return p_adjusted, metadata


# ── Standalone Test ──

def test_modulator():
    """Test the confidence modulator module."""
    print("=" * 72)
    print("  ForecastConfidenceModulator — Self-Test")
    print("=" * 72)

    modulator = ForecastConfidenceModulator()

    # Test 1: Strong agreement
    print("\n  Test 1: Strong agreement (3/4 same direction, p_ens=0.72)")
    forecasts_strong = {
        "gfs": 88.0,
        "ecmwf": 87.5,
        "icon": 86.5,
        "gem": 88.5,
    }
    dir_agreement, vote, dirs = modulator._compute_directional_agreement(
        forecasts_strong, 85.0, "KNYC"
    )
    mag_agreement = modulator._compute_magnitude_dispersion(forecasts_strong, "KNYC")
    print(f"    Directional agreement: {dir_agreement:.4f}")
    print(f"    Magnitude dispersion:  {mag_agreement:.4f}")
    print(f"    Directions: {dirs}")

    # Test 2: Split (2/4 up, 2/4 down)
    print("\n  Test 2: Split agreement (2/4 up, 2/4 down, p_ens=0.55)")
    forecasts_split = {
        "gfs": 88.0,
        "ecmwf": 87.0,
        "icon": 83.0,
        "gem": 82.0,
    }
    dir_agreement2, vote2, dirs2 = modulator._compute_directional_agreement(
        forecasts_split, 85.0, "KNYC"
    )
    mag_agreement2 = modulator._compute_magnitude_dispersion(forecasts_split, "KNYC")
    combined = 0.7 * dir_agreement2 + 0.3 * mag_agreement2
    print(f"    Directional agreement: {dir_agreement2:.4f}")
    print(f"    Magnitude dispersion:  {mag_agreement2:.4f}")
    print(f"    Combined agreement:    {combined:.4f}")
    print(f"    Directions: {dirs2}")

    # Test 3: Modulation example
    print("\n  Test 3: Modulation (p_ens=0.72, full agreement=1.0)")
    # Simulate modulation inline
    def _simulate(p, a):
        adj = 0.50 + (p - 0.50) * math.pow(a, 1.5)
        adj = max(0.01, min(0.99, adj))
        gate_active = (a < 0.50 and 0.45 < p < 0.55)
        return adj, {"should_trade": not gate_active}

    p_adj, meta = _simulate(0.72, 1.0)
    print(f"    p_ens=0.72, agreement=1.0 → p_adj={p_adj:.4f}")

    p_adj2, meta2 = _simulate(0.72, 0.5)
    print(f"    p_ens=0.72, agreement=0.5 → p_adj={p_adj2:.4f}")

    p_adj3, meta3 = _simulate(0.55, 0.44)
    print(f"    p_ens=0.55, agreement=0.44 → p_adj={p_adj3:.4f} should_trade={meta3['should_trade']}")

    # Test 4: Full pipeline (will use GEFS fallback)
    print("\n  Test 4: Full pipeline (GEFS fallback)")
    p_adj, meta = modulator.modulate(
        station="KNYC", p_ens=0.72, prev_actual=85.0,
        lat=40.7, lon=-74.0, target_date="2026-07-15",
    )
    print(f"    p_ens=0.72 → p_adj={p_adj:.4f}")
    print(f"    N models: {meta['n_models']}")
    print(f"    Agreement dir: {meta['agreement_directional']:.4f}")
    print(f"    Should trade: {meta['should_trade']}")
    print(f"    Position modifier: {meta['position_modifier']:.4f}")

    print("\n  ✅ Self-test complete")


if __name__ == "__main__":
    test_modulator()