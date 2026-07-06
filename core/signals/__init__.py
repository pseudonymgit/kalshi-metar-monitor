"""
Signal Registry for Weather Engine

This module maintains a registry of all available signals for the ensemble.
Each signal should implement the interface described by BaseSignal.

The unified signal module imports all the specialized signal classes.

S6 task: Includes new signals Pressure×Regime, DTR Trend, and Wind Direction Shift.
"""

from .pressure_regime_interaction import PressureRegimeSignal
from .dtr_trend import DTRTrendSignal  
from .wind_direction_shift import WindDirectionShiftSignal

# Import legacy signals if needed here (will be in core/ later)

class SignalRegistry:
    """
    Registry containing all available signals for use in the ensemble.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.signals = {
            'pressure_regime': PressureRegimeSignal(db_path),
            'dtr_trend': DTRTrendSignal(db_path),
            'wind_direction_shift': WindDirectionShiftSignal(db_path),
            # Legacy signals will be added here as they're migrated
        }
    
    def get_signal(self, signal_name: str):
        """Get a signal by name."""
        return self.signals.get(signal_name)
    
    def get_all_signals(self):
        """Get all available signals."""
        return self.signals
    
    def add_signal(self, name: str, signal_obj):
        """Add a new signal to the registry."""
        self.signals[name] = signal_obj


# Convenient function for initializing the registry with all signals
def create_signal_registry(db_path: str):
    """
    Factory function to create a signal registry with all available signals.
    """
    return SignalRegistry(db_path)