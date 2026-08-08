#!/usr/bin/env python3
"""
core/bma_emos.py — BMA/EMOS post‑processing for ensemble fraction.

Bayesian Model Averaging (BMA) and Ensemble Model Output Statistics (EMOS)
implemented with station clustering, pooled estimation, and empirical Bayes shrinkage.

B‑Mode compliant — no AI/ML in the loop.
Pure numpy/scipy.

The actual fitting/training of BMA weights and EMOS coefficients lives in the
training pipeline (see scripts/train_bma_emos.py).  This module loads pre‑computed
parameters from cache and applies them for real‑time calibration.  The
``regularization`` parameter controls L2 ridge shrinkage of EMOS params toward
their identity prior (a=0, b=1, sigma=3.0) at load time.

Exports:
    bma_calibrate(station, raw_forecast) -> adjusted fraction
    emos_calibrate(station, ensemble_mean, ensemble_var, threshold, date) -> (prob, ci_lower, ci_upper)
    calibrate_ensemble(station, gefs_forecast, ecmwf_forecast, threshold, date) -> (adjusted_prob, ci_lower, ci_upper)

Main class: BMAEMOSPostProcessor
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

# Season mapping: month -> season name (DJF, MAM, JJA, SON)
_SEASON_MAP = {12: "DJF", 1: "DJF", 2: "DJF",
               3: "MAM", 4: "MAM", 5: "MAM",
               6: "JJA", 7: "JJA", 8: "JJA",
               9: "SON", 10: "SON", 11: "SON"}


def get_season(date: str) -> str:
    """Return season string DJF, MAM, JJA, SON for a date 'YYYY-MM-DD'."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        return _SEASON_MAP.get(dt.month, "DJF")
    except (ValueError, TypeError):
        return "DJF"


def get_station_cluster(station: str, cluster_path: Optional[str] = None) -> str:
    """
    Look up cluster name for a station from station_clusters.json.

    Args:
        station: ICAO station code
        cluster_path: Path to station_clusters.json; defaults to data/station_clusters.json

    Returns:
        cluster name (cold_semi_arid, hot_semi_arid, humid_subtropical, continental_other)
        Raises ValueError if station not found.
    """
    if cluster_path is None:
        cluster_path = "data/station_clusters.json"

    try:
        with open(cluster_path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise ValueError(f"Cannot load station clusters from {cluster_path}: {e}")

    cluster = data.get("station_to_cluster", {}).get(station)
    if cluster is None:
        raise ValueError(f"Station {station} not found in station_clusters.json")
    return cluster


def decode_member_values(blob: Union[bytes, np.ndarray, list]) -> np.ndarray:
    """
    Deserialize member values.

    Args:
        blob: Either bytes (pickle) or already decoded array/list.

    Returns:
        numpy array of member values (float64).
    """
    if isinstance(blob, bytes):
        import pickle
        values = pickle.loads(blob)
    else:
        values = blob

    if not isinstance(values, np.ndarray):
        values = np.array(values, dtype=np.float64)

    return values


def compute_ensemble_stats(
    members_gefs: np.ndarray,
    members_ecmwf: np.ndarray,
) -> Tuple[float, float]:
    """
    Compute combined ensemble mean and variance across all members.

    Guards degenerate cases (empty, single‑member, all‑NaN) by returning
    (0.0, 0.0) so callers can degrade to the BMA path instead of NaN.

    Args:
        members_gefs: GEFS member values
        members_ecmwf: ECMWF member values

    Returns:
        (mean, variance) of pooled ensemble.  (0.0, 0.0) if insufficient
        valid members.
    """
    all_members = np.concatenate([members_gefs, members_ecmwf])
    valid = all_members[~np.isnan(all_members)]
    if len(valid) < 2:
        return 0.0, 0.0
    mean = np.nanmean(valid)
    var = np.nanvar(valid, ddof=1)
    if np.isnan(var):
        var = 0.0
    return mean, var


@dataclass
class BMAParams:
    """BMA parameters for a station-season."""
    station: str
    season: str
    w_gefs: float
    w_ecmwf: float
    n_pairs: int
    shrinkage_alpha: float = 0.0


@dataclass
class EMOSParams:
    """EMOS parameters for a station-season."""
    station: str
    season: str
    a: float          # intercept
    b: float          # slope for ensemble mean
    sigma: float      # homoscedastic standard deviation
    c: float = 0.0    # heteroscedastic intercept for log variance
    d: float = 0.0    # heteroscedastic slope for log variance
    method: str = "homoscedastic"


class BMAEMOSPostProcessor:
    """
    Main post‑processor combining BMA and EMOS.

    Configurable with mode: "none", "bma", "emos", "both".

    Parameters
    ----------
    mode : str
        Which calibration to apply ("none", "bma", "emos", "both").
    emos_method : str
        "homoscedastic" or "heteroscedastic".
    regularization : float
        L2 ridge strength.  At load time EMOS params are shrunk toward their
        identity prior (a=0, b=1, sigma=3.0) by this factor:

            a_reg = (1 - λ) * a_cache
            b_reg = 1.0 - (1 - λ) * (1.0 - b_cache)
            sigma_reg = 3.0 - (1 - λ) * (3.0 - sigma_cache)

        where λ = regularization.  λ=0 means no change, λ=1 means full
        shrinkage to identity.  Default 0.1 gives a mild correction.
    cache_path : str
        Path to the pre‑computed parameter cache JSON.
    """

    def __init__(
        self,
        mode: str = "both",
        emos_method: str = "homoscedastic",
        regularization: float = 0.1,
        cache_path: str = "data/bma_emos_cache.json",
    ):
        self.mode = mode
        self.emos_method = emos_method
        self.regularization = regularization
        self.cache_path = cache_path

        self._bma_weights: Dict[str, Dict[str, BMAParams]] = {}
        self._emos_params: Dict[str, Dict[str, EMOSParams]] = {}
        self._station_clusters: Dict[str, str] = {}
        self._loaded = False

    def load_cache(self) -> None:
        """Load pre‑computed BMA/EMOS parameters from cache."""
        try:
            with open(self.cache_path, "r") as f:
                cache = json.load(f)
        except FileNotFoundError:
            logger.warning(f"Cache file {self.cache_path} not found; using defaults")
            self._default_params()
            return

        # Load station clusters
        self._station_clusters = cache.get("station_to_cluster", {})
        if not self._station_clusters:
            try:
                with open("data/station_clusters.json", "r") as f:
                    data = json.load(f)
                    self._station_clusters = data.get("station_to_cluster", {})
            except FileNotFoundError:
                pass

        # Load BMA weights
        bma_station = cache.get("bma_weights_station", {})
        for station, seasons in bma_station.items():
            self._bma_weights[station] = {}
            for season, wdict in seasons.items():
                self._bma_weights[station][season] = BMAParams(
                    station=station,
                    season=season,
                    w_gefs=wdict.get("gefs", 0.5),
                    w_ecmwf=wdict.get("ecmwf", 0.5),
                    n_pairs=wdict.get("n_pairs", 0),
                    shrinkage_alpha=wdict.get("shrinkage_alpha", 0.0),
                )

        # Load EMOS params with ridge regularization toward identity prior
        lam = self.regularization
        emos_station = cache.get("emos_params_station", {})
        for station, seasons in emos_station.items():
            self._emos_params[station] = {}
            for season, pdict in seasons.items():
                a_raw = pdict.get("a", 0.0)
                b_raw = pdict.get("b", 1.0)
                sigma_raw = pdict.get("sigma", 3.0)
                # Ridge shrinkage toward identity prior (a=0, b=1, sigma=3.0)
                a_reg = (1.0 - lam) * a_raw
                b_reg = 1.0 - (1.0 - lam) * (1.0 - b_raw)
                sigma_reg = 3.0 - (1.0 - lam) * (3.0 - sigma_raw)

                self._emos_params[station][season] = EMOSParams(
                    station=station,
                    season=season,
                    a=a_reg,
                    b=b_reg,
                    sigma=sigma_reg,
                    c=pdict.get("c", 0.0),
                    d=pdict.get("d", 0.0),
                    method=pdict.get("method", "homoscedastic"),
                )

        self._loaded = True
        logger.info(f"Loaded BMA/EMOS cache from {self.cache_path} (regularization={lam})")

    def _default_params(self) -> None:
        """Initialize with default (identity) parameters."""
        try:
            from core.station_registry import STATIC_MAPPING
            stations = list(STATIC_MAPPING.keys())
        except ImportError:
            stations = ["KDEN", "KPHX", "KATL", "KBOS", "KORD", "KSEA", "KPDX", "KMSP",
                        "KABQ", "KELY", "KRKS", "KLAR", "KACT", "KINK", "KHOB", "KMAF",
                        "KJAN", "KMEM", "KLCH", "KMOB"]

        seasons = ["DJF", "MAM", "JJA", "SON"]
        for station in stations:
            self._bma_weights[station] = {}
            self._emos_params[station] = {}
            for season in seasons:
                self._bma_weights[station][season] = BMAParams(
                    station=station,
                    season=season,
                    w_gefs=0.5,
                    w_ecmwf=0.5,
                    n_pairs=0,
                    shrinkage_alpha=1.0,
                )
                self._emos_params[station][season] = EMOSParams(
                    station=station,
                    season=season,
                    a=0.0,
                    b=1.0,
                    sigma=3.0,
                    c=0.0,
                    d=0.0,
                    method="homoscedastic",
                )
        self._loaded = True
        logger.info("Using default BMA/EMOS parameters (equal weights, identity)")

    def _get_bma_weights(self, station: str, season: str) -> Tuple[float, float]:
        """Return (w_gefs, w_ecmwf) for station‑season, with empirical Bayes shrinkage applied."""
        if not self._loaded:
            self.load_cache()
        station_weights = self._bma_weights.get(station)
        if not station_weights:
            return 0.5, 0.5
        params = station_weights.get(season)
        if not params:
            return 0.5, 0.5

        # Empirical Bayes shrinkage toward pooled (0.5/0.5) estimate.
        # final_weight = shrinkage_alpha * pooled_weight + (1 - shrinkage_alpha) * station_weight
        alpha = params.shrinkage_alpha
        w_gefs = alpha * 0.5 + (1.0 - alpha) * params.w_gefs
        w_ecmwf = alpha * 0.5 + (1.0 - alpha) * params.w_ecmwf
        return w_gefs, w_ecmwf

    def _get_emos_params(self, station: str, season: str) -> EMOSParams:
        """Return EMOS parameters for station‑season."""
        if not self._loaded:
            self.load_cache()
        station_params = self._emos_params.get(station)
        if not station_params:
            return EMOSParams(station=station, season=season, a=0.0, b=1.0, sigma=3.0)
        params = station_params.get(season)
        if not params:
            return EMOSParams(station=station, season=season, a=0.0, b=1.0, sigma=3.0)
        return params

    def bma_calibrate(
        self,
        gefs_fraction: Optional[float],
        ecmwf_fraction: Optional[float],
        station: str,
        date: str,
    ) -> float:
        """
        Apply BMA weighting to per‑model ensemble fractions.

        Empirical Bayes shrinkage is applied inside ``_get_bma_weights``:
        ``final_weight = shrinkage_alpha * 0.5 + (1 - shrinkage_alpha) * station_weight``.

        Args:
            gefs_fraction: GEFS ensemble fraction (0‑1) or None if missing
            ecmwf_fraction: ECMWF ensemble fraction (0‑1) or None
            station: ICAO station code
            date: Date string 'YYYY‑MM‑DD'

        Returns:
            Weighted fraction clipped to [0, 1].  Falls back to equal weight
            or single model when one source is missing.
        """
        gefs_available = gefs_fraction is not None
        ecmwf_available = ecmwf_fraction is not None

        if not gefs_available and not ecmwf_available:
            logger.error(f"Both models missing for {station} on {date}")
            return 0.5
        if not gefs_available:
            logger.warning(f"GEFS missing for {station} on {date} — using ECMWF‑only")
            return float(np.clip(ecmwf_fraction, 0.0, 1.0))
        if not ecmwf_available:
            logger.warning(f"ECMWF missing for {station} on {date} — using GEFS‑only")
            return float(np.clip(gefs_fraction, 0.0, 1.0))

        season = get_season(date)
        w_gefs, w_ecmwf = self._get_bma_weights(station, season)
        result = w_gefs * gefs_fraction + w_ecmwf * ecmwf_fraction
        return float(np.clip(result, 0.0, 1.0))

    def emos_calibrate(
        self,
        ensemble_mean: float,
        ensemble_var: float,
        threshold: float,
        station: str,
        date: str,
    ) -> Tuple[float, float, float]:
        """
        Compute P(T_obs > threshold) under EMOS predictive distribution.

        The 90% confidence band is a sigma‑perturbation heuristic: we evaluate
        the exceedance probability with a wider (sigma*1.645) and narrower
        (sigma/1.645) spread, then assign min → ci_lower, max → ci_upper so
        the band is always correctly ordered regardless of which side of mu
        the threshold falls.

        Args:
            ensemble_mean: Mean of pooled ensemble (GEFS+ECMWF)
            ensemble_var: Variance of pooled ensemble
            threshold: Temperature threshold (°F)
            station: ICAO station code
            date: Date string 'YYYY‑MM‑DD'

        Returns:
            (prob, ci_lower, ci_upper) where ci_lower ≤ prob ≤ ci_upper.
        """
        season = get_season(date)
        params = self._get_emos_params(station, season)

        # Predictive parameters
        mu = params.a + params.b * ensemble_mean
        if params.method == "heteroscedastic":
            log_sigma2 = params.c + params.d * np.log(max(ensemble_var, 1e-6))
            sigma = np.sqrt(np.exp(log_sigma2))
        else:
            sigma = params.sigma

        # Guard degenerate sigma
        if sigma <= 0 or np.isnan(sigma):
            sigma = 3.0

        # Exceedance probability
        prob = 1.0 - stats.norm.cdf(threshold, loc=mu, scale=sigma)

        # 90% confidence band (sigma‑perturbation heuristic)
        p_wide = 1.0 - stats.norm.cdf(threshold, loc=mu, scale=sigma * 1.645)
        p_narrow = 1.0 - stats.norm.cdf(threshold, loc=mu, scale=sigma / 1.645)
        ci_lower = min(p_wide, p_narrow)
        ci_upper = max(p_wide, p_narrow)

        # Clip to valid ranges
        prob = float(np.clip(prob, 0.001, 0.999))
        ci_lower = float(np.clip(ci_lower, 0.0, 1.0))
        ci_upper = float(np.clip(ci_upper, 0.0, 1.0))

        return prob, ci_lower, ci_upper

    def calibrate_ensemble(
        self,
        gefs_forecast: Union[np.ndarray, list],
        ecmwf_forecast: Union[np.ndarray, list],
        threshold: float,
        station: str,
        date: str,
    ) -> Tuple[float, float, float]:
        """
        Full calibration pipeline: BMA weighting + EMOS adjustment.

        Degenerate ensembles (empty, all‑NaN, single member) degrade
        gracefully to BMA fraction / 0.5 instead of NaN.

        Args:
            gefs_forecast: GEFS member values
            ecmwf_forecast: ECMWF member values
            threshold: Temperature threshold (°F)
            station: ICAO station code
            date: Date string 'YYYY‑MM‑DD'

        Returns:
            (adjusted_prob, ci_lower, ci_upper)
        """
        # Decode member values if needed
        gefs_members = decode_member_values(gefs_forecast)
        ecmwf_members = decode_member_values(ecmwf_forecast)

        # Step 1: Compute per‑model ensemble fractions (NaN‑aware)
        def _safe_fraction(members: np.ndarray) -> Optional[float]:
            """Return fraction of non‑NaN members > threshold, or None if no valid members."""
            valid = members[~np.isnan(members)]
            if len(valid) == 0:
                return None
            return float(np.mean(valid > threshold))

        gefs_fraction = _safe_fraction(gefs_members)
        ecmwf_fraction = _safe_fraction(ecmwf_members)

        # If both models have no valid members, return neutral
        if gefs_fraction is None and ecmwf_fraction is None:
            logger.warning(f"All members NaN/empty for {station} on {date} — returning 0.5")
            return 0.5, 0.0, 1.0

        # Step 2: Apply BMA weighting (if mode includes BMA)
        if self.mode in ("bma", "both"):
            bma_weighted = self.bma_calibrate(gefs_fraction, ecmwf_fraction, station, date)
        else:
            # Equal weight average (per‑model avg)
            vals = [v for v in (gefs_fraction, ecmwf_fraction) if v is not None]
            bma_weighted = sum(vals) / len(vals) if vals else 0.5

        # Step 3: Compute ensemble moments for EMOS
        mean, var = compute_ensemble_stats(gefs_members, ecmwf_members)

        # Step 4: Apply EMOS calibration (if mode includes EMOS and ensemble is sufficient)
        if self.mode in ("emos", "both") and mean != 0.0:
            prob, ci_lower, ci_upper = self.emos_calibrate(mean, var, threshold, station, date)
        else:
            prob = bma_weighted
            # Rough confidence interval based on ensemble spread
            n_total = len(gefs_members[~np.isnan(gefs_members)]) + len(ecmwf_members[~np.isnan(ecmwf_members)])
            if n_total > 0:
                ci_lower = max(0.0, prob - 1.0 / np.sqrt(n_total))
                ci_upper = min(1.0, prob + 1.0 / np.sqrt(n_total))
            else:
                ci_lower, ci_upper = 0.0, 1.0

        return prob, ci_lower, ci_upper


# Public exported functions (thin wrappers)

_bma_emos_singleton: Optional[BMAEMOSPostProcessor] = None


def _get_processor() -> BMAEMOSPostProcessor:
    """Lazy‑load singleton processor."""
    global _bma_emos_singleton
    if _bma_emos_singleton is None:
        _bma_emos_singleton = BMAEMOSPostProcessor(mode="both")
    return _bma_emos_singleton


def bma_calibrate(station: str, raw_forecast: Tuple[Optional[float], Optional[float]]) -> float:
    """
    Apply BMA weighting to per‑model ensemble fractions.

    Args:
        station: ICAO station code
        raw_forecast: tuple (gefs_fraction, ecmwf_fraction) where each can be None

    Returns:
        Weighted fraction [0, 1].
    """
    gefs_frac, ecmwf_frac = raw_forecast
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    proc = _get_processor()
    return proc.bma_calibrate(gefs_frac, ecmwf_frac, station, date)


def emos_calibrate(
    station: str,
    ensemble_mean: float,
    ensemble_var: float,
    threshold: float,
    date: str,
) -> Tuple[float, float, float]:
    """
    EMOS calibration for given ensemble moments.

    Args:
        station: ICAO station code
        ensemble_mean: Mean of pooled ensemble
        ensemble_var: Variance of pooled ensemble
        threshold: Temperature threshold (°F)
        date: Date string 'YYYY‑MM‑DD'

    Returns:
        (prob, ci_lower, ci_upper) where ci_lower ≤ prob ≤ ci_upper.
    """
    proc = _get_processor()
    return proc.emos_calibrate(ensemble_mean, ensemble_var, threshold, station, date)


def calibrate_ensemble(
    station: str,
    gefs_forecast: Union[np.ndarray, list],
    ecmwf_forecast: Union[np.ndarray, list],
    threshold: float,
    date: str,
) -> Tuple[float, float, float]:
    """
    Full calibration pipeline for a given station and forecasts.

    Args:
        station: ICAO station code
        gefs_forecast: GEFS member values
        ecmwf_forecast: ECMWF member values
        threshold: Temperature threshold (°F)
        date: Date string 'YYYY‑MM‑DD'

    Returns:
        (adjusted_prob, ci_lower, ci_upper)
    """
    proc = _get_processor()
    return proc.calibrate_ensemble(gefs_forecast, ecmwf_forecast, threshold, station, date)


# Self‑test

def test_module() -> bool:
    """Quick self‑test to verify module loads and basic functions work."""
    print("=" * 72)
    print("  BMA/EMOS Module — Self‑Test")
    print("=" * 72)

    # Load station clusters
    try:
        cluster = get_station_cluster("KDEN")
        print(f"  KDEN cluster: {cluster}")
    except Exception as e:
        print(f"  ⚠️  Could not load station clusters: {e}")

    # Create processor
    proc = BMAEMOSPostProcessor(mode="both")
    proc.load_cache()

    # Test BMA calibration with dummy data
    gefs_frac = 0.7
    ecmwf_frac = 0.5
    date = "2024-07-15"
    station = "KDEN"
    bma_result = proc.bma_calibrate(gefs_frac, ecmwf_frac, station, date)
    print(f"  BMA weighted fraction: {bma_result:.4f}")

    # Test EMOS calibration — hot side (threshold > mu)
    mean = 85.0
    var = 25.0
    threshold = 90.0
    prob, ci_lower, ci_upper = proc.emos_calibrate(mean, var, threshold, station, date)
    print(f"  EMOS hot‑side prob >{threshold}°F: {prob:.4f} ({ci_lower:.4f}, {ci_upper:.4f})")
    assert ci_lower <= prob <= ci_upper, f"CI inverted on hot side: {ci_lower} vs {prob} vs {ci_upper}"

    # Test EMOS calibration — cold side (threshold < mu)
    threshold_cold = 80.0
    prob_c, ci_l_c, ci_u_c = proc.emos_calibrate(mean, var, threshold_cold, station, date)
    print(f"  EMOS cold‑side prob >{threshold_cold}°F: {prob_c:.4f} ({ci_l_c:.4f}, {ci_u_c:.4f})")
    assert ci_l_c <= prob_c <= ci_u_c, f"CI inverted on cold side: {ci_l_c} vs {prob_c} vs {ci_u_c}"

    # Test full calibration with dummy member arrays
    gefs_members = np.array([88.0, 90.0, 92.0, 85.0, 87.0, 91.0, 86.0, 89.0])
    ecmwf_members = np.array([87.0, 89.0, 91.0, 84.0, 86.0, 90.0, 85.0, 88.0])
    prob, ci_lower, ci_upper = proc.calibrate_ensemble(
        gefs_members, ecmwf_members, threshold, station, date
    )
    print(f"  Full calibration probability: {prob:.4f} ({ci_lower:.4f}, {ci_upper:.4f})")

    # Test degenerate ensembles
    prob_d, ci_l_d, ci_u_d = proc.calibrate_ensemble([], [], threshold, station, date)
    print(f"  Empty ensemble: {prob_d:.4f} ({ci_l_d:.4f}, {ci_u_d:.4f})")
    assert not np.isnan(prob_d), "Empty ensemble returned NaN"

    prob_n, ci_l_n, ci_u_n = proc.calibrate_ensemble(
        [np.nan, np.nan], [np.nan, np.nan], threshold, station, date
    )
    print(f"  All‑NaN ensemble: {prob_n:.4f} ({ci_l_n:.4f}, {ci_u_n:.4f})")
    assert not np.isnan(prob_n), "All‑NaN ensemble returned NaN"

    print("\n  ✅ Self‑test complete")
    return True


if __name__ == "__main__":
    test_module()
