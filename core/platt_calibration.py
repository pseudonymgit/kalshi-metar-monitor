#!/usr/bin/env python3
"""
Calibration Phase 1 + Phase 2: Platt scaling + market split + climate groups.

Phase 1 (baseline):
1. PlattCalibrator — 2-param logistic MLE with Beta(2,2) regularization
2. Market type split (HIGH/LOW)
3. Climate group pooling (5 groups, 20 stations)
4. Signal family pooling (GEFS / heuristic)
5. MIN_SAMPLES 200→50, remove 0.01 no-change zone
6. 7-level hierarchical fallback cascade
7. 10-bin diagnostics layer + dual-method validation

Phase 2 (this file):
8. Regime-aware calibration — SYNOPTIC_REGIMES dimension via
   SynopticRegimeDetector (cloud cover, pressure tendency, wind direction)
9. Seasonal split — SEASONS (winter/spring/summer/fall) dimension
10. Nowcasting integration — nowcast_path() uses current METAR observations
    to activate the regime-specific calibration curve
11. Drift diagnostics — check_drift() flags cells where |Δα|>0.5 or |Δβ|>0.3

Fallback order: regime-aware → season-aware → Phase 1 (L1..L6) → identity.

Consumes: data/calibration_curves.json (v2, read-only)
Produces: data/platt_calibration.json (v3)

B-Mode: No AI/ML models in the loop. Pure numpy/scipy MLE + rule-based regimes.
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

MIN_SAMPLES = 50  # Reduced from 200
MIN_SAMPLES_BIN_DIAGNOSTIC = 30  # Minimum for bin-level diagnostics
REGULARIZATION_THRESHOLD = 200  # Beta(2,2) prior applies when n < 200
DIVERGENCE_FLAG_THRESHOLD = 0.10  # Flag if Platt vs bin empirics diverge > 0.10

N_BINS = 10
BIN_EDGES = np.array([0.50 + i * 0.05 for i in range(N_BINS)] + [1.00])

# Climate groups (5 groups × 20 stations)
CLIMATE_GROUPS: Dict[str, List[str]] = {
    "desert":          ["KPHX", "KLAS", "KAUS", "KSAT"],
    "coastal_warm":    ["KLAX", "KSFO", "KSEA"],
    "coastal_humid":   ["KHOU", "KMIA", "KMSY", "KATL"],
    "continental":     ["KNYC", "KBOS", "KDCA", "KPHL", "KMDW"],
    "interior":        ["KDEN", "KDFW", "KOKC", "KMSP"],
}

# Build reverse map: station → climate_group
STATION_TO_GROUP: Dict[str, str] = {}
for group_name, stations in CLIMATE_GROUPS.items():
    for s in stations:
        STATION_TO_GROUP[s] = group_name

# ──────────────────────────────────────────────
# Phase 2: Season + Regime constants
# ──────────────────────────────────────────────

SEASONS = ["winter", "spring", "summer", "fall"]
SEASON_MONTHS = {
    "winter": [12, 1, 2],
    "spring": [3, 4, 5],
    "summer": [6, 7, 8],
    "fall":   [9, 10, 11],
}

SYNOPTIC_REGIMES = ["quiescent", "frontal", "cold_advection", "convective", "unknown"]

REGIME_CALIBRATION_ENABLED = True
SEASON_CALIBRATION_ENABLED = True

# Drift detection thresholds
DRIFT_ALPHA_THRESHOLD = 0.5   # Flag if |Δα| > 0.5
DRIFT_BETA_THRESHOLD = 0.3    # Flag if |Δβ| > 0.3

# Signal families
# GEFS-family: signals that derive from NWP ensemble model output
GEFS_SIGNALS = {
    "forecast_disagreement", "ensemble_fraction", "nwp_direct",
    "nwp_dtdt_fusion", "ai_composite", "spread_based_entry",
    "nwp_analog", "ecmwf_bias_corrected", "hrrr_bias_corrected",
    "multi_model_ensemble", "nine_signal_ensemble",
}

# Heuristic-family: signals derived from METAR observations, historical
# climatology, time-series patterns, or rule-based reasoning
HEURISTIC_SIGNALS = {
    "gaussian", "gaussian_v2", "goldilocks", "persistence",
    "pressure_delta", "calendar_climatology", "wind_direction_shift",
    "frontal_detector", "frontal_passage_intraday", "frontal_passage_detector",
    "frontal_passage_nowcast", "intraday_metar_confirmation",
    "fogr_reversion", "metar_dtdt", "pressure_tendency",
    "settlement_arbitrage", "volume_momentum", "seasonal_regime",
    "regime", "reversion", "spike_reversion", "simple_trend",
    "temperature_advection", "metar_nowcast", "dewpoint_depression_modulator",
    "dual_polarity", "esdr", "late_day_momentum",
    # Big Sweep 2026-08-07 additions
    "metar_trend", "trajectory_lane", "eighty_two_member_ensemble",
    "eighty_two_member_ensemble_ece", "eighty_two_member_ensemble_pooled",
}

ALL_STATIONS = sorted(STATION_TO_GROUP.keys())

DIRECTIONS = ["up", "down"]
MARKET_TYPES = ["HIGH", "LOW"]
SIGNAL_FAMILIES = ["gefs", "heuristic"]


def get_signal_family(signal_name: str) -> str:
    """Return 'gefs' or 'heuristic' for a given signal name."""
    if signal_name in GEFS_SIGNALS:
        return "gefs"
    return "heuristic"


def get_climate_group(station: str) -> str:
    """Return climate group name for a station. Falls back to 'interior' if unknown."""
    return STATION_TO_GROUP.get(station, "interior")


def logit_transform(p: np.ndarray) -> np.ndarray:
    """Safe logit: ln(p / (1-p)), clamps to avoid inf."""
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def platt_function(logits: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    """P(correct) = 1 / (1 + exp(-(alpha * logit + beta)))"""
    return expit(alpha * logits + beta)


def get_season_from_month(month: int) -> str:
    """Return season name (winter/spring/summer/fall) from month number (1-12)."""
    for season, months in SEASON_MONTHS.items():
        if month in months:
            return season
    return "fall"  # defensive fallback, never reached for valid 1-12


def get_season_from_date(date_str: str) -> str:
    """Return season name from a date string.

    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SSZ' (ISO 8601). Month is
    drawn from chars [5:7] in both forms.
    """
    try:
        month = int(str(date_str)[5:7])
    except (ValueError, IndexError):
        return "summer"  # defensive fallback
    return get_season_from_month(month)


# ──────────────────────────────────────────────
# PlattCalibrator — core 2-param logistic model
# ──────────────────────────────────────────────

class PlattCalibrator:
    """
    2-parameter logistic calibration (Platt scaling).

    P(correct) = 1 / (1 + exp(-(alpha * logit(p_raw) + beta)))

    Fit via scipy MLE (L-BFGS-B).
    Beta(2,2) prior regularization for 50 ≤ n < 200.
    """

    def __init__(self, alpha: float = 1.0, beta: float = 0.0, n: int = 0):
        self.alpha = alpha
        self.beta = beta
        self.n = n
        self.fitted = False

    @classmethod
    def from_data(cls, raw_logits: np.ndarray, outcomes: np.ndarray) -> "PlattCalibrator":
        """Fit a new calibrator from raw logits and binary outcomes."""
        cal = cls()
        cal.fit(raw_logits, outcomes)
        return cal

    def fit(self, raw_logits: np.ndarray, outcomes: np.ndarray) -> None:
        """Fit α and β via MLE on logit(p_raw) → binary outcome, with regularization."""
        raw_logits = np.asarray(raw_logits, dtype=np.float64)
        outcomes = np.asarray(outcomes, dtype=np.float64)
        n = len(raw_logits)

        if n < 2:
            self.alpha, self.beta = 1.0, 0.0
            self.n = n
            self.fitted = True
            return

        def neg_log_likelihood(params):
            a, b = params
            p = platt_function(raw_logits, a, b)
            p = np.clip(p, 1e-9, 1 - 1e-9)

            # MLE: -sum(outcomes * log(p) + (1-outcomes) * log(1-p))
            nll = -np.sum(outcomes * np.log(p) + (1 - outcomes) * np.log(1 - p))

            # Beta(2,2) prior regularization for n < 200
            if n < REGULARIZATION_THRESHOLD:
                # Beta(2,2) density: proportional to (p * (1-p))^(2-1) = p * (1-p)
                # log-prior = sum(log(p) + log(1-p)) = sum(log(p * (1-p)))
                # This pulls toward p=0.5 → α=1, β=0
                prior = -np.sum(np.log(p * (1-p) + 1e-9))
                # Scale prior weight: strong when n is small, weak as n→200
                lam = 1.0 - n / REGULARIZATION_THRESHOLD
                nll += lam * prior

            return nll

        result = minimize(
            neg_log_likelihood,
            [1.0, 0.0],
            method='L-BFGS-B',
            bounds=[(0.01, 5.0), (-3.0, 3.0)],
            options={'maxiter': 500, 'ftol': 1e-8}
        )

        self.alpha, self.beta = result.x
        self.n = n
        self.fitted = True

    def transform(self, raw_logits: np.ndarray) -> np.ndarray:
        """Apply Platt scaling to raw logits. No regularization at transform time."""
        return platt_function(raw_logits, self.alpha, self.beta)

    def to_dict(self) -> dict:
        return {"alpha": round(self.alpha, 4), "beta": round(self.beta, 4), "n": self.n}

    @classmethod
    def from_dict(cls, d: dict) -> "PlattCalibrator":
        return cls(alpha=d["alpha"], beta=d["beta"], n=d["n"])


# ──────────────────────────────────────────────
# 10-bin diagnostics
# ──────────────────────────────────────────────

def compute_bin_diagnostics(
    raw_confs: np.ndarray,
    outcomes: np.ndarray,
    calibrator: PlattCalibrator,
) -> Tuple[List[dict], float, float]:
    """
    Compute 10-bin reliability diagnostics for a cell.

    Returns:
        bins: list of bin dicts with confidence range, n, p_correct, ci, ece_contribution
        ece: Expected Calibration Error (n-weighted)
        brier: Brier score
    """
    raw_logits = logit_transform(raw_confs)
    cal_confs = calibrator.transform(raw_logits)

    bins = []
    total_n = len(cal_confs)
    ece_total = 0.0
    brier_total = 0.0

    for i in range(N_BINS):
        lo = BIN_EDGES[i]
        hi = BIN_EDGES[i + 1]
        mask = (cal_confs >= lo) & (cal_confs < hi)
        bin_n = int(np.sum(mask))

        if bin_n < MIN_SAMPLES_BIN_DIAGNOSTIC:
            bins.append({
                "conf_low": round(lo, 2),
                "conf_high": round(hi, 2),
                "n": bin_n,
                "p_correct": None,
                "ci_low": None,
                "ci_high": None,
                "ece_contribution": 0.0,
            })
            continue

        bin_outcomes = outcomes[mask]
        bin_confs = cal_confs[mask]
        p_correct = float(np.mean(bin_outcomes))
        avg_conf = float(np.mean(bin_confs))

        # Wilson score interval
        z = 1.96
        p_hat = p_correct
        denom = 1 + z**2 / bin_n
        center = (p_hat + z**2 / (2 * bin_n)) / denom
        margin = z * np.sqrt(p_hat * (1 - p_hat) / bin_n + z**2 / (4 * bin_n**2)) / denom
        ci_low = max(0.0, center - margin)
        ci_high = min(1.0, center + margin)

        ece_contrib = (bin_n / total_n) * abs(p_correct - avg_conf)
        ece_total += ece_contrib

        bins.append({
            "conf_low": round(lo, 2),
            "conf_high": round(hi, 2),
            "n": bin_n,
            "p_correct": round(p_correct, 4),
            "ci_low": round(ci_low, 4),
            "ci_high": round(ci_high, 4),
            "ece_contribution": round(ece_contrib, 6),
        })

    # Brier score
    for conf, outcome in zip(cal_confs, outcomes):
        brier_total += (float(outcome) - float(conf)) ** 2
    brier = brier_total / total_n if total_n > 0 else 0.0

    return bins, ece_total, brier


def check_platt_bin_divergence(
    calibrator: PlattCalibrator,
    raw_confs: np.ndarray,
    outcomes: np.ndarray,
) -> bool:
    """
    Dual-method validation: check if Platt curve and bin empirics diverge > 0.10.
    Returns True if divergence is OK (no flag), False if flagged.
    """
    raw_logits = logit_transform(raw_confs)
    cal_confs = calibrator.transform(raw_logits)

    for i in range(N_BINS):
        lo = BIN_EDGES[i]
        hi = BIN_EDGES[i + 1]
        mask = (cal_confs >= lo) & (cal_confs < hi)
        bin_n = int(np.sum(mask))
        if bin_n < MIN_SAMPLES_BIN_DIAGNOSTIC:
            continue

        bin_outcomes = outcomes[mask]
        bin_confs = cal_confs[mask]
        p_empirical = float(np.mean(bin_outcomes))
        p_platt = float(np.mean(bin_confs))

        if abs(p_empirical - p_platt) > DIVERGENCE_FLAG_THRESHOLD:
            return False  # Flagged

    return True  # OK


# ──────────────────────────────────────────────
# Phase 2: SynopticRegimeDetector — METAR-based regime classification
# ──────────────────────────────────────────────

class SynopticRegimeDetector:
    """
    Detects current synoptic regime from METAR-derived observations.

    Uses: cloud cover, pressure tendency, wind direction, temperature trend.

    Regime types:
    - quiescent:  stable conditions, no significant weather
    - frontal:    active frontal passage (pressure + wind shift)
    - cold_advection: post-frontal cooling (rising pressure, dropping temp)
    - convective:  unstable conditions, high cloud cover, falling pressure
    - unknown:     insufficient data to classify

    B-Mode compliant: pure rule-based, no AI/ML in the loop.
    """

    # Thresholds (aligned with FrontalDetectorSignal)
    PRESSURE_THRESHOLD = 1.5      # mb change in 3h
    WIND_SHIFT_THRESHOLD = 45.0   # degrees
    CLOUD_COVER_THRESHOLD = 0.6   # fraction (60%+ coverage)
    TEMP_DROP_THRESHOLD = 3.0     # °F drop in 3h

    @classmethod
    def detect_regime(
        cls,
        pressure_tendency_3h: Optional[float] = None,
        wind_direction_shift: Optional[float] = None,
        cloud_cover_pct: Optional[float] = None,
        temp_trend_3h: Optional[float] = None,
    ) -> str:
        """
        Detect synoptic regime from METAR-derived observations.

        Args:
            pressure_tendency_3h: Pressure change in last 3 hours (mb).
                Positive = rising, Negative = falling.
            wind_direction_shift: Wind direction change (degrees).
            cloud_cover_pct: Cloud cover fraction (0-1).
            temp_trend_3h: Temperature change in last 3 hours (°F).
                Positive = warming, Negative = cooling.

        Returns:
            Regime string: 'quiescent', 'frontal', 'cold_advection',
            'convective', or 'unknown'.
        """
        # Count available signals; need at least 1 to classify
        n_avail = sum(
            1 for s in [pressure_tendency_3h, wind_direction_shift, cloud_cover_pct]
            if s is not None
        )
        if n_avail < 1:
            return "unknown"

        # Boolean features
        has_frontal_pressure = (
            pressure_tendency_3h is not None
            and abs(pressure_tendency_3h) > cls.PRESSURE_THRESHOLD
        )
        has_frontal_wind = (
            wind_direction_shift is not None
            and abs(wind_direction_shift) > cls.WIND_SHIFT_THRESHOLD
        )
        has_high_cloud = (
            cloud_cover_pct is not None
            and cloud_cover_pct > cls.CLOUD_COVER_THRESHOLD
        )
        has_cold_advection_temp = (
            temp_trend_3h is not None
            and temp_trend_3h < -cls.TEMP_DROP_THRESHOLD
        )
        is_rising_pressure = (
            pressure_tendency_3h is not None
            and pressure_tendency_3h > cls.PRESSURE_THRESHOLD
        )
        is_falling_pressure = (
            pressure_tendency_3h is not None
            and pressure_tendency_3h < -cls.PRESSURE_THRESHOLD
        )

        # Decision logic (priority order: most specific → generic)
        if has_frontal_pressure and has_frontal_wind:
            return "frontal"

        if is_rising_pressure and has_cold_advection_temp:
            return "cold_advection"

        if is_falling_pressure and has_high_cloud:
            return "convective"

        if has_frontal_pressure:
            return "frontal"

        if has_frontal_wind:
            return "frontal"

        if has_high_cloud:
            return "convective"

        return "quiescent"

    @classmethod
    def detect_regime_from_metar_record(cls, record: dict) -> str:
        """
        Detect regime from a METAR observation record dict.

        Expected keys:
            pressure_tendency_3h (float): mb change in 3h
            wind_dir_shift_3h (float): degrees
            cloud_cover_pct (float): 0-1
            temp_trend_3h (float): °F
        """
        return cls.detect_regime(
            pressure_tendency_3h=record.get("pressure_tendency_3h"),
            wind_direction_shift=record.get("wind_dir_shift_3h"),
            cloud_cover_pct=record.get("cloud_cover_pct"),
            temp_trend_3h=record.get("temp_trend_3h"),
        )


# ──────────────────────────────────────────────
# PlattCalibrationPipeline — full system
# ──────────────────────────────────────────────

class PlattCalibrationPipeline:
    """
    Full Platt scaling calibration pipeline with:

    7-level hierarchical fallback:
        L1: direction × market × signal_family × climate_group
        L2: direction × market × signal_family
        L3: direction × market (pooled signals)
        L4: direction (pooled markets + signals)
        L5: direction × signal_family (pooled markets)
        L6: _global (all data, all directions)
        L7: identity (p_calibrated = p_raw, clamped to [0.5, 0.95])

    Data model v3: platt_calibration.json
    """

    def __init__(self):
        # Internal storage: key → list of (raw_conf, outcome_bool)
        self._data: Dict[str, list] = defaultdict(list)

        # Fitted calibrators: key → PlattCalibrator
        self._calibrators: Dict[str, PlattCalibrator] = {}
        self._fallback_calibrators: Dict[str, PlattCalibrator] = {}

        # Regime/season-aware calibrators (Phase 2)
        self._regime_calibrators: Dict[str, PlattCalibrator] = {}
        self._season_calibrators: Dict[str, PlattCalibrator] = {}

        # Bin diagnostics per cell
        self._bins: Dict[str, List[dict]] = {}
        self._ece: Dict[str, float] = {}
        self._brier: Dict[str, float] = {}

        # Metadata
        self.refitted = False
        self.generated_ts: Optional[str] = None
        self.flagged_cells: List[str] = []
        self.cells_on_fallback: int = 0
        self.total_cells: int = 0

        # Phase 2 metadata
        self.regime_cells_on_fallback: int = 0
        self.regime_total_cells: int = 0
        self.season_cells_on_fallback: int = 0
        self.season_total_cells: int = 0

    # ── Key helpers ────────────────────────────────────────────

    def _cell_key(self, direction: str, market_type: str,
                  signal_family: str, climate_group: str) -> str:
        """L1 key: direction_market_family_group"""
        return f"{direction}_{market_type}_{signal_family}_{climate_group}"

    def _cell_key_regime(self, direction: str, market_type: str,
                         signal_family: str, climate_group: str,
                         regime: str) -> str:
        """Regime-aware key: direction_market_family_group_regime"""
        return f"{direction}_{market_type}_{signal_family}_{climate_group}_{regime}"

    def _cell_key_season(self, direction: str, market_type: str,
                         signal_family: str, climate_group: str,
                         season: str) -> str:
        """Season-aware key: direction_market_family_group_season"""
        return f"{direction}_{market_type}_{signal_family}_{climate_group}_{season}"

    def _l2_key(self, direction: str, market_type: str, signal_family: str) -> str:
        return f"{direction}_{market_type}_{signal_family}"

    def _l3_key(self, direction: str, market_type: str) -> str:
        return f"{direction}_{market_type}"

    def _l4_key(self, direction: str) -> str:
        return direction

    def _l5_key(self, direction: str, signal_family: str) -> str:
        return f"{direction}_{signal_family}"

    # ── Data ingestion ─────────────────────────────────────────

    def record_outcome(
        self,
        station: str,
        direction: str,
        market_type: str,
        signal_name: str,
        raw_conf: float,
        was_correct: bool,
        regime: Optional[str] = None,
        season: Optional[str] = None,
    ) -> None:
        """
        Record a single trading outcome. Stores in all relevant pooling buckets.

        Phase 2 extension: accepts optional regime and season labels.
        When provided, stores regime-specific and season-specific data.
        """
        group = get_climate_group(station)
        family = get_signal_family(signal_name)

        # L1: direction × market × signal_family × climate_group
        l1_key = self._cell_key(direction, market_type, family, group)
        self._data[l1_key].append((raw_conf, was_correct))

        # L2: direction × market × signal_family (no group)
        l2_key = self._l2_key(direction, market_type, family)
        self._data[l2_key].append((raw_conf, was_correct))

        # L3: direction × market (pooled signals)
        l3_key = self._l3_key(direction, market_type)
        self._data[l3_key].append((raw_conf, was_correct))

        # L4: direction (pooled markets + signals)
        l4_key = self._l4_key(direction)
        self._data[l4_key].append((raw_conf, was_correct))

        # L5: direction × signal_family (pooled markets)
        l5_key = self._l5_key(direction, family)
        self._data[l5_key].append((raw_conf, was_correct))

        # L6: _global (all data)
        self._data["_global"].append((raw_conf, was_correct))

        # Phase 2: regime-specific storage
        if regime is not None:
            regime_key = self._cell_key_regime(direction, market_type, family, group, regime)
            self._data[regime_key].append((raw_conf, was_correct))

        # Phase 2: season-specific storage
        if season is not None:
            season_key = self._cell_key_season(direction, market_type, family, group, season)
            self._data[season_key].append((raw_conf, was_correct))

    def record_outcomes_batch(
        self,
        records: List[Tuple],
    ) -> None:
        """Batch-insert many outcomes for efficiency.

        Each record may be a 6-tuple (Phase 1) or an 8-tuple
        (Phase 2: adds regime and season labels).
        """
        for r in records:
            if len(r) == 8:
                self.record_outcome(
                    r[0], r[1], r[2], r[3], r[4], r[5],
                    regime=r[6], season=r[7],
                )
            elif len(r) == 6:
                self.record_outcome(*r)
            else:
                raise ValueError(f"record_outcomes_batch: record must be 6 or 8 elements, got {len(r)}")

    # ── Refit ──────────────────────────────────────────────────

    def refit(self) -> None:
        """
        Fit all calibrators from accumulated data.
        Walks the 7-level fallback hierarchy (Phase 1),
        plus Phase 2 regime-aware and seasonal calibrators.
        """
        self._calibrators.clear()
        self._fallback_calibrators.clear()
        self._bins.clear()
        self._ece.clear()
        self._brier.clear()
        self.flagged_cells.clear()
        self.cells_on_fallback = 0
        self.total_cells = 0

        # Phase 2: regime and season calibrator storage
        self._regime_calibrators.clear()
        self._season_calibrators.clear()
        self.regime_cells_on_fallback = 0
        self.regime_total_cells = 0
        self.season_cells_on_fallback = 0
        self.season_total_cells = 0

        # Phase 1 fitting: L1 → L2 → L3 → L5 → L4 → L6
        self._fit_level_cells()
        self._fit_l2()
        self._fit_l3()
        self._fit_l5()
        self._fit_l4()
        self._fit_global()

        # Phase 2 fitting: regime-aware and seasonal cells
        if REGIME_CALIBRATION_ENABLED:
            self._fit_regime_cells()
        if SEASON_CALIBRATION_ENABLED:
            self._fit_season_cells()

        self.refitted = True
        self.generated_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Count Phase 1 cells on fallback
        attempted_l1 = 0
        fell_back = 0
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group in CLIMATE_GROUPS.keys():
                        attempted_l1 += 1
                        l1_key = self._cell_key(direction, mt, fam, group)
                        if l1_key not in self._calibrators:
                            fell_back += 1
        self.total_cells = attempted_l1
        self.cells_on_fallback = fell_back

        # Count Phase 2 regime cells on fallback
        if REGIME_CALIBRATION_ENABLED and SYNOPTIC_REGIMES:
            attempted_regime = 0
            regime_fell_back = 0
            for direction in DIRECTIONS:
                for mt in MARKET_TYPES:
                    for fam in SIGNAL_FAMILIES:
                        for group in CLIMATE_GROUPS.keys():
                            for regime in SYNOPTIC_REGIMES:
                                if regime == "unknown":
                                    continue
                                attempted_regime += 1
                                r_key = self._cell_key_regime(direction, mt, fam, group, regime)
                                if r_key not in self._regime_calibrators:
                                    regime_fell_back += 1
            self.regime_total_cells = attempted_regime
            self.regime_cells_on_fallback = regime_fell_back

        # Count Phase 2 season cells on fallback
        if SEASON_CALIBRATION_ENABLED and SEASONS:
            attempted_season = 0
            season_fell_back = 0
            for direction in DIRECTIONS:
                for mt in MARKET_TYPES:
                    for fam in SIGNAL_FAMILIES:
                        for group in CLIMATE_GROUPS.keys():
                            for season in SEASONS:
                                attempted_season += 1
                                s_key = self._cell_key_season(direction, mt, fam, group, season)
                                if s_key not in self._season_calibrators:
                                    season_fell_back += 1
            self.season_total_cells = attempted_season
            self.season_cells_on_fallback = season_fell_back

    def _fit_cell(self, key: str) -> Optional[PlattCalibrator]:
        """Fit a PlattCalibrator for a key if data >= MIN_SAMPLES."""
        records = self._data.get(key, [])
        if len(records) < MIN_SAMPLES:
            return None

        raw_confs = np.array([r[0] for r in records], dtype=np.float64)
        outcomes = np.array([float(r[1]) for r in records], dtype=np.float64)
        raw_logits = logit_transform(raw_confs)

        calibrator = PlattCalibrator.from_data(raw_logits, outcomes)

        # Compute bin diagnostics
        bins, ece_val, brier_val = compute_bin_diagnostics(raw_confs, outcomes, calibrator)
        self._bins[key] = bins
        self._ece[key] = ece_val
        self._brier[key] = brier_val

        # Dual-method validation
        if not check_platt_bin_divergence(calibrator, raw_confs, outcomes):
            self.flagged_cells.append(key)

        return calibrator

    def _fit_level_cells(self) -> None:
        """Fit L1 cells: direction × market × signal_family × climate_group."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group in CLIMATE_GROUPS.keys():
                        key = self._cell_key(direction, mt, fam, group)
                        cal = self._fit_cell(key)
                        if cal is not None:
                            self._calibrators[key] = cal

    def _fit_l2(self) -> None:
        """Fit L2: direction × market × signal_family (pooled groups)."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    key = self._l2_key(direction, mt, fam)
                    cal = self._fit_cell(key)
                    if cal is not None:
                        self._calibrators[key] = cal

    def _fit_l3(self) -> None:
        """Fit L3: direction × market (pooled signals + groups)."""
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                key = self._l3_key(direction, mt)
                cal = self._fit_cell(key)
                if cal is not None:
                    self._calibrators[key] = cal

    def _fit_l4(self) -> None:
        """Fit L4: direction (pooled markets + signals + groups)."""
        for direction in DIRECTIONS:
            key = self._l4_key(direction)
            cal = self._fit_cell(key)
            if cal is not None:
                self._calibrators[key] = cal

    def _fit_l5(self) -> None:
        """Fit L5: direction × signal_family (pooled markets)."""
        for direction in DIRECTIONS:
            for fam in SIGNAL_FAMILIES:
                key = self._l5_key(direction, fam)
                cal = self._fit_cell(key)
                if cal is not None:
                    self._calibrators[key] = cal

    def _fit_global(self) -> None:
        """Fit L6: _global."""
        cal = self._fit_cell("_global")
        if cal is not None:
            self._calibrators["_global"] = cal

    # ── Phase 2: Regime and Season fitting ────────────────────

    def _fit_regime_cells(self) -> None:
        """
        Fit regime-aware calibrators: direction × market × family × group × regime.

        Regime serves as a dynamic selector on top of the climate-group base.
        These are stored separately from Phase 1 calibrators and used in
        the calibrate() cascade before L1 fallback.
        """
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group in CLIMATE_GROUPS.keys():
                        for regime in SYNOPTIC_REGIMES:
                            if regime == "unknown":
                                continue
                            r_key = self._cell_key_regime(direction, mt, fam, group, regime)
                            cal = self._fit_cell(r_key)
                            if cal is not None:
                                self._regime_calibrators[r_key] = cal

    def _fit_season_cells(self) -> None:
        """
        Fit season-aware calibrators: direction × market × family × group × season.

        Seasonal dimension (winter/spring/summer/fall) is added when n≥50 per
        season-cell. Stored separately and used in calibrate() before L1.
        """
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    for group in CLIMATE_GROUPS.keys():
                        for season in SEASONS:
                            s_key = self._cell_key_season(direction, mt, fam, group, season)
                            cal = self._fit_cell(s_key)
                            if cal is not None:
                                self._season_calibrators[s_key] = cal

    # ── Calibrate (inference) ──────────────────────────────────

    def calibrate(
        self,
        station: str,
        direction: str,
        market_type: str,
        signal_name: str,
        raw_conf: float,
        regime: Optional[str] = None,
        season: Optional[str] = None,
    ) -> float:
        """
        Convert raw confidence to calibrated P(correct) using the hierarchical
        fallback cascade.

        Phase 2: when regime and/or season are provided, the cascade first
        attempts regime-aware and season-aware calibrators, then falls back
        to the Phase 1 7-level cascade.

        Fallback order (regime-aware → regime-pooled → Phase 1):
            R1: direction × market × family × group × regime
            S1: direction × market × family × group × season
            L1: direction × market × family × group
            L2: direction × market × family
            L3: direction × market
            L5: direction × family
            L4: direction
            L6: _global
            L7: identity

        Args:
            station: station code (e.g., "KNYC")
            direction: "up" or "down"
            market_type: "HIGH" or "LOW"
            signal_name: signal name (e.g., "gaussian")
            raw_conf: raw confidence in [0.0, 1.0]
            regime: optional regime string (e.g., "frontal")
            season: optional season string (e.g., "winter")

        Returns:
            Calibrated probability in [0.5, 1.0] for UP, [0.0, 0.5] for DOWN
        """
        raw_conf = float(np.clip(raw_conf, 0.0, 1.0))
        group = get_climate_group(station)
        family = get_signal_family(signal_name)
        raw_logit = logit_transform(np.array([raw_conf]))

        # Phase 2: regime-aware cascade level (most specific)
        if regime is not None and regime not in ("", "unknown"):
            r_key = self._cell_key_regime(direction, market_type, family, group, regime)
            r_cal = self._regime_calibrators.get(r_key)
            if r_cal is not None:
                return float(np.clip(r_cal.transform(raw_logit)[0], 0.0, 1.0))

        # Phase 2: season-aware cascade level
        if season is not None and season in SEASONS:
            s_key = self._cell_key_season(direction, market_type, family, group, season)
            s_cal = self._season_calibrators.get(s_key)
            if s_cal is not None:
                return float(np.clip(s_cal.transform(raw_logit)[0], 0.0, 1.0))

        # Phase 1: 7-level fallback cascade
        cascade = [
            # L1: direction × market × signal_family × climate_group
            ("L1", self._calibrators.get(self._cell_key(direction, market_type, family, group))),
            # L2: direction × market × signal_family
            ("L2", self._calibrators.get(self._l2_key(direction, market_type, family))),
            # L3: direction × market (pooled signals)
            ("L3", self._calibrators.get(self._l3_key(direction, market_type))),
            # L5: direction × signal_family (pooled markets)
            ("L5", self._calibrators.get(self._l5_key(direction, family))),
            # L4: direction (pooled markets + signals)
            ("L4", self._calibrators.get(self._l4_key(direction))),
            # L6: _global
            ("L6", self._calibrators.get("_global")),
        ]

        for level_name, cal in cascade:
            if cal is not None:
                p_cal = float(cal.transform(raw_logit)[0])
                return float(np.clip(p_cal, 0.0, 1.0))

        # L7: identity
        return float(np.clip(raw_conf, 0.0, 1.0))

    # ── Phase 2: Nowcasting Integration ───────────────────────

    def nowcast_path(
        self,
        station: str,
        direction: str,
        market_type: str,
        signal_name: str,
        raw_conf: float,
        metar_record: dict,
        season: Optional[str] = None,
    ) -> float:
        """
        Produce a short-term calibrated probability using current-hour
        METAR observations for regime detection, then regime-aware calibration.

        This is the nowcasting bridge: the current METAR observation determines
        which synoptic regime we are in, which selects the appropriate
        regime-conditional calibration curve.

        Args:
            station: station code (e.g., "KNYC")
            direction: "up" or "down"
            market_type: "HIGH" or "LOW"
            signal_name: signal name (e.g., "gaussian")
            raw_conf: raw confidence in [0.0, 1.0]
            metar_record: dict with METAR observation fields:
                - pressure_tendency_3h (float): mb change in 3h
                - wind_dir_shift_3h (float): wind direction shift in degrees
                - cloud_cover_pct (float): cloud cover fraction (0-1)
                - temp_trend_3h (float): temperature change in 3h (°F)
            season: optional season string for additional context

        Returns:
            Calibrated probability in [0.0, 1.0]
        """
        # Detect regime from current METAR observations
        regime = SynopticRegimeDetector.detect_regime_from_metar_record(metar_record)

        # Season from date if not provided
        effective_season = season
        if effective_season is None:
            # Try to derive from metar_record date if available
            date_str = metar_record.get("date_utc", metar_record.get("timestamp", ""))
            if date_str:
                effective_season = get_season_from_date(date_str)

        # Calibrate using regime-aware cascade
        return self.calibrate(
            station=station,
            direction=direction,
            market_type=market_type,
            signal_name=signal_name,
            raw_conf=raw_conf,
            regime=regime,
            season=effective_season,
        )

    # ── Phase 2: Drift Diagnostics ────────────────────────────

    def check_drift(self, prior_state: Optional[dict] = None) -> dict:
        """
        Compare current Platt α/β against prior week's values.

        Flags cells where:
        - α changes by > DRIFT_ALPHA_THRESHOLD (0.5)
        - β changes by > DRIFT_BETA_THRESHOLD (0.3)

        Args:
            prior_state: dict from a prior to_json_dict() call, or None.
                If None, returns an empty drift report (no baseline).

        Returns:
            dict with:
                drifted_cells: list of dicts with key, alpha_change,
                               beta_change, prior/current values
                total_cells: total calibrators compared
                drift_pct: percentage of cells that drifted
        """
        if prior_state is None:
            return {
                "drifted_cells": [],
                "total_cells": 0,
                "drift_pct": 0.0,
                "note": "no prior state provided for comparison",
            }

        current = self.to_json_dict()
        drifted = []

        # Collect all calibrator entries from both current and prior
        prior_calibrators = {}
        prior_calibrators.update(prior_state.get("calibrators", {}))
        prior_calibrators.update(prior_state.get("_fallback", {}))

        current_calibrators = {}
        current_calibrators.update(current.get("calibrators", {}))
        current_calibrators.update(current.get("_fallback", {}))

        # Also check regime and season calibrators if present
        prior_calibrators.update(prior_state.get("_regime_calibrators", {}))
        prior_calibrators.update(prior_state.get("_season_calibrators", {}))
        current_calibrators.update(current.get("_regime_calibrators", {}))
        current_calibrators.update(current.get("_season_calibrators", {}))

        for key, cur_entry in current_calibrators.items():
            pri_entry = prior_calibrators.get(key)
            if pri_entry is None:
                continue

            cur_alpha = cur_entry.get("alpha", 1.0)
            cur_beta = cur_entry.get("beta", 0.0)
            pri_alpha = pri_entry.get("alpha", 1.0)
            pri_beta = pri_entry.get("beta", 0.0)

            alpha_change = abs(cur_alpha - pri_alpha)
            beta_change = abs(cur_beta - pri_beta)

            if alpha_change > DRIFT_ALPHA_THRESHOLD or beta_change > DRIFT_BETA_THRESHOLD:
                drifted.append({
                    "key": key,
                    "alpha_change": round(alpha_change, 4),
                    "beta_change": round(beta_change, 4),
                    "prior_alpha": round(pri_alpha, 4),
                    "prior_beta": round(pri_beta, 4),
                    "current_alpha": round(cur_alpha, 4),
                    "current_beta": round(cur_beta, 4),
                })

        total = len(current_calibrators)
        return {
            "drifted_cells": drifted,
            "total_cells": total,
            "drifted_count": len(drifted),
            "drift_pct": round(len(drifted) / max(1, total) * 100, 2),
        }

    # ── Serialization ──────────────────────────────────────────

    def to_json_dict(self) -> dict:
        """Serialize the full calibration state to a JSON-compatible dict (v3 format)."""
        calibrators_out = {}
        for key, cal in self._calibrators.items():
            entry = cal.to_dict()
            # Embed bins, ece, brier if available
            if key in self._bins:
                entry["bins"] = self._bins[key]
            if key in self._ece:
                entry["ece"] = round(self._ece[key], 6)
            if key in self._brier:
                entry["brier"] = round(self._brier[key], 6)
            calibrators_out[key] = entry

        # Separate fallback-level calibrators (L2-L6) for clarity
        fallback_out = {}
        l2_keys = set()
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                for fam in SIGNAL_FAMILIES:
                    k = self._l2_key(direction, mt, fam)
                    if k in calibrators_out:
                        fallback_out[k] = calibrators_out.pop(k)
                        l2_keys.add(k)

        l3_keys = set()
        for direction in DIRECTIONS:
            for mt in MARKET_TYPES:
                k = self._l3_key(direction, mt)
                if k in calibrators_out:
                    fallback_out[k] = calibrators_out.pop(k)
                    l3_keys.add(k)

        l4_keys = set()
        for direction in DIRECTIONS:
            k = self._l4_key(direction)
            if k in calibrators_out:
                fallback_out[k] = calibrators_out.pop(k)
                l4_keys.add(k)

        l5_keys = set()
        for direction in DIRECTIONS:
            for fam in SIGNAL_FAMILIES:
                k = self._l5_key(direction, fam)
                if k in calibrators_out:
                    fallback_out[k] = calibrators_out.pop(k)
                    l5_keys.add(k)

        if "_global" in calibrators_out:
            fallback_out["_global"] = calibrators_out.pop("_global")

        # Per-station ECE report
        per_station_ece = {}
        for station in ALL_STATIONS:
            group = get_climate_group(station)
            station_eces = []
            for direction in DIRECTIONS:
                for mt in MARKET_TYPES:
                    for fam in SIGNAL_FAMILIES:
                        l1_key = self._cell_key(direction, mt, fam, group)
                        if l1_key in self._ece:
                            station_eces.append(self._ece[l1_key])
            if station_eces:
                per_station_ece[station] = round(float(np.mean(station_eces)), 4)

        # Count L1 cells that resolved
        total_l1 = len(DIRECTIONS) * len(MARKET_TYPES) * len(SIGNAL_FAMILIES) * len(CLIMATE_GROUPS)
        resolved_l1 = sum(
            1 for d in DIRECTIONS for mt in MARKET_TYPES
            for fam in SIGNAL_FAMILIES for grp in CLIMATE_GROUPS.keys()
            if self._cell_key(d, mt, fam, grp) in self._calibrators
        )

        pct_on_fallback = round(
            (self.cells_on_fallback / self.total_cells * 100) if self.total_cells > 0 else 0,
            2
        )

        # Phase 2: regime/season calibrator serialization
        regime_calibrators_out = {}
        for key, cal in self._regime_calibrators.items():
            entry = cal.to_dict()
            if key in self._bins:
                entry["bins"] = self._bins[key]
            if key in self._ece:
                entry["ece"] = round(self._ece[key], 6)
            if key in self._brier:
                entry["brier"] = round(self._brier[key], 6)
            regime_calibrators_out[key] = entry

        season_calibrators_out = {}
        for key, cal in self._season_calibrators.items():
            entry = cal.to_dict()
            if key in self._bins:
                entry["bins"] = self._bins[key]
            if key in self._ece:
                entry["ece"] = round(self._ece[key], 6)
            if key in self._brier:
                entry["brier"] = round(self._brier[key], 6)
            season_calibrators_out[key] = entry

        regime_pct = round(
            (self.regime_cells_on_fallback / max(1, self.regime_total_cells)) * 100,
            2
        )
        season_pct = round(
            (self.season_cells_on_fallback / max(1, self.season_total_cells)) * 100,
            2
        )

        return {
            "_metadata": {
                "version": 3,
                "generated": self.generated_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "method": "platt_scaling_primary_bin_diagnostics",
                "min_samples_per_cell": MIN_SAMPLES,
                "regularization": f"Beta(2,2)_prior_n<{REGULARIZATION_THRESHOLD}",
                "climate_groups": list(CLIMATE_GROUPS.keys()),
                "signal_families": SIGNAL_FAMILIES,
                "phase2": {
                    "regime_calibration": REGIME_CALIBRATION_ENABLED,
                    "season_calibration": SEASON_CALIBRATION_ENABLED,
                    "synoptic_regimes": SYNOPTIC_REGIMES,
                    "seasons": SEASONS,
                },
            },
            "calibrators": calibrators_out,
            "_fallback": fallback_out,
            "_regime_calibrators": regime_calibrators_out,
            "_season_calibrators": season_calibrators_out,
            "diagnostics": {
                "per_station_ece": per_station_ece,
                "groups_flagged": self.flagged_cells,
                "cells_on_fallback_pct": pct_on_fallback,
                "total_l1_cells": total_l1,
                "resolved_l1_cells": resolved_l1,
                "total_calibrators": len(self._calibrators),
                "phase2": {
                    "regime_cells_on_fallback_pct": regime_pct,
                    "regime_total_cells": self.regime_total_cells,
                    "regime_resolved_cells": self.regime_total_cells - self.regime_cells_on_fallback,
                    "season_cells_on_fallback_pct": season_pct,
                    "season_total_cells": self.season_total_cells,
                    "season_resolved_cells": self.season_total_cells - self.season_cells_on_fallback,
                    "regime_calibrators": len(self._regime_calibrators),
                    "season_calibrators": len(self._season_calibrators),
                },
            },
        }

    def save(self, path: str) -> None:
        """Save to platt_calibration.json."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_json_dict(), f, indent=2)
        _logger.info(f"Saved calibration to {path}")

    @classmethod
    def load(cls, path: str) -> "PlattCalibrationPipeline":
        """Load from platt_calibration.json (v3 format)."""
        pipeline = cls()
        with open(path) as f:
            data = json.load(f)

        # Restore calibrators
        for key, entry in data.get("calibrators", {}).items():
            pipeline._calibrators[key] = PlattCalibrator.from_dict(entry)
            if "bins" in entry:
                pipeline._bins[key] = entry["bins"]
            if "ece" in entry:
                pipeline._ece[key] = entry["ece"]
            if "brier" in entry:
                pipeline._brier[key] = entry["brier"]

        for key, entry in data.get("_fallback", {}).items():
            pipeline._calibrators[key] = PlattCalibrator.from_dict(entry)
            if "bins" in entry:
                pipeline._bins[key] = entry["bins"]
            if "ece" in entry:
                pipeline._ece[key] = entry["ece"]
            if "brier" in entry:
                pipeline._brier[key] = entry["brier"]

        # Phase 2: restore regime and season calibrators
        for key, entry in data.get("_regime_calibrators", {}).items():
            pipeline._regime_calibrators[key] = PlattCalibrator.from_dict(entry)
            if "bins" in entry:
                pipeline._bins[key] = entry["bins"]
            if "ece" in entry:
                pipeline._ece[key] = entry["ece"]
            if "brier" in entry:
                pipeline._brier[key] = entry["brier"]

        for key, entry in data.get("_season_calibrators", {}).items():
            pipeline._season_calibrators[key] = PlattCalibrator.from_dict(entry)
            if "bins" in entry:
                pipeline._bins[key] = entry["bins"]
            if "ece" in entry:
                pipeline._ece[key] = entry["ece"]
            if "brier" in entry:
                pipeline._brier[key] = entry["brier"]

        pipeline.refitted = True
        pipeline.generated_ts = data.get("_metadata", {}).get("generated")
        pipeline.flagged_cells = data.get("diagnostics", {}).get("groups_flagged", [])
        return pipeline
