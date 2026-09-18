# Signal Registry Reconciliation

**Date:** 2026-09-17 21:45 UTC
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Type:** Reconciliation + documentation. Minimal registry-metadata cleanup only.
**Source audit:** `docs/plans/WE-COMPLETENESS-AUDIT-2026-09-17.md` (sections 1–2)
**Scope boundary:** No signal logic rewritten. No calibration changes. No data/DB changes. No backtests run. Stage-only, no commit.

---

## Purpose

Reconcile the split between two signal registries that disagree:

1. **`core/signal_families.py` → `SIGNAL_REGISTRY`** — a *taxonomy/metadata* layer (48 unique keys after dedup; was 49 with a duplicate). Imported by `platt_calibration.py`, `signal_fusion.py`, `additive/historical_adapter.py`, `migrate_calibration_v3.py`, `sweep_engine.py`, `big_sweep.py` — but it is **not** an execution registry.
2. **`core/signals/__init__.py` → `SignalRegistry` class** — the *live* registry of **12 actually-wired signals**. This is what `calibrate_all_signals.py` imports (`from signals import SignalRegistry`).

The goal is a **definitive classification** of every taxonomy signal as: (a) live-wired, (b) implemented-but-not-wired, (c) hollow stub, or (d) killed/parked/gated/orphaned.

---

## Definitive Classification Table — all 48 signals

Legend:
- **LIVE** = instantiated in `SignalRegistry` (wired into live pipeline).
- **IMPL-NOT-WIRED** = real `evaluate()`/`evaluate_for_station()` logic exists, but NOT instantiated in `SignalRegistry`.
- **HOLLOW** = no real logic; returns `(None, 0.5)` or `(None, 0.0)` unconditionally.
- **PARKED / KILLED / GATED / ORPHANED** = taxonomy `status` field.

| # | Taxonomy key | Family | Taxonomy status | Live-wired? | Final classification | Notes |
|---|---|---|---|---|---|---|
| 1 | eighty_two_member_ensemble_signal | A | ACTIVE | **No** | IMPL-NOT-WIRED | 82-member GEFS ensemble fraction |
| 2 | gaussian_signal | A | ACTIVE | **Yes** (`gaussian`) | **LIVE** | 66.53% settlement-validated |
| 3 | gaussian_v2_signal | A | ACTIVE | **Yes** (`gaussian_v2`) | **LIVE** | 63.95% |
| 4 | calendar_climatology_signal | A | ACTIVE | **Yes** (`calendar_climatology`) | **LIVE** | 69.00% (best) |
| 5 | ecmwf_bias_corrected_signal | A | ACTIVE | **Yes** (`ecmwf_bias_corrected`) | **LIVE** | NWP source signal |
| 6 | nwp_direct_signal | A | ACTIVE | **No** | IMPL-NOT-WIRED | GFS/IFS/ICON/GEM multi-model mean |
| 7 | nwp_analog_signal | A | KILLED | **No** (code gated `NWP_ANALOG_ENABLED=False`) | **KILLED** (code retained) | 32.63% accuracy; code path exists behind env flag |
| 8 | hrrr_bias_corrected_signal | A | ACTIVE | **No** | IMPL-NOT-WIRED | HRRR 3km; needs HRRR data infra |
| 9 | spread_based_entry_signal | A | ACTIVE | **No** | IMPL-NOT-WIRED | Execution-layer; `evaluate_for_station` returns placeholder `(None, 0.0)` |
| 10 | metar_nowcast_signal | A | ACTIVE | **Yes** (`metar_nowcast`) | **LIVE** | Returns `(None, 0.5)` fresh / `(None, 0.0)` stale — deliberate confidence-only nowcast, NOT hollow |
| 11 | forecast_disagreement_signal | A | ACTIVE | **Yes** (`forecast_disagreement`) | **LIVE** | 64.36% |
| 12 | metar_trend_signal | A | ACTIVE | **Yes** (`metar_trend`) | **LIVE** | Day-over-day METAR high trend |
| 13 | radiational_cooling_signal | A | ACTIVE | **Yes** (`radiational_cooling`) | **LIVE** | LOW-only; v2026-08-06 |
| 14 | rdae_mos_signal | A | PARK | **No** | **PARKED** | RDAE MOS skeleton, not wired |
| 15 | cross_model_divergence_signal | B | ACTIVE | **Yes** (`cross_model_divergence`) | **LIVE** | `evaluate_for_station` has real NWP-divergence logic; `evaluate()` returns `(None,0.5)` neutral (documented). Fallback `(None,0.5)` is a guarded no-data branch, not hollow. |
| 16 | frontal_detector_signal | B | ACTIVE | **No** | IMPL-NOT-WIRED | Deprecated per `__init__.py` DISABLED note |
| 17 | frontal_passage_detector | B | ACTIVE | **No** | IMPL-NOT-WIRED | Alternative frontal algo |
| 18 | frontal_passage_intraday_signal | B | ACTIVE | **Yes** (`frontal_passage_intraday`) | **LIVE** | Sure Thing lane (Lane 2) |
| 19 | frontal_passage_nowcast_signal | B | ACTIVE | **No** | IMPL-NOT-WIRED | Used by `paper_trading_engine`/`signal_pipeline` via direct instantiation, but NOT in `SignalRegistry` |
| 20 | temperature_advection_signal | B | ACTIVE | **No** | IMPL-NOT-WIRED | Used by `paper_trading_engine`/`signal_pipeline` directly; needs live GFS data |
| 21 | pressure_delta_signal | B | ACTIVE | **Yes** (`pressure_delta`) | **LIVE** | 60.86% |
| 22 | pressure_tendency_signal | B | ACTIVE | **No** | IMPL-NOT-WIRED | `evaluate()` returns `(None,0.0)`; `evaluate_for_station` real. Not in registry. |
| 23 | warm_momentum_signal | B | ACTIVE | **No** | **IMPL-NOT-WIRED (backtest-only)** | `evaluate()` real; `evaluate_for_station()` HOLLOW stub. Marked backtest-only 2026-09-17. |
| 24 | cooling_failure_signal | B | ACTIVE | **No** | **IMPL-NOT-WIRED (backtest-only)** | `evaluate()` real; `evaluate_for_station()` HOLLOW stub. Marked backtest-only 2026-09-17. |
| 25 | ghcn_calibration_signal | B | PARK | **No** | **PARKED** | GHCN long-term bias calibration stub |
| 26 | sensor_bias_correction_signal | B | PARK | **No** | **PARKED** | METAR sensor bias correction stub |
| 27 | volume_momentum_signal | C | ACTIVE | **No** | IMPL-NOT-WIRED | Kalshi order-flow; needs execution data infra |
| 28 | settlement_arbitrage_signal | C | ACTIVE | **No** | IMPL-NOT-WIRED | Kalshi execution |
| 29 | spike_reversion_signal | C | ACTIVE | **No** | IMPL-NOT-WIRED | Removed from registry (49.85% negative EV, Fix 5) |
| 30 | fogr_reversion_signal | C | ACTIVE | **No** | IMPL-NOT-WIRED | FOGR reversion; needs HRRR/FOGR data |
| 31 | simple_trend_signal | C | ACTIVE | **No** | IMPL-NOT-WIRED | Price momentum trend |
| 32 | regime_signal | D | ACTIVE | **No** | IMPL-NOT-WIRED | Weather regime |
| 33 | dewpoint_depression_modulator | D | ACTIVE | **No** | IMPL-NOT-WIRED | Humidity → confidence modifier |
| 34 | cloud_cover_index_signal | D | ACTIVE | **Yes** (`cloud_cover_index`) | **LIVE** | WeatherAPI P0-1 |
| 35 | feels_like_delta_signal | D | ACTIVE | **Yes** (`feels_like_delta`) | **LIVE** | WeatherAPI P0-2 |
| 36 | wind_direction_shift | D | ACTIVE | **No** | IMPL-NOT-WIRED | 49.72% — removed from registry (below coin flip) |
| 37 | ai_composite_signal | D | GATED | **No** | **GATED** | ML composite, off by default |
| 38 | trajectory_state_vector_signal | D | PARK | **No** | **PARKED (HOLLOW stub)** | Fully hollow — both methods `(None,0.5)`. Marked PARKED 2026-09-17. Needs HYSPLIT/back-trajectory data. |
| 39 | weatherapi_base_signal | D | ACTIVE | **No** | IMPL-NOT-WIRED | WeatherAPI archive base (CCI/FeelsLike infra) |
| 40 | persistence_signal | E | KILLED | **No** | **KILLED** | 48.31% accuracy |
| 41 | goldilocks_signal | E | KILLED | **No** | **KILLED** | 49.85% negative EV |
| 42 | late_day_momentum | E | KILLED | **No** | **KILLED** | 48.31% |
| 43 | intraday_metar_confirmation | E | ORPHANED | **No** | **ORPHANED** | Replaced by METAR nowcast |
| 44 | intraday_metar_confirmation_signal | E | ORPHANED | **No** | **ORPHANED** | Replaced by METAR nowcast |
| 45 | nwp_dtdt_fusion_signal | E | ORPHANED | **No** | **ORPHANED** | Different model architecture |
| 46 | metar_dtdt_signal | E | ORPHANED | **No** | **ORPHANED** | Dead-end pipeline |
| 47 | dual_polarity_signal | E | ORPHANED | **No** | **ORPHANED** | Never wired (note: `CorrectedPressureDeltaSignal` IS imported from this file as a *different* class) |
| 48 | esdr_signal | E | ORPHANED | **No** | **ORPHANED** | Never wired |

---

## Summary Counts

| Final classification | Count | Names |
|---|---|---|
| **LIVE** (in `SignalRegistry`) | **12** | gaussian, gaussian_v2, calendar_climatology, ecmwf_bias_corrected, metar_nowcast, forecast_disagreement, metar_trend, radiational_cooling, cross_model_divergence, frontal_passage_intraday, cloud_cover_index, feels_like_delta |
| **IMPL-NOT-WIRED** (real logic, not in registry) | 18 | eighty_two_member_ensemble, nwp_direct, hrrr_bias_corrected, spread_based_entry, frontal_detector, frontal_passage_detector, frontal_passage_nowcast, temperature_advection, pressure_tendency, warm_momentum (backtest-only), cooling_failure (backtest-only), volume_momentum, settlement_arbitrage, spike_reversion, fogr_reversion, simple_trend, regime, dewpoint_depression_modulator, wind_direction_shift, weatherapi_base — *see note* |
| **PARKED** | 4 | rdae_mos, ghcn_calibration, sensor_bias_correction, trajectory_state_vector |
| **KILLED** | 4 | nwp_analog, persistence, goldilocks, late_day_momentum |
| **GATED** | 1 | ai_composite |
| **ORPHANED** | 6 | intraday_metar_confirmation (×2), nwp_dtdt_fusion, metar_dtdt, dual_polarity, esdr |
| **HOLLOW** (fully or station-path) | 3 | trajectory_state_vector (fully), warm_momentum (station-path), cooling_failure (station-path) |

> **Note on IMPL-NOT-WIRED count:** `dual_polarity_signal` (Family E, ORPHANED) is a special case — its *file* is imported (`CorrectedPressureDeltaSignal`), but the `dual_polarity_signal` *taxonomy entry* itself refers to the never-wired dual-polarity signal, so it stays ORPHANED. `warm_momentum` and `cooling_failure` are counted under IMPL-NOT-WIRED but carry an explicit "backtest-only" marker because their `evaluate_for_station()` is hollow.

---

## Hollow / Missing Implementations (final disposition)

| Signal | What's hollow | Disposition |
|---|---|---|
| `trajectory_state_vector_signal` | **FULLY hollow** — `evaluate()` AND `evaluate_for_station()` both `return None, 0.5` unconditionally (36-line file, no logic) | **PARKED** (already `status="PARK"` in taxonomy; docstring marker added 2026-09-17). Needs real upstream trajectory data. NOT wired. |
| `cooling_failure_signal` | `evaluate()` has real logic; `evaluate_for_station()` hollow `return None, 0.5` | **Backtest-only.** Docstring + method docstring marked 2026-09-17. Not wired to live loop. |
| `warm_momentum_signal` | Same: real `evaluate()`, hollow `evaluate_for_station()` | **Backtest-only.** Marked 2026-09-17. Not wired to live loop. |

**Not hollow (clarified vs audit):**
- `metar_nowcast_signal` — `evaluate_for_station` returns `(None, 0.5)` when METAR is *fresh* (within 2h) and `(None, 0.0)` when stale. This is a **deliberate confidence-only nowcast** (direction always None, confidence 0.5 means "neutral/no edge"). It IS wired and intentional. Not a hollow stub.
- `cross_model_divergence_signal` — `evaluate_for_station` has real NWP-divergence logic; its `(None, 0.5)` return is a **guarded no-data fallback** in `evaluate()` (documented as "cannot fire independently; use evaluate_for_station"). It IS wired. Not hollow.

---

## Code Changes Applied (minimal metadata cleanup)

1. **Duplicate key removed** — `nwp_analog_signal` appeared twice in `SIGNAL_REGISTRY` (Family A KILLED *and* Family E KILLED). The Family E duplicate was removed; the authoritative Family A entry (with `pool="nwp_direct"`, `module_path="core.signals"`) is retained. A comment note documents the dedup. Result: 48 unique keys (was 49).

2. **Stale docstring count fixed** — module header said "39 signals" and "Family A: 10 / B: 8 / C: 5 / D: 6 / E: 10". Corrected to the actual "48 signals" and "A: 14 / B: 12 / C: 5 / D: 8 / E: 9", plus an explicit note that this is a taxonomy layer (not the execution registry) pointing at this reconciliation doc.

3. **Explicit PARKED marker on the fully-hollow stub** — `trajectory_state_vector_signal.py` docstring now states `STATUS: PARKED`, clarifies it is a hollow stub not wired into `SignalRegistry`, and records the missing data dependency (HYSPLIT/back-trajectory). No logic was added.

4. **Backtest-only markers** — `cooling_failure_signal.py` and `warm_momentum_signal.py` docstrings + their `evaluate_for_station()` method docstrings now state "BACKTEST-ONLY — hollow stub, not wired to live loop."

**No signal logic was altered.** All changes are docstrings/comments and the removal of one duplicate dict key. No `evaluate()`/`evaluate_for_station()` return values changed.

---

## What Was NOT Done (explicitly out of scope)

- **No signal logic rewritten or fabricated.** The `trajectory_state_vector_signal` hollow stub was NOT given a fake implementation.
- **No calibration changes.** `core/calibration_pipeline.py` / `core/calibration/hourly_calibration.py` untouched.
- **No data/DB changes.**
- **No backtests run.**
- **No signals deleted** (including `nwp_analog_signal`, whose code path is still imported by `paper_trading_engine.py` behind `NWP_ANALOG_ENABLED=False`).
- **No commit** — staged/working-tree only (repo already has extensive pre-existing untracked/dirty state unrelated to this task).

---

## Remaining Risks / Follow-ups (for Gerri/Dan, not this task)

1. **Calibration surface split** (audit §2): the live `calibrate_all_signals.py` calibrates only the 12 live-wired signals. The 18 IMPL-NOT-WIRED + 4 PARKED signals calibrate nowhere. If any are meant to be promoted to live, they need (a) `evaluate_for_station()` implementation and (b) calibration coverage.
2. **`dual_polarity_signal` file naming** — the file `dual_polarity_signal.py` exports `CorrectedPressureDeltaSignal` (a live-used class) but the taxonomy entry `dual_polarity_signal` is ORPHANED. Naming collision worth a future cleanup (do not conflate the two).
3. **`retired/` directory scaffolding** — `core/signals/retired/` contains `persistence_signal.py`, `pressure_tendency_signal.py`, `settlement_arbitrage_signal.py`, `volume_momentum_signal.py`, but the *same files also still exist* in `core/signals/`. This is a half-finished cleanup (out of this task's scope, but should be resolved — either delete the retired copies or the active ones, not both).
4. **`nwp_analog_signal` dead-but-present code** — the signal is KILLED in taxonomy but `paper_trading_engine.py` still imports and conditionally uses it (gated by `NWP_ANALOG_ENABLED=False`). Not a bug, but a latent surface to keep flagged until the code path is fully retired.

---

## Verification

- `python3 -c "from core.signal_families import SIGNAL_REGISTRY"` → imports clean, **48 unique keys**, `nwp_analog_signal` appears exactly once.
- All three edited signal files import cleanly.
- No `evaluate()`/`evaluate_for_station()` return values changed (verified by diff review of the four edited files).
- Duplicate-key fix is the only functional change to `SIGNAL_REGISTRY` (48 keys vs 49).
