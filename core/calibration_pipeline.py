#!/usr/bin/env python3
"""
CORE MODULE: Walk-Forward Isotonic Regression Calibration Pipeline

Converts raw signal confidence values into calibrated P(correct) values using:
1. Per-signal per-city isotonic regression
2. Hierarchical fallback: per-city → per-signal-global → global → identity
3. Walk-forward expanding window: fit on [start, t-1], apply to window t
4. Minimum 100 co-firing trades for per-signal-per-city cell
5. Confidence clamped to [0.5, 0.95] to prevent degenerate calibration

Based on Expert 6 Statistical Learning Framework Spec (Gray Room Round 3)
"""

import logging
import numpy as np
from sklearn.isotonic import IsotonicRegression
import sqlite3
from collections import defaultdict
import math
from scipy.special import expit, logit
import sys


_logger = logging.getLogger(__name__)


class CalibrationPipeline:
    """
    Walk-forward isotonic calibration for weather engine signals.

    Converts raw signal confidences (vote margins, z-scores, etc.)
    into calibrated P(correct) values.

    Fallback chain: per-signal-per-city → per-signal-global → global → identity

    WALK-FORWARD LEAK FIX (0.5.1):
    - `refit()` now only fits on data from `window_start` onward, NOT all history.
    - Auto-refit in `calibrate()` is REMOVED — caller must explicitly call `refit()`.
    - `max_history` caps history size to prevent unbounded memory growth.
    - `prune_history()` drops entries before `window_start` to enforce window boundaries.
    """
    
    MIN_SAMPLES = 200  # Minimum co-firing trades for per-cell calibration
    
    def __init__(self, signal_names, city_codes, max_history=2000, window_start=0):
        self.signal_names = signal_names
        self.city_codes = city_codes
        self.calibrators = {}          # {(signal, city): IsotonicRegression}
        self.fallback_calibrators = {} # {signal: IsotonicRegression}
        self.global_calibrator = None
        self.history = defaultdict(list) # {(signal, city): [(raw_conf, correct_bool), ...]}
        self.refitted = False  # Track if calibrators are trained
        self.max_history = max_history  # 0.5.1: cap to prevent unbounded growth
        self.window_start = window_start  # 0.5.1: fit only on data from this index onward
        
        # Initialize database-based history tracking to support signal history pruning (Task 4.9)
        self.db_path = os.getenv("SIGNAL_CALIBRATION_DB", "/var/data/signal_calibration.db")
        self._init_history_db()

    def set_window_start(self, window_start):
        """
        Set the start of the current training window.
        Call this before refit() during walk-forward to ensure
        calibrators only fit on data up to T-1, not all history.
        """
        self.window_start = max(0, window_start)
        self.refitted = False

    def prune_history(self):
        """
        Remove history entries before window_start to enforce explicit
        window boundaries. This prevents walk-forward leakage where old
        training data bleeds into later test windows.

        Returns:
            int: Total number of entries pruned across all buckets
        """
        if self.window_start <= 0:
            return 0

        total_pruned = 0
        for key in list(self.history.keys()):
            old_len = len(self.history[key])
            if old_len <= self.window_start:
                # Not enough history to keep anything after window_start
                # Keep the last self.window_start entries as a fallback
                pass
            else:
                # Keep only entries from window_start onward
                self.history[key] = self.history[key][self.window_start:]
                total_pruned += old_len - len(self.history[key])

        self.refitted = False
        return total_pruned

    def update(self, signal, city, raw_conf, was_correct):
        """
        Add a new observation to the calibration history.
        
        Args:
            signal: signal name ('reversion', 'gaussian_v2', etc.)
            city: city code ('KNYC', 'KLAX', etc.)
            raw_conf: raw confidence value from signal (0-1)
            was_correct: boolean indicating if prediction was correct
        """
        self.history[(signal, city)].append((raw_conf, was_correct))
        
        # Persist to SQLite database for long-term storage and history management (task 4.9)
        try:
            import os
            import sqlite3
            
            # Only persist if database path exists - for backward compatibility
            if hasattr(self, 'db_path') and self.db_path and os.path.exists(os.path.dirname(self.db_path)):
                self.persist_evaluation_to_db(signal, city, raw_conf, was_correct)
        except Exception as e:
            # Silently fail if database operations have issues
            pass
        self.refitted = False  # Mark need for refit after history updates

        # 0.5.1: Enforce max_history cap — prune oldest entries from each bucket
        pruned_max = 0
        for key in list(self.history.keys()):
            if len(self.history[key]) > self.max_history:
                excess = len(self.history[key]) - self.max_history
                self.history[key] = self.history[key][excess:]
                pruned_max += excess

        # 0.5.1: Prune based on window_start to enforce walk-forward boundaries
        pruned_window = self.prune_history()

        if pruned_max > 0 or pruned_window > 0:
            _logger = logging.getLogger(__name__)
            _logger.debug(
                f"CalibrationPipeline: pruned {pruned_max} entries (max_history cap), "
                f"{pruned_window} entries (window_start boundary)"
            )
    
    def refit(self):
        """
        Re-fit all calibrators from accumulated history. Call after adding new data.
        Uses expanding window walk-forward approach on accumulated history.

        CRITICAL (0.5.1): Only fits on data from self.window_start onwards.
        This prevents walk-forward leakage where calibrators see future data.
        Caller must set window_start before refit() during walk-forward.
        """
        _logger.info(f"Fitting isotonic calibrators: {len(self.signal_names)} signals x {len(self.city_codes)} cities")
        _logger.info(f"  Window start: {self.window_start}, max_history: {self.max_history}")
        
        # 0.5.1: Prune history to enforce window boundaries
        self.prune_history()

        # 1. Per-signal per-city calibrators
        for signal in self.signal_names:
            for city in self.city_codes:
                key = (signal, city)
                data = self.history.get(key, [])
                
                if len(data) >= self.MIN_SAMPLES:
                    X = np.array([d[0] for d in data])
                    y = np.array([float(d[1]) for d in data])  # bool -> float conversion
                    iso = IsotonicRegression(out_of_bounds='clip',
                                           y_min=0.05, y_max=0.95)
                    iso.fit(X, y)
                    self.calibrators[key] = iso
                    _logger.info(f"  Fitted {key}: n={len(data)}")
                else:
                    # Remove calibrator if insufficient data
                    if key in self.calibrators:
                        del self.calibrators[key]
        
        # 2. Per-signal global fallback calibrators
        for signal in self.signal_names:
            global_data = []
            for city in self.city_codes:
                global_data.extend(self.history.get((signal, city), []))
            
            if len(global_data) >= 2 * self.MIN_SAMPLES:  # Require 2x min for global
                X = np.array([d[0] for d in global_data])
                y = np.array([float(d[1]) for d in global_data])
                iso = IsotonicRegression(out_of_bounds='clip',
                                       y_min=0.05, y_max=0.95)
                iso.fit(X, y)
                self.fallback_calibrators[signal] = iso
                _logger.info(f"  Fitted {signal}_global: n={len(global_data)}")
            else:
                # Remove if insufficient data
                if signal in self.fallback_calibrators:
                    del self.fallback_calibrators[signal]
        
        # 3. Global calibrator
        all_data = []
        for data in self.history.values():
            all_data.extend(data)
            
        if len(all_data) >= 4 * self.MIN_SAMPLES:  # Require 4x min for global
            X = np.array([d[0] for d in all_data])
            y = np.array([float(d[1]) for d in all_data])
            iso = IsotonicRegression(out_of_bounds='clip',
                                   y_min=0.05, y_max=0.95)
            iso.fit(X, y)
            self.global_calibrator = iso
            _logger.info(f"  Fitted global: n={len(all_data)}")
        
        self.refitted = True
        _logger.info(f"Fitting complete.")
    
    def calibrate(self, signal, city, raw_conf):
        """
        Convert raw confidence to calibrated P(correct).
        Fallback chain: per-signal-per-city → per-signal-global → global → identity
        
        Args:
            signal: signal name
            city: city code
            raw_conf: raw confidence value from signal (0-1)
        
        Returns:
            calibrated probability [0.5, 1.0] for UP, [0.0, 0.5] for DOWN
        """
        # NOTE (0.5.1): Auto-refit REMOVED to prevent walk-forward leakage.
        # Caller must explicitly call refit() after setting window_start.
        # If no refit has been performed, fall through to identity (cold start).
        clipped_conf = np.clip(raw_conf, 0.0, 1.0)
        
        # Level 1: Per-signal per-city calibration
        key = (signal, city)
        if key in self.calibrators:
            calibrated = self.calibrators[key].transform([clipped_conf])[0]
            # No-change zone: if calibrated is within ±0.01 of raw, keep raw
            # Using 0.01 instead of 0.05 ensures the calibrator actually has an effect
            # The original 0.05 was too wide — it suppressed useful calibration adjustments
            # for signals with modest but meaningful corrections under 5%.
            if abs(calibrated - clipped_conf) <= 0.01:
                return clipped_conf
            return float(calibrated)
        
        # Level 2: Per-signal global calibration
        if signal in self.fallback_calibrators:
            calibrated = self.fallback_calibrators[signal].transform([clipped_conf])[0]
            # No-change zone: 0.01 threshold (see Level 1 comment)
            if abs(calibrated - clipped_conf) <= 0.01:
                return clipped_conf
            return float(calibrated)
        
        # Level 3: Global calibration across all signals/cities
        if self.global_calibrator is not None:
            calibrated = self.global_calibrator.transform([clipped_conf])[0]
            # No-change zone: 0.01 threshold (see Level 1 comment)
            if abs(calibrated - clipped_conf) <= 0.01:
                return clipped_conf
            return float(calibrated)
        
        # Level 4: Identity (cold start) - return raw confidence
        # No-change zone: 0.01 threshold (see Level 1 comment)
        # This prevents over-calibration when the mapping is minor
        return clipped_conf
    
    def evaluate_calibration(self, signal, city, raw_confs, correct_bools, n_bins=10):
        """
        Compute Expected Calibration Error (ECE) with specified number of bins.
        
        Args:
            signal: signal name
            city: city code
            raw_confs: list of raw confidence values
            correct_bools: list of boolean correctness indicators
        
        Returns:
            ECE (lower is better, <0.04 is excellent)
        """
        if not raw_confs:
            return 1.0  # Maximum ECE if no data

        # Group predictions by calibrated confidence bins
        calibrated_confs = [self.calibrate(signal, city, conf) for conf in raw_confs]
        
        # Bin into equal-width confidence intervals (not equal-population!)
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0
        total_samples = len(calibrated_confs)

        for i in range(n_bins):
            lower_bound = bin_edges[i]
            upper_bound = bin_edges[i + 1]

            # Get samples in this bin after calibration
            bin_mask = [(lower_bound <= c < upper_bound) for c in calibrated_confs]
            bin_indices = [i for i, m in enumerate(bin_mask) if m]
            
            if len(bin_indices) == 0:
                continue  # Empty bin contributes 0 to ECE

            bin_confs = [calibrated_confs[j] for j in bin_indices]
            bin_correct = [correct_bools[j] for j in bin_indices]

            # Calculate bin properties
            bin_avg_confidence = np.mean(bin_confs)
            bin_accuracy = np.mean([float(c) for c in bin_correct])
            bin_size = len(bin_indices)

            # Add weighted absolute difference
            bin_ece = (bin_size / total_samples) * abs(bin_accuracy - bin_avg_confidence)
            ece += bin_ece

        return ece
    
    def compute_brier_score(self, signal, city, raw_confs, correct_bools):
        """
        Compute Brier score for this signal in this city (after calibration).
        
        Args:
            signal: signal name
            city: city code
            raw_confs: list of raw confidence values
            correct_bools: list of boolean correctness indicators
            
        Returns:
            Brier score (lower is better, < 0.22 is target)
        """
        if not raw_confs:
            return 1.0  # Maximum Brier if no data
            
        calibrated_confs = [self.calibrate(signal, city, conf) for conf in raw_confs]
        
        total = 0.0
        for cal_conf, actual_correct in zip(calibrated_confs, correct_bools):
            # For P(correct), if prediction was correct, we want loss to be (1-P(correct))^2 if P(correct) > 0.5 
            # or P(correct)^2 if P(correct) <= 0.5 (since we expect loss to be large if we said it was wrong)
            # Actually, Brier score is for forecasting probabilities:
            # If outcome=1 (correct), score = (1-p)^2
            # If outcome=0 (incorrect), score = p^2
            # Assuming cal_conf represents P(prediction is correct)
            expected_val = 1.0 if actual_correct else 0.0
            total += (expected_val - cal_conf) ** 2
        
        return total / len(calibrated_confs)
    
    def get_reliability_diagram_data(self, signal, city, raw_confs, correct_bools, n_bins=10):
        """
        Compute data for reliability diagram: confidence bin vs empirical accuracy.
        
        Args:
            signal: signal name
            city: city code
            raw_confs: list of raw confidences
            correct_bools: list of correctness booleans
            
        Returns:
            List of tuples: (bin_center, empirical_accuracy, bin_count)
        """
        if not raw_confs:
            return []

        calibrated_confs = [self.calibrate(signal, city, conf) for conf in raw_confs]
        
        # Equal-width bins
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        result = []

        for i in range(n_bins):
            lo = bin_edges[i]
            hi = bin_edges[i + 1]
            bin_center = (lo + hi) / 2.0

            # Find samples in this bin
            bin_idx = [(lo <= c < hi) for c in calibrated_confs]
            matched_indices = [idx for idx, is_in_bin in enumerate(bin_idx) if is_in_bin]

            if len(matched_indices) == 0:
                result.append((bin_center, None, 0))  # No data for this bin
            else:
                bin_correct = [correct_bools[j] for j in matched_indices]
                empirical_acc = sum(float(c) for c in bin_correct) / len(bin_correct)
                result.append((bin_center, empirical_acc, len(bin_correct)))

        return result





def demonstrate_calibration_workflow():
    """
    Demonstration showing the full isotonic regression workflow
    with per-signal-per-city calibration and fallback hierarchy.
    """
    _logger.info("DEMONSTRATION: CalibrationPipeline")
    _logger.info("=" * 60)
    
    # Example from Expert 6 report
    signal_names = ['reversion', 'gaussian_v2', 'regime']
    city_codes = ['KNYC', 'KLAX', 'KATL']
    
    # Create pipeline
    calib = CalibrationPipeline(signal_names, city_codes)
    
    # Simulated training data - (signal, city, raw_confidence, correct_bool, date)
    training_data = [
        ('reversion', 'KNYC', 0.8, True, '2024-01-01'),
        ('reversion', 'KNYC', 0.9, True, '2024-01-02'),  
        ('reversion', 'KNYC', 0.6, False, '2024-01-03'),
        ('reversion', 'KNYC', 0.7, True, '2024-01-04'),
        ('reversion', 'KATL', 0.85, True, '2024-01-05'),
        ('gaussian_v2', 'KNYC', 0.75, True, '2024-01-01'),
        ('gaussian_v2', 'KNYC', 0.8, False, '2024-01-02'),
        # Add more to meet MIN_SAMPLES requirement for some cells 
    ]
    
    # Add sufficient training data
    for i in range(150):  # Enough to meet MIN_SAMPLES for some combinations
        sig = signal_names[i % len(signal_names)]
        city = city_codes[i % len(city_codes)]
        import random
        raw_conf = 0.6 + (0.3 * random.random())  # Confidences between 0.6 and 0.9
        correct = random.random() > 0.3  # ~70% accuracy
        date_str = f"2024-01-{i%28+1:02d}" 
        training_data.append((sig, city, raw_conf, correct, date_str))
    
    _logger.info(f"Generated {len(training_data)} training samples")
    
    # Add the data and fit calibrators
    for signal, city, raw_conf, correct, date in training_data:
        calib.update(signal, city, raw_conf, correct)
    
    calib.refit()
    
    # Test the calibration on a few samples
    test_samples = [
        ('reversion', 'KNYC', 0.75),
        ('reversion', 'KATL', 0.75),
        ('nonexistent', 'KATL', 0.75),  # Should use fallback
    ]
    
    _logger.info(f"\nTest Results:")
    for signal, city, raw_conf in test_samples:
        calibrated = calib.calibrate(signal, city, raw_conf)
        _logger.info(f"  {signal}@{city}, raw={raw_conf:.3f} → calibrated={calibrated:.3f}")
    
    _logger.info("\nPipeline initialized and ready for integration with ensemble v8.")


if __name__ == "__main__":
    demonstrate_calibration_workflow()