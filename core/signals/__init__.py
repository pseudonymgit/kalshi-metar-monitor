# CHANGELOG (last 10 broad changes):
# 1. [2026-07-25 B-Mode Cycle 3 post-fix: Removed 10 non-firing signals from registry.
#     Correlation matrix proved 10 signals return ρ=0.000 — they never fire.
#     Kept: 7 core signals + spike_reversion + frontal_passage_intraday = 9 active]
# 2. [2026-07-25 B-Mode Cycle 3.1: Removed 6 dead signals (persistence, metar_dtdt,
#     pressure_tendency, seasonal_regime, esdr, nwp_direct) from registry]
# 3. [2026-07-21 Phase 9: Full 11-signal combinatorial search + calibration + purged CV]
# 4. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 5. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 6. [2026-07-16 T9: Build 5 Tier 1 signals + combinatorial backtest harness]
# 7. [2026-07-16 T2: Remove 4 dead signals from all code paths]
# 8. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 9. [2026-07-22 Phase 18: Add intraday signals — FOGR Reversion, METAR dT/dt, Pressure Tendency, HRRR Bias-Corrected, ESDR, NWP+METAR Fusion]
#


"""
Signal Registry for Weather Engine

This module maintains a registry of all available signals for the ensemble.
Only signals that actually produce output are registered.
Non-firing signals are DISABLED — kept in codebase, not in registry.

Active signal count: 9 (after B-Mode Cycle 3 cleanup, verified by correlation matrix)
  - 5 core signals that fire and produce accuracy (gaussian, gaussian_v2, pressure_delta,
    forecast_disagreement, calendar_climatology)
  - 2 orthogonal signals (wind_direction_shift, corrected_pressure_delta)
  - 1 spike-reversion lane signal (spike_reversion/goldilocks — separate lane)
  - 1 intraday frontal passage signal (frontal_passage_intraday — Sure Thing lane)

ADVANCE (wired into pipeline — operational, via integration layer):
  ecmwf_bias_corrected     — core/signals/ecmwf_bias_corrected_signal.py  (NWP source signal)
  metar_nowcast            — core/signals/metar_nowcast_signal.py        (intraday nowcast)
  spread_based_entry       — core/signals/spread_based_entry_signal.py   (execution optimizer)

DISABLED (in codebase, removed from registry — need data infrastructure):
  temperature_advection, intraday_metar_confirmation    — NWP / ERA5 backfill
  fogr_reversion, hrrr_bias_corrected, nwp_dtdt_fusion  — HRRR / FOGR NWP data
  volume_momentum, settlement_arbitrage                  — Kalshi API execution
  spread_based_entry                                     — Kalshi API execution
  frontal_detector                                       — deprecated
  ai_composite                                           — ML pipeline not deployed
"""


from .wind_direction_shift import WindDirectionShiftSignal
from .gaussian_signal import GaussianSignal
from .gaussian_v2_signal import GaussianV2Signal
from .pressure_delta_signal import PressureDeltaSignal
from .forecast_disagreement_signal import ForecastDisagreementSignal
from .calendar_climatology_signal import CalendarClimatologySignal
from .dual_polarity_signal import CorrectedPressureDeltaSignal
from .frontal_passage_intraday_signal import FrontalPassageIntradaySignal

# ── Radiational Cooling Signal (v2026-08-06) ──
from .radiational_cooling_signal import RadiationalCoolingSignal

# ── WeatherAPI P0 Signals (v2026-08-05) ──
from .cloud_cover_index_signal import CloudCoverIndexSignal
from .feels_like_delta_signal import FeelsLikeDeltaSignal
from .ecmwf_bias_corrected_signal import ECMWFBiasCorrectedSignal
from .metar_trend_signal import MetarTrendSignal
from .cross_model_divergence_signal import CrossModelDivergenceSignal

# ── Sub-hourly trajectory nowcast (OOS-validated ef1e9d8c0) ──
from .subhourly_trajectory_signal import SubhourlyTrajectorySignal

# ── Gamma Signals (G1-G6) for 82-member ensemble enhancement ──
from .gamma_signals import (
    G1_UncertaintySignal,
    G2_RegimeSignal, 
    G3_AnalogSignal,
    G4_PressureAdvectionSignal,
    G5_DewpointSignal,
    G6_CloudCoverSignal
)


class SignalRegistry:
    """
    Registry containing signals that actually produce output.
    Verified by correlation matrix (2026-07-25) — only signals with non-zero
    variance are registered here.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.signals = {
            # ── Sub-hourly trajectory nowcast (OOS-validated ef1e9d8c0) ──
            # Registered for scoring only; scored via evaluate_nowcast() /
            # iter_nowcast_events(), NOT the daily evaluate(idx, days) interface.
            # NOT wired into the live/paper trading path (separate Dan-gated step).
            # QC 789b30027: was attribute-only, so get_all_signals() omitted it and
            # it was silently dropped (NO_FIRES). Key added 2026-10-05.
            'subhourly_trajectory': SubhourlyTrajectorySignal(db_path),

            # ── Forecasting Ensemble (Lane 1) ──
            'gaussian': GaussianSignal(db_path),               # 66.53% settlement-validated
            'gaussian_v2': GaussianV2Signal(db_path),          # 63.95%
            'pressure_delta': PressureDeltaSignal(db_path),    # 60.86%
            'forecast_disagreement': ForecastDisagreementSignal(db_path),  # 64.36%
            'calendar_climatology': CalendarClimatologySignal(db_path),    # 69.00% (best)

            # ── WeatherAPI P0 Signals (independent from METAR) ──
            'cloud_cover_index': CloudCoverIndexSignal(db_path),        # P0-1: Cloud Cover Index
            'feels_like_delta': FeelsLikeDeltaSignal(db_path),           # P0-2: Feels-Like Delta

            # ── NWP Signals ──
            'metar_trend': MetarTrendSignal(db_path),  # METAR trend (was ecmwf_bias_corrected)
            'cross_model_divergence': CrossModelDivergenceSignal(),      # Model disagreement → confidence

            # ── Sure Thing Lane (Lane 2) ──
            'frontal_passage_intraday': FrontalPassageIntradaySignal(db_path),

            # ── Radiational Cooling (LOW only) ──
            'radiational_cooling': RadiationalCoolingSignal(db_path),

            # ── Physics-Corrected Pressure Delta ──
            # WAVE-B/7 (2026-09-28): RE-REMOVED from registry. Settlement validation
            # 2026-07-25 gave MCC=-0.2167 (actively harmful) and the header comment
            # already listed it as removed — but it was still registered here, so it
            # silently re-entered the ensemble. Kept out until a re-test clears it.
            # 'corrected_pressure_delta': CorrectedPressureDeltaSignal(db_path),

            # ── Gamma Signals (G1-G6) for 82-member ensemble enhancement ──
            'gamma_g1_uncertainty': G1_UncertaintySignal(db_path),
            'gamma_g2_regime': G2_RegimeSignal(db_path),
            'gamma_g3_analog': G3_AnalogSignal(db_path),
            'gamma_g4_pressure_adv': G4_PressureAdvectionSignal(db_path),
            'gamma_g5_dewpoint': G5_DewpointSignal(db_path),
            'gamma_g6_cloud_cover': G6_CloudCoverSignal(db_path),
            # SpikeReversionSignal removed — Fix 5: 49.85% accuracy (negative EV)
            # GoldilocksSignal removed — same signal, same negative EV
        }

        # Convenience attribute (mirrors the registry key; some callers use it).
        self.subhourly_trajectory = self.signals['subhourly_trajectory']

        # REMOVED from registry (verified by settlement validation 2026-07-25):
        # wind_direction_shift         — 49.72% settlement accuracy (below coin flip)
        # corrected_pressure_delta     — MCC=-0.2167 (actively harmful)

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