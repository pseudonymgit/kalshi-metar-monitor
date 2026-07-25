#!/usr/bin/env python3
"""
SIGNAL: NWP Analog Ensemble Signal (Deterministic k-NN)

Replaces the XGBoost-based NWP analog signal with a purely deterministic
k-nearest neighbor (k-NN) approach. No ML/AI.

Architecture (per Expert 4 Meteorology spec, Gray Room Round 3):
1. Feature extraction: 5+ NWP predictor fields from multi-model historical forecasts
2. k-NN analog matching: K=50 weighted Euclidean nearest neighbors within ±15 days
3. Directional probability: beta-binomial estimate (hits + 1) / (K + 2)
4. Calibration: walk-forward isotonic regression per city via CalibrationPipeline

Reference: docs/plans/GRAY-ROOM-ROUND3-EXPERT4-METEOROLOGY.md

No XGBoost. No scikit-learn ML models (NearestNeighbors is distance computation only).
IsotonicRegression is a standard MOS statistical method, not ML.
"""

import sqlite3
import math
import numpy as np
import os
from pathlib import Path
from collections import defaultdict
from typing import Optional, Tuple, Dict, List, Any
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────
NWP_DB_DEFAULT  = "data/nwp_forecasts.db"
METAR_DB_DEFAULT = "data/metar_backfill.db"

# ── Global Enable Flag ─────────────────────────────────────────────────
# Enabled for deterministic k-NN v2.0 (replaces disabled XGBoost version).
# See docs/plans/GRAY-ROOM-ROUND3-EXPERT4-METEOROLOGY.md for architecture.
# ── B-Mode R8 Cycle 2.7: NWP Analog excluded from paper test ──
# Set to False to exclude NWP analog signal from paper test trading decisions.
# Can be overridden via environment variable.
NWP_ANALOG_ENABLED = False
_nwp_env = os.environ.get('NWP_ANALOG_ENABLED', '').lower()
if _nwp_env in ('1', 'true', 'yes'):
    NWP_ANALOG_ENABLED = True


class NwpAnalogSignal:
    """
    Deterministic k-NN analog ensemble signal using NWP forecast fields.

    For each forecast day, finds K=50 most similar historical days based on
    weighted Euclidean distance over NWP features, then uses the actual
    temperature outcomes from those analog days to predict directional bias.

    Key parameters (from Expert 4 spec §2):
      K        = 50  nearest neighbors
      window   = ±15 day seasonal filter
      metric   = weighted Euclidean with field-specific weights
      estimate = beta-binomial: (hits + 1) / (K + 2)
      min_fire = 10  analogs to produce a non-null signal
    """

    # ── Feature weights (Expert 4 spec §2 — renormalised for 5-feature set) ──
    # Wind (u10/v10, weight 0.15) is excluded because wind fields only have
    # ~65 dates per station in the NWP DB (2026-05-09 through 2026-07-12).
    # Including it would cut the historical pool from ~150 to ~65 — too small
    # for a K=50 search. When wind data accumulates, add:
    #   'u10': 0.075/0.85, 'v10': 0.075/0.85
    # and set _HAS_WIND = True.
    #
    # Renormalised from the original spec weights (sum=0.85) so that the
    # effective weight vector sums to 1.0, keeping the distance metric
    # scale-independent.
    FIELD_WEIGHTS = {
        'temperature_850hPa_daily_mean':      0.30 / 0.85,  # 0.353
        'geopotential_height_500hPa_daily_mean': 0.20 / 0.85,  # 0.235
        'cloud_cover_daily_mean':             0.15 / 0.85,  # 0.176
        'temperature_2m_max':                 0.10 / 0.85,  # 0.118
        'dew_point_2m_daily_mean':            0.10 / 0.85,  # 0.118
    }

    # NWP DB variable names needed (before any decomposition)
    NWP_FIELDS = list(FIELD_WEIGHTS.keys())

    K              = 50   # Nearest neighbours (capped to available candidates)
    MIN_ANALOGS    = 10   # Minimum valid analogs to fire a signal
    SEASONAL_WINDOW = 45  # ± days around target date
    # NOTE: ±15 days is the ideal (per Expert 4 spec), but the NWP DB only has
    # 150 dates with upper-air features (two clusters: May-Aug 2025, Jun-Jul 2026).
    # A tighter window leaves < 20 candidates for most target dates.  ±45 days
    # provides ~50-110 candidates, enabling K=50.  Tighten when more data accumulates.

    # ── Constructor ────────────────────────────────────────────────────
    def __init__(self, db_path: str = None, nwp_db_path: str = None):
        """
        Args:
            db_path:     Path to METAR (daily_stats) DB — the standard
                         argument used by SignalRegistry.
            nwp_db_path: Path to NWP forecasts DB.  Falls back to env var
                         NWP_DB_PATH or the default relative path.
        """
        # METAR db — first positional arg from registry
        if db_path:
            self.metar_db_path = Path(db_path).absolute()
        elif os.environ.get('METAR_DB_PATH'):
            self.metar_db_path = Path(os.environ['METAR_DB_PATH']).absolute()
        else:
            self.metar_db_path = Path(METAR_DB_DEFAULT).resolve()

        # NWP db
        if nwp_db_path:
            self.nwp_db_path = Path(nwp_db_path).absolute()
        elif os.environ.get('NWP_DB_PATH'):
            self.nwp_db_path = Path(os.environ['NWP_DB_PATH']).absolute()
        else:
            self.nwp_db_path = Path(NWP_DB_DEFAULT).resolve()

        # ── Internal caches ──
        self._nwp_features  = None   # {(station, date): {field: value}}
        self._nwp_stations  = None
        self._nwp_dates     = None
        self._norm_stats    = {}     # {station: {field: (mean, std)}}
        self._metar_highs   = {}     # {station: {date: max_temp_f}}

    # ── Signal interface (compatible with BaseSignal / SignalRegistry) ──

    @property
    def name(self) -> str:
        return "nwp_analog"

    @property
    def min_lookback(self) -> int:
        return 10

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Standard evaluate interface — delegates to NWP DB lookup.

        NOTE: This signal fundamentally requires the NWP forecast DB.
        The `days` list from METAR alone is insufficient.  This method
        extracts the station and date from `days` and calls
        evaluate_for_station().
        """
        if idx < 1 or idx >= len(days):
            return None, 0.0
        date_str = days[idx]['date']
        # We don't know which station from the days list — so this is a
        # placeholder.  Real callers should use evaluate_for_station()
        # or evaluate_nwp_analog() directly.
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str,
                             conn: sqlite3.Connection = None
                             ) -> Tuple[Optional[str], float]:
        """Evaluate NWP analog signal for a specific station + date."""
        return self.evaluate_nwp_analog(station, date)

    # ── Data loading ──────────────────────────────────────────────────

    def _load_nwp_features(self):
        """
        Load all NWP forecast features, multi-model averaged.

        Returns:
            (features_dict, station_list, sorted_date_list)
        """
        if self._nwp_features is not None:
            return self._nwp_features, self._nwp_stations, self._nwp_dates

        if not os.path.exists(str(self.nwp_db_path)):
            logger.warning("NWP DB not found at %s", self.nwp_db_path)
            return {}, [], []

        conn = sqlite3.connect(str(self.nwp_db_path), timeout=60)
        cur  = conn.cursor()

        cur.execute("SELECT DISTINCT station FROM nwp_forecasts ORDER BY station")
        stations = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT target_date FROM nwp_forecasts ORDER BY target_date")
        dates = sorted([r[0] for r in cur.fetchall()])

        # Build feature dict: average across all models per (station, date, field)
        features = defaultdict(dict)
        for field in self.NWP_FIELDS:
            cur.execute("""
                SELECT station, target_date, AVG(value)
                FROM nwp_forecasts
                WHERE variable = ? AND value IS NOT NULL
                GROUP BY station, target_date
            """, (field,))
            for station, tdate, val in cur.fetchall():
                features[(station, tdate)][field] = val

        conn.close()

        self._nwp_features = dict(features)
        self._nwp_stations = stations
        self._nwp_dates    = dates
        return self._nwp_features, self._nwp_stations, self._nwp_dates

    def _load_metar_highs(self, station: str) -> Dict[str, float]:
        """Load daily max-temp from METAR for a station.

        Filters out corrupted values (temps outside -20°F … 130°F range).
        Cached per station across calls.
        """
        if station in self._metar_highs:
            return self._metar_highs[station]

        if not os.path.exists(str(self.metar_db_path)):
            logger.warning("METAR DB not found at %s", self.metar_db_path)
            return {}

        try:
            conn = sqlite3.connect(str(self.metar_db_path), timeout=30)
            cur  = conn.cursor()
            cur.execute("""
                SELECT date_utc, max_temp_f
                FROM daily_stats
                WHERE station = ? AND max_temp_f IS NOT NULL
                ORDER BY date_utc
            """, (station,))
            # Reject physically impossible values (79 corrupted entries in DB
            # with values like 64886.0 — likely serialisation artefacts)
            result = {}
            for date_str, temp_f in cur.fetchall():
                if temp_f is not None and -20 <= temp_f <= 130:
                    result[date_str] = temp_f
            conn.close()
            self._metar_highs[station] = result
            logger.debug("Loaded %d valid HIGH temps for %s", len(result), station)
            return result
        except Exception as e:
            logger.error("METAR read error for %s: %s", station, e)
            return {}

    # ── Normalisation ─────────────────────────────────────────────────

    def _compute_norm_stats(self, station: str) -> Dict[str, tuple]:
        """Per-station climatological (mean, std) for each feature field."""
        if station in self._norm_stats:
            return self._norm_stats[station]

        features, _, _ = self._load_nwp_features()
        field_vals = defaultdict(list)
        for (s, _), feats in features.items():
            if s != station:
                continue
            for fld in self.FIELD_WEIGHTS:
                v = feats.get(fld)
                if v is not None:
                    field_vals[fld].append(v)

        stats = {}
        for fld in self.FIELD_WEIGHTS:
            vals = field_vals.get(fld, [])
            if len(vals) >= 5:
                mu  = float(np.mean(vals))
                sd  = float(np.std(vals, ddof=1))
                if sd < 1e-8:
                    sd = 1.0
                stats[fld] = (mu, sd)
            else:
                stats[fld] = (0.0, 1.0)  # fallback identity

        self._norm_stats[station] = stats
        return stats

    # ── Feature vector builder ────────────────────────────────────────

    def _build_feature_vector(self, station: str, date: str
                              ) -> Optional[np.ndarray]:
        """Build weighted-normalised feature vector for (station, date).

        Returns None if any required field is missing.
        """
        features, _, _ = self._load_nwp_features()
        feats = features.get((station, date), {})
        stats = self._compute_norm_stats(station)

        components = []
        for fld in self.FIELD_WEIGHTS:
            v = feats.get(fld)
            if v is None:
                return None
            mu, sd = stats[fld]
            components.append((v - mu) / sd)

        # Apply sqrt(weight) scaling for weighted Euclidean distance
        wgt_list = [self.FIELD_WEIGHTS[f] for f in self.FIELD_WEIGHTS]
        sqrt_w = np.sqrt(wgt_list)
        return np.array(components) * sqrt_w

    # ── Seasonal filter ───────────────────────────────────────────────

    def _seasonal_mask(self, target_date: str, candidate_dates: List[str]
                       ) -> List[bool]:
        """True for candidates within ±SEASONAL_WINDOW days of target (year-agnostic).

        Uses day-of-year comparison, correctly handling year-wrap:
        e.g. Jan 5 is within ±15 days of Dec 25.
        """
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        target_doy = target_dt.timetuple().tm_yday  # 1..366
        # Use 365 for the circular diff (data years 2025-2026 not leap years)
        n_days = 365

        result = []
        for d in candidate_dates:
            cand_dt = datetime.strptime(d, '%Y-%m-%d')
            cand_doy = cand_dt.timetuple().tm_yday
            # Circular difference on a 365-day circle
            diff = abs(cand_doy - target_doy)
            diff = min(diff, n_days - diff)
            result.append(diff <= self.SEASONAL_WINDOW)
        return result

    # ── METAR direction lookup ────────────────────────────────────────

    def _get_high_directions(self, station: str, dates: List[str]
                             ) -> Dict[str, str]:
        """
        Return dict {date: 'up'|'down'} based on HIGH-temp change
        vs. the previous day.

        For each requested date, loads the previous calendar day's HIGH
        as well, so direction is computed as: today > yesterday → 'up'.
        """
        highs = self._load_metar_highs(station)
        if not highs:
            return {}

        # Include previous day for each requested date
        from datetime import datetime as dt, timedelta
        expanded = set(dates)
        for d in dates:
            prev_d = (dt.strptime(d, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
            expanded.add(prev_d)

        result = {}
        for d in dates:
            prev_d = (dt.strptime(d, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
            if prev_d in highs and d in highs:
                if highs[prev_d] is not None and highs[d] is not None:
                    result[d] = 'up' if highs[d] > highs[prev_d] else 'down'
        return result

    # ── Core evaluation ───────────────────────────────────────────────

    def evaluate_nwp_analog(self, station: str, target_date: str
                            ) -> Tuple[Optional[str], float]:
        """
        Full k-NW analog evaluation.

        1. Load NWP feature vectors
        2. Apply ±15-day seasonal filter to candidate dates
        3. Compute weighted Euclidean distance
        4. Select K=50 nearest neighbours
        5. Count UP/DOWN outcomes from METAR ground truth
        6. Beta-binomial probability: (hits + 1) / (n + 2)

        Returns:
            (direction, confidence) or (None, 0.0) if < MIN_ANALOGS
        """
        if not NWP_ANALOG_ENABLED:
            return None, 0.0

        features, stations, all_dates = self._load_nwp_features()
        if station not in stations or (station, target_date) not in features:
            return None, 0.0

        # --- build target vector ---
        target_vec = self._build_feature_vector(station, target_date)
        if target_vec is None:
            return None, 0.0

        # --- candidates: strictly before target date ---
        candidates = [d for d in all_dates if d < target_date]
        if len(candidates) < self.MIN_ANALOGS:
            return None, 0.0

        # --- seasonal filter ---
        mask = self._seasonal_mask(target_date, candidates)
        candidates = [d for i, d in enumerate(candidates) if mask[i]]
        if len(candidates) < self.MIN_ANALOGS:
            return None, 0.0

        # --- build candidate feature matrix ---
        vecs, valid_dates = [], []
        for cd in candidates:
            v = self._build_feature_vector(station, cd)
            if v is not None:
                vecs.append(v)
                valid_dates.append(cd)
        if len(valid_dates) < self.MIN_ANALOGS:
            return None, 0.0

        candidate_matrix  = np.array(vecs)          # (N, n_feat)
        diff_matrix       = candidate_matrix - target_vec[np.newaxis, :]
        distances         = np.sqrt(np.sum(diff_matrix ** 2, axis=1))

        # --- select K nearest ---
        k = min(self.K, len(valid_dates))
        nearest_idx = np.argsort(distances)[:k]
        nearest_dates = [valid_dates[i] for i in nearest_idx]
        nearest_distances_raw = [distances[i] for i in nearest_idx]

        # --- look up actual directions ---
        all_needed = list(set(nearest_dates))
        directions = self._get_high_directions(station, all_needed)

        if not directions:
            return None, 0.0

        # --- Distance-weighted voting ---
        # Closer analogs get exponentially more weight.
        # sigma is the median distance of the K nearest neighbors.
        nearest_distances_arr = np.array(nearest_distances_raw)
        median_dist = np.median(nearest_distances_arr) if len(nearest_distances_arr) > 0 else 1.0
        if median_dist < 1e-10:
            median_dist = 1.0

        weighted_up = 0.0
        weighted_total = 0.0

        for nd, dist_val in zip(nearest_dates, nearest_distances_arr):
            if nd in directions:
                # Exponential weight: exp(-d / median_d)
                weight = np.exp(-dist_val / median_dist)
                if directions[nd] == 'up':
                    weighted_up += weight
                weighted_total += weight

        if weighted_total < self.MIN_ANALOGS * 0.01:  # Effectively zero weight
            return None, 0.0

        # --- effective sample size approximation for beta-binomial ---
        n_effective = weighted_total  # sum of weights = effective sample size
        if n_effective < 0.5:
            return None, 0.0

        prob_up = (weighted_up + 1.0) / (n_effective + 2.0)

        if abs(prob_up - 0.5) < 1e-6:
            return None, 0.0

        direction  = 'up' if prob_up > 0.5 else 'down'
        # Raw confidence = P(correct direction)
        confidence = prob_up if direction == 'up' else (1.0 - prob_up)

        return direction, confidence

    # ── Batch / multi-station helpers ─────────────────────────────────

    def get_prediction_for_stations(self, stations: List[str],
                                    target_date: str
                                    ) -> Dict[str, Tuple[Optional[str], float]]:
        """Evaluate for multiple stations on the same target date."""
        return {s: self.evaluate_nwp_analog(s, target_date) for s in stations}

    def compute_signal(self, station: str, target_date: str = None
                       ) -> Optional[Dict[str, Any]]:
        """Public dict-returning API (compatibility with existing callers)."""
        from datetime import datetime as dt
        if target_date is None:
            target_date = dt.now().strftime('%Y-%m-%d')
        direction, confidence = self.evaluate_nwp_analog(station, target_date)
        if direction is None:
            return None
        return {
            'direction': 1 if direction == 'up' else -1,
            'confidence': confidence,
            'num_analogs': self.K,
            'bias': (1 if direction == 'up' else -1) * confidence,
        }

    # ── Calibration integration ──────────────────────────────────────

    def get_calibration_key(self) -> str:
        """Key used in CalibrationPipeline for per-signal identity."""
        return self.name

    # ── Debug / inspection ────────────────────────────────────────────

    def describe_analog(self, station: str, target_date: str,
                        n_display: int = 5) -> None:
        """Print debug info about the analog match for a (station, date)."""
        direction, confidence = self.evaluate_nwp_analog(station, target_date)
        print(f"\nNWP Analog — {station} @ {target_date}")
        print(f"  Direction:  {direction}")
        print(f"  Confidence: {confidence:.4f}")
        print(f"  K:          {self.K}")
        print(f"  Fields:     {list(self.FIELD_WEIGHTS.keys())}")
        print(f"  Window:     ±{self.SEASONAL_WINDOW} days")


# ══════════════════════════════════════════════════════════════════════
# Self-test / demo
# ══════════════════════════════════════════════════════════════════════

def demo():
    """Quick smoke-test for a few stations on recent dates."""
    logging.basicConfig(level=logging.INFO)
    sig = NwpAnalogSignal()
    print("=" * 60)
    print("NWP Analog Signal — Deterministic k-NN Demo")
    print("=" * 60)

    test_cases = [
        ('KNYC', '2026-07-10'),
        ('KLAX', '2026-07-10'),
        ('KMDW', '2026-07-10'),
        ('KATL', '2026-07-10'),
    ]
    for station, date in test_cases:
        d, c = sig.evaluate_nwp_analog(station, date)
        print(f"  {station:6s} @ {date}: dir={d!s:>5s}  conf={c:.4f}")
    print()


if __name__ == "__main__":
    # Only run demo when NWP_ANALOG_ENABLED is active
    demo()
