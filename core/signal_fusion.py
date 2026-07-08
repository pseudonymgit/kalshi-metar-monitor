#!/usr/bin/env python3
"""
CORE MODULE: Signal Fusion & Information-Theoretic Aggregation

Implements the three-layer fusion stack:
1. Layer 0: Signal conditioning with isotonic calibration (uses core/calibration_pipeline)
2. Layer 1: Mutual-information-based decorrelation weighting
3. Layer 2: Log-odds linear opinion pool (LLOP)
4. Layer 3: Dempster-Shafer conflict detection

Based on Expert 2 Signal Fusion & Information Theory Analysis (Gray Room Round 3)
"""

import os
import numpy as np
import math
from collections import defaultdict
from scipy.special import expit, logit
try:
    from calibration_pipeline import CalibrationPipeline
except ImportError:
    try:
        from core.calibration_pipeline import CalibrationPipeline
    except ImportError:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        from calibration_pipeline import CalibrationPipeline


def mutual_information_from_boolean_pairs(pairs_i, pairs_j, outcomes):
    """
    Compute mutual information I(X_i; X_j | outcomes) for binary predictions.
    
    Args:
        pairs_i: List of (direction_pred_i, confidence_i) 
        pairs_j: List of (direction_pred_j, confidence_i)
        outcomes: List of (actual_direction)
        
    Returns:
        Mutual information in bits
    """
    if len(pairs_i) < 50 or len(pairs_j) < 50 or len(outcomes) < 50:
        return 0.0  # Insufficient data
        
    if len(pairs_i) != len(pairs_j) or len(pairs_i) != len(outcomes):
        raise ValueError("All arrays must have same length")
        
    # Create discretized agreement levels for both signals
    # Convert (direction, conf) to a combined binary concept representing prediction
    pred_i = []
    pred_j = []
    for (dir_i, conf_i), (dir_j, conf_j), (actual) in zip(pairs_i, pairs_j, outcomes):
        # Create a combined prediction value: + if UP and conf>0.5, - if DOWN and conf>0.5, etc.
        # Use high confidence and low confidence categories
        i_strength = 'strong' if conf_i > 0.7 else 'weak'
        i_pred = 1 if dir_i == 'up' else 0
        pred_i.append((i_strength, i_pred))
        
        j_strength = 'strong' if conf_j > 0.7 else 'weak'
        j_pred = 1 if dir_j == 'up' else 0
        pred_j.append((j_strength, j_pred))

    # Count occurrences
    joint_counts = defaultdict(lambda: defaultdict(int))
    i_counts = defaultdict(int)
    j_counts = defaultdict(int)
    total = len(pairs_i)

    for pred_i_val, pred_j_val in zip(pred_i, pred_j):
        joint_counts[pred_i_val][pred_j_val] += 1
        i_counts[pred_i_val] += 1
        j_counts[pred_j_val] += 1

    # Calculate mutual information: I(X;Y) = Σ p(x,y) * log2(p(x,y)/(p(x)*p(y)))
    mi = 0.0
    for x in joint_counts:
        for y in joint_counts[x]:
            px = i_counts[x] / total
            py = j_counts[y] / total
            pxy = joint_counts[x][y] / total
            
            if px > 0 and py > 0 and pxy > 0:
                mi += pxy * math.log2(pxy / (px * py))
    
    return max(0.0, mi)  # Return non-negative MI


def mutual_information_matrix(signals_history, signal_names, n_bins=3):
    """
    Compute symmetric mutual information matrix for all signal pairs.
    
    Args:
        signals_history: dict {signal_name: [(direction, conf, correct, date), ...]}
        signal_names: list of signal names
        n_bins: number of bins for discretization (to simplify MI calculation)
        
    Returns:
        2D array: MI matrix [n_signals x n_signals]
    """
    n_signals = len(signal_names)
    if n_signals == 0:
        return []
        
    mi_matrix = [[0.0 for _ in range(n_signals)] for _ in range(n_signals)]
    
    # For now, compute based on co-firing days across signal pairs
    # A simplified MI computation comparing predictions across same days
    all_dates = set()
    signal_predictions = {}
    
    # Collect data by date
    for sig_name in signal_names:
        if sig_name in signals_history:
            sig_hist = signals_history[sig_name]
            # Map date to (direction, confidence) only when signal predicted
            date_mappings = {entry[3]: (entry[0], entry[1]) for entry in sig_hist if len(entry) > 3}
            signal_predictions[sig_name] = date_mappings
            all_dates.update(date_mappings.keys())
    
    # Only consider days where both signals predicted (co-fired)
    for i, sig_i in enumerate(signal_names):
        for j, sig_j in enumerate(signal_names):
            if i == j:
                mi_matrix[i][j] = 0  # MI with itself is 0 in this context
                continue
            
            predictions_i = signal_predictions.get(sig_i, {})
            predictions_j = signal_predictions.get(sig_j, {})
            
            # Find days when both signals fired
            common_dates = set(predictions_i.keys()).intersection(set(predictions_j.keys()))
            
            if len(common_dates) < 50:  # Need min samples
                continue
                
            # Get paired predictions
            pairs_i = [predictions_i[date] for date in common_dates if date in predictions_i]
            pairs_j = [predictions_j[date] for date in common_dates if date in predictions_j]
            outcomes = ['unknown'] * len(common_dates)  # dummy outcomes - could improve with actual results
            
            # Note: Simplified version - MI just based on prediction patterns
            mi_matrix[i][j] = mutual_information_simple_correlation(pairs_i, pairs_j)
            
    return mi_matrix


def mutual_information_simple_correlation(pairs_i, pairs_j):
    """
    Simplified measure of correlation between signal predictions.
    
    Args:
        pairs_i: [(direction, confidence), ...]
        pairs_j: [(direction, confidence), ...]
              
    Returns:
        Correlation coefficient as proxy to MI (0-1 range)
    """
    if len(pairs_i) < 10 or len(pairs_j) < 10:
        return 0.0
    
    # Convert to directional agreement: same direction weighted by confidence
    agreements = []
    for (dir_i, conf_i), (dir_j, conf_j) in zip(pairs_i, pairs_j):
        # Agreement = sign based (both up, both down) multiplied by confidence product
        direction_agree = 1.0 if dir_i == dir_j else -1.0
        confidence_product = conf_i * conf_j
        agreements.append(direction_agree * confidence_product)
    
    # Compute correlation-like measure
    mean_agreement = sum(agreements) / len(agreements)
    # Take absolute to get how strongly correlated they are, regardless of direction of correlation
    correlation_strength = abs(mean_agreement)
    
    # Cap to reasonable range based on confidence-weighted agreement
    return min(correlation_strength, 1.0)


def unique_information_fraction(mi_matrix, signal_idx, accuracy_estimates):
    """
    Compute unique information fraction for a signal based on its correlation with others.
    
    Args:
        mi_matrix: mutual information matrix [n_signals x n_signals]
        signal_idx: index of current signal
        accuracy_estimates: list of accuracy estimates for all signals
        
    Returns:
        Unique information fraction (0.1 - 1.0, clamped)
    """
    n = len(mi_matrix) if mi_matrix else 0
    if n <= signal_idx:
        return 0.1  # Very low unique information if not enough data
        
    if n == 1:
        return 1.0  # Only signal, fully unique
        
    # Sum up correlation with other signals
    total_correlation = sum(mi_matrix[signal_idx][j] for j in range(n) if j != signal_idx)
    
    # Normalize by potential maximum correlation - in our correlation proxy system, this is approximately the number of other signals
    max_possible_correlation = (n - 1)  # max if perfectly correlated with all others
    
    # Unique fraction = 1 - (normalized correlation)
    unique_frac = 1.0 - (total_correlation / max_possible_correlation) if max_possible_correlation > 0 else 0.0
    return max(0.1, min(1.0, unique_frac))  # Clamp to [0.1, 1.0]


def compute_weights_from_significance(mi_matrix, accuracies, signal_names):
    """
    Compute final weights using unique information fraction and accuracy.
    
    Args:
        mi_matrix: mutual information matrix
        accuracies: list of accuracy for each signal
        signal_names: list of signal names (for debug)
        
    Returns:
        List of normalized weights [0, 1] that sum to 1
    """
    n_signals = len(signal_names)
    weights = []
    
    for i in range(n_signals):
        acc = max(0.51, min(0.95, accuracies[i])) if i < len(accuracies) else 0.6
        accuracy_logodds = math.log(acc / (1 - acc)) if acc < 0.95 else math.log(0.95 / 0.05)
        
        unique_frac = unique_information_fraction(mi_matrix, i, accuracies)
        raw_weight = unique_frac * accuracy_logodds
        
        weights.append(raw_weight)
    
    # Normalize to sum to 1
    total_weight = sum(weights) if weights else 1.0
    
    if total_weight == 0:
        # Uniform fallback if all weights are 0
        return [1.0 / len(signal_names)] if signal_names else [1.0] if n_signals > 0 else []
    
    return [w / total_weight for w in weights]


def dempster_shafer_conflict(signals_and_confs):
    """
    Compute Dempster's conflict mass K between signals.
    
    Args:
        signals_and_confs: [(direction, calibrated_confidence), ...]
        
    Returns:
        Conflict mass K (0.0 to 1.0) - higher = more disagreement
    """
    # Simplified version: use the variance of directions or disagreements to assess conflict
    if len(signals_and_confs) < 2:
        return 0.0
        
    # Calculate how much signals disagree on direction
    ups = sum(1 for direction, conf in signals_and_confs if direction == 'up')
    downs = len(signals_and_confs) - ups
    
    # If all directions agree, minimal conflict
    if ups == 0 or downs == 0:
        return 0.1  # Very low but not zero
    
    # Proportional to disagreement
    avg_confidence = sum(conf for _, conf in signals_and_confs) / len(signals_and_confs)
    
    # Measure discordance between directions 
    direction_variability = 4.0 * (ups / len(signals_and_confs)) * (downs / len(signals_and_confs))  # Like variance of binary vars
    raw_conflict = direction_variability * (1.0 - avg_confidence)  # Higher when disagreement is low-confidence
    
    # Normalize to [0, 1]
    return min(1.0, max(0.0, raw_conflict))


def apply_conflict_modulation(final_prob, conflict_mass, method='kennedy'):
    """
    Apply conflict-based adjustment to final probability.
    
    Args:
        final_prob: Final probability from ensemble
        conflict_mass: K from DS conflict computation
        method: 'kennedy' or 'traditional' - different approaches
    
    Returns:
        Adjusted final probability (0.0 to 1.0), or None if conflict too high
    """
    if conflict_mass >= 0.95:
        return 0.5  # Suppressed to 50% only if extreme conflict
    elif conflict_mass >= 0.8:
        # Suppress towards 0.5
        suppression_factor = 1.0 - ((conflict_mass - 0.8) / 0.15) * 0.5
        distance_from_half = final_prob - 0.5
        adjusted_distance = distance_from_half * suppression_factor
        return 0.5 + adjusted_distance
    elif conflict_mass < 0.3:
        # Amplify slightly to reflect high coherence
        distance_from_half = final_prob - 0.5
        amplified_distance = distance_from_half * 1.1 # Amplify by 10%
        # Make sure we stay in bounds
        amplified_prob = 0.5 + amplified_distance
        return max(0.01, min(0.99, amplified_prob))
    else:
        # No conflict, return as-is
        return final_prob


class SignalFusionEngine:
    """
    Orchestrate the 4-layer fusion stack:
    0: Signal conditioning (with isotonic regression calibration)
    1: MI-based decorrelation weighting
    2: Log-odds linear opinion pool
    3: Dempster-Shafer conflict detection
    """
    def __init__(self, signal_names, city_codes):
        self.signal_names = signal_names
        self.city_codes = city_codes
        
        # Layer 0: Calibration pipeline
        self.calibration_pipeline = CalibrationPipeline(signal_names, city_codes)
        
        # Layer 1-2: Weights and MI matrix
        self.mi_matrix = None
        self.current_weights = None
        
        # Statistics about calibration for fallback
        self.signal_accuracies = {name: 0.6 for name in signal_names}  # Default estimates

    def fit_calibration(self, all_station_signal_history):
        """
        Train the calibration pipeline on historical data.
        
        Args:
            all_station_signal_history: dict of {(signal_name, station): [(raw_conf, correct), ...]}
        """
        print(f"Training calibration pipeline for {len(self.signal_names)} signals, {len(self.city_codes)} cities...")
        
        for (signal, station), history in all_station_signal_history.items():
            for raw_conf, was_correct in history:
                self.calibration_pipeline.update(signal, station, raw_conf, was_correct)
        
        print(f"Fitting calibration calibrators...")
        self.calibration_pipeline.refit()
        print("Calibration training complete.")

    def compute_fusion_weights(self, mi_matrix=None, recalibrate_accuracies=False):
        """
        Compute mutual information matrix and final fusion weights.
        """
        if mi_matrix is None:
            print("Computing mutual information matrix and fusion weights...")
            # We'd need the signal history here, for now use default approach
            # This function assumes weights have been computed elsewhere or use defaults
        
        # Calculate weights using MI-based decorrelation
        # For now just return equally distributed initial weights as placeholder
        # until MI data is provided
        n = len(self.signal_names)
        if n == 0:
            return []
        return [1.0 / n] * n

    def fuse_signals(self, signals, city_code, use_ds_conflict=True):
        """
        Main fusion method integrating all 4 layers.
        
        Args:
            signals: List of (signal_name, direction, raw_confidence)
            city_code: City code for calibration lookup
            use_ds_conflict: Whether to apply Dempster-Shafer conflict detection
            
        Returns:
            (direction, probability, confidence) or (None, 0.0, 0.0) if conflict too high
        """
        if not signals:
            return None, 0.0, 0.0
        
        # Layer 0: Apply calibration to convert raw confidences to probabilities of correctness
        calibrated_signals = []
        for signal_name, direction, raw_conf in signals:
            calibrated_prob = self.calibration_pipeline.calibrate(signal_name, city_code, raw_conf)
            
            # Convert P(correct) to directional probability
            if direction == 'up':
                prob_direction = calibrated_prob
            else:  # direction == 'down'
                prob_direction = 1.0 - calibrated_prob # Probability of down
                
            calibrated_signals.append((direction, prob_direction, calibrated_prob))
        
        # If no calibrated signals, return defaults
        if not calibrated_signals:
            return None, 0.0, 0.0
            
        # Layer 1 & 2: Compute weights and perform Log-Odds Linear Opinion Pooling
        # For now, use equal or estimated weights since we need full training data for MI
        n_signals = len(calibrated_signals)
        equal_weights = [1.0 / n_signals if n_signals > 0 else 1.0 for _ in calibrated_signals]
        
        # Calculate log odds for each signal's directional probability
        log_odds = []
        for direction, prob, _ in calibrated_signals:
            # Convert probability to log-odds (natural log of odds ratio)
            # For a probability p: log-odds = ln(p/(1-p))
            prob = max(0.01, min(0.99, prob))  # Clamp to avoid inf
            odds = prob / (1.0 - prob)
            log_odd = math.log(odds)
            log_odds.append(log_odd)
        
        # Weighted log-odds combination (LLOP)
        weighted_log_odds = 0.0
        for w, lo in zip(equal_weights, log_odds):
            weighted_log_odds += w * lo  
        
        # Convert back to probability with sigmoid
        final_prob = expit(weighted_log_odds)  # expit is sigmoid function
        
        # Determine final direction and confidence
        # Confidence = probability of the predicted direction (not doubled distance from 0.5)
        if final_prob >= 0.5:
            final_direction = 'up'
            confidence = final_prob  # P(up)
        else:
            final_direction = 'down'
            confidence = 1.0 - final_prob  # P(down)
            
        # Layer 3: Apply Dempster-Shafer conflict detection
        ds_conflict_signals_input = [(direction, prob) for direction, prob, _ in calibrated_signals]
        
        conflict_k = dempster_shafer_conflict(ds_conflict_signals_input)
        
        if use_ds_conflict:
            adjusted_prob = apply_conflict_modulation(final_prob, conflict_k)
            
            # If conflict suppression sets probability to 0.5, we interpret as no trade signal
            if abs(adjusted_prob - 0.5) < 0.005:  # Very close to chance — raised threshold slightly
                return None, 0.0, 0.0
            
            # Recompute confidence after adjustment — use probability directly
            if adjusted_prob >= 0.5:
                adjusted_direction = 'up'
                adjusted_confidence = adjusted_prob
            else:
                adjusted_direction = 'down'
                adjusted_confidence = 1.0 - adjusted_prob
                
            return adjusted_direction, adjusted_prob, adjusted_confidence
        else:
            return final_direction, final_prob, confidence


def demonstrate_fusion_stack():
    """
    Demonstrate the complete 4-layer fusion stack with toy data.
    """
    print("DEMONSTRATION: 4-Layer Fusion Stack")
    print("=" * 60)
    
    # Example from Expert 2 report
    signal_names = ['gaussian_v2', 'pressure', 'calendar_climatology', 'goldilocks']  
    city_codes = ['KNYC', 'KLAX', 'KATL']
    
    # Create fusion engine
    fusion = SignalFusionEngine(signal_names, city_codes)
    
    # Simulated history to train calibration
    history = {
        ('gaussian_v2', 'KNYC'): [(0.70, True), (0.80, True), (0.60, False), (0.75, True)],
        ('gaussian_v2', 'KLAX'): [(0.68, True), (0.75, False), (0.82, True), (0.65, True)],
        ('pressure', 'KNYC'): [(0.65, True), (0.72, True), (0.58, False), (0.70, True)],
        ('pressure', 'KLAX'): [(0.60, False), (0.73, True), (0.80, True), (0.68, True)],
        ('calendar_climatology', 'KNYC'): [(0.66, True), (0.78, True), (0.62, False), (0.74, True)],
        ('goldilocks', 'KLAX'): [(0.69, True), (0.76, False), (0.83, True), (0.67, True)],
    }
    
    # Add many more to meet MIN_SAMPLES requirement
    for i in range(100):
        sig_idx = i % len(signal_names)
        city_idx = i % len(city_codes)
        signal = signal_names[sig_idx]
        city = city_codes[city_idx]
        raw_conf = 0.55 + (0.3 * (i % 20) / 20)  # Between 0.55 and 0.85
        correct = (i % 3) != 0  # ~66% accuracy
        if (signal, city) not in history:
            history[(signal, city)] = []
        history[(signal, city)].append((raw_conf, correct))
    
    # Train calibration
    fusion.fit_calibration(history)
    
    # Test fusion on example signals
    # Test fusion on example signals (dead signals removed: reversion, regime, pressure_regime_interaction, dtr_trend)
    test_signals = [
        ('gaussian_v2', 'up', 0.68),
        ('pressure', 'up', 0.70),
        ('calendar_climatology', 'down', 0.72)
    ]
    
    print(f"\nTest Fusion Input:")
    for signal, direction, raw_conf in test_signals:
        print(f"  {signal}: {direction} @ {raw_conf}")
        
    result = fusion.fuse_signals(test_signals, 'KNYC')
    print(f"\nFusion Output: {result}")
    print(f"Result: Direction={result[0]}, Probability={result[1]:.3f}, Confidence={result[2]:.3f}")
    
    print("\nFusion engine initialized and ready for integration with ensemble.")
    return True


if __name__ == "__main__":
    demonstrate_fusion_stack()