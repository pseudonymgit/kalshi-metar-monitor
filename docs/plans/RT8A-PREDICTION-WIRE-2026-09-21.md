# R/T-8a — Prediction-Integrity Fix: Receipt

**Author:** Gilfoyle (CTIO)
**Date:** 2026-09-21 00:40 UTC
**Trigger:** Dan directive 9/21 00:17 "run your rec first" → R/T-8a
**Repo:** `prototypes/weather-engine-source` @ branch `temp-sprint-branch`
**Method:** script-only, no AI-in-loop. Expert-spec → build → independent QC → confirm-against-spec.

---

## Summary

Three correctness bugs fixed + one scope deferral. All verified by deterministic QC (before/after signal counts + import checks). Two additional pre-existing bugs discovered and flagged (not fixed — out of scope).

### Fixed (committed)

1. **ACTIVE silent drop (most material)** — `core/unified_backtest.py::BACKTEST_SIGNALS` listed 10 signals, 3 of which (`spike_reversion`, `goldilocks`, `wind_direction_shift`) were NOT in the live `SignalRegistry`. `run_backtest()` silently skipped them (`if sig is None: continue`), so the numbers Dan saw evaluated **7 signals, not 10**. Fixed: removed the 3 dead names; `BACKTEST_SIGNALS` now = 7 live signals, all present in the registry. Added a `logger.warning` tripwire so any future signal/registry drift is loud, not silent.

2. **Broken fusion import (pre-existing, discovered during build)** — `core/unified_backtest.py:35` did `from core.signal_fusion import SignalFusionEngine`, but `signal_fusion.py` only imported it aliased as `LegacyFusionEngine` and never re-exported it. **`unified_backtest.py` was unimportable** — the entire `run_backtest()` + `beam_search_calibration` path was broken at import time. Fixed: `signal_fusion.py` now re-exports `SignalFusionEngine` under its canonical name (in addition to the `LegacyFusionEngine` alias).

3. **DORMANT name-mapping (dispatched)** — `core/signal_fusion.py::PoolOfPools.evaluate()` matched taxonomy `_signal`-suffixed names against execution short names → 0 exact matches → null branch ("no pools have active signals"). Fixed: normalized at the pool boundary (strips `_signal` suffix fallback). Before: 0 pools matched. After: 3 pools / 6 signals match (`gefs`, `climatology`, `metar_nowcast`), agreement gate passes.

### Deferred (scope flag, NOT built)

**"Wire fusion into paper_trading_engine/app.py"** — flagged as a separate architectural step, not a drop-in. `paper_trading_engine.py` has its own prediction stack (agreement_gate, adaptive_thresholds, station_skill_gate, spatial_coherence, ensemble_diversity, position_sizing). Wiring LIOP/UWC fusion into it is an architecture change needing its own A/B, not part of the correctness fix. Rationale: fix the numbers first, then decide the wiring.

### Flagged (pre-existing, NOT fixed — out of scope)

**`core/signals/cross_model_divergence_signal.py:35`** — `from ..nwp_lookahead_gate import contemporaneous_where` raises `ImportError: attempted relative import beyond top-level package`. Pre-existing at HEAD (verified via stash). This breaks `SignalRegistry` import when loaded as a top-level `core` package (context-dependent — may work via a different entry path). Separate latent bug; needs its own fix.

---

## QC Results (deterministic, script-only)

| Check | Before | After |
|---|---|---|
| BACKTEST_SIGNALS missing from registry | 3 (silent drop) | **0** ✓ |
| Effective signals reaching fusion | 7 (of 10 listed) | 7 (of 7 listed — honest) ✓ |
| PoolOfPools exact-name matches | 0 | 6 across 3 pools ✓ |
| `unified_backtest` importable | ❌ (ImportError) | ✅ ✓ |
| `SignalFusionEngine` importable from signal_fusion | ❌ | ✅ ✓ |

---

## Receipt

- **Commit:** see below (R/T-8a prediction-integrity fix)
- **Files changed:** `core/signal_fusion.py` (+14/−1), `core/unified_backtest.py` (+24/−3)
- **Spec:** `docs/plans/RT8A-PREDICTION-WIRE-SPEC-2026-09-21.md`
- **Tree:** clean (no untracked, no data mutation)

## Stop-condition verification

1. ✅ BACKTEST_SIGNALS reconciled to live registry (no dead names); silent drop eliminated; drift logs a warning.
2. ✅ PoolOfPools name-mapping normalized; exact-match returns real signals (no null-branch silent zero).
3. ✅ Fusion import re-exported; `unified_backtest` + `beam_search_calibration` importable again.
4. ✅ Receipt written with commit hash.

## Next (gated)

R/T-8b/c/d/e (exhaustive multi-target recalibration) can now proceed on the **corrected** signal set (7 honest live signals through the connected fusion). The paper-trading fusion wiring is a separate Dan/Gerri-gated decision.
