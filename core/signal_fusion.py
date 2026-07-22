#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - facade module re-exporting from fusion_logic.py, compatibility_checks.py]
# 2. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 3. [2026-07-06 fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL]
# 4. [2026-07-06 feat: add SAS, conviction score, Brier decomposition, regime adjustment, Bayesian weights]
# 5. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
CORE MODULE: Signal Fusion & Information-Theoretic Aggregation

FACADE MODULE — re-exports from decomposed sub-modules:
  - fusion_logic.py: Fusion engine, signal weighting, LLOP, Dempster-Shafer
  - compatibility_checks.py: Compatibility checks

All public API surface is preserved. Import from core.signal_fusion continues to work.
"""

from .fusion_logic import (
    SignalFusionEngine,
    TimeDecaySignalManager,
    apply_conflict_modulation,
    compute_weights_from_significance,
    dempster_shafer_conflict,
    mutual_information_from_boolean_pairs,
    mutual_information_matrix,
    mutual_information_simple_correlation,
    unique_information_fraction,
)

from .compatibility_checks import (
    demonstrate_fusion_stack,
)

__all__ = [
    "SignalFusionEngine",
    "TimeDecaySignalManager",
    "apply_conflict_modulation",
    "compute_weights_from_significance",
    "demonstrate_fusion_stack",
    "dempster_shafer_conflict",
    "mutual_information_from_boolean_pairs",
    "mutual_information_matrix",
    "mutual_information_simple_correlation",
    "unique_information_fraction",
]