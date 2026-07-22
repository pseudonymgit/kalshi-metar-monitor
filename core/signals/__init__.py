# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 9: Full 11-signal combinatorial search + calibration + purged CV]
# 2. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 3. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 4. [2026-07-16 T9: Build 5 Tier 1 signals + combinatorial backtest harness]
# 5. [2026-07-16 T2: Remove 4 dead signals from all code paths]
# 6. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 8. [2026-07-22 Phase 18: Add intraday signals — FOGR Reversion, METAR dT/dt, Pressure Tendency, HRRR Bias-Corrected, ESDR, NWP+METAR Fusion]
#


"""
Signal Registry for Weather Engine

This module maintains a registry of all available signals for the ensemble.
Each signal should implement the interface described by BaseSignal.

The unified signal module imports all the specialized signal classes.

S6 task: Updated to exclude dead signals (pressure_regime, dtr_trend, reversion, regime_signal), now includes Wind Direction Shift, NWP Analog, and Goldilocks.
"""

  
from .wind_direction_shift import WindDirectionShiftSignal
from .nwp_direct_signal import NwpDirectSignal
from .goldilocks_signal import GoldilocksSignal
from .persistence_signal import PersistenceSignal
from .gaussian_signal import GaussianSignal
from .gaussian_v2_signal import GaussianV2Signal
from .pressure_delta_signal import PressureDeltaSignal
from .forecast_disagreement_signal import ForecastDisagreementSignal
from .calendar_climatology_signal import CalendarClimatologySignal
from .temperature_advection_signal import TemperatureAdvectionSignal
from .frontal_detector_signal import FrontalDetectorSignal
from .intraday_metar_confirmation_signal import IntradayMetarConfirmationSignal
from .fogr_reversion_signal import FogrReversionSignal
from .metar_dtdt_signal import MetarDtdtSignal
from .pressure_tendency_signal import PressureTendencySignal
from .hrrr_bias_corrected_signal import HrrrBiasCorrectedSignal
from .esdr_signal import EsdrSignal
from .nwp_dtdt_fusion_signal import NwpDtdtFusionSignal
from .spread_based_entry_signal import SpreadBasedEntrySignal
from .volume_momentum_signal import VolumeMomentumSignal
from .settlement_arbitrage_signal import SettlementTimeArbitrageSignal


class SignalRegistry:
    """
    Registry containing all available signals for use in the ensemble.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.signals = {
            'wind_direction_shift': WindDirectionShiftSignal(db_path),
            'nwp_direct': NwpDirectSignal(db_path),
            'goldilocks': GoldilocksSignal(db_path),
            'persistence': PersistenceSignal(db_path),
            'gaussian': GaussianSignal(db_path),
            'gaussian_v2': GaussianV2Signal(db_path),
            'pressure_delta': PressureDeltaSignal(db_path),
            'forecast_disagreement': ForecastDisagreementSignal(db_path),
            'calendar_climatology': CalendarClimatologySignal(db_path),
            'temperature_advection': TemperatureAdvectionSignal(db_path),
            'frontal_detector': FrontalDetectorSignal(db_path),
            'intraday_metar_confirmation': IntradayMetarConfirmationSignal(db_path),
            'fogr_reversion': FogrReversionSignal(db_path),
            'metar_dtdt': MetarDtdtSignal(db_path),
            'pressure_tendency': PressureTendencySignal(db_path),
            'hrrr_bias_corrected': HrrrBiasCorrectedSignal(db_path),
            'esdr': EsdrSignal(db_path),
            'nwp_dtdt_fusion': NwpDtdtFusionSignal(db_path),
            'spread_based_entry': SpreadBasedEntrySignal(db_path),
            'volume_momentum': VolumeMomentumSignal(db_path),
            'settlement_arbitrage': SettlementTimeArbitrageSignal(db_path),
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