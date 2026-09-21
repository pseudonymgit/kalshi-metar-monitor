# R/T-8a — Prediction-Integrity Fix: Spec (expert-spec → build → QC → confirm)

**Author:** Gilfoyle (CTIO)
**Date:** 2026-09-21 00:25 UTC
**Trigger:** Dan directive 9/21 00:17 "run your rec first" → R/T-8a (prereq for R/T-8b/c/d/e recalibration).
**Repo:** `prototypes/weather-engine-source` @ `98882303b` / branch `temp-sprint-branch`
**Method:** script-only, no AI-in-loop. Expert-spec → build → independent QC → confirm-against-spec.

---

## Investigation findings (pre-spec, evidence-backed)

R/T-7 (my own audit) + this dispatch identified two issues. Deeper tracing today (9/21 00:18–00:25) surfaced a **third, more material bug** that the dispatch did not name, plus a **scope correction**.

### Finding 1 — ACTIVE silent drop (the real correctness bug)

`core/unified_backtest.py:39` `BACKTEST_SIGNALS` lists **10** signal names, but **3 are not in the live `SignalRegistry`** (12 keys):

| BACKTEST_SIGNALS name | In live registry? |
|---|---|
| gaussian, gaussian_v2, pressure_delta, calendar_climatology, forecast_disagreement, corrected_pressure_delta, frontal_passage_intraday | ✅ LIVE |
| **spike_reversion** | ❌ MISSING (removed — 49.85% negative EV, Fix 5) |
| **goldilocks** | ❌ MISSING (removed — same) |
| **wind_direction_shift** | ❌ MISSING (removed — 49.72% below coin flip) |

In `run_backtest()`, `registry.get_signal(name)` returns `None` for the 3 missing → `if sig is None: continue` → **silently skipped**. So the fusion/backtest that produces Dan's accuracy numbers actually evaluates **7 signals, not 10**, and the 3 dead names are silently dropped. This is *active* (feeds `unified_backtest.run_backtest()` and `beam_search_calibration.run_backtest_combo()`), unlike the dormant `PoolOfPools` issue.

### Finding 2 — DORMANT name-mapping bug (as dispatched)

`core/signal_families.py::get_pool_members()` returns taxonomy names (`gaussian_signal`, `_signal` suffix). The execution registry uses short names (`gaussian`). Exact-name match = NONE (verified empirically 9/20). This zeroes `PoolOfPools.evaluate()` *if* the UWC cascade were ever wired with execution names. Dormant — UWC is not invoked by the live path.

### Finding 3 — SCOPE CORRECTION (must flag before building)

The dispatch's step (b) — "wire fusion into `paper_trading_engine.py`/`app.py`" — is **not a drop-in fix**. `paper_trading_engine.py` already has its OWN prediction stack (`agreement_gate`, `adaptive_thresholds`, `station_skill_gate`, `spatial_coherence`, `ensemble_diversity`, `position_sizing`, `late_day_momentum_hourly`, `intraday_metar_confirmation`). The LIOP `SignalFusionEngine` and the UWC `PoolOfPools` are a *separate* fusion architecture. Wiring one into the other is an architectural change with real regression risk, not a name fix.

The correct reading of Dan's "run your rec first": **fix the correctness bugs (Findings 1 + 2) so the numbers are real, THEN decide whether/how to wire fusion into paper_trading as a separate gated step.**

---

## Spec

### Change 1 — Fix the active silent drop (correctness, highest priority)

**Goal:** `run_backtest()` must evaluate exactly the signals that are actually wired, with no silent drops.

**Build:** Derive the signal list from the live registry rather than a hardcoded list that can drift:
- Add a helper that returns the intersection of `BACKTEST_SIGNALS` (or a caller-supplied list) with `SignalRegistry.get_all_signals()` keys, OR
- Replace the 3 dead names in `BACKTEST_SIGNALS` with the correct live names, and add a runtime guard that warns (not silently skips) when a requested signal is absent from the registry.

**Chosen approach (minimal, reversible):** keep `BACKTEST_SIGNALS` as the canonical *requested* list but (a) remove the 3 dead names, and (b) add an explicit `logger.warning` + counter when `registry.get_signal()` returns `None`, so any future drift is loud, not silent. This preserves the current numeric baseline while eliminating the silent drop and adding a drift tripwire.

### Change 2 — Fix the name-mapping (dormant, defense-in-depth)

**Goal:** `get_pool_members()` / `PoolOfPools.evaluate()` must match execution-registry short names.

**Build:** normalize taxonomy pool-member names to execution short names by stripping a trailing `_signal` suffix at the boundary where `PoolOfPools` consumes them, OR map via an explicit alias. Minimal: in `PoolOfPools.evaluate()`, when checking `if sig_name in signal_predictions`, also try the suffix-stripped form. Do NOT mutate the taxonomy registry (that's authoritative metadata); normalize only at the fusion boundary.

### Change 3 — Wire fusion into live path (DEFERRED, separate gated step)

**Do NOT build in this pass.** Flag for a dedicated spec. Rationale: architectural risk, needs its own A/B (LIOP vs UWC vs majority) on the corrected signal set, and should only land after Change 1's corrected numbers are established as the new baseline.

---

## Write scope

`core/signal_families.py`, `core/signal_fusion.py`, `core/unified_backtest.py` only. No data mutation. No new DB writes.

## Stop conditions

1. `BACKTEST_SIGNALS` reconciled to live registry (no dead names); silent drop eliminated (0 signals skipped due to `None`); drift now logs a warning.
2. `get_pool_members()`/`PoolOfPools` name-mapping normalized; exact-match against execution short names returns the real signals (no null-branch silent zero).
3. Both changes verified by a deterministic check script (count signals reaching fusion before/after), committed, tree clean.
4. Receipt at `docs/plans/RT8A-PREDICTION-WIRE-2026-09-21.md` with commit hash.

## Escalation trigger

Escalate to Gerri/Dan if (a) removing the 3 dead names changes the baseline accuracy materially (indicating prior numbers were inflated/deflated by the drop), or (b) the paper-trading fusion wiring is required as part of THIS task rather than a follow-on — that needs an explicit scope decision.
