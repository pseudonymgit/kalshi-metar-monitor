#!/usr/bin/env python3
"""
Test script to verify Gamma signal integration with 82-member ensemble.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals.gamma_signals import get_gamma_signals, G1_UncertaintySignal, G2_RegimeSignal, G3_AnalogSignal, G4_PressureAdvectionSignal, G5_DewpointSignal, G6_CloudCoverSignal
from core.signals.eighty_two_member_ensemble_signal import EightyTwoMemberEnsembleSignal
from core.gamma_ensemble_fusion import GammaEnsembleFusion, GammaEnhancedEnsembleSignal


def test_gamma_signals():
    """Test individual Gamma signals."""
    print("Testing individual Gamma signals...")
    
    # Create mock days data for testing
    mock_days = [
        {'station': 'KJFK', 'date': '2023-06-15', 'high': 75.0, 'temp': 72.0, 'dewpoint': 60.0, 'pressure': 1013.0},
        {'station': 'KJFK', 'date': '2023-06-16', 'high': 78.0, 'temp': 74.0, 'dewpoint': 62.0, 'pressure': 1012.5},
        {'station': 'KJFK', 'date': '2023-06-17', 'high': 80.0, 'temp': 76.0, 'dewpoint': 61.0, 'pressure': 1012.0},
        {'station': 'KJFK', 'date': '2023-06-18', 'high': 82.0, 'temp': 78.0, 'dewpoint': 60.0, 'pressure': 1011.5},
        {'station': 'KJFK', 'date': '2023-06-19', 'high': 85.0, 'temp': 80.0, 'dewpoint': 59.0, 'pressure': 1011.0},
    ]
    
    gamma_signals = get_gamma_signals()
    
    for i, signal in enumerate(gamma_signals, 1):
        try:
            direction, confidence = signal.evaluate(4, mock_days)  # Test on last day
            print(f"  G{i} ({signal.name}): direction={direction}, confidence={confidence:.3f}")
        except Exception as e:
            print(f"  G{i} ({signal.name}): ERROR - {e}")
    
    print("Individual Gamma signal tests completed.\n")


def test_82_member_ensemble():
    """Test the 82-member ensemble signal."""
    print("Testing 82-member ensemble signal...")
    
    # Create mock days data
    mock_days = [
        {'station': 'KJFK', 'date': '2023-06-15', 'high': 75.0},
        {'station': 'KJFK', 'date': '2023-06-16', 'high': 78.0},
        {'station': 'KJFK', 'date': '2023-06-17', 'high': 80.0},
        {'station': 'KJFK', 'date': '2023-06-18', 'high': 82.0},
        {'station': 'KJFK', 'date': '2023-06-19', 'high': 85.0, 'station': 'KJFK'},
    ]
    
    try:
        ensemble_signal = EightyTwoMemberEnsembleSignal()
        direction, confidence = ensemble_signal.evaluate(4, mock_days)
        print(f"  82-member ensemble: direction={direction}, confidence={confidence:.3f}")
    except Exception as e:
        print(f"  82-member ensemble: ERROR - {e}")
    
    print("82-member ensemble test completed.\n")


def test_gamma_fusion():
    """Test the Gamma-Ensemble fusion."""
    print("Testing Gamma-Ensemble fusion...")
    
    # Create mock days data
    mock_days = [
        {'station': 'KJFK', 'date': '2023-06-15', 'high': 75.0, 'temp': 72.0, 'dewpoint': 60.0, 'pressure': 1013.0, 'cloud_cover': 30.0},
        {'station': 'KJFK', 'date': '2023-06-16', 'high': 78.0, 'temp': 74.0, 'dewpoint': 62.0, 'pressure': 1012.5, 'cloud_cover': 40.0},
        {'station': 'KJFK', 'date': '2023-06-17', 'high': 80.0, 'temp': 76.0, 'dewpoint': 61.0, 'pressure': 1012.0, 'cloud_cover': 50.0},
        {'station': 'KJFK', 'date': '2023-06-18', 'high': 82.0, 'temp': 78.0, 'dewpoint': 60.0, 'pressure': 1011.5, 'cloud_cover': 60.0},
        {'station': 'KJFK', 'date': '2023-06-19', 'high': 85.0, 'temp': 80.0, 'dewpoint': 59.0, 'pressure': 1011.0, 'cloud_cover': 70.0},
    ]
    
    try:
        fusion = GammaEnsembleFusion()
        result = fusion.fuse(4, mock_days)
        
        print(f"  Fusion result: direction={result.direction}, confidence={result.confidence:.3f}")
        print(f"  Ensemble: direction={result.ensemble_direction}, confidence={result.ensemble_confidence:.3f}")
        print(f"  Gamma: direction={result.gamma_direction}, confidence={result.gamma_confidence:.3f}")
        print(f"  Agreement: {result.agreement_score:.3f}")
        print(f"  Adjusted confidence: {result.adjusted_confidence:.3f}")
        
        # Print individual gamma signal results
        for name, (dir_, conf_) in result.gamma_signals.items():
            print(f"    {name}: direction={dir_}, confidence={conf_:.3f}")
            
    except Exception as e:
        print(f"  Gamma-Ensemble fusion: ERROR - {e}")
        import traceback
        traceback.print_exc()
    
    print("Gamma-Ensemble fusion test completed.\n")


def test_gamma_enhanced_ensemble():
    """Test the enhanced ensemble that integrates gamma signals."""
    print("Testing Gamma-Enhanced 82-member ensemble...")
    
    # Create mock days data
    mock_days = [
        {'station': 'KJFK', 'date': '2023-06-15', 'high': 75.0, 'temp': 72.0, 'dewpoint': 60.0, 'pressure': 1013.0, 'cloud_cover': 30.0},
        {'station': 'KJFK', 'date': '2023-06-16', 'high': 78.0, 'temp': 74.0, 'dewpoint': 62.0, 'pressure': 1012.5, 'cloud_cover': 40.0},
        {'station': 'KJFK', 'date': '2023-06-17', 'high': 80.0, 'temp': 76.0, 'dewpoint': 61.0, 'pressure': 1012.0, 'cloud_cover': 50.0},
        {'station': 'KJFK', 'date': '2023-06-18', 'high': 82.0, 'temp': 78.0, 'dewpoint': 60.0, 'pressure': 1011.5, 'cloud_cover': 60.0},
        {'station': 'KJFK', 'date': '2023-06-19', 'high': 85.0, 'temp': 80.0, 'dewpoint': 59.0, 'pressure': 1011.0, 'cloud_cover': 70.0, 'station': 'KJFK'},
    ]
    
    try:
        enhanced_ensemble = GammaEnhancedEnsembleSignal()
        direction, confidence = enhanced_ensemble.evaluate(4, mock_days)
        print(f"  Gamma-Enhanced ensemble: direction={direction}, confidence={confidence:.3f}")
    except Exception as e:
        print(f"  Gamma-Enhanced ensemble: ERROR - {e}")
        import traceback
        traceback.print_exc()
    
    print("Gamma-Enhanced ensemble test completed.\n")


def main():
    print("Running Gamma Signal Integration Tests")
    print("=" * 50)
    
    test_gamma_signals()
    test_82_member_ensemble()
    test_gamma_fusion()
    test_gamma_enhanced_ensemble()
    
    print("All tests completed!")


if __name__ == "__main__":
    main()