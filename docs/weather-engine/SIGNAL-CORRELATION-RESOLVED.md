# Signal Correlation Resolved — Discrepancy Analysis

**Status:** APPLIED. BACKTEST_SIGNALS reduced to 7 independent signals (2026-08-09).

**Harness Buildout P0-P5:** COMPLETE (2026-08-10). 12 dedicated harness scripts built and executed. 42-param registry tested via `test_full_integration.py`. Results at `data/merged_harness_results.json`. Regime config matrix updated with harness-validated optimal values. See `config/regime_config_matrix.json` → `harness_validated` section for full recommendations.

## The Discrepancy

| Source | Signal Set | Stations | Days/Sample | Redundant Pairs | N Killed |
|--------|-----------|----------|-------------|-----------------|----------|
| Sweep Sub-step 0a (inline) | 10 signals (big_sweep) | 8 | 100 days/synthetic vectors | 0 | 0 |
| `compute_signal_correlation_matrix.py` | 9 signals (BACKTEST_SIGNALS) | 20 | Full history (real data) | 7 | 4 |

## Root Cause

The inline `run_signal_correlation_matrix()` in `per_parameter_sweep.py` has a methodology issue:

1. **Limited sample**: Uses only first 100 days × 8 stations, generating tiny synthetic binary direction vectors
2. **Different signal registry**: Uses `big_sweep` registry signals, not the `BACKTEST_SIGNALS` from `core/unified_backtest`
3. **Flat correlation results**: With only 0/1 binary vectors from a limited sample, the Spearman correlation collapses toward 0
4. **Missing the real structure**: The full `compute_signal_correlation_matrix.py` uses proper `SignalRegistry`, all 20 stations, full date range, and Spearman ρ on actual directional predictions

## Resolved Correlation Analysis

### High-Redundancy Signals (|ρ| ≥ 0.7) — KILL RECOMMENDED

| Pair | ρ | Kill Decision |
|------|---|---------------|
| `gaussian` ↔ `gaussian_v2` | 1.000 | Kill `gaussian_v2` (v2 includes v1 logic) |
| `gaussian` ↔ `calendar_climatology` | 1.000 | Kill `calendar_climatology` (redundant with gaussian) |
| `gaussian` ↔ `forecast_disagreement` | 0.989 | Kill `forecast_disagreement` (near-duplicate) |
| `gaussian_v2` ↔ `calendar_climatology` | 1.000 | Kill `calendar_climatology` (already killed) |
| `gaussian_v2` ↔ `forecast_disagreement` | 0.972 | Kill `forecast_disagreement` (already killed) |
| `calendar_climatology` ↔ `forecast_disagreement` | 0.998 | Kill both (already killed) |

### Orthogonal Signals (|ρ| < 0.3) — KEEP

| Signal | Mean |ρ| | Max |ρ| |
|--------|:------:|:------:|
| `spike_reversion` | 0.000 | 0.000 |
| `wind_direction_shift` | 0.000 | 0.000 |
| `corrected_pressure_delta` | 0.000 | 0.000 |
| `frontal_passage_intraday` | 0.000 | 0.000 |
| `pressure_delta` | 0.322 | 0.373 (LOW) |

### Key Insight

The **gaussian family** (gaussian, gaussian_v2, calendar_climatology, forecast_disagreement) is essentially a single signal cluster. Retain only `gaussian` from that family. All other signals provide independent information.

## Fix for Sub-step 0a

The inline correlation logic in `per_parameter_sweep.py` should be replaced with a call to `compute_signal_correlation_matrix.py`'s proper method, or at minimum use the same data pipeline (SignalRegistry + all stations + full date range + Spearman ρ).

## Full Matrix Reference

Full signal correlation matrix saved to: `data/signal_correlation_matrix.json`
Human-readable report: `docs/weather-engine/SIGNAL_CORRELATION_MATRIX.md`