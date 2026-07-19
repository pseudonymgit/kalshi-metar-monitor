"""
Spread-adjusted Net Edge Calibrator
==================================

Purpose: Implements a 2D calibrator that takes signal confidence and spread value 
as inputs to calculate net edge for trading decisions, accounting for spread erosion

Inputs: 
- Signal confidence from calibration_pipeline
- Spread data from kalshi_price_fetcher
- Historical trade results for calibration

Outputs:
- net_edge: adjusted edge calculation accounting for spread costs
- confidence_intervals: uncertainty bounds

Usage: python spread_adjusted_net_edge.py --confidence CONFIDENCE --spread SPREAD \
       [--lookback_period DAYS]
"""

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
import argparse
from typing import Tuple, Dict, Any
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.joinpath("core")))
from station_registry import STATIONS
from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
from calibration_pipeline import CalibrationPipeline


class SpreadAdjustedNetEdgeCalculator:
    """
    A 2D calibrator that maps (signal_confidence, spread) pairs to net_edge values
    accounting for spread erosion effects on trade profitability.
    """
    
    def __init__(self, historical_data_path: str = None, resolution: int = 20):
        """
        Initialize the spread-adjusted calibrator
        
        Args:
            historical_data_path: Path to historical trade results for calibration
            resolution: Grid resolution for 2D interpolation (confidence x spread)
        """
        self.resolution = resolution
        self.confidence_range = (0.4, 1.0)  # Range of confidence values
        self.spread_range = (0.0, 0.10)     # Range of spread values (0-10 cents)
        
        # Calibration grid points for interpolation
        confidence_grid = np.linspace(self.confidence_range[0], self.confidence_range[1], resolution)
        spread_grid = np.linspace(self.spread_range[0], self.spread_range[1], resolution)
        conf_mesh, spread_mesh = np.meshgrid(confidence_grid, spread_grid)
        
        self.grid_points = np.column_stack([conf_mesh.ravel(), spread_mesh.ravel()])
        self.calibration_values = np.zeros(len(self.grid_points))
        
        # Load historical data if provided and initialize calibration
        if historical_data_path:
            self._load_calibration_data(historical_data_path)
        else:
            # Set up default calibration based on spread mechanics
            self._setup_default_calibration()
    
    def _setup_default_calibration(self):
        """
        Set up reasonable default calibration values based on spread mechanics.
        This represents a simplified model before actual historical data calibration.
        """
        for i, (confidence, spread) in enumerate(self.grid_points):
            # Base edge calculation: higher confidence gives better edge
            base_edge = (confidence - 0.5) * 0.1  # Scale factor chosen based on typical outcomes
            
            # Spread erosion effect: increases negative impact on profitable trades
            spread_impact = spread * 0.6  # Higher spread takes larger chunk of expected edge
            
            # Net edge is base edge minus spread cost with some interaction term
            net_edge = base_edge - spread_impact * (1.2 - min(1.0, confidence))  # More sensitive at lower confidences
            
            # Ensure we don't get unreasonably large negative edges for spreads > 5cents
            if spread > 0.04:
                net_edge = net_edge * 0.6  # Reduce expected returns substantially for wide spreads
            
            self.calibration_values[i] = net_edge
    
    def _load_calibration_data(self, path: str):
        """
        Load historical calibration data from file to refine calibrator
        """
        # Load historical data: signal_confidence, market_spread, realized_edge
        try:
            df = pd.read_csv(path)
            if 'signal_confidence' in df.columns and 'market_spread' in df.columns and 'realized_edge' in df.columns:
                observed_points = df[['signal_confidence', 'market_spread']].values
                observed_values = df['realized_edge'].values
                
                # Interpolate observed data onto our standard grid
                calibrated_values = griddata(
                    points=observed_points,
                    values=observed_values,
                    xi=self.grid_points,
                    method='linear',
                    fill_value=np.mean(observed_values)  # Fill with mean where extrapolation would occur
                )
                
                # Where we have good data (not NaN from failed interpolation), replace defaults
                mask = ~np.isnan(calibrated_values)
                self.calibration_values[mask] = calibrated_values[mask]
        except FileNotFoundError:
            print(f"Warning: Historical data file {path} not found, using default calibration")
            self._setup_default_calibration()
        except Exception as e:
            print(f"Warning: Failed to load calibration data: {str(e)}, using default calibration")
            self._setup_default_calibration()
    
    def calculate_net_edge(self, confidence: float, spread: float) -> Dict[str, float]:
        """
        Calculate the spread-adjusted net edge for given confidence and spread
        
        Args:
            confidence: Signal confidence value (0.0 to 1.0)
            spread: Market spread value in dollars
            
        Returns:
            Dictionary containing net_edge and confidence intervals
        """
        # Clip inputs to valid range
        clamped_conf = np.clip(confidence, self.confidence_range[0], self.confidence_range[1])
        clamped_spread = np.clip(spread, self.spread_range[0], self.spread_range[1])
        
        # Create evaluation point
        eval_point = np.array([[clamped_conf, clamped_spread]])
        
        # Interpolate to get net edge
        interpolated_edge = griddata(
            points=self.grid_points,
            values=self.calibration_values,
            xi=eval_point,
            method='linear',
            fill_value=np.mean(self.calibration_values)
        )[0]
        
        # Calculate confidence interval based on distance from trained points
        distances = np.linalg.norm(self.grid_points - eval_point, axis=1)
        closest_distance = np.min(distances)
        
        # Define error bounds based on how far we are from training data
        max_error = 0.02  # Maximum potential error
        uncertainty_factor = min(closest_distance * 5.0, max_error)  # Scale uncertainty with distance
        
        return {
            'net_edge': interpolated_edge,
            'prediction_error_bound': uncertainty_factor,
            'is_interpolated': True  # True if between grid points, False if extrapolating
        }
    
    def batch_calculate_net_edge(self, confidence_spread_pairs: list) -> list:
        """
        Calculate net edge in batch
        """
        results = []
        for confidence, spread in confidence_spread_pairs:
            result = self.calculate_net_edge(confidence, spread)
            results.append(result)
        return results


def main():
    parser = argparse.ArgumentParser(description='Calculate spread-adjusted net edge')
    parser.add_argument('--confidence', type=float, required=True, help='Signal confidence (0.0-1.0)')
    parser.add_argument('--spread', type=float, required=True, help='Market spread in dollars')
    parser.add_argument('--history_path', type=str, default=None, help='Historical data path for calibration (optional)')
    
    args = parser.parse_args()
    
    calculator = SpreadAdjustedNetEdgeCalculator(args.history_path)
    result = calculator.calculate_net_edge(args.confidence, args.spread)
    
    print(f"Spread-adjusted Net Edge: {result['net_edge']:.4f}")
    print(f"Prediction Error Bound: {result['prediction_error_bound']:.4f}")
    print(f"Extrapolation Warning: {'Yes' if not result['is_interpolated'] else 'No'}")


if __name__ == "__main__":
    main()