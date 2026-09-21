# Downstream Crawl — ultimate_sweep.py Consumers Audit

**Author:** Ray Donovan (independent SRE audit — NOT Gilfoyle's build/QC)
**Date:** 2026-09-21
**Repo:** `prototypes/weather-engine-source` @ `b70442566` / branch `temp-sprint-branch`
**Method:** read-only, script-only, no AI-in-loop, no data mutation.

---

## Scope

Dan directive 9/21 03:04: after finding **13+ signals missing from the suite's
registry upstream**, crawl every DOWNSTREAM consumer of `scripts/ultimate_sweep.py`
output and audit for missing/broken/stale/wrong.

---

## Headline finding

**No downstream script reads the emitted report files (`reports/ultimate_sweep_*.json/.md`).**
All 8 code consumers `import scripts.ultimate_sweep` as a Python module and call
`build_signal_registry()` directly. This has one material consequence:

> The "13+ missing signals" is an **upstream registry defect**, but downstream
> consumers that *import the module* get the **live** (broken) registry — they
> silently inherit the missing signals, rather than reading a stale report. The
> report files are for human/Dan consumption only; the machine-to-machine path is
> module import.

The actual upstream gap is **17 unregistered signal modules** (confirmed below),
larger than the "13+" initially reported.

---

## Upstream registry gap (context, not the crawl target)

> ⚠️ **MOVING TARGET (9/21 03:xx, mid-crawl):** an uncommitted +26-line edit to
> `scripts/ultimate_sweep.py` (HEAD moved `b70442566` → `1f5c4c731` during this
> crawl) is **actively re-registering 10 of the missing signals** — `persistence`,
> `simple_trend`, `honest_trend`, `rdae_mos`, `ghcn_calibration`,
> `sensor_bias_correction`, `asos_microstructure`, `500mb_height_pattern`,
> `850mb_temp_advection`, `ecmwf_bias_corrected`. This is Gilfoyle's in-flight
> reconciliation of the exact gap this crawl documents. The "17 unregistered"
> count below is the **pre-fix snapshot**; after that edit lands it drops to ~7
> (the legacy-dupes/base-helpers/data-gated remain).

`build_signal_registry()` in `scripts/ultimate_sweep.py` registered 43 signal
names at crawl start. `core/signals/` holds 56 signal module files. **17 modules
are NOT referenced** by any `_reg()` / `_reg_func()` call (pre-fix snapshot):

| Unregistered module | Note |
|---|---|
| `500mb_height_pattern_signal` | data-gated (NWP) |
| `850mb_temp_advection_signal` | data-gated (NWP) |
| `asos_microstructure_signal` | not registered |
| `corrected_pressure_delta_signal` | legacy dup — live class lives in `dual_polarity_signal.py` (registered as `corrected_pressure_delta`) |
| `ecmwf_bias_corrected_signal` | not registered |
| `ghcn_calibration_signal` | not registered |
| `honest_trend_signal` | not registered |
| `hrrr_bias_corrected_signal` | not registered |
| `intraday_metar_confirmation` | (non-`_signal` module) |
| `persistence_signal` | not registered |
| `rdae_mos_signal` | not registered (was a live next-day predictor) |
| `sensor_bias_correction_signal` | not registered |
| `simple_trend_signal` | not registered |
| `subhourly_trajectory_signal` | not registered |
| `temperature_trend_signal` | not registered |
| `upper_atmosphere_signal` | not registered |
| `weatherapi_base_signal` | helper base (may be intentional) |

**Caveat:** a subset of these are false-positives for "missing": `corrected_pressure_delta_signal.py`
is a legacy duplicate (the class is registered from `dual_polarity_signal.py`);
`weatherapi_base_signal.py` is a base-class helper. But ≥13 are genuine
unregistered signals — matching Dan's "13+ missing." This is the UPSTREAM defect
(Gilfoyle's build/QC lane); this crawl only documents the downstream blast radius.

---

## Downstream consumer inventory

### Category A — import `ultimate_sweep` module (inherit live registry)

These get the **live** registry via `build_signal_registry()`. They do NOT read
report files. They will silently reflect whatever the registry currently contains
(missing signals → `registry[name] = None` → skipped/scored-as-unreachable).

| Consumer | Status | Consumption | Gap |
|---|---|---|---|
| `scripts/rt3_lane_gate_rebuild.py` (R/T-3 lane/gate) | **healthy-ish** | `suite.build_signal_registry()` (line 115); iterates `suite.LANES` / `suite.GATES` | Uses live registry (correct); but its LANES/GATES reachability inherits the 17 missing modules |
| `scripts/rt8b_exhaustive_recalibration.py` (R/T-8b) | **healthy** | `from scripts.ultimate_sweep import ...` + `run_suite` re-run | Correctly re-runs canonical harness; references R/T-8F A/B |
| `scripts/rt8h_exhaustive_calibration_grid.py` (R/T-8h) | **STALE — untracked** | `build_signal_registry()` + **hardcoded `BACKTEST_SIGNALS = [7 signals]`** (line 113) | ❌ NOT committed (untracked). Hardcodes 7-signal list rather than deriving from registry — the exact drift-risk RT8A flagged. |
| `scripts/rr1a_rescore.py` (R/R-1a) | **healthy** | `import scripts.ultimate_sweep as us` | Uses canonical `_score_signal` scorer |
| `scripts/rr1a_asof_redo_rescore.py` (R/R-1a redo) | **healthy** | `import scripts.ultimate_sweep as us` | Uses canonical scorer w/ corrected gate |
| `scripts/rr1b_axis_rescore.py` (R/R-1b) | **healthy** | `import scripts.ultimate_sweep as us` | Axis rescore |
| `scripts/rr1c_strict_asof_rescore.py` (R/R-1c) | **healthy** | `import scripts.ultimate_sweep as us` | Strict as-of rescore |
| `scripts/rt8g_smoke_repro.py` (R/T-8g repro) | **healthy** | `importlib.import_module("ultimate_sweep")` | Read-only smoke repro of parallelization |

### Category B — independent (do NOT consume suite output)

| Consumer | Status | Consumption |
|---|---|---|
| `scripts/rr1_review_qc.py` (R/R-1 QC) | **healthy (correct design)** | Reads DB directly (`metar_backfill.db`, `nwp_forecasts.db`, `kalshi_strike_ladder.db`); re-derives ground-truth independently |
| `scripts/rt4_noside_entry_verification.py` (R/T-4) | **healthy** | Inspects `intraday_entry_stub.py` symbols/schema — not suite output |
| `scripts/rr1c_qc_gate_units.py` (R/R-1c) | **healthy** | gate-unit QC (not suite-output consumer) |

### Category C — report-file readers (would be stale)

| Consumer | Status |
|---|---|
| *(none found)* | — |

Confirmed: **zero scripts open `reports/ultimate_sweep_*.json/.md`.** The report
files are human-facing manifests; all machine consumers use module import.

---

## Git / handoff integrity

| Item | Status |
|---|---|
| Branch / HEAD | `temp-sprint-branch` @ `1f5c4c731` (moved from `b70442566` mid-crawl) |
| **`scripts/ultimate_sweep.py`** | ⚠️ **MODIFIED (+26, uncommitted)** — in-flight registry reconciliation (adds 10 missing signals) |
| **`scripts/rt8h_exhaustive_calibration_grid.py`** | ❌ **UNTRACKED** — newest calibration consumer not committed |
| **`reports/ultimate_sweep_20260921-030537.json`** | ❌ **UNTRACKED** — newest suite output not committed |
| **`reports/ultimate_sweep_20260921-030537.md`** | ❌ **UNTRACKED** — newest suite output not committed |
| **`docs/plans/DOWNSTREAM-CRAWL-2026-09-21.md`** | ❌ **UNTRACKED** — this crawl report (advisory, no code change) |
| All other consumers | ✅ committed |

**Material gap:** the newest suite run (9/21 03:05) and the newest downstream
consumer (rt8h) are both uncommitted. Downstream work (R/T-3 wire-in shortlist,
R/T-4 calibration freeze, R/R review, Dan's wire-in) that cites "the latest suite
output" is pointing at files that are not in git — a handoff-integrity gap that
violates the "done means committed" gate.

---

## Specific dispatch questions — answered

1. **What reads `reports/ultimate_sweep_*.json/.md`?** — Nothing (code-wise).
   Only humans/Dan. Machine consumers import the module.
2. **Wire-in shortlist (R/T-3) reads full or stale subset?** — `rt3_lane_gate_rebuild.py`
   uses the **live** registry (`suite.build_signal_registry()`), so it reflects the
   current (broken, 17-missing) registry — not a stale subset, but a **live-broken** one.
3. **Calibration freeze (R/T-4) consumes suite or its own?** — R/T-4 has no single
   "freeze" script; calibration is spread across `rt8b`/`rt8h` (which import the
   suite) and legacy `*_calibration*.py` (which use `core.calibration_pipeline`,
   not the suite). `rt8h` hardcodes its own 7-signal list.
4. **Downstream pointing at nonexistent/changed schema?** — `rt8h`'s hardcoded
   `BACKTEST_SIGNALS` duplicates the 7-signal list rather than deriving it — a
   drift risk, not yet a broken reference.
5. **Handoff integrity** — 3 uncommitted artifacts (rt8h + 2 report files).

---

## Recommendations (route to owners)

1. **Upstream registry gap (17 unregistered modules)** → Gilfoyle (build/QC). This
   is the root defect; the downstream crawl only documents its blast radius.
2. **`rt8h_exhaustive_calibration_grid.py` untracked + hardcoded 7-signal list** →
   commit it, and derive the signal list from `build_signal_registry()` (or
   `SignalRegistry.get_all_signals()`) instead of a hardcoded literal, so it can't
   drift like `unified_backtest.BACKTEST_SIGNALS` did (RT8A Finding 1).
3. **Uncommitted report files** → commit or explicitly gitignore `reports/` as
   runtime output (current state leaves them as silent dirty state).

---

## Receipt

- Read-only crawl. No data mutation. No commits made by this audit (report is advisory).
- `commit=` none (audit produced no code change; findings only).
- **Note:** crawl ran against a moving target — `ultimate_sweep.py` gained an
  uncommitted +26-line registry-reconciliation edit mid-crawl (10 signals re-added).
  The upstream fix is in-flight; this report documents the pre-fix blast radius.
