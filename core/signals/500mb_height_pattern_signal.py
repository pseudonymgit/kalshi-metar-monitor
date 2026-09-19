"""
500mb_height_pattern_signal — Thin re-export from upper_atmosphere_signal.

This signal lives inside upper_atmosphere_signal.py as
FiveHundredMbHeightPatternSignal. This stub enables the import test and
sweep registry to resolve it by the canonical registry name.

Import is package-qualified (the original used a bare `from upper_atmosphere_signal`
that only worked when core/signals was on sys.path).
"""

from core.signals.upper_atmosphere_signal import FiveHundredMbHeightPatternSignal

evaluate = FiveHundredMbHeightPatternSignal.evaluate
evaluate_for_station = FiveHundredMbHeightPatternSignal.evaluate_for_station

__all__ = ['FiveHundredMbHeightPatternSignal', 'evaluate', 'evaluate_for_station']
