# B4 Review: cross_model_divergence_signal.py

**Reviewer:** B-Mode Signal Review (Subagent)  
**Date:** 2026-08-06  
**Spec reference:** HRRR-PIVOT-PLAN.md (Definition A — Cross-model divergence as confidence modulator)  
**NOTE:** `EDGE20-COMPLETION-REVIEW.md` does not exist at the documented path. Design spec sourced from `HRRR-PIVOT-PLAN.md` §"Definition A" and the signal's own docstring, which both describe this as a meta-signal / confidence modulator.

---

## VERDICT: REVISE

---

## Issues Found

### 1. Does not inherit from `BaseSignal` (ABC) — interface violation

`CrossModelDivergenceSignal` is a plain class that does not extend `BaseSignal`. This means:

- **Missing `name` property** — required by the abstract interface.
- **Missing `min_lookback` property** — required by the abstract interface.
- **Missing `evaluate(idx, days)` method** — the core contract that every signal in the registry must satisfy. The method signature required by the abstract base class is `evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]`.
- **Missing `evaluate_for_station()` method** — inherited default logic from `BaseSignal` is not available.
- **`validate_signal` decorator** is not applied, so the return-value validation that wraps every other signal's `evaluate()` is absent.

### 2. Public API does not match the standard signal interface

The class exposes `get_divergence()` (returns a dict) and `get_confidence_modulator()` (returns a float) instead of `evaluate()`. While the docstring explicitly labels this as a "meta-signal that modulates other signals rather than producing independent direction", the registry treats it as a standard signal. Any code path that calls `signal.evaluate(...)` on a signal retrieved from the registry will get an `AttributeError`.

### 3. Registry will reject this signal via `SignalRegistry.add_signal()`

`SignalRegistry.add_signal()` (in `core/signals/__init__.py`) performs an `isinstance(obj, BaseSignal)` check and raises `TypeError` for non-BaseSignal objects. The signal is currently placed directly into the `self.signals` dict in `SignalRegistry.__init__()`, bypassing `add_signal()`. If any code path later calls `add_signal()` for this signal, it will crash.

### 4. `big_sweep.py` dict-based registry works but is fragile

The `build_signal_registry()` function in `big_sweep.py` uses a plain dict and `_try_instantiate()` with `nwp_db_path` and `metar_db_path` kwargs — this works because `CrossModelDivergenceSignal.__init__(nwp_db_path=..., metar_db_path=...)` accepts those parameters. However, this is inconsistent with the `SignalRegistry` class in `__init__.py`. Two different registry paths with different constraints.

### 5. Spec file missing at documented path

The task references `docs/plans/EDGE20-COMPLETION-REVIEW.md` as the design spec. This file does not exist. The closest design specification is in `HRRR-PIVOT-PLAN.md` which describes this as a high-value confidence modulator. The signal's docstring serves as the de facto spec.

---

## What's Correct

- **Core logic matches the spec.** Bias-corrected GFS and ECMWF forecasts are fetched from `nwp_forecasts.db`, rolling 14-day bias is computed per station, divergence is computed as `|GFS_corrected - ECMWF_corrected|`, and confidence is normalized to [0, 1].
- **`get_confidence_modulator()`** correctly implements the "confidence modulator" design intent from the spec.
- **`get_divergence()`** returns a rich dict with all intermediate values (raw temperatures, bias terms, corrected values, divergence, direction, n_sources).
- **Direction detection** (prev_day_high with ≥2°F threshold) is reasonable.
- **No AI/ML, no API calls** — B-Mode compliant.
- **Registered in `big_sweep.py`** as `"cross_model_divergence"` — confirmed at line 134.
- **Registered in `core/signals/__init__.py`** as `'cross_model_divergence'` — confirmed at line 84.

---

## Recommended Action

**Option A: Subclass `BaseSignal`** — Add `evaluate()`, `name`, and `min_lookback` to make this a standard signal. `evaluate()` would return `(None, confidence)` if this is purely a confidence modulator, or `(direction, confidence)` if it should also produce direction. This is the cleanest path for registry compatibility.

**Option B: Formalize as a non-signal modulator** — Remove from `SignalRegistry` and `big_sweep.py`'s signal registry, and wire it as a standalone confidence-adjustment module that signals call via `get_confidence_modulator()`. This is more honest about its role but requires refactoring all consumers.

**Recommendation: Option A** is simpler and preserves backward compatibility with the existing registry infrastructure.