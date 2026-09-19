"""
850mb_temp_advection_signal — Thin re-export from upper_atmosphere_signal.

This signal lives inside upper_atmosphere_signal.py as
EightHundredFiftyMbAdvectionSignal. This stub enables the import test and
sweep registry to resolve it by the canonical registry name.

Import is package-qualified (the original used a bare `from upper_atmosphere_signal`
that only worked when core/signals was on sys.path).
"""

from core.signals.upper_atmosphere_signal import EightHundredFiftyMbAdvectionSignal

evaluate = EightHundredFiftyMbAdvectionSignal.evaluate
evaluate_for_station = EightHundredFiftyMbAdvectionSignal.evaluate_for_station

__all__ = ['EightHundredFiftyMbAdvectionSignal', 'evaluate', 'evaluate_for_station']
