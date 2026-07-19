"""
Liquidity-weighted Ensemble Signal Voter
=======================================

Purpose: Implements a weighted ensemble of trading signals where each signal's 
vote is weighted by its historical accuracy multiplied by current liquidity 
(volume or open interest)

Inputs:
- Multiple signal sources with confidence scores
- Liquidity data from Kalshi orderbook
- Historical accuracy of each signal source

Outputs:
- Ensembled prediction with weightings applied
- Confidence score for combined prediction
- Per-signal contribution breakdown

Usage: python liquidity_weighted_ensemble.py --signal_data SIGNAL_DATA_PATH \
       --liquidity_data LIQUIDITY_DATA_PATH --station "STATION_CODE"
"""

import numpy as np
import pandas as pd
import argparse
from typing import Dict, List, Tuple, Any
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.joinpath("core")))
from station_registry import STATIONS
from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
from kalshi_monitor import KalshiMonitor


class LiquidityWeightedEnsemble:
    """
    A weighted ensemble system that combines multiple trading signals 
    with weights based on historical accuracy * current liquidity
    """
    
    def __init__(self):
        """
        Initialize weight history tracking
        """
        # Dictionary to track historical accuracy for each signal
        self.signal_accuracies = {}
        # Dictionary to track recent liquidity values for each market
        self.current_liquidity = {}
        
    def register_signal_source(self, signal_name: str, accuracy: float = 0.51):
        """
        Register a new signal source with assumed or baseline accuracy
        
        Args:
            signal_name: Name identifying the signal
            accuracy: Baseline accuracy (0.0 to 1.0, defaults to 51% for weak edge)
        """
        self.signal_accuracies[signal_name] = float(accuracy)
    
    def update_signal_accuracy(self, signal_name: str, accuracy: float):
        """
        Update the historical accuracy for a given signal
        
        Args:
            signal_name: Name of the signal
            accuracy: New accuracy value (0.0 to 1.0)
        """
        self.signal_accuracies[signal_name] = float(accuracy)
    
    def update_liquidity(self, market_id: str, liquidity: float):
        """
        Update the current liquidity for a market
        
        Args:
            market_id: Identifier for the market/contract
            liquidity: Current liquidity metric (volume, open_interest, etc.)
        """
        self.current_liquidity[market_id] = float(liquidity)
    
    def calculate_weighted_vote(self, signals: List[Dict[str, Any]], market_id: str) -> Dict[str, Any]:
        """
        Calculate weighted ensemble result based on accuracy * liquidity
        
        Args:
            signals: List of signal dictionaries with 'name', 'prediction', 'confidence'
            market_id: Identifier for the market whose liquidity affects weights
        
        Returns:
            Dictionary containing ensemble results
        """
        # Get the current liquidity for this market
        current_liquidity = self.current_liquidity.get(market_id, 1.0)  # Default weight if no liquidity data
        
        total_weight = 0.0
        total_weighted_prediction = 0.0
        signal_contributions = []
        
        # Calculate weighted votes
        for signal in signals:
            name = signal['name']
            prediction = signal['prediction']  # Expected to be -1 (short) to 1 (long)
            confidence = signal['confidence']  # 0.0 to 1.0
            
            # Get the signal's historical accuracy
            accuracy = self.signal_accuracies.get(name, 0.51)  # Default to 51% if unknown
            
            # Calculate the weight: accuracy * liquidity * confidence
            weight = (accuracy - 0.5) * 2  # Convert accuracy to effectiveness (how much it deviates from 50%)
            if weight <= 0:
                weight = 0.01  # Ensure small positive weight even for sub-50% accuracy signals
            weight *= current_liquidity
            weight *= confidence  # Use signal's confidence as additional weight factor
            
            # Apply the weighted prediction
            weighted_prediction = prediction * weight
            total_weight += weight
            total_weighted_prediction += weighted_prediction
            
            # Track individual contributions
            signal_contributions.append({
                'name': name,
                'prediction': prediction,
                'confidence': confidence,
                'accuracy': accuracy,
                'liquidity': current_liquidity,
                'weight': weight,
                'weighted_contribution': weighted_prediction
            })
        
        # Calculate final ensemble prediction if we have weights
        if total_weight == 0:
            ensemble_prediction = 0.0
            overall_confidence = 0.0
        else:
            ensemble_prediction = total_weighted_prediction / total_weight
            overall_confidence = total_weight / len(signals) if signals else 0.0
        
        # Clamp prediction to reasonable scale (-1 to 1)
        ensemble_prediction = max(-1.0, min(1.0, ensemble_prediction))
        
        return {
            'ensemble_prediction': ensemble_prediction,
            'overall_confidence': overall_confidence,
            'total_weight': total_weight,
            'signal_contributions': signal_contributions,
            'market_liquidity': current_liquidity,
            'timestamp': pd.Timestamp.now().isoformat()
        }
    
    def batch_process_ensemble(self, batches: List[Tuple[List[Dict], str]]) -> List[Dict[str, Any]]:
        """
        Process multiple batches of signals
        
        Args:
            batches: List of tuples containing ([signals], market_id)
            
        Returns:
            List of ensemble results for each batch
        """
        results = []
        for signals, market_id in batches:
            result = self.calculate_weighted_vote(signals, market_id)
            results.append(result)
        return results


def main():
    parser = argparse.ArgumentParser(description='Calculate liquidity-weighted ensemble prediction')
    parser.add_argument('--station_id', type=str, required=True, help='Kalshi market station ID')
    parser.add_argument('--mock_signals', action='store_true', help='Use mock signal data for demo')
    
    args = parser.parse_args()
    
    # Initialize ensemble calculator
    ensemble = LiquidityWeightedEnsemble()
    
    # Register signal sources with mock accuracies
    ensemble.register_signal_source("metar_pattern_analyzer", 0.54)
    ensemble.register_signal_source("nws_forecast_comparison", 0.52)
    ensemble.register_signal_source("historical_reliability_tracker", 0.56)
    ensemble.register_signal_source("round_number_anchoring_detector", 0.51)
    ensemble.register_signal_source("trend_continuation_signal", 0.53)
    
    # Mock signal data for demonstration
    mock_signals = [
        {
            'name': 'metar_pattern_analyzer',
            'prediction': 0.7,
            'confidence': 0.8
        },
        {
            'name': 'nws_forecast_comparison',
            'prediction': -0.5,
            'confidence': 0.6
        },
        {
            'name': 'historical_reliability_tracker',
            'prediction': 0.6,
            'confidence': 0.75
        },
        {
            'name': 'round_number_anchoring_detector',
            'prediction': 0.3,
            'confidence': 0.4
        },
        {
            'name': 'trend_continuation_signal',
            'prediction': -0.2,
            'confidence': 0.5
        }
    ]
    
    # Mock liquidity data
    liquidity_value = 25000.0  # Example liquidity value for the market
    
    # Set up the market ID and update liquidity
    market_id = f"Weather_{args.station_id}_Market"
    ensemble.update_liquidity(market_id, liquidity_value)
    
    # Calculate weighted ensemble
    result = ensemble.calculate_weighted_vote(mock_signals, market_id)
    
    print(f"Liquidity-Weighted Ensemble Analysis for: {market_id}")
    print(f"Market Liquidity: {liquidity_value:,}")
    print(f"Ensemble Prediction: {result['ensemble_prediction']:.3f}")
    print(f"Overall Confidence: {result['overall_confidence']:.3f}")
    print("\nSignal Contributions:")
    print("-" * 80)
    for contrib in result['signal_contributions']:
        print(f"{contrib['name']:.<25} Pred:{contrib['prediction']:>6.2f} "
              f"Wgt:{contrib['weight']:>6.2f}  Acc:{contrib['accuracy']:>5.2f}")


if __name__ == "__main__":
    main()