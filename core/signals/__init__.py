"""
Signal Registry for Weather Engine

This module maintains a registry of all available signals for the ensemble.
Each signal should implement the interface described by BaseSignal.

The unified signal module imports all the specialized signal classes.

S6 task: Updated to exclude dead signals (pressure_regime, dtr_trend, reversion, regime_signal), now includes Wind Direction Shift, NWP Analog, and Goldilocks.
"""

  
from .wind_direction_shift import WindDirectionShiftSignal
from .nwp_analog_signal import NwpAnalogSignal
from .goldilocks_signal import GoldilocksSignal
from .persistence_signal import PersistenceSignal
from .gaussian_signal import GaussianSignal
from .gaussian_v2_signal import GaussianV2Signal
from .pressure_delta_signal import PressureDeltaSignal
from .forecast_disagreement_signal import ForecastDisagreementSignal
from .calendar_climatology_signal import CalendarClimatologySignal
from .temperature_advection_signal import TemperatureAdvectionSignal


class SignalRegistry:
    """
    Registry containing all available signals for use in the ensemble.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.signals = {
            'wind_direction_shift': WindDirectionShiftSignal(db_path),
            'nwp_analog': NwpAnalogSignal(db_path),
            'goldilocks': GoldilocksSignal(db_path),
            'persistence': PersistenceSignal(db_path),
            'gaussian': GaussianSignal(db_path),
            'gaussian_v2': GaussianV2Signal(db_path),
            'pressure_delta': PressureDeltaSignal(db_path),
            'forecast_disagreement': ForecastDisagreementSignal(db_path),
            'calendar_climatology': CalendarClimatologySignal(db_path),
            'temperature_advection': TemperatureAdvectionSignal(db_path),
        }


    def get_signal(self, signal_name: str):
        """Get a signal by name."""
        return self.signals.get(signal_name)

    def get_all_signals(self):
        """Get all available signals."""
        return self.signals

    def add_signal(self, name: str, signal_obj):
        """Add a new signal to the registry.

        Raises TypeError if signal_obj does not inherit from BaseSignal.
        """
        from .base_signal import BaseSignal
        if not isinstance(signal_obj, BaseSignal):
            raise TypeError(
                f"Cannot register '{name}': object of type {type(signal_obj).__name__} "
                f"does not inherit from `BaseSignal`. All signals must implement "
                f"the `evaluate(idx, days)`, `name`, and `min_lookback` interface."
            )
        self.signals[name] = signal_obj


# Convenient function for initializing the registry with all signals
def create_signal_registry(db_path: str):
    """
    Factory function to create a signal registry with all available signals.
    """
    return SignalRegistry(db_path)