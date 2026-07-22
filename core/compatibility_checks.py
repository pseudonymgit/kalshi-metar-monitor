"""
Compatibility checks Module

Extracted from signal_fusion.py during Phase 20.1 monolith decomposition.
"""



import os
import logging
import numpy as np
import math
from collections import defaultdict
from scipy.special import expit, logit
from core.calibration_pipeline import CalibrationPipeline

_logger = logging.getLogger(__name__)


def demonstrate_fusion_stack():
    """
    Demonstrate the complete 4-layer fusion stack with toy data.
    """
    print("DEMONSTRATION: 4-Layer Fusion Stack")
    print("=" * 60)
    
    # Example from Expert 2 report
    signal_names = ['reversion', 'gaussian_v2', 'regime', 'pressure']  
    city_codes = ['KNYC', 'KLAX', 'KATL']
    
    # Create fusion engine
    fusion = SignalFusionEngine(signal_names, city_codes)
    
    # Simulated history to train calibration
    history = {
        ('reversion', 'KNYC'): [(0.75, True), (0.82, True), (0.68, False), (0.77, True)],
        ('reversion', 'KLAX'): [(0.65, False), (0.78, True), (0.85, True), (0.72, True)],
        ('gaussian_v2', 'KNYC'): [(0.70, True), (0.80, True), (0.60, False), (0.75, True)],
        ('gaussian_v2', 'KLAX'): [(0.68, True), (0.75, False), (0.82, True), (0.65, True)],
        # ... add more entries per city-signal combination...
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
    test_signals = [
        ('reversion', 'up', 0.75),
        ('gaussian_v2', 'up', 0.68),
        ('regime', 'down', 0.80)
    ]
    
    print(f"\nTest Fusion Input:")
    for signal, direction, raw_conf in test_signals:
        print(f"  {signal}: {direction} @ {raw_conf}")
        
    result = fusion.fuse_signals(test_signals, 'KNYC')
    print(f"\nFusion Output: {result}")
    print(f"Result: Direction={result[0]}, Probability={result[1]:.3f}, Confidence={result[2]:.3f}")
    
    print("\nFusion engine initialized and ready for integration with ensemble.")
    return True
