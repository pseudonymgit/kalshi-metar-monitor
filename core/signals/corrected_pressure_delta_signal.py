"""
corrected_pressure_delta_signal — Thin re-export from dual_polarity_signal.

This signal lives inside dual_polarity_signal.py as CorrectedPressureDeltaSignal.
This stub enables the import test and sweep registry to resolve it by the
canonical registry name.

Import is package-qualified (the original used a bare `from dual_polarity_signal`
that only worked when core/signals was on sys.path; this re-export fixes the
ModuleNotFoundError so the signal resolves under any import root).
"""

from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal

evaluate = CorrectedPressureDeltaSignal.evaluate
evaluate_for_station = CorrectedPressureDeltaSignal.evaluate_for_station

__all__ = ['CorrectedPressureDeltaSignal', 'evaluate', 'evaluate_for_station']
