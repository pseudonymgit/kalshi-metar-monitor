# SWEEP Task 2 — All 15 Fire-Able Signals in Sweep — SPEC

**Date:** 2026-08-10
**Engineer:** Donna Task 2 execution
**Difficulty:** Medium (config change + verification)

## Objective
Restore the full 15-signal ensemble in both the sweep engine and the unified
backtest. The previous Comprehensive Signal Sweep killed 8 signals for
correlation (ρ ≥ 0.7), but the sweep should include all fire-able signals and
let the optimization discover redundancy naturally.

## First-Principles Analysis
Preemptive signal removal based on pairwise correlation discards information
that could improve ensemble diversity across different regime conditions.
A signal with ρ=1.0 with gaussian in one regime may provide non-redundant
coverage in another. The correct approach: include all fire-able signals,
let the per-parameter sweep and scalarized optimizer determine which matter.

## Changes

### 1. `core/unified_backtest.py` — BACKTEST_SIGNALS
Replace the 7-signal list with all 15. Update comments. Update FULL_ENSEMBLE.

### 2. `scripts/per_parameter_sweep.py` — SIGNAL_SAMPLE
Replace the 7-signal list with all 15. Update ALL_SIGNALS.

## Signal List (15 total)
```python
BACKTEST_SIGNALS = [
    'gaussian',              # Forecasting ensemble anchor
    'gaussian_v2',           # Re-added: let sweep decide
    'pressure_delta',        # Pressure-change directional
    'corrected_pressure_delta', # Re-added: let sweep decide
    'forecast_disagreement', # Re-added: let sweep decide
    'calendar_climatology',  # Re-added: let sweep decide
    'seasonal_regime',       # Most orthogonal signal
    'ecmwf_bias_corrected',  # NWP source
    'metar_trend',           # Trend-following
    'persistence',           # Re-added: let sweep decide
    'simple_trend',          # Re-added: let sweep decide
    'wind_direction_shift',  # Low correlation
    'spike_reversion',       # Down-only reversion
    'goldilocks',            # Re-added: let sweep decide
    'radiational_cooling',   # Re-added: let sweep decide
]
```

## QC Criteria (must all pass)
- [ ] Fast-mode sweep (`python3 scripts/per_parameter_sweep.py --fast`) completes without errors
- [ ] All 15 signals appear in sweep output
- [ ] No import errors
- [ ] Timing is reasonable vs 7-signal baseline (371s)

## Out of Scope
- Fire-rate audit (Task 3)
- Non-firing signal investigation (Task 5)