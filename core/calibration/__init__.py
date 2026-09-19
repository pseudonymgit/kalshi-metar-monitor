"""
core/calibration — Calibration infrastructure for the weather engine.

Modules:
    hourly_calibration.py     — Hourly bucket loader & verifier
    hierarchical_bayes.py     — Bayesian shrinkage across stations/groups
    sample_floor.py           — Sample count floor rules (smooth transitions)

Usage:
    from core.calibration.hourly_calibration import HourlyCalibrationLoader
    from core.calibration.hierarchical_bayes import HierarchicalBayesianCalibration

Re-authored 2026-09-19: the original __init__ referenced wiped modules with bare
(top-level) imports that only resolved when this package dir was on sys.path.
Imports are now package-qualified and only reference modules that actually exist.
"""

from core.calibration.hourly_calibration import (
    HourlyCalibrationLoader,
    HourlyCalibration,
)
from core.calibration.hierarchical_bayes import (
    HierarchicalBayesianCalibration as HierarchicalBayes,
)
from core.calibration.sample_floor import (
    resolve_floor,
    compute_confidence_floor,
    sigmoid_blend,
    linear_blend,
    blend_estimates,
)

__all__ = [
    'HourlyCalibrationLoader',
    'HourlyCalibration',
    'HierarchicalBayes',
    'resolve_floor',
    'compute_confidence_floor',
    'sigmoid_blend',
    'linear_blend',
    'blend_estimates',
]
