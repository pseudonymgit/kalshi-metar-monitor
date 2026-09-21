# UPSTREAM CRAWL — Weather-Engine Test Suite Input Audit

- **Date:** 2026-09-21
- **Scope:** Every input that feeds `scripts/ultimate_sweep.py` (the R/T-1 canonical suite)
- **Method:** Read-only, script-only, no AI-in-loop, no data mutation. Independent verification against actual files/DBs, NOT re-confirmation of Gilfoyle's claims.
- **Author:** Miles O'Brien (independent audit, per Dan directive 9/21 03:04)

---

## TL;DR

The suite is fed by a **healthy ground-truth spine** but is surrounded by **three diverged signal registries, 13 missing real signals, 2 hard import breaks, and several orphaned/0-byte data paths**. The ground truth itself has a **missing station (KDAL)** and a **3-day stale front edge**. Details below.

---

## 1. Data Sources — status per input

| Input | File | Size | Rows | Status | Gap |
|---|---|---|---|---|---|
| METAR observations | `data/metar_backfill.db` → `metar_observations` | 2.56 GB | 12,840,837 | **HEALTHY** | ~77 stations, dates 2021-01-01 → 2026-09-21 |
| NWP forecasts | `data/nwp_forecasts.db` → `nwp_forecasts` | 331 MB | 943,231 | **HEALTHY** | feeds nwp_direct/analog/dtdt_fusion/cross_model/esdr/ai_composite |
| GEFS archive | `data/gefs_archive.db` → `gefs_archive` | 55 MB | 369,305 | **HEALTHY** | feeds eighty_two_member_ensemble |
| TIGGE archive | `data/tigge_archive.db` → `tigge_archive` | 57 MB | 182,100 | **HEALTHY** | feeds eighty_two_member_ensemble (ECMWF members) |
| ERA5 archive | `data/era5_archive.db` → `era5_archive` | 3.3 MB | 41,380 | **ORPHANED** | ZERO signal modules reference era5_archive.db. Confirmed removed-from-training, but the data is dead weight |
| GHCN settlements | `data/ghcn_settlements.db` → `ghcn_settlements` | 67 MB | 619,428 | **HEALTHY** | 21 stations, dates 1869→2026-09-12. Feeds rdae_mos + temp ladder |
| Kalshi snapshots | `data/kalshi_market_snapshots.db` → `market_snapshots` | 152 MB | 1,142,036 | **HEALTHY** | declared in suite but not directly read by scoring path |
| WeatherAPI | `data/weatherapi_archive.db` → `daily`/`hourly` | 315 MB | 121K / 2.4M | **HEALTHY** | feeds cloud_cover_index + feels_like_delta |

### 0-byte / dead data paths (flagged in dispatch — CONFIRMED)

| File | Status |
|---|---|
| `data/metar.db` | **0 rows (4096 bytes)** — dead path, NOT used by suite (suite uses metar_backfill.db) |
| `data/ghcn_daily.db` | **0 bytes** — dead, dispatch-flagged |
| `data/settlement_truth.db` | **0 bytes** — dead; real truth lives inside metar_backfill.db as table `settlement_truth` |
| `data/settlements.db` | **0 bytes** — dead |
| `data/kalshi_settlement.db` | **0 bytes** — dead |
| `data/kalshi.db` | **0 bytes** — dead |
| `data/ecmwf_ensembles.db` | **0 bytes** — dead |
| `kalshi_market_snapshots.db` (repo root) | **0 bytes** — dead duplicate; real one is under `data/` |

These are **orphaned scaffolding**, not suite inputs. They do NOT break the suite (the suite reads the correct paths), but they are silent dirty state that should be removed/gitignored.

---

## 2. Signal Modules & DB Dependencies — THREE diverged registries

**This is the headline finding.** There is no single source of truth for "which signals exist." Three parallel registries have drifted apart:

### Registry A — `scripts/ultimate_sweep.py::build_signal_registry()` (the "canonical" R/T-1 suite)
- **43 signals enumerated, 43 reachable (dry-run).** No import failures.

### Registry B — `core/signal_families.py::SIGNAL_REGISTRY` (49 signals)
- Includes 7+ signals Registry A does NOT: `rdae_mos_signal`, `ghcn_calibration_signal`, `sensor_bias_correction_signal`, `simple_trend_signal`, `weatherapi_base_signal`, `microstructure_spike_reversion`, `late_day_momentum`, `persistence_signal`, `hrrr_bias_corrected_signal`, `ecmwf_bias_corrected_signal`.

### Registry C — `core/signals/__init__.py::SignalRegistry` (only ~14 active signals)
- Used by `unified_backtest.py`, `l2_shared.py`, phase1/6/8 scripts.
- **Explicitly REMOVES** `wind_direction_shift` (49.72%), `spike_reversion` (49.85%), `goldilocks` as "negative EV" — but Registry A still scores all three.

**Consequence:** the same signal name resolves to different live/dead states depending on which script imports it. `ultimate_sweep.py` scores `wind_direction_shift` and `spike_reversion`; the phase pipelines (via `core.signals`) treat them as removed.

### 13 real signal classes MISSING from the canonical suite (Registry A)

Verified against actual `.py` files — these have `BaseSignal` subclasses but are **not** in `build_signal_registry()`:

| # | Module | Signal Class | Notes |
|---|---|---|---|
| 1 | `rdae_mos_signal` | `RDAEMOSSignal` | **65% paper/settlement accuracy** (per MEMORY). Flagship next-day predictor. Absent from suite. |
| 2 | `asos_microstructure_signal` | `AsosMicrostructureSignal` | Real signal |
| 3 | `ecmwf_bias_corrected_signal` | `ECMWFBiasCorrectedSignal` | Referenced in `SIGNALS_NEEDING_FOR_STATION` set but NOT registered |
| 4 | `ghcn_calibration_signal` | `GHCNCalibrationSignal` | Real signal, GHCN-backed |
| 5 | `honest_trend_signal` | `HonestTrendSignal` | Real signal |
| 6 | `hrrr_bias_corrected_signal` | `HRRRBiasCorrectedSignal` | Real signal |
| 7 | `persistence_signal` | `PersistenceSignal` | Referenced in `STANDARD_EVALUATE_SIGNALS` set but NOT registered |
| 8 | `sensor_bias_correction_signal` | `SensorBiasCorrectionSignal` | Real signal |
| 9 | `simple_trend_signal` | `SimpleTrendSignal` | Real signal |
| 10 | `subhourly_trajectory_signal` | `SubhourlyTrajectorySignal` | Registered in Registry C for scoring only |
| 11 | `temperature_trend_signal` | `TemperatureTrendSignal` | Real signal |
| 12 | `upper_atmosphere_signal` | `EightHundredFiftyMbAdvectionSignal` + `FiveHundredMbHeightPatternSignal` | **TWO signals** — the data-gated 850mb/500mb pair. The thin re-export stubs (`500mb_height_pattern_signal.py`, `850mb_temp_advection_signal.py`) exist precisely to resolve these names, but the registry never calls them. |
| 13 | `weatherapi_base_signal` | `WeatherAPIBaseSignal` | Abstract base — not a tradable signal, but still unregistered |

**Count:** 12 modules, but `upper_atmosphere_signal` carries 2 signals and `weatherapi_base_signal` is abstract → **~13 genuinely-missing tradable signals**, matching Dan's "13+ real signals missing."

### Helper modules not in registry (correctly excluded — not signals)
`dewpoint_depression_modulator` (modulate_confidence func), `frontal_passage_detector` (evaluate func) — these are registered via `_reg_func` so they ARE covered. `intraday_metar_confirmation` (no `_signal` suffix) is a duplicate of `intraday_metar_confirmation_signal`.

---

## 3. Calibration Inputs

| Input | Location | Status |
|---|---|---|
| Date-tiered temp ladder (GHCN → Kalshi finalized → WeatherAPI) | `settlement_truth.temp_source` | **HEALTHY** — 14,023 ghcn + 79 kalshi_finalized + 162 weatherapi = 14,264 |
| Sample floor | `core/calibration_pipeline.py::MIN_SAMPLES = 200` | **HEALTHY** |
| Empirical Bayes shrinkage | `core/bma_emos.py` (shrinkage_alpha, ridge L2) | **HEALTHY** |
| Calibration cache | `data/signal_cache.db` → `signal_cache` (353,735 rows) | **HEALTHY** |
| Strike ladder | `data/kalshi_strike_ladder.db` → `strike_ladder` (85,846 rows) | **HEALTHY** |

---

## 4. Ground-Truth Source — `settlement_truth.strike_price`

| Metric | Value | Status |
|---|---|---|
| Total `settlement_truth` rows | 1,239,559 | — (most have `strike_price=NULL`) |
| Valid rows (temp + strike non-NULL) | **14,264** | ⚠️ Docstring claims **14,305** |
| strike_type distribution | greater=10,139 / less=4,112 / between=54 | 14,305 = 10,139+4,112+54 (counts "between") |
| trustworthy=0 (excluded) | 80 | correctly excluded |
| Valid distinct stations | 20 | ⚠️ see KDAL gap |

### Discrepancies / gaps

1. **Docstring number mismatch.** `ultimate_sweep.py` docstring + report say "14,305 rows" but the actual valid-label count is **14,264** (14,305 minus 41 rows where `settlement_temp_f IS NULL`). The suite's `load_labels()` reports 14,130 after excluding 54 between + 80 untrusted. This is a cosmetic-but-real provenance drift in the header.

2. **KDAL is dead.** `settlement_truth` contains **63,614 rows for station KDAL** but ALL have `strike_type=NULL`, `settlement_temp_f=NULL`, `strike_price=NULL` → **0 valid labels.** KDAL has 1,700 METAR dates in `metar_observations` but no ground truth. It is silently excluded because `STATIONS` (20) omits it. If KDAL was ever intended as a 21st station, its settlement backfill never completed.

3. **3-day stale front edge.** Ground-truth `settlement_truth` ends **2026-09-18**, but METAR observations extend to **2026-09-21**. The most recent 3 days of signal data have no settlement truth to score against — the suite cannot evaluate current signals.

4. **Shallow truth history (per-station variance).** Truth depth is highly uneven:
   - KATL: truth starts 2026-02-04 (224 dates) — only ~7 months
   - KDEN: truth starts 2024-11-20 (665 dates)
   - KMDW/KNYC: truth starts 2022-12-22 (1,363 dates)
   - All 20 stations have METAR back to 2021-01, so the truth is far shallower than the signal data for most stations. This is a data-availability constraint, not a bug, but it caps the training window.

---

## 5. Hard Import Breaks (blockers)

1. **`REGISTERED_SIGNAL_NAMES` / `DEGENERATE_SIGNAL_NAMES` are undefined.**
   - Imported by `scripts/l2_shared.py:42`, `tests/test_signal_repairs_acceptance.py`, `tests/test_degenerate_selector.py`, `tests/test_failure_accounting_reconciliation.py`.
   - Verified: `from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES` → **`ImportError: cannot import name 'REGISTERED_SIGNAL_NAMES'`**.
   - These names are NOT defined in `core/signals/__init__.py` (141 lines, no such symbols) or anywhere else in `core/`.
   - **Impact:** the L2 intraday loop (`l2_shared.py`) and 3+ test files crash on import. This is a real broken upstream path.

2. **`test_signal_repairs_acceptance.py` asserts signals the suite doesn't register.**
   - Expects `eighty_two_member_ensemble`, `temperature_trend`, `asos_microstructure` in `REGISTERED_SIGNAL_NAMES`.
   - These map to the missing-registry problem above; the tests describe an intended state that Registry A never achieved.

---

## 6. Summary of Gaps (ranked)

| # | Severity | Gap |
|---|---|---|
| 1 | **BLOCKER** | `REGISTERED_SIGNAL_NAMES`/`DEGENERATE_SIGNAL_NAMES` undefined → `l2_shared.py` + tests crash |
| 2 | **HIGH** | 13 real signals missing from canonical suite registry (`rdae_mos` = 65% flagship among them) |
| 3 | **HIGH** | Three diverged registries (A/B/C) — no single source of truth; same signal is live/dead depending on importer |
| 4 | **MEDIUM** | KDAL: 63,614 dead rows, no settlement backfill, silently dropped from STATIONS |
| 5 | **MEDIUM** | 3-day stale truth front edge (settlement ends 9-18, METAR to 9-21) |
| 6 | **LOW** | Docstring "14,305" vs actual "14,264" (41 rows with temp NULL) — provenance drift |
| 7 | **LOW** | ERA5 archive (41K rows) orphaned — zero consumers, dead weight |
| 8 | **LOW** | ~10 orphaned 0-byte `.db` files (scaffolding, not suite inputs) |

---

## 7. What is NOT broken (verified healthy)

- Ground-truth axis (`strike_crossing_direction`) — correct strike-crossing, not day-over-day.
- METAR, NWP, GEFS, TIGGE, GHCN, Kalshi, WeatherAPI DBs all present and non-empty.
- Calibration floor (200) + shrinkage infrastructure reachable.
- `eighty_two_member_ensemble` gefs path (`gefs_archive.db`) resolves correctly (the `gefs.db` string is a fallback comment, not a live ref).
- The suite's 43 registered signals all instantiate without error in dry-run.

---

*Read-only audit. No data mutated. No signal re-enabled/wired.*
