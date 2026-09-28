#!/usr/bin/env python3
"""
gamma_ensemble_fusion.py — Gamma-82 Ensemble Fusion Module

Integrates Gamma signal classes (G1-G6) with the 82-member ensemble
to create an enhanced prediction system with orthogonal information channels.
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
import logging

from core.signals.gamma_signals import (
    get_gamma_signals, G1_UncertaintySignal, G2_RegimeSignal, 
    G3_AnalogSignal, G4_PressureAdvectionSignal, G5_DewpointSignal, G6_CloudCoverSignal
)
from core.signals.eighty_two_member_ensemble_signal import EightyTwoMemberEnsembleSignal
from core.signals.base_signal import validate_signal

logger = logging.getLogger(__name__)


@dataclass
class FusionResult:
    """Result container for ensemble fusion."""
    direction: Optional[str]
    confidence: float
    ensemble_direction: Optional[str]
    ensemble_confidence: float
    gamma_direction: Optional[str]
    gamma_confidence: float
    gamma_signals: Dict[str, Tuple[Optional[str], float]]
    agreement_score: float
    adjusted_confidence: float


class GammaEnsembleFusion:
    """
    Fuses 82-member ensemble with Gamma signals (G1-G6) to enhance predictions.
    
    The fusion mechanism:
    1. Gets base prediction from 82-member ensemble
    2. Gets complementary signals from Gamma family
    3. Applies uncertainty modulation from G1
    4. Applies regime adjustment from G2
    5. Combines with weighted voting based on signal agreement
    """
    
    def __init__(self, 
                 db_path: str = None,
                 ensemble_weight: float = 0.7,
                 gamma_weight: float = 0.3,
                 uncertainty_modulation: bool = True,
                 agreement_threshold: float = 0.5,
                 use_signals: List[str] = None):
        """
        Initialize the fusion engine.
        
        Args:
            db_path: Database path for both ensemble and gamma signals
            ensemble_weight: Weight for 82-member ensemble (0-1)
            gamma_weight: Weight for gamma signals (0-1, should complement ensemble_weight)
            uncertainty_modulation: Whether to apply G1 uncertainty modulation
            agreement_threshold: Minimum agreement for signal inclusion
        """
        self.db_path = db_path
        self.ensemble_weight = ensemble_weight
        self.gamma_weight = gamma_weight
        self.uncertainty_modulation = uncertainty_modulation
        self.agreement_threshold = agreement_threshold
        
        # Initialize ensemble and gamma signals
        self.ensemble_signal = EightyTwoMemberEnsembleSignal(db_path=db_path)
        self.gamma_signals = get_gamma_signals(db_path=db_path)
        
        # Create named mapping for easier access
        self.gamma_signal_map = {
            'g1_uncertainty': self.gamma_signals[0],  # G1_UncertaintySignal
            'g2_regime': self.gamma_signals[1],      # G2_RegimeSignal
            'g3_analog': self.gamma_signals[2],      # G3_AnalogSignal
            'g4_pressure_adv': self.gamma_signals[3], # G4_PressureAdvectionSignal
            'g5_dewpoint': self.gamma_signals[4],    # G5_DewpointSignal
            'g6_cloud_cover': self.gamma_signals[5]  # G6_CloudCoverSignal
        }
        # GS-B4 (2026-09-28): only G1+G3 carry real edge (G2/G4/G5/G6 parked —
        # honest holdout <=0.51). Gate the fusion to the keepers by default.
        self.use_signals = use_signals or ['g1_uncertainty', 'g3_analog']
    
    def fuse(self, idx: int, days: List[dict]) -> FusionResult:
        """
        Perform fusion of 82-member ensemble with Gamma signals.
        
        Args:
            idx: Index into days list for the target day
            days: List of daily dicts with station/date/temp data
            
        Returns:
            FusionResult with combined prediction and individual signal results
        """
        # Get ensemble prediction
        ensemble_dir, ensemble_conf = self.ensemble_signal.evaluate(idx, days)
        
        # Get gamma signal predictions
        gamma_results = {}
        active_gamma_signals = []
        
        for name, signal in self.gamma_signal_map.items():
            try:
                gamma_dir, gamma_conf = signal.evaluate(idx, days)
                gamma_results[name] = (gamma_dir, gamma_conf)

                # GS-B4/B6: only include allowlisted signals, and exclude any that
                # are not meaningfully confident (conf<=0.5 carries no signed edge).
                if name not in self.use_signals:
                    continue
                if gamma_dir is not None and gamma_conf > 0.5:
                    active_gamma_signals.append((name, gamma_dir, gamma_conf))
            except Exception as e:
                logger.warning(f"Gamma signal {name} failed: {e}")
                gamma_results[name] = (None, 0.0)
        
        if not active_gamma_signals:
            # No gamma signals active, return ensemble result
            return FusionResult(
                direction=ensemble_dir,
                confidence=ensemble_conf,
                ensemble_direction=ensemble_dir,
                ensemble_confidence=ensemble_conf,
                gamma_direction=None,
                gamma_confidence=0.0,
                gamma_signals=gamma_results,
                agreement_score=0.0,
                adjusted_confidence=ensemble_conf
            )
        
        # Calculate gamma ensemble result (simple weighted average of active signals)
        gamma_direction, gamma_confidence = self._aggregate_gamma_signals(active_gamma_signals)
        
        # Calculate agreement between ensemble and gamma signals
        agreement_score = self._calculate_agreement(ensemble_dir, gamma_direction, ensemble_conf, gamma_confidence)
        
        # Apply fusion with agreement-based weighting
        final_direction, adjusted_confidence = self._fuse_predictions(
            ensemble_dir, ensemble_conf, gamma_direction, gamma_confidence, agreement_score
        )
        
        return FusionResult(
            direction=final_direction,
            confidence=adjusted_confidence,
            ensemble_direction=ensemble_dir,
            ensemble_confidence=ensemble_conf,
            gamma_direction=gamma_direction,
            gamma_confidence=gamma_confidence,
            gamma_signals=gamma_results,
            agreement_score=agreement_score,
            adjusted_confidence=adjusted_confidence
        )
    
    def _aggregate_gamma_signals(self, active_signals: List[tuple]) -> Tuple[Optional[str], float]:
        """
        Aggregate active gamma signals into a single prediction.

        GS-B6 (2026-09-28): SIGNED aggregation. Each signal votes with weight
        max(0, 2*conf - 1) — a conf of 0.5 is zero weight, conf of 1.0 is full
        weight. Signals at conf<=0.5 were already excluded upstream. This avoids
        the old bug where a 0.51-conf signal counted as strongly as a 0.95 one.

        Args:
            active_signals: list of (name, direction, confidence) tuples

        Returns:
            (direction, confidence) aggregated result
        """
        if not active_signals:
            return None, 0.0

        up_weight = 0.0
        down_weight = 0.0
        total_weight = 0.0
        for entry in active_signals:
            name, direction, confidence = entry
            w = max(0.0, 2.0 * confidence - 1.0)  # signed weight; 0.5 -> 0
            if w <= 0:
                continue
            total_weight += w
            if direction == 'up':
                up_weight += w
            elif direction == 'down':
                down_weight += w

        if total_weight == 0:
            return None, 0.0

        if up_weight > down_weight:
            return 'up', min(1.0, up_weight / total_weight)
        if down_weight > up_weight:
            return 'down', min(1.0, down_weight / total_weight)
        # Tie — no directional edge
        return 'neutral', 0.3
    
    def _calculate_agreement(self, 
                           ensemble_dir: Optional[str], 
                           gamma_dir: Optional[str], 
                           ensemble_conf: float, 
                           gamma_conf: float) -> float:
        """
        Calculate agreement score between ensemble and gamma predictions.
        
        Args:
            ensemble_dir: Direction from 82-member ensemble
            gamma_dir: Direction from gamma signal aggregation
            ensemble_conf: Confidence from 82-member ensemble
            gamma_conf: Confidence from gamma signal aggregation
            
        Returns:
            Agreement score between 0 and 1
        """
        if ensemble_dir is None or gamma_dir is None:
            return 0.0
        
        # If both are neutral or both predict same direction, high agreement
        if ensemble_dir == gamma_dir:
            # Strong agreement when both are confident
            return min(1.0, (ensemble_conf + gamma_conf) / 2.0)
        elif ensemble_dir == 'neutral' or gamma_dir == 'neutral':
            # One neutral, one directional - medium disagreement
            return 0.3
        else:
            # Opposing directions - disagreement
            return max(0.0, 1.0 - (ensemble_conf + gamma_conf))
    
    def _fuse_predictions(self, 
                         ensemble_dir: Optional[str], 
                         ensemble_conf: float,
                         gamma_dir: Optional[str], 
                         gamma_conf: float,
                         agreement_score: float) -> Tuple[Optional[str], float]:
        """
        Fuse ensemble and gamma predictions based on agreement and confidence.
        
        Args:
            ensemble_dir: Ensemble direction
            ensemble_conf: Ensemble confidence
            gamma_dir: Gamma direction
            gamma_conf: Gamma confidence
            agreement_score: Agreement between predictions
            
        Returns:
            (direction, confidence) fused result
        """
        # If one is None, return the other
        if ensemble_dir is None:
            return gamma_dir, gamma_conf
        if gamma_dir is None:
            return ensemble_dir, ensemble_conf

        # WAVE-B/8 (2026-09-28): 'neutral' is the ABSENCE of a directional call,
        # not a competing direction. Previously a neutral gamma (or ensemble) could
        # win the low-agreement weighting and discard the other side's real call.
        # Defer to whichever side has an actual direction.
        if ensemble_dir == 'neutral' and gamma_dir != 'neutral':
            return gamma_dir, gamma_conf
        if gamma_dir == 'neutral' and ensemble_dir != 'neutral':
            return ensemble_dir, ensemble_conf

        # If both are neutral, stay neutral
        if ensemble_dir == 'neutral' and gamma_dir == 'neutral':
            return 'neutral', (ensemble_conf + gamma_conf) / 2
        
        # Calculate weighted result based on agreement
        if agreement_score >= self.agreement_threshold:
            # High agreement - boost confidence
            if ensemble_dir == gamma_dir:
                # Same direction, high agreement - increase confidence
                base_conf = (ensemble_conf * self.ensemble_weight + gamma_conf * self.gamma_weight)
                boosted_conf = min(1.0, base_conf * (1.0 + agreement_score * 0.3))
                return ensemble_dir, boosted_conf
            else:
                # Different directions but somehow high agreement score - unlikely but handle
                # Return direction with higher weighted confidence
                ensemble_weighted = ensemble_conf * self.ensemble_weight
                gamma_weighted = gamma_conf * self.gamma_weight
                
                if ensemble_weighted > gamma_weighted:
                    return ensemble_dir, ensemble_conf
                else:
                    return gamma_dir, gamma_conf
        else:
            # Low agreement - reduce confidence and consider which signal is stronger
            ensemble_weighted = ensemble_conf * self.ensemble_weight
            gamma_weighted = gamma_conf * self.gamma_weight
            
            if ensemble_weighted >= gamma_weighted:
                # Ensemble is stronger, but reduce confidence due to disagreement
                reduced_conf = ensemble_conf * agreement_score
                return ensemble_dir, reduced_conf
            else:
                # Gamma is stronger, but reduce confidence due to disagreement
                reduced_conf = gamma_conf * agreement_score
                return gamma_dir, reduced_conf
    
    def evaluate_single_signal(self, signal_name: str, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate a single gamma signal by name.
        
        Args:
            signal_name: Name of the gamma signal ('g1_uncertainty', 'g2_regime', etc.)
            idx: Index into days list
            days: List of daily data
            
        Returns:
            (direction, confidence) from the specified signal
        """
        if signal_name not in self.gamma_signal_map:
            raise ValueError(f"Unknown gamma signal: {signal_name}")
        
        signal = self.gamma_signal_map[signal_name]
        return signal.evaluate(idx, days)


class GammaEnhancedEnsembleSignal(EightyTwoMemberEnsembleSignal):
    """
    Extended 82-member ensemble that internally integrates Gamma signals.
    
    This class maintains the same interface as the original 82-member ensemble
    but enhances predictions with Gamma signal information.
    """
    
    def __init__(self, 
                 db_path=None, 
                 gefs_db=None, 
                 tigge_db=None,
                 weighting_mode='equal', 
                 threshold=None,
                 fusion_weight=0.2,  # How much to blend with gamma signals
                 **kwargs):
        super().__init__(db_path, gefs_db, tigge_db, weighting_mode, threshold, **kwargs)
        
        self.fusion_weight = fusion_weight
        self.gamma_fusion = GammaEnsembleFusion(
            db_path=db_path,
            ensemble_weight=1.0 - fusion_weight,
            gamma_weight=fusion_weight
        )
        # Store the original ensemble for reference
        self.ensemble_signal = EightyTwoMemberEnsembleSignal(db_path, gefs_db, tigge_db, weighting_mode, threshold, **kwargs)
    
    @property
    def name(self) -> str:
        return "gamma_enhanced_eighty_two_member"
    
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Enhanced evaluation that combines 82-member ensemble with Gamma signals.
        """
        fusion_result = self.gamma_fusion.fuse(idx, days)
        return fusion_result.direction, fusion_result.confidence


def create_gamma_enhanced_ensemble(db_path: str = None) -> GammaEnhancedEnsembleSignal:
    """
    Factory function to create a gamma-enhanced 82-member ensemble.
    
    Args:
        db_path: Path to the database
        
    Returns:
        GammaEnhancedEnsembleSignal instance
    """
    return GammaEnhancedEnsembleSignal(db_path=db_path)


if __name__ == "__main__":
    # Test the fusion module
    print("Testing Gamma-82 Ensemble Fusion Module")
    print("=" * 50)
    
    # Create fusion instance
    fusion = GammaEnsembleFusion()
    print(f"Fusion initialized with {len(fusion.gamma_signals)} gamma signals")
    
    # Show signal names
    for name, signal in fusion.gamma_signal_map.items():
        print(f"- {name}: {type(signal).__name__}")
    
    print("\nGamma-Enhanced Ensemble Signal factory test:")
    enhanced_ensemble = create_gamma_enhanced_ensemble()
    print(f"Enhanced ensemble name: {enhanced_ensemble.name}")
    print(f"Original ensemble name: {enhanced_ensemble.ensemble_signal.name}")
    
    print("\nModule ready for integration with 82-member ensemble!")