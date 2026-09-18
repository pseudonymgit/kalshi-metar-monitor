<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Fusion Engine Default Decision

**Date:** 2026-08-30
**Decision:** LIOP (Log-Odds Linear Opinion Pool) is the default fusion engine
**Status:** LOCKED (change requires Gray Room review)
**Authority:** Dan Gabriel, Phase 1 from Gray Room findings

## Decision

The system uses **LIOP (Log-Odds Linear Opinion Pool)** as the default fusion engine. UWC (Uncertainty-Weighted Cascade) is retained as an **optional mode** for future tuning and experimentation — it is not deleted.

## Implementation

- **Adapter:** `core/fusion_adapter.py` — `FusionEngineAdapter` provides a common interface
- **Default engine:** `"liop"` — uses `SignalFusionEngine` from `core/fusion_logic.py`
- **Optional mode:** `"uwc"` — uses `UncertaintyWeightedCascadeBacktestAdapter` from `core/signal_fusion.py`
- **Configuration:** pass `fusion_params={'fusion_engine': {'name': 'uwc'}}` to `run_backtest()` to select UWC
- **Default wiring:** `core/unified_backtest.py` line 335-347 initializes LIOP via `FusionEngineAdapter`

## Evidence

### Gray Room findings (2026-08-03)
The Gray Room panel found LIOP produced **59.86% settlement accuracy** as the proven baseline. UWC was found to reject **98.7% of trades** at **55.81% settlement accuracy**. The Gray Room recommended "Discard flat LLOP in signal_fusion.py. Adopt E1's uncertainty-weighted cascade" but this was contingent on CLI validation (P0) which remains incomplete.

### Phase 1 A/B sweep (2026-08-30)
Full 20-station × 180d holdout backtest with 28 non-degenerate signals:

| Metric | LIOP | UWC | Delta |
|--------|------|-----|-------|
| Paper accuracy | [result] | [result] | [result] |
| Settlement accuracy | [result] | [result] | [result] |
| Trades | [result] | [result] | [result] |
| Settlement Sharpe | [result] | [result] | [result] |

*Results to be filled from `data/phase1_ab_sweep_results.json` after full sweep completes.*

### Quick-mode validation (2 stations, 30d holdout)
| Metric | LIOP | UWC |
|--------|------|-----|
| Paper accuracy | 59.54% (3,235 trades) | 53.12% (32 trades) |
| Settlement accuracy | 56.10% (795 trades) | 50.00% (8 trades) |
| Settlement trades | 795 | 8 |

Clear advantage for LIOP: higher accuracy, dramatically more trades. UWC rejects ~99% of trades under its current configuration, making it unsuitable as the default.

## Historical context

### LIOP (fusion_logic.py)
- Log-Odds Linear Opinion Pool with 4-layer fusion: calibration, MI-based decorrelation, log-odds pooling, Dempster-Shafer conflict detection
- Proven baseline: 59.86% accuracy on 20-station holdout
- Produces high trade volume (thousands of trades per backtest)
- Well-calibrated with isotonic regression

### UWC (signal_fusion.py)
- Uncertainty-Weighted Cascade with pool-of-pools + camp dispute resolution
- Full 4-layer cascade: Beta-binomial Layer 1, camp dispute, Kelly sizing, regime modulation
- Aggressive trade rejection: 98.7% of candidate trades rejected
- Rejects most trades due to edge gates, agreement thresholds, and floor values
- May be useful as a "high conviction" filter in the future if tuned

## Future path

1. **Keep LIOP as default** for production and paper trading
2. **Retain UWC as configurable mode** for experimentation
3. **UWC tuning candidate:** If UWC trade rejection can be reduced from 98.7% to ~50% with acceptable accuracy (≥58%), consider re-evaluation
4. **Any change to fusion default** requires Gray Room review and definitive A/B evidence

## Files

- `core/fusion_adapter.py` — adapter with common interface
- `core/fusion_logic.py` — LIOP implementation (the default)
- `core/signal_fusion.py` — UWC implementation (re-exported as optional mode)
- `core/unified_backtest.py` — backtest runner using the adapter
- `data/phase1_ab_sweep_results.json` — definitive A/B comparison results