# Weather Engine — Completeness Audit + Reconciliation

**Date:** 2026-09-17 21:11 UTC
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Type:** Read-only audit. No code/data mutated. No commit.
**Purpose:** Certify whether the post-wipe recovery is COMPLETE across the full signal surface, and reconcile three known inconsistencies that gate calibration → Gray Room → SBOX.

---

## TL;DR — Headline Findings

| # | Section | Verdict | Headline |
|---|---|---|---|
| 1 | Signal completeness | **INCOMPLETE** | 49 signals in taxonomy (not 48, not the 39 the docstring claims). Only **12** are actually wired into the live registry. ~11 are unclassified by status. 1 fully-hollow stub, 4+ hollow station-path methods. |
| 2 | Calibration surface | **SPLIT** | LIVE path = `core/calibration_pipeline.py` + `core/calibration/hourly_calibration.py`. Historical `phase6/8/9/11` are dead search scripts. Calibration covers only ~12 signals, not 49. |
| 3a | Sharpe | **TWO FORMULAS** | Authoritative = **non-annualized `mean/std` on per-trade returns** (`pnl_tracking.compute_sharpe`, default `0.0`). Stale = annualized `(mean−0.5)/std·√252` (`multi_model_ensemble`) + `√n`-scaled win/loss (`phase6_combinatorial_search`). |
| 3b | Fees | **THREE FORMULAS** | Authoritative = **`core/fees.py`** `ceil(0.07·C·P·(1−P))/100` dollars (order-level ceil). Stale = `market_cost_model.ROUND_TRIP_FEE=0.0205` flat + `continuous_kelly`/`paper_trading_engine` returning **cents** (missing `/100`). |
| 3c | z8 | **UNDEFINED FORWARD REF (name collision)** | Two distinct `z8`s: (1) `scripts/z8_kalshi_market_cron.py` = real Kalshi snapshot collector; (2) `z8` in H1/H2 prereg = abstract hash-pinned provenance tier, explicitly "not yet a defined artifact". The H1/H2 reference is broken/forward-looking. |
| 4 | Market coverage | **SILENT DROP CONFIRMED** | z8 scrapes **18** stations; canonical universe = **20**. **KPHL and KSAT are silently dropped** from market snapshot capture. |

---

## 1. Signal Completeness

### Two registries — a structural inconsistency

The repo maintains **two distinct signal registries** that disagree with each other:

1. **`core/signal_families.py` → `SIGNAL_REGISTRY`** — a *taxonomy* of 49 entries (families A–E, pools, metadata). This is what the task calls "the 48 signals" (actually 49; and its own docstring says 39 — both are wrong).
2. **`core/signals/__init__.py` → `SignalRegistry` class** — the *live* registry of **12 actually-wired signals**. This is what `calibrate_all_signals.py` imports (`from signals import SignalRegistry`).

`signal_families.py` is imported by `platt_calibration.py`, `signal_fusion.py`, `additive/historical_adapter.py`, `migrate_calibration_v3.py`, `sweep_engine.py`, `big_sweep.py` — but it is a **taxonomy/metadata** layer, not an execution registry. The live execution registry is `core/signals/__init__.py`.

### Registry enumeration (49 entries in `SIGNAL_REGISTRY`)

| Signal | Family | Status | Notes |
|---|---|---|---|
| eighty_two_member_ensemble_signal | A | ACTIVE | |
| gaussian_signal | A | ACTIVE | |
| gaussian_v2_signal | A | ACTIVE | |
| calendar_climatology_signal | A | ACTIVE | |
| ecmwf_bias_corrected_signal | A | ACTIVE | |
| nwp_direct_signal | A | ACTIVE | |
| nwp_analog_signal | A | **KILLED** | (dup — see below) |
| hrrr_bias_corrected_signal | A | ACTIVE | |
| spread_based_entry_signal | A | ACTIVE | |
| metar_nowcast_signal | A | ACTIVE | |
| forecast_disagreement_signal | A | ACTIVE | |
| metar_trend_signal | A | ACTIVE | |
| radiational_cooling_signal | A | ACTIVE | |
| rdae_mos_signal | A | **PARK** | |
| cross_model_divergence_signal | B | ACTIVE | |
| frontal_detector_signal | B | ACTIVE | |
| frontal_passage_detector | B | ACTIVE | |
| frontal_passage_intraday_signal | B | ACTIVE | |
| frontal_passage_nowcast_signal | B | ACTIVE | |
| temperature_advection_signal | B | ACTIVE | |
| pressure_delta_signal | B | ACTIVE | |
| pressure_tendency_signal | B | ACTIVE | |
| warm_momentum_signal | B | ACTIVE | |
| cooling_failure_signal | B | ACTIVE | |
| ghcn_calibration_signal | B | **PARK** | |
| sensor_bias_correction_signal | B | **PARK** | |
| volume_momentum_signal | C | ACTIVE | |
| settlement_arbitrage_signal | C | ACTIVE | |
| spike_reversion_signal | C | ACTIVE | |
| fogr_reversion_signal | C | ACTIVE | |
| simple_trend_signal | C | ACTIVE | |
| regime_signal | D | ACTIVE | |
| dewpoint_depression_modulator | D | ACTIVE | |
| cloud_cover_index_signal | D | ACTIVE | |
| feels_like_delta_signal | D | ACTIVE | |
| wind_direction_shift | D | ACTIVE | |
| ai_composite_signal | D | **GATED** | |
| trajectory_state_vector_signal | D | **PARK** | fully hollow |
| weatherapi_base_signal | D | ACTIVE | |
| persistence_signal | E | **KILLED** | |
| goldilocks_signal | E | **KILLED** | |
| late_day_momentum | E | **KILLED** | |
| nwp_analog_signal | E | **KILLED** | (dup of Family A entry) |
| intraday_metar_confirmation | E | **ORPHANED** | |
| intraday_metar_confirmation_signal | E | **ORPHANED** | |
| nwp_dtdt_fusion_signal | E | **ORPHANED** | |
| metar_dtdt_signal | E | **ORPHANED** | |
| dual_polarity_signal | E | **ORPHANED** | |
| esdr_signal | E | **ORPHANED** | |

### Classification breakdown

| Class | Count | Names |
|---|---|---|
| ACTIVE (default, no explicit status) | **36** | all non-marked entries |
| KILLED | 5 | persistence, goldilocks, late_day_momentum, nwp_analog (×2) |
| PARK | 4 | rdae_mos, ghcn_calibration, sensor_bias_correction, trajectory_state_vector |
| GATED | 1 | ai_composite |
| ORPHANED | 6 | intraday_metar_confirmation (×2), nwp_dtdt_fusion, metar_dtdt, dual_polarity, esdr |

**Note on the ~11 "unclassified" signals flagged in the task:** the registry uses `status="ACTIVE"` as the dataclass **default**, so ~36 signals have *no explicit* `status=` line — they are "ACTIVE by default," not explicitly classified. The genuinely *ambiguous* cases are the ones marked **PARK** (4) and **GATED** (1) that have no active code path, plus the duplicate `nwp_analog_signal` (appears in both Family A KILLED and Family E KILLED — a registry bug, not a signal).

### Duplicate entry

`nwp_analog_signal` appears **twice** in `SIGNAL_REGISTRY` (Family A and Family E, both KILLED). This is a dict, so the second overwrites the first — the Family A entry (with pool="nwp_direct") is silently lost. This is why the count is 49 keys but effectively 48 unique names.

### Hollow / missing implementations

| Signal | Method | Verdict |
|---|---|---|
| `trajectory_state_vector_signal` | `evaluate()` + `evaluate_for_station()` | **FULLY HOLLOW** — 36-line file, both methods `return None, 0.5` unconditionally. No logic. |
| `cooling_failure_signal` | `evaluate_for_station()` | **HOLLOW** — `evaluate()` has logic, but the station-path always `return None, 0.5`. Never fires live. |
| `warm_momentum_signal` | `evaluate_for_station()` | **HOLLOW** — same pattern: real `evaluate()`, station-path `return None, 0.5`. |
| `cross_model_divergence_signal` | (fallback path) | Returns `(None, 0.5)` in no-data branch — guarded, but worth flagging. |
| `metar_nowcast_signal` | (fallback path) | Returns `(None, 0.5)` in no-data branch — guarded. |

**Key insight:** Many Family B/C/D "ACTIVE" signals implement `evaluate(idx, days)` (backtest path) but leave `evaluate_for_station()` as a hollow `return None, 0.5`. Since the live hourly loop calls `evaluate_for_station()`, these signals **silently contribute nothing** in production despite being "ACTIVE" in the taxonomy.

---

## 2. Calibration Surface

### Live path vs historical scripts

**LIVE (authoritative):**
- `core/calibration_pipeline.py` — walk-forward isotonic regression (`CalibrationPipeline`). This is the canonical calibration core.
- `core/calibration/hourly_calibration.py` — `HourlyCalibrationLoader`, applies per-station/hour JSON curves + NWS revision correction. This is the **live runtime** path.
- `core/calibrate_all_signals.py` — the driver; imports `from signals import SignalRegistry` (the 12-signal live registry), not the 49-entry taxonomy.

**HISTORICAL / DEAD (search scripts, not live):**
- `scripts/phase6_calibrated_search.py`, `phase6_calibration_pipeline.py`, `phase6_clean_combinatorial_search.py`, `phase6_combinatorial_search.py`, `phase6_parameter_sweep.py`, `phase6_validate_calibration.py`
- `scripts/phase8_calibrated_search.py`, `phase8_combinatorial_search.py`, `phase8_parameter_sweep.py`, `phase8_purged_cv.py`
- `scripts/phase9_calibrated_search.py`, `phase9_combinatorial_search.py`, `phase9_combinatorial_search_fast.py`, `phase9_purged_cv.py`
- `scripts/phase11/`, `scripts/phaseB_calibration_pipeline.py`, etc.

These are **parameter/combinatorial search harnesses** from earlier development phases. They are not imported by the live pipeline and are effectively archived (though not moved to `scripts/archive/`).

### Coverage gap

`calibrate_all_signals.py` calibrates only the signals in `SignalRegistry` (~12: gaussian, gaussian_v2, pressure_delta, forecast_disagreement, calendar_climatology, cloud_cover_index, feels_like_delta, metar_trend, cross_model_divergence, frontal_passage_intraday, radiational_cooling, + others per registry).

**Result:** the 49-entry taxonomy vs 12-entry live registry means **~37 taxonomy signals calibrate nowhere** in the live pipeline. Specifically the Family B/C/D modulators and microstructure signals (volume_momentum, settlement_arbitrage, spike_reversion, fogr_reversion, simple_trend, regime, dewpoint, wind_direction_shift, etc.) have **no live calibration coverage** — they either fall back to `(None, 0.5)` or are gated/parked.

### Signals that calibrate nowhere (explicit flag)

All 37 taxonomy-only signals. The most material (named ACTIVE but with no calibration + hollow station path):
- `volume_momentum_signal`, `settlement_arbitrage_signal`, `spike_reversion_signal`, `fogr_reversion_signal`, `simple_trend_signal` (Family C)
- `temperature_advection_signal`, `pressure_tendency_signal`, `frontal_detector_signal`, `frontal_passage_detector`, `frontal_passage_nowcast_signal`, `warm_momentum_signal`, `cooling_failure_signal` (Family B)
- `regime_signal`, `dewpoint_depression_modulator`, `wind_direction_shift`, `weatherapi_base_signal` (Family D)

---

## 3. Three Critical Reconciliations

### 3a. SHARPE — two (actually three) conflicting formulas

**Authoritative (binding):** `core/pnl_tracking.py::compute_sharpe()` (lines 697–729)

```python
returns = [t.get('pnl', 0) / max(abs(t.get('position_size_usd', 100)), 1) for t in trades]
mean_return = statistics.mean(returns)
std_return = statistics.stdev(returns) if len(returns) > 1 else 0.01
sharpe = mean_return / std_return          # NON-ANNUALIZED
# default 0.0 if < 2 trades
```

This is the **non-annualized** per-trade `mean/std` formula. It is the one wired to the production gate: `paper_trader.get_sharpe()` → `compute_sharpe(self, trades)`, and `production_gate.meets_requirements(..., sharpe=...)` compares against `MIN_SHARPE = 1.0`.

**Stale/conflicting formulas:**

1. `core/paper_trading_engine.py::compute_sharpe()` (line 2953) — **nearly identical** to `pnl_tracking` (non-annualized `mean/std` on per-trade returns) **BUT defaults to `1.0` on insufficient data** vs `pnl_tracking`'s `0.0`. This is a **default-value divergence**, not a formula divergence — but it matters: two identical-looking functions disagree on the cold-start return, and `paper_trading_engine` will *report Sharpe 1.0* (passing the gate) when there is no data, while `pnl_tracking` reports 0.0 (failing).

2. `core/multi_model_ensemble.py` (line 686) — **annualized, mean-referenced**:
   ```python
   sharpe = (mean_w - 0.5) / std_w * math.sqrt(252)
   ```
   This treats win/loss as a binary with reference 0.5 and annualizes by √252. **Different formula entirely** — produces values an order of magnitude larger than the non-annualized version.

3. `scripts/phase6_combinatorial_search.py::compute_sharpe()` (line 551) — **√n-scaled win/loss**:
   ```python
   expected_return = win_rate*1.0 + loss_rate*(-1.0)
   sharpe = expected_return / sqrt(variance) * sqrt(total)
   ```
   A sample-size-normalized binomial Sharpe. Also stale (phase-6 search harness).

4. `core/pnl_dashboard.py::_compute_sharpe()` (line 34) — **annualized** `(mean − rf)/std` with comment "scale by sqrt(252)" but the `sqrt(252)` is **actually omitted** in the return (docstring says annualized, code returns raw `mean/std`). Another inconsistency (doc vs code).

5. `core/unified_backtest.py::compute_sharpe()` (line 106) — **gross-minus-fee** Sharpe on (confidence, correct) pairs, not P&L-based.

**Verdict:** The **binding gate** must be computed by **exactly one formula**. The authoritative one is `pnl_tracking.compute_sharpe()` (non-annualized `mean/std`, default 0.0) because it is the path feeding `production_gate`. All other formulas are stale/divergent and MUST NOT be used for the `Sharpe ≥ 1.0` gate. **Immediate risk:** `paper_trading_engine.compute_sharpe` returns `1.0` on cold start (vs `0.0`), which can falsely satisfy `MIN_SHARPE=1.0`. These two near-duplicate functions must be reconciled to a single source.

### 3b. FEES — three conflicting models

**Authoritative (binding, correct for executable edge):** `core/fees.py`

```python
def kalshi_fee(contracts, price):
    return math.ceil(0.07 * price * (1.0 - price) * contracts) / 100.0   # DOLLARS, order-level ceil
```

This is the documented "single source of truth": `ceil(0.07·C·P·(1−P))` **cents**, returned in **dollars** (`/100`), with `ceil()` applied to the **whole order** (not per contract). This is the correct Kalshi taker fee for executable edge.

**Stale/conflicting:**

1. `core/market_cost_model.py` — has **two** problems:
   - `ROUND_TRIP_FEE = (MEASURED_MEAN_SPREAD / 2.0) + DEFAULT_COMMISSION + DEFAULT_SLIPPAGE = 0.031/2 + 0 + 0.005 = 0.0205` — a **flat 2.05%** round-trip cost (spread+slippage model, NOT the taker fee). This is a *different concept* (transaction cost including spread) but is named `ROUND_TRIP_FEE` and imported by `pnl_tracking`, `position_sizer`, `kelly_position_sizer`, `fee_aware_kelly`, `unified_backtest` as if it were the taker fee.
   - Its own `kalshi_fee()` method (line 111) uses **per-contract** ceil: `ceil(0.07·P·(1−P)·100)/100` per contract, then `× contracts`. This differs from `core/fees.py` (order-level ceil) and produces a different result.

2. `core/continuous_kelly.py::kalshi_fee()` (line 107) — **returns CENTS, not dollars**:
   ```python
   return math.ceil(KALSHI_FEE_RATE * contracts * price * (1.0 - price))   # NO /100
   ```
   Off by 100× from `core/fees.py`.

3. `core/paper_trading_engine.py` (line 1924) — **returns CENTS, not dollars**:
   ```python
   per_side_fee = math.ceil(0.07 * quantity * effective_price * (1.0 - effective_price))  # NO /100
   round_trip_fee = per_side_fee * 2
   ```
   Off by 100×. This is in the live paper trading fill path.

4. `core/fee_aware_filter.py` — `fee_amount = 0` with comment "No commission charged by Kalshi" (line 99), while elsewhere commission is modeled as `0.07·P·(1−P)`. The header even states "~5% on profits" and "commission_rate" is a constructor param.

**Verdict:** The **correct fee model for executable edge** is `core/fees.py::kalshi_fee()` (order-level `ceil(0.07·C·P·(1−P))/100` dollars). The `0.0205` flat `ROUND_TRIP_FEE` is a spread/slippage transaction-cost model, not the taker fee — it is a *different metric* being conflated with the fee. The `continuous_kelly` and `paper_trading_engine` implementations are **off by 100×** (missing `/100`), which is a material bug in live P&L/edge computation.

### 3c. z8 — undefined forward reference (name collision)

There are **two distinct `z8` entities** sharing the label, and they are **not the same thing**:

1. **`scripts/z8_kalshi_market_cron.py`** — a real, working cron script ("Z.8 — Kalshi market snapshot collector"). Captures bid/ask/mid/volume/OI for Kalshi weather markets into `data/kalshi_market_snapshots.db`, hourly (`30 * * * *`). This is the **Kalshi market snapshot capture**.

2. **`z8` referenced in `docs/plans/H1-H2-PREREGISTRATION-2026-09-17.md`** — an **abstract stability/provenance data tier**, defined as:
   > "A point-in-time-frozen, provenance-tagged, corruption-audited dataset whose identity is pinned by content hash (sha256)…"

   The prereg doc **explicitly states**: "At the time of this preregistration, `z8` is **not yet a defined, verified artifact** in the repository — it is a forward reference to a stability/provenance tier that must be established before either hypothesis is executable."

**What z8 is supposed to be:** The H1/H2 hypotheses (weather prediction + volume/order-flow) both name a shared data dependency `z8` that must be a **hash-pinned, frozen, provenance-tagged dataset** (a "lineage freeze" tier). It is NOT the cron script. The cron script happens to be labeled "Z.8" but produces a *live, mutating* DB — the opposite of the frozen/hash-pinned `z8` tier the hypotheses require.

**Is the reference broken?** Yes, in the sense that:
- The H1/H2 `z8` reference is **forward-looking and undefined** — no artifact exists yet, both hypotheses are explicitly "PARKED at the execution layer."
- The concrete `z8_kalshi_market_cron.py` exists and runs, but is a *different* `z8` (live snapshot collector), and does **not** satisfy the hash-pinned provenance requirement.
- The naming collision is a latent hazard: a reader could mistake the running cron's output for the required frozen tier.

---

## 4. Market Coverage — silent drop CONFIRMED

**Directive:** "Every market we scrape must be included everywhere, no silent drops."

### Scrape → store → use trace

1. **Scrape:** `scripts/z8_kalshi_market_cron.py` defines `STATION_SERIES` — **18 stations**.
2. **Store:** into `data/kalshi_market_snapshots.db`.
3. **Use:** consumed by settlement/edge/order-flow logic.

### The drop

**Canonical station universe = 20 stations** (from `core/calibrate_all_signals.py::STATIONS`, `core/metar_monitor.py` default stations, `core/station_registry.py`):

```
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

**z8 scrapes only 18:**

```
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW KMIA KMSP KMSY KNYC KOKC KPHX KSEA KSFO
```

**MISSING from z8: `KPHL` (Philadelphia) and `KSAT` (San Antonio).**

This is a **silent drop**: both stations are in the canonical universe (and in `station_registry.py` with Kalshi price-fetcher mappings), but the market-snapshot collector never scrapes them. Any consumer relying on `kalshi_market_snapshots.db` for full-market coverage will be missing KPHL and KSAT without any error or warning.

**Note:** the ingestion-coverage manifest (`docs/plans/INGESTION-COVERAGE-MANIFEST-2026-09-17.md`) records Kalshi settlements covering **21 stations** (anomalously *more* than the canonical 20 — flagging the settlement source may include an extra station or a phantom), while z8 snapshots cover 18. There is no single reconciled station universe.

### Other coverage caveats (from prior ingestion manifest)

- Daily LOOP producer was **missing** (silent since 2026-09-06) → **rebuilt** (`scripts/daily_loop_producer.py`).
- GEFS archive **stale** (2 days), ECMWF bias **stale** (~1 day).
- `WE-auth-verification` and weekly-backup crons in **error** state.

---

## Bottom Line

**The post-wipe recovery is NOT complete.** Specifically:

1. **Signal surface** — 49 taxonomy entries vs 12 live-wired signals; 1 fully-hollow stub; 4+ hollow station-path methods; a duplicate registry key; a wrong docstring count (says 39).
2. **Calibration** — live path covers ~12 signals, not the 49-entry taxonomy; ~37 signals calibrate nowhere.
3. **Sharpe** — two (three) formulas; the binding gate must use `pnl_tracking.compute_sharpe` (non-annualized, default 0.0). `paper_trading_engine` defaults to 1.0 (false pass risk).
4. **Fees** — three models; `core/fees.py` is correct (order-level ceil, dollars). `continuous_kelly` + `paper_trading_engine` are off 100× (missing `/100`); `market_cost_model.ROUND_TRIP_FEE=0.0205` is a different metric (spread cost, not taker fee).
5. **z8** — undefined forward reference (provenance tier), colliding with the real `z8_kalshi_market_cron.py` snapshot collector.
6. **Market coverage** — KPHL + KSAT silently dropped from market snapshot scrape (18 vs 20).

**No fixes applied.** This is a certification-only audit. Each flagged item is a concrete reconciliation decision for the next (calibration/Gray Room/SBOX) stage.
