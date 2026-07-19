"""
Settlement Cascade Timing Predictor
===================================

Purpose: Predicts the behavior of unwind cascades in the final 2 hours before settlement,
looking for indicators such as volume acceleration, spread compression, and price flipping

Inputs:
- Kalshi calendar for settlement times
- Historical orderbook data near settlements
- Volume and spread patterns approaching deadlines

Outputs:
- Cascade probability score (0-1 scaling)
- Warning flags for early unwinds
- Optimal exit timing recommendations

Usage: python settlement_cascade_timing.py --station STATION_CODE \
       --settlement_date YYYY-MM-DD [--lookback_days N]
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Optional
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.joinpath("core")))
from station_registry import STATIONS
from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
from kalshi_calendar import KalshiCalendar, SettlementCalendar


class SettlementCascadePredictor:
    """
    Models the pre-settlement period to predict when unwinding will accelerate
    and cascade, affecting position sizing and exit strategies
    """
    
    def __init__(self, calendar_path: str = None):
        """
        Initialize with calendar information for determining settlement proximity
        
        Args:
            calendar_path: Path to settlement calendar if available
        """
        self.calendar = KalshiCalendar()
        self.cascade_indicators_log = []  # Historical cascade patterns detection
        self.market_state_history = []  # Track volume, spread, and pricing near settlement
        
    def analyze_pre_settlement_patterns(self, settlement_datetime: datetime, 
                                     orderbook_data: List[Dict[str, Any]],
                                     volume_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyze market behavior in the hours leading up to settlement
        
        Args:
            settlement_datetime: Time of settlement
            orderbook_data: Historical orderbook measurements
            volume_data: Historical volume measurements
            
        Returns:
            Dictionary with settlement cascade analysis
        """
        # Filter data within 4 hours before settlement window
        time_windows = [(60, 120), (30, 60), (15, 30), (0, 15)]  # In minutes before settlement
        cascade_metrics = {}
        
        for window_start, window_end in time_windows:
            window_start_dt = settlement_datetime - timedelta(minutes=window_end)
            window_end_dt = settlement_datetime - timedelta(minutes=window_start)
            
            # Filter data for this window
            window_ob_data = [ob_data for ob_data in orderbook_data 
                             if window_start_dt <= datetime.fromisoformat(ob_data['timestamp']) <= window_end_dt]
            window_vol_data = [vol_data for vol_data in volume_data 
                              if window_start_dt <= datetime.fromisoformat(vol_data['timestamp']) <= window_end_dt]
            
            # Calculate indicators for this window
            metrics = {
                'window_minutes_before_settlement': f"{window_start}-{window_end}",
                'volume_avg': np.mean([data['volume'] for data in window_vol_data]) if window_vol_data else 0,
                'volume_max': max([data['volume'] for data in window_vol_data]) if window_vol_data else 0,
                'spread_avg': np.mean([data['ask'] - data['bid'] for data in window_ob_data]) if window_ob_data else 0.03,
                'spread_min': min([data['ask'] - data['bid'] for data in window_ob_data]) if window_ob_data else 0.01,
                'price_volatility': np.std([data['midpoint_price'] for data in window_ob_data]) if window_ob_data else 0
            }
            
            # Check for pre-cascade indicators
            volume_acceleration = (metrics['volume_max'] > metrics['volume_avg'] * 2) if metrics['volume_avg'] > 0 else False
            spread_contraction = (metrics['spread_min'] < 0.005)  # Unusually tight spread
            price_flipping = metrics['price_volatility'] > 0.02  # Excessive price volatility
            
            cascade_metrics[f"window_{window_start}_{window_end}_min"] = {
                **metrics,
                'volume_acceleration_sig': volume_acceleration,
                'tight_spread_sig': spread_contraction,
                'price_flipping_sig': price_flipping,
                'cascade_indicator_score': (
                    int(volume_acceleration) * 2 +  # Higher weight for volume acceleration
                    int(spread_contraction) +
                    int(price_flipping) * 3  # Higher weight for price flipping
                ) / 6.0  # Normalize to 0-1 scale
            }
        
        # Overall cascade probability: weighted combination of indicators in final windows
        final_metrics = cascade_metrics.get("window_0_15_min", {}).get('cascade_indicator_score', 0)
        near_final_metrics = cascade_metrics.get("window_15_30_min", {}).get('cascade_indicator_score', 0)
        
        overall_cascade_probability = min(1.0, final_metrics * 1.5 + near_final_metrics * 0.5)
        
        # Detect early cascade warning signs
        early_cascade_warning = 0.0
        if cascade_metrics.get("window_30_60_min", {}).get('cascade_indicator_score', 0) > 0.5 or \
           cascade_metrics.get("window_60_120_min", {}).get('cascade_indicator_score', 0) > 0.7:
            early_cascade_warning = 1.0
        
        return {
            'settlement_datetime': settlement_datetime,
            'cascade_probability': overall_cascade_probability,
            'early_cascade_warning': early_cascade_warning,
            'final_window_metrics': cascade_metrics.get("window_0_15_min", {}),
            'pre_cascade_windows': {k: v for k, v in cascade_metrics.items() if 'window_' in k and k != 'window_0_15_min'},
            'recommended_timing': {
                'optimal_exit_minutes_before': 25 if early_cascade_warning > 0.5 else 10,
                'avoid_last_minutes': 5
            },
            'risk_level': self._categorize_risk_level(overall_cascade_probability)
        }
    
    def _categorize_risk_level(self, probability: float) -> str:
        """
        Convert probability to descriptive risk level
        
        Args:
            probability: Cascade probability
        
        Returns:
            Risk category as string
        """
        if probability > 0.7:
            return "HIGH_RISK"
        elif probability > 0.4:
            return "MODERATE_RISK"
        elif probability > 0.1:
            return "LOW_RISK"
        else:
            return "NEGLIGIBLE_RISK"
    
    def get_exit_recommendations(self, cascade_analysis: Dict[str, Any], 
                                current_position_minutes_to_settlement: int) -> Dict[str, Any]:
        """
        Provide exit timing recommendations based on cascade analysis
        
        Args:
            cascade_analysis: Result from analyze_pre_settlement_patterns
            current_position_minutes_to_settlement: Minutes remaining to settlement
            
        Returns:
            Exit timing recommendations
        """
        prob = cascade_analysis['cascade_probability']
        early_warning = cascade_analysis['early_cascade_warning']
        
        # Determine exit aggression based on risk
        if early_warning > 0.5:
            # Early warning of cascade - get out sooner
            suggested_exit_minutes = max(30, current_position_minutes_to_settlement * 0.8)
        elif prob > 0.7:
            # High probability cascade - urgent exit
            suggested_exit_minutes = min(8, current_position_minutes_to_settlement - 3)  # At least 3 min buffer
        elif prob > 0.4:
            # Moderate cascade risk - plan early exit
            suggested_exit_minutes = min(18, max(5, current_position_minutes_to_settlement * 0.6))
        else:
            # Low risk - normal settlement timeline
            suggested_exit_minutes = current_position_minutes_to_settlement * 0.2  # Exit earlier than last minute
        
        # Additional risk mitigation advice
        risk_mitigation = []
        if prob > 0.7:
            risk_mitigation.append("EXIT IMMEDIATELY - HIGH CASCADE RISK")
        elif early_warning > 0.5:
            risk_mitigation.append("MONITOR EXPOSURE - POTENTIAL EARLY CASCADE")
        elif prob > 0.4:
            risk_mitigation.append("PLAN PHASED EXIT - MONITOR FOR ACCELERATION")
        else:
            risk_mitigation.append("NORMAL SETTLEMENT APPROACH - CONTINUE POSITION AS PLANNED")
        
        return {
            'suggested_exit_minutes_before': suggested_exit_minutes,
            'exit_timing_rationale': f"Cascade probability indicates {cascade_analysis['risk_level']} conditions",
            'risk_mitigation_advice': risk_mitigation,
            'monitoring_required': prob > 0.3 or early_warning > 0.0
        }

    def simulate_cascade_prediction(self, station: str, settlement_date_str: str) -> Dict[str, Any]:
        """
        Simulate cascade prediction using mock data
        
        Args:
            station: Station ID
            settlement_date_str: Date in YYYY-MM-DD format
            
        Returns:
            Mock cascade prediction results
        """
        settlement_date = datetime.strptime(settlement_date_str, "%Y-%m-%d")
        # Assume settlement at end of day EST (UTC might be different date)
        settlement_datetime = datetime.combine(settlement_date, datetime.max.time().replace(hour=23, minute=0, second=0))
        
        # Generate mock orderbook and volume data approaching settlement
        time_points = []
        ob_data = []
        vol_data = []
        
        # 4 hours before settlement, generating samples every 10 minutes
        current_time = settlement_datetime - timedelta(hours=4)
        
        while current_time < settlement_datetime:
            mins_to_settle = (settlement_datetime - current_time).total_seconds() / 60 
            
            # Create realistic-looking trends
            volume_level = 50  # Base volume
            if mins_to_settle < 60:
                # Increase volume approaching deadline
                volume_level *= (1.0 + (60 - mins_to_settle) / 60.0)
            elif mins_to_settle < 180 and mins_to_settle > 150:
                # Surge in middle of window (typical)
                volume_level *= 1.8
            
            # Randomize volume but with pattern
            actual_volume = np.random.normal(volume_level, volume_level * 0.3)
            actual_volume = max(10, actual_volume)  # Minimum volume
            
            # Spreads tighten as settlement approaches, with occasional spikes
            base_spread = 0.035  # Base spread
            if mins_to_settle < 45:
                base_spread *= (mins_to_settle / 60.0)
            
            # Randomize spread with spikes
            spread = base_spread * np.random.uniform(0.7, 1.5)
            spread = min(0.10, max(0.01, spread))  # Reasonable limits
            
            # Midpoint price movement simulation
            midpoint = 0.50 + np.random.normal(0, 0.02)  # Base midpoint with noise
            
            # Add pattern closer to settlement
            if mins_to_settle < 30:
                # Increase volatility as prices converge
                midpoint += np.random.normal(0, 0.05)
            
            ob_data.append({
                'timestamp': current_time.isoformat(),
                'bid': midpoint - spread/2,
                'ask': midpoint + spread/2,
                'midpoint_price': midpoint
            })
            
            vol_data.append({
                'timestamp': current_time.isoformat(),
                'volume': actual_volume
            })
            
            time_points.append(current_time)
            current_time += timedelta(minutes=5)
        
        analysis = self.analyze_pre_settlement_patterns(settlement_datetime, ob_data, vol_data)
        return analysis


def main():
    parser = argparse.ArgumentParser(description='Predict settlement cascade timing')
    parser.add_argument('--station', type=str, required=True, help='Station code (e.g., KJFK)')
    parser.add_argument('--settlement_date', type=str, required=True, help='Settlement date YYYY-MM-DD')
    
    args = parser.parse_args()
    
    predictor = SettlementCascadePredictor()
    
    # Use the simulate_cascade_prediction method to run the analysis
    result = predictor.simulate_cascade_prediction(args.station, args.settlement_date)
    
    print(f"Settlement Cascade Analysis for {args.station}")
    print(f"  Settlement Time: {result['settlement_datetime']}")
    print(f"  Cascade Probability: {result['cascade_probability']:.2f}")
    print(f"  Early Cascade Warning: {result['early_cascade_warning']:.2f}")
    print(f"  Risk Level: {result['risk_level']}")
    
    current_minutes_to_settlement = 45  # Default assumption for demo
    exit_rec = predictor.get_exit_recommendations(result, current_minutes_to_settlement)
    
    print(f"\nExit Recommendations:")
    print(f"  Suggested Exit: {exit_rec['suggested_exit_minutes_before']:.1f} minutes before settlement")
    print(f"  Rationale: {exit_rec['exit_timing_rationale']}")
    print(f"  Monitoring Required: {exit_rec['monitoring_required']}")
    
    print(f"\nRisk Mitigation Advice: {exit_rec['risk_mitigation_advice'][0]}")


if __name__ == "__main__":
    main()