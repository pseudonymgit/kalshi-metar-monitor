# Pipeline Trace: Full A-to-Z — Data → Signals → Fusion → Calibration → Eval → Ranking → Report

**Expert:** Pipeline Trace (A-to-Z, post-build)
**Date:** 2026-08-14 (UTC)
**Method:** Direct DB inspection (sqlite3) + `inspect.getsource()` audit on all 17 signals + pytest test results against HEAD `48587dfab`.

---

## Verdict Summary

| Stage | Input Source | Output | Status | Evidence |
|-------|-------------|--------|--------|----------|
| **1a. Kalshi Settlements** | Kalshi API (3 live + events backfill) | 9,507 rows → 7,872 non-null temp; 20 stations + TEST | ✅ **REAL** | `data/kalshi_settlements.db`; `ws5_common.py:26-94` |
| **1b. METAR Observations** | AWC TGTFP, IEM ASOS, NOAA ISD-Lite, NWS API, WeatherAPI | 12,822,456 rows; 73 stations; 2021-01-01→2026-08-14 | ✅ **REAL** | `data/metar_backfill.db`; `ws5_common.py:295-345` |
| **1c. Station Alignment** | Static hardcoded `_RESEARCH_STATION_CODES` | 20 stations (KATL..KSFO) | ⚠️ **PARTIAL** (static fallback is primary) | `core/station_registry.py:141-168` |
| **2. Station/Date Alignment** | `settlements[station][date]` ↔ `metar_date_to_idx[today]` | Per-station per-date alignment | ✅ **REAL** | `ws5_common.py:418-460` |
| **3. Active Registry** | `SignalRegistry + get_signal(name)` | 17 signals verified | ✅ **REAL** (S5 test) | `run_sweep.py:391-400` |
| **4. Signal Evaluation** | `signal.evaluate(metar_idx, days)` | (direction, confidence) | ✅ **REAL** — ALL 17 signals history-only | Source audit: ZERO `days[idx]` weather reads |
| **5. Calibration (Isotonic)** | PAVA isotonic per (signal × station) | calibrated probabilities | ✅ **REAL** — correct train/eval split, clean inputs | `calibration_pipeline.py:209-260` |
| **6. Fusion** | LLOP (weighted log-odds) or UWC (Beta-binomial) | fused (direction, confidence) | ✅ **REAL** — both methods structurally sound | `fusion_logic.py:530-660`; `signal_fusion.py:78-200` |
| **7. Eval Folds** | 4 nested folds, contiguous windows | per-fold accuracy, mean accuracy | ✅ **REAL** (S2, E2E-AT1 tests) | `run_sweep.py:69-127` |
| **8. Outer Holdout** | [2026-05-15, 2026-08-13], Fold 4 holdout | final accuracy + (PROXY) P&L | ✅ **REAL** (E2E-AT3 test) | `run_sweep.py:85-86, 314-333` |
| **9. Baselines** | Always-UP, Always-DOWN | comparison accuracies | ✅ **REAL** | `ws5_common.py:556-605` |
| **10. Per-Signal / Per-Station** | accuracy, fire_rate, brier_raw, brier_cal | per-signal/+station stats | ✅ **REAL** (S3, E2E-AT6 tests) | `ws5_common.py:498-555` |
| **11. Proxy P&L** | `compute_pnl_metrics()` | (PROXY) disclaimer + 7 keys | ✅ **REAL** (S7, E2E-AT7 tests) | `ws5_common.py:610-690` |
| **12. JSON Report** | `sweep_results.json` | structured output + provenance | ✅ **REAL** | `run_sweep.py:430-460` |

---

## §1. Data Sources — Schema, Row Counts, Date Ranges

### 1a. Kalshi Settlements
- **DB:** `data/kalshi_settlements.db`
- **Table:** `kalshi_settlements` (9,507 rows)
  - Non-null temp: 7,872 rows
  - Date range: 2021-08-19 → 2026-08-11
  - Distinct stations: 21 (20 trading + TEST)
  - Source types: `historical_api` (7,097), `finalized` (2,410)
  - Dedup priority: `finalized(0) > historical_api(1) > live_api(2)`
- **Evidence:** `sqlite3` query: `SELECT MIN/MIN(target_date), COUNT(*), COUNT(DISTINCT station) FROM kalshi_settlements`

### 1b. METAR Observations
- **DB:** `data/metar_backfill.db` (2.4 GB)
- **Table:** `metar_observations` (12,822,456 rows, 73 stations, 2021-01-01→2026-08-14)
- **Table:** `daily_stats` (58,064 rows, 30 stations)
- **Primary load path:** `load_daily_stats_batched()` (ws5_common.py:295-345) — single query, `GROUP BY station, date_utc`
- **Evidence:** `sqlite3` query: 20 trading stations each ~49K–50K rows

### 1c. Station Registry
- **Primary:** Static `_RESEARCH_STATION_CODES` list (20 stations)
- **STATIONS** (config.py:20-25): `["KATL"... "KSFO"]`
- **Status:** The 20 stations are real and match settlement data, but not series-based discovery.

---

## §2. Station/Date Alignment

The evaluation loop (ws5_common.py:437-440) aligns by:
```python
metar_idx = metar_date_to_idx[today]  # target day index in METAR list
signal.evaluate(metar_idx, metar_days)  # evaluate with target index
```

**CRITICAL FINDING:** The target-day index is passed to signals, but all 17 signals use history-only sub-indices (`idx-1`, `idx-2`, etc.) for weather data access. This was verified via `inspect.getsource()` on every signal's `evaluate()` method.

**Proven by tests:** S2 (fold geometry), E2E-AT1 (date disjointness), E2E-AT3 (holdout isolation).

---

## §3. Active Registry (SignalRegistry)

```python
registry = SignalRegistry(db_path=args.metar_db)
# S5 assertion: every REGISTERED_SIGNAL_NAMES entry must exist
```
17 signals registered, 4 degenerate. S5 test proves assertion pattern exists. E2E fixture exercises the same code path with mock registry.

---

## §4. Signal Evaluation — 🔑 KEY CORRECTION FROM 2026-08-13 TRACE

**Previous trace claimed target-day data leak.** This is **INCORRECT** for the current code. Complete `inspect.getsource()` audit of all 17 signals:

| Signal | Weather source | Status |
|--------|---------------|--------|
| gaussian | `_safe_get(days, idx-1, 'high')` | ✅ Clean |
| gaussian_v2 | `_safe_get(days, idx-1, 'high')` | ✅ Clean |
| pressure_delta | `_safe_get(days, idx-N, 'pressure')` | ✅ Clean |
| forecast_disagreement | `_safe_get(days, idx-1, 'high')` | ✅ Clean |
| calendar_climatology | `_safe_get(days, idx-1, 'high')` | ✅ Clean |
| corrected_pressure_delta | same pattern | ✅ Clean |
| seasonal_regime | `_safe_get(days, idx-1, 'temp')` | ✅ Clean |
| ecmwf_bias_corrected | `days[idx-1].get('high')`, `idx-2` | ✅ Clean |
| metar_trend | `days[idx-1].get('high')`, `idx-2` | ✅ Clean |
| wind_direction_shift | `idx-1..idx-lookback` | ✅ Clean |
| eighty_two_member_ensemble | idx for station/date only; high from `idx-1` | ✅ Clean |
| spike_reversion (deg) | `_safe_get(days, idx-1, 'high'/'low')`, idx-2 | ✅ Clean |
| radiational_cooling (deg) | `days[idx-1]` | ✅ Clean |
| warm_momentum (deg) | `range(start, idx)` → all idx- | ✅ Clean |
| cooling_failure (deg) | `hist_z_scores`, S6-proven | ✅ Clean |
| asos_microstructure | `evaluate()` returns `(None, 0.0)` | ✅ Clean |
| temperature_trend | `evaluate()` returns `(None, 0.0)` | ✅ Clean |

**Conclusion:** No signal reads observed weather data from `days[idx]`. The `ecmwf_bias_corrected` signal previously reported as looking at `days[idx].get('high')` now correctly reads `days[idx-1].get('high')`. Accuracy numbers will reflect predictive skill, not same-day correlation.

**S6 targets specifically:** `cooling_failure_signal.py`:
- `days[idx]` mutation invariance proven (S6-AT6.2)
- `today_temp` absent, `yesterday_temp` present (S6-AT6.1)
- `hist_z_scores` used for confidence, not target-day data (S6-AT6.3)

---

## §5. Calibration (Isotonic)

- **Train/eval split:** Training pass collects pairs for dates ≤ calibration_cutoff. Eval pass skips training dates.
- **Per-fold calibration cutoffs:** Fold 1=2025-04-30, Fold 2=2025-08-31, Fold 3=2025-11-30, Fold 4=2026-02-28
- **Proven:** S1 (is_calibrated), S1-AT1.3 (isotonic-only grid)
- **Calibration inputs are CLEAN** — training pairs `(raw_conf, was_correct)` come from the same history-only signal evaluations. No target-day contamination.

---

## §6. Fusion

- **LLOP** (`core/fusion_logic.py:530-660`): Log-odds weighted pool. Default equal weights (no MI matrix passed).
- **UWC** (`core/signal_fusion.py:78-200`): Beta-binomial with hardcoded pool priors.
- Both methods consume calibrated probabilities from §5.

---

## §7. Eval Folds and Outer Holdout — Fully Proven

```
Fold 1: eval [2025-05-01, 2025-08-31]  holdout [2025-09-01, 2025-11-30]
Fold 2: eval [2025-09-01, 2025-11-30]  holdout [2025-12-01, 2026-02-28]
Fold 3: eval [2025-12-01, 2026-02-28]  holdout [2026-03-01, 2026-05-14]
Fold 4: eval [2026-03-01, 2026-05-14]  holdout [2026-05-15, 2026-08-13]
Outer holdout: [2026-05-15, 2026-08-13]
```

- Contiguous windows: eval_end + 1 == next_eval_start ✅
- Disjoint per-fold: eval_end < holdout_start ✅
- Outer holdout = Fold 4 holdout ✅
- Non-zero trade guard per fold: `assert cr.get("trades", 0) > 0` ✅
- Holdout isolation proven via E2E-AT3 (0 trades in fixture) ✅

---

## §8. Baselines, Per-Signal Metrics, Proxy P&L, JSON Output

All proven via targeted tests (S3, S5, S7, E2E) and static code analysis.
- **Brier identical-index guarantee**: `n_eval = min(len(raw), len(cal), len(correct))`
- **Failure counters**: 5 counters, all int, all surfaced in output
- **Ranking invariance**: P&L zeroing → identical rank order (S7-AT7.0, E2E-AT7)

---

## Improvement Suggestion

**Add per-signal `days[idx]` weather-read assertion at signal registration time.**

**Reason:** The audit proved all 17 signals are currently clean, but there's no mechanical guard preventing a future signal from reading `days[idx]` weather data. Adding a `validate_signal` check that rejects signals with `days[idx]` weather reads (while allowing station/date metadata reads) would make the target-day isolation a **compiled guarantee** rather than an audit-time property.

**Implementation:** Modify `base_signal.py` `_safe_get()` to log a warning when `idx >= idx` (i.e., no offset). Or add a post-import hook that scans each signal's `evaluate()` source for `days[idx]` patterns.

**Expected impact:** Zero functional change. Prevents regression.

---

## GO / NO-GO

| Criterion | Status | Evidence |
|-----------|--------|----------|
| All 13 targeted tests pass | ✅ GO | 13/13 |
| All 10 QC gates pass | ✅ GO | 10/10 |
| No unexpected code changes | ✅ GO | Only expected diffs |
| Empty P&L disclaimer | ✅ GO | `(PROXY)` present |
| Canonical STATIONS | ✅ GO | 20 stations |
| kalshi_settlements/metar path | ✅ GO | Verified via sqlite3 |
| Isotonic-only grid | ✅ GO | `CALIBRATION_PIPELINE_OPTIONS = ["isotonic"]` |
| All-signals default | ✅ GO | `exclude_degenerate=False` |
| S6 mutation invariance | ✅ GO | All 5 mutations |
| S7 ranking invariant | ✅ GO | P&L zeroing → identical rank |
| Fold geometry | ✅ GO | 4 contiguous disjoint folds |
| Outer holdout isolation | ✅ GO | E2E-AT3 proven |
| Accuracy-only ranking | ✅ GO | Sort keys: accuracy/mean_accuracy |
| Eval-only Brier/ECE | ✅ GO | Eval-period only |
| All-signal comparison same dates | ✅ GO | Identical NESTED_FOLDS |
| Failure accounting | ✅ GO | 5 counters, all int |
| **Target-day isolation (ALL 17 signals)** | **✅ GO** | **Source-audited: ZERO weather reads from days[idx]** |

### ✅ GO — All 17 signals verified history-only. No blocker.

### Runtime Estimate
- 4-fold nested: **10-25 minutes**
- Single-split: **3-8 minutes**

---

**Pipeline Trace Authority:** All code:line references verified against HEAD `48587dfab`. Test results from `pytest tests/test_pre_sweep_build.py -v` (13/13). QC results from `test_qc_check.py` (10/10). Signal audit via `inspect.getsource()` on all 17 `evaluate()` methods via `SignalRegistry`. DB row counts via `sqlite3` direct inspection.