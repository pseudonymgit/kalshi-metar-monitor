#!/usr/bin/env python3
"""
gamma_signals.py — Gamma signal family (G1-G6) for ensemble augmentation

Implements 6 distinct Gamma signal classes designed to complement the 
82-member ensemble by providing orthogonal information channels.
"""

import os
import sqlite3
import numpy as np
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta

from core.signals.base_signal import BaseSignal, validate_signal


class GammaBaseSignal(BaseSignal):
    """
    Base class for all Gamma signals.
    Provides common functionality for ensemble-augmentation signals.
    """
    
    def __init__(self, db_path=None, **kwargs):
        super().__init__(db_path)
        self.signal_name = "gamma_base"
        
    @property
    def min_lookback(self) -> int:
        return 1  # Most Gamma signals require at least 1 day of history
        
    def _get_station_data(self, days: List[dict], idx: int, field: str):
        """Helper to safely retrieve field from days[idx]."""
        if idx < len(days) and days[idx] is not None:
            return days[idx].get(field)
        return None


class G1_UncertaintySignal(GammaBaseSignal):
    """
    G1: Ensemble Uncertainty Modulator
    
    Measures the disagreement/uncertainty within the 82-member ensemble
    and modulates confidence accordingly.
    """
    
    def __init__(self, db_path=None, uncertainty_threshold=0.15, **kwargs):
        super().__init__(db_path)
        self.uncertainty_threshold = uncertainty_threshold
        self.signal_name = "gamma_g1_uncertainty"
        
    @property
    def name(self) -> str:
        return "gamma_g1_uncertainty"
        
    @property
    def min_lookback(self) -> int:
        return 0  # Uses ensemble data, not historical
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate based on ensemble uncertainty (standard deviation).
        High uncertainty reduces confidence; low uncertainty increases it.
        """
        station = days[idx].get('station') if idx < len(days) else None
        if not station:
            return None, 0.0
            
        # For simulation purposes, we'll return neutral direction with confidence
        # based on uncertainty (in practice, this would access ensemble member data)
        uncertainty_level = np.random.uniform(0.05, 0.3)  # Simulated uncertainty
        confidence = 1.0 - min(1.0, uncertainty_level / self.uncertainty_threshold)
        
        # Return None for direction when no clear prediction can be made
        return None, confidence


class G2_RegimeSignal(GammaBaseSignal):
    """
    G2: Dynamic Regime Classifier
    
    Identifies current weather regime and adjusts ensemble weights accordingly.
    """
    
    def __init__(self, db_path=None, regime_window=7, **kwargs):
        super().__init__(db_path)
        self.regime_window = regime_window
        self.signal_name = "gamma_g2_regime"
        
    @property
    def name(self) -> str:
        return "gamma_g2_regime"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Classify current regime based on recent temperature patterns.
        """
        if idx < self.regime_window:
            return None, 0.0
            
        # Extract recent temperature data
        recent_temps = []
        for i in range(self.regime_window):
            if idx - i >= 0:
                temp = self._get_station_data(days, idx - i, 'high')
                if temp is not None:
                    recent_temps.append(temp)
                    
        if len(recent_temps) < 3:
            return None, 0.0
            
        # Calculate trend and volatility
        trend = recent_temps[0] - recent_temps[-1]  # Latest vs oldest
        volatility = np.std(recent_temps)
        
        # Determine regime
        if volatility < 2.0:  # Stable regime
            direction = 'up' if trend > 0 else 'down'
            confidence = min(0.7, 0.3 + abs(trend) * 0.1)
        elif abs(trend) < 1.0:  # Oscillating regime
            direction = 'down'  # Expect reversion in oscillating regime
            confidence = 0.6
        else:  # Trending regime
            direction = 'up' if trend > 0 else 'down'
            confidence = min(0.8, 0.4 + abs(trend) * 0.15)
            
        return direction, confidence


class G3_AnalogSignal(GammaBaseSignal):
    """
    G3: Historical Analog Matcher
    
    Finds historical days with similar patterns and uses their outcomes.
    """
    
    def __init__(self, db_path=None, analog_window=30, min_matches=5, **kwargs):
        super().__init__(db_path)
        self.analog_window = analog_window
        self.min_matches = min_matches
        self.signal_name = "gamma_g3_analog"
        
    @property
    def name(self) -> str:
        return "gamma_g3_analog"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Find historical analogs and return their consensus.
        """
        if idx < self.analog_window:
            return None, 0.0
            
        # Get current pattern (simplified as recent temp trend)
        current_temp = self._get_station_data(days, idx, 'high')
        prev_temp = self._get_station_data(days, idx-1, 'high')
        
        if current_temp is None or prev_temp is None:
            return None, 0.0
            
        # Simulate finding analogs (in practice, this would query historical data)
        # For now, simulate with reasonable values based on trend
        trend = current_temp - prev_temp
        if abs(trend) < 0.5:
            # Small change - look for similar patterns
            direction = 'up' if trend > 0 else 'down'
            confidence = 0.55 + abs(trend) * 0.2
        else:
            # Larger change - higher confidence in continuation
            direction = 'up' if trend > 0 else 'down'
            confidence = min(0.75, 0.55 + abs(trend) * 0.1)
            
        return direction, confidence


class G4_PressureAdvectionSignal(GammaBaseSignal):
    """
    G4: Pressure-Based Advection Indicator
    
    Uses pressure gradients and wind patterns to predict temperature movement.
    """
    
    def __init__(self, db_path=None, pressure_threshold=2.0, **kwargs):
        super().__init__(db_path)
        self.pressure_threshold = pressure_threshold
        self.signal_name = "gamma_g4_pressure_adv"
        
    @property
    def name(self) -> str:
        return "gamma_g4_pressure_adv"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Use pressure changes to infer air mass movement and temperature trend.
        """
        if idx < 1:  # Need at least yesterday's data
            return None, 0.0
            
        # Get pressure data
        current_press = self._get_station_data(days, idx, 'pressure')
        prev_press = self._get_station_data(days, idx-1, 'pressure')
        
        if current_press is None or prev_press is None:
            return None, 0.0
            
        pressure_change = current_press - prev_press
        
        # Falling pressure often indicates warmer air approaching (low pressure systems)
        # Rising pressure often indicates cooler air (high pressure systems)
        if abs(pressure_change) >= self.pressure_threshold:
            if pressure_change < 0:  # Pressure falling
                direction = 'up'  # Expect warming
                confidence = 0.6 + min(0.3, abs(pressure_change) * 0.05)
            else:  # Pressure rising
                direction = 'down'  # Expect cooling
                confidence = 0.6 + min(0.3, pressure_change * 0.05)
        else:
            # Small pressure change - return None when no clear direction
            return None, 0.0


class G5_DewpointSignal(GammaBaseSignal):
    """
    G5: Dewpoint Depression Indicator
    
    Uses humidity information to predict temperature outcomes.
    """
    
    def __init__(self, db_path=None, dewpoint_threshold=10.0, **kwargs):
        super().__init__(db_path)
        self.dewpoint_threshold = dewpoint_threshold
        self.signal_name = "gamma_g5_dewpoint"
        
    @property
    def name(self) -> str:
        return "gamma_g5_dewpoint"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Use dewpoint depression (temp - dewpoint) to predict temperature behavior.
        Lower dewpoint depression suggests drier air, affecting heating/cooling rates.
        """
        # Get temperature and dewpoint data
        temp = self._get_station_data(days, idx, 'temp')
        dewpoint = self._get_station_data(days, idx, 'dewpoint')
        
        if temp is None or dewpoint is None:
            return None, 0.0
            
        dewpoint_dep = temp - dewpoint  # Dewpoint depression
        
        # Higher dewpoint depression (drier air) can lead to larger temp swings
        if dewpoint_dep > self.dewpoint_threshold:
            # Dry air - expect stronger temperature trends to continue
            # Infer direction from recent trend if available
            if idx >= 1:
                prev_temp = self._get_station_data(days, idx-1, 'temp')
                if prev_temp is not None:
                    trend = temp - prev_temp
                    direction = 'up' if trend > 0 else 'down'
                    confidence = 0.55 + min(0.25, dewpoint_dep * 0.01)
                else:
                    direction = 'neutral'
                    confidence = 0.4
            else:
                direction = 'neutral'
                confidence = 0.4
        else:
            # Moist air - expect more moderated temperature changes
            direction = 'neutral'
            confidence = 0.5 + (self.dewpoint_threshold - dewpoint_dep) * 0.02
            
        return direction, confidence


class G6_CloudCoverSignal(GammaBaseSignal):
    """
    G6: Cloud Cover Radiational Effect
    
    Uses cloud information to predict temperature outcomes based on radiational cooling/heating.
    """
    
    def __init__(self, db_path=None, cloud_threshold=50.0, **kwargs):
        super().__init__(db_path)
        self.cloud_threshold = cloud_threshold
        self.signal_name = "gamma_g6_cloud_cover"
        
    @property
    def name(self) -> str:
        return "gamma_g6_cloud_cover"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Use cloud cover to predict temperature outcomes.
        Clear skies allow more radiational cooling/warming.
        """
        # Get cloud cover data (if available)
        cloud_cover = self._get_station_data(days, idx, 'cloud_cover')
        
        # If no cloud data, try sky condition or visibility as proxy
        if cloud_cover is None:
            sky_condition = self._get_station_data(days, idx, 'sky_condition')
            visibility = self._get_station_data(days, idx, 'visibility')
            
            # Convert to rough cloud cover percentage
            if sky_condition:
                # Map common sky conditions to cloud percentages
                condition_map = {
                    'CLR': 0, 'SKC': 0, 'NSC': 0, 'NCD': 0,  # Clear
                    'FEW': 15, 'SCT': 40,  # Partly cloudy
                    'BKN': 80, 'VV': 95,  # Mostly cloudy
                    'OVC': 100  # Overcast
                }
                cloud_cover = condition_map.get(sky_condition.upper(), 50)
            elif visibility is not None:
                # Lower visibility often correlates with more clouds
                # This is a very rough approximation
                cloud_cover = max(0, min(100, 100 - visibility * 2))
            else:
                # No cloud data available
                return None, 0.0
        
        # High clouds (clear skies) -> more radiational effect
        if cloud_cover < self.cloud_threshold:
            # Clear skies - expect stronger temperature trends
            # Determine direction from recent trend if available
            if idx >= 1:
                current_temp = self._get_station_data(days, idx, 'temp')
                prev_temp = self._get_station_data(days, idx-1, 'temp')
                
                if current_temp is not None and prev_temp is not None:
                    trend = current_temp - prev_temp
                    direction = 'up' if trend > 0 else 'down'
                    confidence = 0.55 + (self.cloud_threshold - cloud_cover) * 0.008
                else:
                    direction = 'neutral'
                    confidence = 0.45
            else:
                direction = 'neutral'
                confidence = 0.45
        else:
            # Cloudy skies - return None when no clear direction
            return None, 0.0


# Convenience function to get all Gamma signals
def get_gamma_signals(db_path=None):
    """
    Returns a list of all Gamma signal instances.
    """
    return [
        G1_UncertaintySignal(db_path),
        G2_RegimeSignal(db_path),
        G3_AnalogSignal(db_path),
        G4_PressureAdvectionSignal(db_path),
        G5_DewpointSignal(db_path),
        G6_CloudCoverSignal(db_path)
    ]


if __name__ == "__main__":
    # Test the Gamma signals
    print("Testing Gamma signal classes:")
    
    gamma_signals = get_gamma_signals()
    
    for i, signal in enumerate(gamma_signals, 1):
        print(f"G{i}: {signal.name}")
        print(f"  Min lookback: {signal.min_lookback}")
        print(f"  Instance type: {type(signal).__name__}")
    
    print("\nAll Gamma signals created successfully!")