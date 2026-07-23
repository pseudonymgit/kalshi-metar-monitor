#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-08 Deploy: Merge risk-guardrails-2026-07-08 to main for 9-signal ensemble release]
#

"""
CONVICTION SCORING MODULE - Real-time conviction and agreement computation

Implements the Signal Agreement Score (SAS) and calibrated conviction scoring
from the weather trading contract specification (SH6).

Formula: Conviction = |LLOP_prob − 0.5| × 2 × (0.3 + SAS) × (1 − ECE) × tanh(current_sample_count / 50)

Edge Calculation: 2 × |conviction_prob - 0.5|
Calibration State: Expected Calibration Error (ECE) with bias correction
"""

import math
from typing import List, Tuple, Dict, Optional, Any
from datetime import datetime, timezone
import statistics


class ConvictionScorer:
    """
    Real-time conviction and agreement scorer.
    
    Implements SH6 spec:
      - Signal Agreement Score (SAS) computation
      - ECE-adjusted conviction scoring
      - Regime fit assessment
    """
    
    def __init__(self):
        # Cache for performance optimization
        self.agreement_cache = {}
        self.conviction_cache = {}
        
    def compute_signal_agreement_score(
        self, 
        signal_list: List[Tuple[str, str, float]],  # (name, direction: 'up'/'down', calibrated_confidence)
        ensemble_direction: str,  # 'up' or 'down' 
        weights: List[float] = None
    ) -> float:
        """
        Compute Signal Agreement Score (SAS).
        
        Formula: SAS = Σ(wᵢ × aᵢ × cᵢ) / Σ(wᵢ)
        
        where:
          wᵢ = mutual-information-based weight
          aᵢ = agreement indicator (1.0 if signal i agrees with ensemble direction, 0.0 otherwise)
          cᵢ = calibrated confidence quality (clamped to [0.5, 1.0])
          
        Args:
            signal_list: List of (signal_name, direction, calibrated_confidence)
            ensemble_direction: Final ensemble direction ('up' or 'down')
            weights: Optional weights for each signal (equal if None)
        
        Returns:
            Signal Agreement Score (0.0 to 1.0)
        """
        n_signals = len(signal_list)
        if n_signals == 0:
            return 0.0
        
        if not weights:
            # Equal weights if not provided
            weights = [1.0 / n_signals if n_signals > 0 else 0.0] * n_signals if n_signals > 0 else []
            
        total_agreement = 0.0
        total_weight = 0.0
        
        for i, (name, direction, conf) in enumerate(signal_list):
            # Agreement indicator: 1.0 if agrees with ensemble direction
            agrees = 1.0 if direction == ensemble_direction else 0.0
            
            # Confidence quality: clamp to [0.5, 1.0]
            conf_quality = max(0.5, min(1.0, conf))
            
            weight = weights[i] if i < len(weights) else 1.0 / n_signals if n_signals > 0 else 0.0
            
            contribution = weight * agrees * conf_quality
            total_agreement += contribution
            total_weight += weight
            
        if total_weight == 0:
            return 0.0
            
        return total_agreement / total_weight if total_weight > 0 else 0.0
    
    def compute_calibration_state(self, historical_predictions: List[Tuple[float, bool]]) -> Dict[str, float]:
        """
        Compute Expected Calibration Error (ECE) and related metrics.
        
        Uses 10-bin equal-width decomposition:
        ECE = Σ(|Bin_k|/n × |acc_k - conf_k|)
        
        Args:
            historical_predictions: List of (predicted_prob, was_correct) pairs
         
        Returns:
            Dict with ECE and other calibration metrics
        """
        if not historical_predictions:
            return {
                'ece': 1.0,  # Poor calibration if no data
                'reliability_score': 0.0,
                'bias': 0.5,
                'sharpness': 0.0
            }
        
        n_bins = 10
        bin_size = 1.0 / n_bins if n_bins > 0 else 0.0
        bins = [[] for _ in range(n_bins)]
        
        # Bin the predictions
        for pred_prob, correct in historical_predictions:
            bin_idx = min(n_bins - 1, int(pred_prob / bin_size))
            bins[bin_idx].append((pred_prob, correct))
        
        # Calculate metrics
        ece_sum = 0.0
        reliability_sum = 0.0
        total_examples = len(historical_predictions)
        
        bin_accuracies = []
        bin_confs = []
        
        for bin_data in bins:
            if not bin_data:
                continue  # Skip empty bins
                
            bin_probs = [prob for prob, _ in bin_data]
            bin_correct = [correct for _, correct in bin_data]
            avg_prob = sum(bin_probs) / len(bin_probs) if bin_probs else 0.0
            avg_acc = sum(bin_correct) / len(bin_correct) if bin_correct else 0.0
            
            bin_accuracies.append(avg_acc)
            bin_confs.append(avg_prob)
            
            ece_term = (len(bin_data) / total_examples if total_examples > 0 else 0.0) * abs(avg_acc - avg_prob)
            ece_sum += ece_term
            
            reliability_sum += (len(bin_data) / total_examples if total_examples > 0 else 0.0) * ((avg_acc - avg_prob)**2)
        
        sharpness = statistics.stdev([p for p, _ in historical_predictions]) if len(historical_predictions) > 1 else 0.0
        avg_conf = sum([p for p, _ in historical_predictions]) / len(historical_predictions) if historical_predictions else 0.0
        bias = abs(0.5 - avg_conf)  # How biased from neutral
        
        return {
            'ece': ece_sum,
            'reliability_score': reliability_sum,
            'mse_calibration': ece_sum,  # Same as reliability term in classical formulation
            'sample_count': len(historical_predictions),
            'bias': bias,
            'sharpness': sharpness
        }
    
    def compute_conviction_score(
        self, 
        ensemble_prob: float, 
        signal_agreement: float, 
        calibration_data: Dict[str, float],
        sample_count: int = 100  # Recent sample count
    ) -> Dict[str, Any]:
        """
        Compute final conviction score with all SH6 components.
        
        Formula: Conviction = |LLOP_prob - 0.5| × 2 × (0.3 + SAS) × (1 - ECE) × tanh(sample_count/50)
        
        Includes edge calculation and regime assessments.
        
        Args:
            ensemble_probability: The fused probability from ensemble (0.0-1.0)
            signal_agreement: SAS score (0.0-1.0)
            calibration_data: Output from compute_calibration_state
            sample_count: How many recent samples used
            
        Returns:
            Dict with conviction score and all components for diagnostic purposes
        """
        avg_calibration_error = calibration_data.get('ece', 0.1)
        
        # Formula components
        edge_magnitude = abs(ensemble_prob - 0.5) * 2.0  # Scaled 0-1
        agreement_factor = 0.3 + signal_agreement  # Biased upward (0.3 to 1.3)
        calibration_quality = max(0.1, 1.0 - avg_calibration_error)  # Quality score 0-1 (minimum 0.1)
        sample_quality = math.tanh(sample_count / 50.0)  # Approaches 1.0
            
        conviction_score = edge_magnitude * agreement_factor * calibration_quality * sample_quality
        
        # Edge definition: absolute probability deviation * 2
        edge_after_fee = max(0.0, edge_magnitude - 0.01)  # Assuming 1 cent fee impact
        
        return {
            'conviction_score': conviction_score,
            'edge_after_fee': edge_after_fee,
            'agreement_score': signal_agreement,
            'calibration_state': {
                'ece': avg_calibration_error,
                'quality': calibration_quality,
                'reliability': calibration_data.get('reliability_score', 0),
                'sample_count': sample_count,
                'bias_correction': calibration_data.get('bias', 0)
            },
            'regime_fit': self._assess_regime_fit(ensemble_prob, calibration_data),
            'components': {
                'edge_magnitude': edge_magnitude,
                'agreement_factor': agreement_factor,
                'calibration_quality': calibration_quality,
                'sample_quality': sample_quality
            }
        }
    
    def _assess_regime_fit(self, prob, cal_data) -> str:
        """
        Assess if the current forecast fits the regime.
        """
        ece = cal_data.get('ece', 0.1)
        sharpness = cal_data.get('sharpness', 0.2)
        
        # High ECE + low sharpness = poor regime fit
        if ece > 0.15 and sharpness < 0.1:
            return "poor"
        elif ece > 0.10:
            return "fair"
        else:
            return "excellent"
    
    def assess_market_direction_validity(
        self,
        ensemble_direction: str,  # 'up' or 'down'
        market_type: str  # 'HIGH' or 'LOW'
    ) -> tuple[bool, str]:
        """
        Implement hard gate: never allow incompatible direction-market combinations.
        
        Args:
            ensemble_direction: 'up' or 'down'
            market_type: 'HIGH' or 'LOW'
            
        Returns:
            (valid, reason) tuple
        """
        if market_type.upper() == 'HIGH':
            # HIGH market: bet on temperature going UP in the HIGH range
            # 'up' direction is valid for HIGH market
            valid = (ensemble_direction.lower() == 'up')
            reason = "HIGH market expects UP temperature direction" if valid else "INCOMPATIBILITY: HIGH market with DOWN direction"
        elif market_type.upper() == 'LOW':
            # LOW market: bet on temperature going DOWN in the LOW range
            # 'down' direction is valid for LOW market
            valid = (ensemble_direction.lower() == 'down')
            reason = "LOW market expects DOWN temperature direction" if valid else "INCOMPATIBILITY: LOW market with UP direction"
        else:
            valid = False
            reason = f"INVALID market_type: {market_type}"
        
        return valid, reason


# Backward compatibility for legacy systems
def compute_signal_agreement_score(signal_list, ensemble_direction, weights=None):
    scorer = ConvictionScorer()
    return scorer.compute_signal_agreement_score(signal_list, ensemble_direction, weights)

def compute_conviction_score(ensemble_prob, signal_agreement, calibration_data, sample_count=100):
    scorer = ConvictionScorer()
    return scorer.compute_conviction_score(ensemble_prob, signal_agreement, calibration_data, sample_count)