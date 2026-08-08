#!/usr/bin/env python3
"""
eighty_two_member_ensemble_signal.py — Combined 82-member ensemble meta-signal

Combines GEFS (31 members) and ECMWF (51 members) into a single ensemble fraction
signal with bias correction and multiple weighting modes.

B-Mode compliant. No AI/ML.
"""

import os
import sqlite3
import struct
import json
import logging
from typing import Optional, Tuple, List, Dict

import numpy as np

from core.signals.base_signal import BaseSignal, validate_signal
from core.ensemble_fraction import load_bias_corrections, apply_bias_correction

logger = logging.getLogger(__name__)


class EightyTwoMemberEnsembleSignal(BaseSignal):
    """
    Combined 82-member ensemble meta-signal.
    
    Combines GEFS (31 members) and ECMWF (51 members) into a single
    ensemble fraction signal with bias correction.
    """

    def __init__(self, db_path=None, gefs_db=None, tigge_db=None,
                 weighting_mode='equal', threshold=None,
                 ece_weights_path=None, forecast_step=24, **kwargs):
        """
        Initialize the combined ensemble signal.
        
        Args:
            db_path: METAR database path (inherited from BaseSignal)
            gefs_db: Path to GEFS archive database
            tigge_db: Path to ECMWF TIGGE archive database
            weighting_mode: 'equal', 'ece_inverse', or 'member_pooling'
            threshold: Optional temperature threshold override
            ece_weights_path: Path to ECE weights JSON file
            forecast_step: Forecast hour to use (default 24)
            **kwargs: Additional kwargs for _try_instantiate compatibility
        """
        super().__init__(db_path)
        
        # Accept **kwargs so _try_instantiate() can pass extra keys without crashing
        self.gefs_db = gefs_db or os.path.join("data", "gefs_archive.db")
        self.tigge_db = tigge_db or os.path.join("data", "tigge_archive.db")
        # Validate weighting_mode
        if weighting_mode not in ('equal', 'ece_inverse', 'member_pooling'):
            weighting_mode = 'equal'
        self._weighting_mode = weighting_mode
        self._threshold = threshold
        self._ece_weights_path = ece_weights_path
        self._forecast_step = forecast_step
        
        # Lazy-loaded resources
        self._bias_data = None
        self._ece_weights = None
        self._gefs_conn = None
        self._tigge_conn = None

    @property
    def name(self) -> str:
        """Canonical name for this signal."""
        return "eighty_two_member_ensemble"

    @property
    def min_lookback(self) -> int:
        """Minimum number of prior days required."""
        return 0  # Ensemble is a forecast, not history-dependent

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate combined 82-member signal at day index `idx`.
        
        Station context is extracted from days[idx]['station'].
        The sweep's evaluate_signal_on_station() populates this field
        for each station-specific days list before calling evaluate().

        Args:
            idx: Index into days list for the target day
            days: List of daily dicts; days[idx] must contain:
                'station' (str, ICAO code)
                'date'    (str, ISO date YYYY-MM-DD)
                'high'    (float, observed high temp °F for threshold)

        Returns:
            (direction, confidence) or (None, 0.0) if insufficient data
        """
        # Station context — signal assumes sweep populates days[idx]['station'],
        # but evaluate_signal_on_station() doesn't add it. Use a fallback:
        # check days[idx]['station'] first, then try current_station._current_station.
        station = days[idx].get('station')
        if not station:
            station = getattr(self, '_current_station', None)
        target_date = days[idx].get('date')
        if not station or not target_date:
            return None, 0.0

        # Resolve threshold
        threshold = self._resolve_threshold(days, idx, station, target_date)
        if threshold is None:
            return None, 0.0

        # Load ensemble members
        gefs_members = self._load_gefs_members(station, target_date)
        ecmwf_members = self._load_ecmwf_members(station, target_date)

        if len(gefs_members) == 0 and len(ecmwf_members) == 0:
            return None, 0.0

        # Bias correct members
        gefs_corrected, ecmwf_corrected = self._bias_correct_all(
            gefs_members, ecmwf_members, station, target_date
        )

        # Compute ensemble fraction
        fraction = self._compute_fraction(
            gefs_corrected, ecmwf_corrected, threshold, station
        )

        # Convert fraction to direction and confidence
        direction = 'up' if fraction >= 0.5 else 'down'
        confidence = abs(fraction - 0.5) * 2  # Scale [0,0.5] → [0,1]

        return direction, confidence

    def _resolve_threshold(self, days, idx, station, target_date,
                          conn=None) -> Optional[float]:
        """Resolve the threshold temperature."""
        # Constructor override takes precedence
        if self._threshold is not None:
            return self._threshold
        
        # Read from days[idx-1]['high'] — previous day's actual high, avoids look-ahead bias
        if days is not None and idx >= 1 and idx < len(days):
            high = days[idx - 1].get('high')
            if high is not None:
                return high
        
        # Fallback: settlements DB if sweep provides connection
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT target_high_f FROM kalshi_settlements
                WHERE station=? AND target_date=?
            """, (station, target_date))
            row = cur.fetchone()
            if row:
                return row[0]
        
        # No fallback available
        return None

    def _load_gefs_members(self, station: str, target_date: str,
                          step: int = None) -> np.ndarray:
        """Load GEFS member temperatures in °F."""
        step = step or self._forecast_step
        conn = self._get_gefs_conn()
        
        try:
            cur = conn.execute("""
                SELECT ensemble_mean, member_values, n_members
                FROM gefs_archive
                WHERE target_date=? AND station=? AND step=?
            """, (target_date, station, step))
            row = cur.fetchone()
            if not row:
                return np.array([])
                
            mean, blob, n_members = row
            
            # Validate blob size — GEFS blobs are 31 bytes (padded to max 31 members)
            # but n_members may be 6 (partial backfill). Accept if blob >= n_members.
            if len(blob) < n_members:
                return np.array([])
                
            # Decode signed byte offsets
            offsets = struct.unpack(f'{n_members}b', blob[:n_members])
            temps_c = np.array([mean + o * 0.1 for o in offsets], dtype=np.float32)
            
            # Convert °C to °F
            return temps_c * 9.0/5.0 + 32.0
            
        except (sqlite3.Error, struct.error) as e:
            logger.debug(f"GEFS load error for {station} {target_date}: {e}")
            return np.array([])

    def _load_ecmwf_members(self, station: str, target_date: str,
                           step: int = None) -> np.ndarray:
        """Load ECMWF member temperatures in °F."""
        step = step or self._forecast_step
        conn = self._get_tigge_conn()
        
        try:
            cur = conn.execute("""
                SELECT member_values, n_members
                FROM tigge_archive
                WHERE target_date=? AND station=? AND step=? AND source='tigge_ecmwf'
            """, (target_date, station, step))
            row = cur.fetchone()
            if not row:
                return np.array([])
                
            blob, n_members = row
            
            # Validate blob size matches expected member count
            expected_bytes = 4 * n_members  # 4 bytes per float32
            # Validate blob size — TIGGE blobs vary in size (200 bytes = 50 float32 complete,
            # 62 bytes = 31 int16 partial, etc.). Accept only properly formatted 200-byte blobs.
            if len(blob) < 4 * n_members:
                return np.array([])
                
            # Decode float32 values
            temps_c = np.frombuffer(blob, dtype=np.float32, count=n_members)
            
            # Convert °C to °F
            return temps_c * 9.0/5.0 + 32.0
            
        except (sqlite3.Error, ValueError) as e:
            logger.debug(f"ECMWF load error for {station} {target_date}: {e}")
            return np.array([])

    def _bias_correct_all(self, gefs_members, ecmwf_members, station, target_date):
        """Apply bias correction to both ensembles."""
        bias_data = self._get_bias_data()
        if bias_data is None:
            # Bias file not found — use raw members
            return gefs_members, ecmwf_members
        
        gefs_corrected = apply_bias_correction(gefs_members, station, target_date, bias_data)
        ecmwf_corrected = apply_bias_correction(ecmwf_members, station, target_date, bias_data)
        return gefs_corrected, ecmwf_corrected

    def _compute_fraction(self, gefs_members, ecmwf_members, threshold, station):
        """Dispatch to the active weighting mode."""
        if self._weighting_mode == 'equal':
            return self._compute_fraction_equal(gefs_members, ecmwf_members, threshold)
        elif self._weighting_mode == 'ece_inverse':
            return self._compute_fraction_ece(gefs_members, ecmwf_members, threshold, station)
        elif self._weighting_mode == 'member_pooling':
            return self._compute_fraction_per_model_avg(gefs_members, ecmwf_members, threshold)
        else:
            # Unknown mode — safe fallback
            return self._compute_fraction_equal(gefs_members, ecmwf_members, threshold)

    def _compute_fraction_equal(self, gefs_members, ecmwf_members, threshold):
        """All 82 members, equal weight."""
        all_members = np.concatenate([gefs_members, ecmwf_members])
        valid = all_members[~np.isnan(all_members)]
        if len(valid) == 0:
            return 0.5
        return float(np.mean(valid > threshold))

    def _compute_fraction_ece(self, gefs_members, ecmwf_members, threshold, station):
        """Per-model aggregate ECE-inverse weighting."""
        weights = self._load_ece_weights()
        
        # Per-model aggregate weights
        w_gefs = 1.0 / (weights.get("gefs_agg", 0.01) + 0.001)
        w_ecmwf = 1.0 / (weights.get("ecmwf_agg", 0.01) + 0.001)
        total_w = w_gefs * len(gefs_members) + w_ecmwf * len(ecmwf_members)
        
        if total_w == 0:
            return 0.5
        
        # Apply per-member weights based on model membership
        gefs_valid = gefs_members[~np.isnan(gefs_members)]
        ecmwf_valid = ecmwf_members[~np.isnan(ecmwf_members)]
        
        gefs_contrib = w_gefs * np.sum(gefs_valid > threshold)
        ecmwf_contrib = w_ecmwf * np.sum(ecmwf_valid > threshold)
        
        weighted_frac = (gefs_contrib + ecmwf_contrib) / total_w
        return float(weighted_frac)

    def _compute_fraction_per_model_avg(self, gefs_members, ecmwf_members, threshold):
        """Average the per-model fractions, skipping models with no valid members."""
        fractions = []
        gefs_valid = gefs_members[~np.isnan(gefs_members)]
        ecmwf_valid = ecmwf_members[~np.isnan(ecmwf_members)]
        if len(gefs_valid) > 0:
            fractions.append(self._model_fraction(gefs_members, threshold))
        if len(ecmwf_valid) > 0:
            fractions.append(self._model_fraction(ecmwf_members, threshold))
        if not fractions:
            return 0.5
        return float(np.mean(fractions))

    def _model_fraction(self, members, threshold):
        """Compute fraction > threshold for one model's members."""
        valid = members[~np.isnan(members)]
        if len(valid) == 0:
            return 0.5
        return float(np.mean(valid > threshold))

    def _get_gefs_conn(self):
        """Get cached GEFS database connection."""
        if self._gefs_conn is None:
            self._gefs_conn = sqlite3.connect(self.gefs_db)
        return self._gefs_conn

    def _get_tigge_conn(self):
        """Get cached ECMWF database connection."""
        if self._tigge_conn is None:
            self._tigge_conn = sqlite3.connect(self.tigge_db)
        return self._tigge_conn

    def _get_bias_data(self):
        """Load bias corrections data."""
        if self._bias_data is None:
            try:
                self._bias_data = load_bias_corrections()
            except FileNotFoundError:
                self._bias_data = None
        return self._bias_data

    def _load_ece_weights(self):
        """Load per-model aggregate ECE weights."""
        if self._ece_weights is None:
            path = self._ece_weights_path or os.path.join("data", "calibration", "ece_weights.json")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        self._ece_weights = json.load(f)
                except (json.JSONDecodeError, OSError):
                    self._ece_weights = {"gefs_agg": 1.0, "ecmwf_agg": 1.0}
            else:
                # Fallback to equal weights
                self._ece_weights = {"gefs_agg": 1.0, "ecmwf_agg": 1.0}
        return self._ece_weights

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None):
        """
        Evaluate for a specific station+date (convenience override).
        
        NOTE: This is for direct testing. The primary entry point is evaluate(idx, days).
        """
        # Build a minimal days list with the station field
        days = [{'station': station, 'date': date}]
        
        # Resolve threshold from settlements DB if available
        threshold = self._resolve_threshold(days, 0, station, date, conn)
        if threshold is not None:
            days[0]['high'] = threshold
        
        return self.evaluate(0, days)


# Test the signal can be imported and instantiated
if __name__ == "__main__":
    # Test import and instantiation
    signal = EightyTwoMemberEnsembleSignal()
    print(f"Signal created: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")
    
    # Test _try_instantiate patterns
    test_patterns = [
        {"db_path": "test.db", "gefs_db": "gefs.db", "tigge_db": "tigge.db"},
        {"db_path": "test.db"},
        {}
    ]
    
    for i, kwargs in enumerate(test_patterns):
        try:
            test_signal = EightyTwoMemberEnsembleSignal(**kwargs)
            print(f"Pattern {i+1}: Success")
        except Exception as e:
            print(f"Pattern {i+1}: Failed - {e}")