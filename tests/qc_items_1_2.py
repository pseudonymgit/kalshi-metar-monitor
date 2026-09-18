#!/usr/bin/env python3
"""
QC Test for Items 1-2: Regime-Switching HMM and Walk-Forward Optimization
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime

# Add prototypes/weather-engine-source to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.regime_switching_hmm import RegimeSwitchingHMM, load_metar_data_for_hmm
from core.walk_forward_optimizer import WalkForwardOptimizer
from core.two_bucket_sizing import TwoBucketSizing, analyze_sweep_for_sizing


def test_regime_hmm():
    """Test Regime-Switching HMM functionality."""
    print("\n=== Testing Regime-Switching HMM ===")
    
    # Create synthetic METAR data for testing
    n_samples = 1000
    dates = pd.date_range('2026-01-01', periods=n_samples, freq='H')
    
    synthetic_metar = pd.DataFrame({
        'temperature': np.random.normal(70, 10, n_samples),
        'pressure': np.random.normal(1013, 5, n_samples),
        'humidity': np.random.uniform(30, 90, n_samples),
        'dewpoint': np.random.normal(60, 8, n_samples),
        'wind_speed': np.random.uniform(0, 20, n_samples),
        'wind_direction': np.random.uniform(0, 360, n_samples),
        'cloud_cover': np.random.uniform(0, 100, n_samples)
    }, index=dates)
    
    # Initialize and train HMM
    hmm = RegimeSwitchingHMM(n_states=4)
    hmm.train(synthetic_metar, walk_forward=False)
    
    # Extract features and predict regimes
    features = hmm.extract_features(synthetic_metar)
    regime_probs = hmm.predict_regime_probabilities(features)
    
    # Test confidence multiplier
    for i in range(min(5, len(regime_probs))):
        conf_mult = hmm.get_confidence_multiplier(regime_probs[i])
        print(f"Sample {i}: max prob={np.max(regime_probs[i]):.3f}, conf multiplier={conf_mult:.3f}")
    
    # Describe regimes
    regime_desc = hmm.describe_regimes(synthetic_metar)
    print(f"\nRegime descriptions:")
    for regime_id, desc in regime_desc.items():
        print(f"  Regime {regime_id}: {len(desc)} features")
    
    print("✓ HMM functionality test passed")
    return True


def test_walk_forward_optimizer():
    """Test Walk-Forward Optimizer functionality."""
    print("\n=== Testing Walk-Forward Optimizer ===")
    
    # Create synthetic sweep data
    n_configs = 200
    dates = pd.date_range('2026-01-01', periods=n_configs, freq='D')
    
    synthetic_sweep = pd.DataFrame({
        'timestamp': dates,
        'confidence_floor': np.random.uniform(0.5, 0.95, n_configs),
        'accuracy': np.random.uniform(0.45, 0.75, n_configs),
        'sharpe': np.random.uniform(-1, 3, n_configs),
        'total_pnl': np.random.uniform(-1000, 2000, n_configs),
        'n_trades': np.random.randint(50, 500, n_configs),
        'scalarized': np.random.uniform(0, 1, n_configs)
    })
    synthetic_sweep.set_index('timestamp', inplace=True)
    
    # Initialize optimizer
    optimizer = WalkForwardOptimizer(n_splits=3, test_size=20)
    
    # Add regime labels
    sweep_with_regimes = optimizer.add_regime_labels(synthetic_sweep)
    
    # Optimize per regime
    optimal_thresholds = optimizer.optimize_per_regime(sweep_with_regimes, 'scalarized')
    
    print(f"\nOptimal thresholds by regime:")
    for regime, threshold in optimal_thresholds.items():
        print(f"  {regime}: {threshold:.3f}")
    
    # Test regime threshold lookup
    test_timestamp = pd.Timestamp('2026-07-15 14:00:00')
    test_threshold = optimizer.get_regime_threshold(test_timestamp, 14)
    print(f"\nTest threshold for {test_timestamp} at 14:00: {test_threshold:.3f}")
    
    # Analyze regime differences
    regime_stats = optimizer.regime_performance
    print(f"\nRegime performance analyzed for {len(regime_stats)} regimes")
    
    print("✓ Walk-forward optimizer test passed")
    return True


def test_two_bucket_sizing():
    """Test Two-Bucket Sizing functionality."""
    print("\n=== Testing Two-Bucket Sizing ===")
    
    # Initialize sizing strategy
    sizing = TwoBucketSizing(
        high_confidence_threshold=0.8,
        kelly_fraction=0.25,
        fixed_size=25,
        capital_base=25000
    )
    
    # Test cases
    test_cases = [
        (0.85, 0.15, 0.45, 'DJF'),  # High confidence, winter
        (0.75, 0.10, 0.55, 'JJA'),  # Low confidence, summer
        (0.90, 0.20, 0.40, None),   # Very high confidence
        (0.60, 0.05, 0.60, None),   # Low confidence
    ]
    
    print("\nPosition sizing test cases:")
    for confidence, edge, price, regime in test_cases:
        contracts = sizing.calculate_position_size(confidence, edge, price, regime)
        bucket = "High" if confidence >= 0.8 else "Low"
        print(f"  {bucket} confidence ({confidence:.2f}), edge={edge:.2f}, "
              f"price={price:.2f}, regime={regime}: {contracts} contracts")
    
    # Test sizing summary
    summary = sizing.get_sizing_summary()
    print(f"\nSizing summary: {summary}")
    
    # Create synthetic sweep analysis
    synthetic_sweep = pd.DataFrame({
        'confidence_floor': np.random.uniform(0.5, 0.95, 100),
        'accuracy': np.random.uniform(0.45, 0.75, 100),
        'sharpe': np.random.uniform(-1, 3, 100),
        'total_pnl': np.random.uniform(-1000, 2000, 100),
        'n_trades': np.random.randint(50, 500, 100)
    })
    
    analysis = analyze_sweep_for_sizing(synthetic_sweep, confidence_threshold=0.8)
    print(f"\nSweep analysis results:")
    print(f"  High-confidence configs: {analysis['high_confidence_performance']['n_configs']}")
    print(f"  Low-confidence configs: {analysis['low_confidence_performance']['n_configs']}")
    print(f"  Recommendations: {analysis['sizing_recommendations']}")
    
    print("✓ Two-bucket sizing test passed")
    return True


def main():
    """Run all QC tests."""
    print("=" * 60)
    print("QC Test for Items 1-2: HMM and Walk-Forward Optimization")
    print("=" * 60)
    
    test_results = []
    
    try:
        test_results.append(("Regime HMM", test_regime_hmm()))
    except Exception as e:
        print(f"✗ HMM test failed: {e}")
        test_results.append(("Regime HMM", False))
    
    try:
        test_results.append(("Walk-Forward Optimizer", test_walk_forward_optimizer()))
    except Exception as e:
        print(f"✗ Walk-forward optimizer test failed: {e}")
        test_results.append(("Walk-Forward Optimizer", False))
    
    try:
        test_results.append(("Two-Bucket Sizing", test_two_bucket_sizing()))
    except Exception as e:
        print(f"✗ Two-bucket sizing test failed: {e}")
        test_results.append(("Two-Bucket Sizing", False))
    
    # Summary
    print("\n" + "=" * 60)
    print("QC TEST SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for test_name, passed in test_results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"{test_name:30} {status}")
        if not passed:
            all_passed = False
    
    if all_passed:
        print("\nAll tests passed! Items 1-2 are ready for integration.")
    else:
        print("\nSome tests failed. Review and fix before proceeding.")
    
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)