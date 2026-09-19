"""
seasonal_regime_signal — Thin re-export from dual_polarity_signal.

This signal lives inside dual_polarity_signal.py as SeasonalRegimeClassifier.
This stub enables the import test and sweep registry to resolve it.

Import is package-qualified (the original used a bare `from dual_polarity_signal`
that only worked when core/signals was on sys.path; this re-export fixes the
ModuleNotFoundError so the signal resolves under any import root).
"""

from core.signals.dual_polarity_signal import SeasonalRegimeClassifier

evaluate = SeasonalRegimeClassifier.evaluate
evaluate_for_station = SeasonalRegimeClassifier.evaluate_for_station

__all__ = ['SeasonalRegimeClassifier', 'evaluate', 'evaluate_for_station']
