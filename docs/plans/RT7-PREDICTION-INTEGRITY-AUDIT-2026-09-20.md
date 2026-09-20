# R/T-7 — Prediction Integrity Audit (inventory leg)

**Date:** 2026-09-20 22:35 UTC
**Author:** Gilfoyle (technical audit — inventory leg, script-only, no AI-in-loop)
**Repo:** `prototypes/weather-engine-source` @ commit `98882303b` (R/R-1c)
**Scope:** "Is everything that feeds prediction actually wired and working — no silent drops, disabled paths, or stale data?" (RECONCILE-vs-PRE-WIPE applied to the prediction layer.)
**Separate from:** Gray Room Session 7 weighting/calibration *adjudication* (Gerri's lane). This is the inventory only.

---

## TL;DR

The prediction stack has **three divergent signal layers that are not reconciled at runtime**, and the *live* path does not consume the pool-of-pools/UWC fusion at all. Data paths are healthy and current. The reconciliation was already done on 9/17 (`SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md`) and its classification still matches live code today.

**Headline findings:**
1. **Fusion (pool-of-pools/UWC) is wired but NOT invoked by the live path.** `core/signal_fusion.py` + `core/modulator_stack.py` (the UWC cascade) are only referenced by `calibration_dashboard.py` and their own wrapper — never by `paper_trading_engine.py` or `app.py`. `signal_predictions` is never assembled by any live caller.
2. **Name-mapping mismatch would silently zero the pools *if* the fusion were wired.** Taxonomy pool members use `_signal` suffixes (`gaussian_signal`); the execution registry uses short names (`gaussian`). Direct exact-name match = **NONE** (verified empirically).
3. **Calibration covers only the 12 live signals.** 18 IMPL-NOT-WIRED + 4 PARKED signals calibrate nowhere (reconciliation doc's own "Remaining Risks #1").
4. **Data paths healthy & current** — NWP (max fetch 2026-09-20T06:00), GEFS (→09-16), ERA5 (→08-31, natural lag), GHCN settlements (619K rows). No stale/disabled fetch.

---

## Leg 1 — Pool-of-pools wiring (silent drops?)

**Finding: fusion is not wired to the live path.** The UWC pool-of-pools cascade (`UncertaintyWeightedCascade.fuse()` → `PoolOfPools.evaluate()`) exists and is correct *in isolation*, but:

- `paper_trading_engine.py` imports `agreement_gate`, `adaptive_thresholds`, `station_skill_gate`, `spatial_coherence`, `ensemble_diversity`, `position_sizing`, `late_day_momentum_hourly`, `intraday_metar_confirmation` — **none of `signal_fusion`, `fusion_logic`, `signal_families`, or `SignalRegistry`.**
- `modulator_stack.apply()` / `ModulatorStack` is referenced only by `calibration_dashboard.py` (a dashboard) and its own module wrapper (`modulator_stack.py:304`).
- `signal_predictions` (the dict that feeds `fuse()`) is **never constructed by any live caller** — only by `fusion_logic.py` internally (MI-matrix path) and test scaffolding.

**Conclusion:** The pool-of-pools fusion is *implemented but not in the live prediction path*. It is not silently dropping signals at runtime — it is simply not being invoked. This is a "wired-in-code, unwired-in-execution" gap, not an active drop.

**Name-mapping risk (latent):** `PoolOfPools.evaluate()` resolves members via `get_pool_members()` (taxonomy names with `_signal` suffix) against `signal_predictions` keys. Empirical check today:
- Taxonomy ACTIVE pool members: 13 names, all `_signal`-suffixed (`gaussian_signal`, `gaussian_v2_signal`, `calendar_climatology_signal`, …).
- Execution registry names: 12 short names (`gaussian`, `gaussian_v2`, `calendar_climatology`, …).
- **Direct exact-name matches: NONE.** (suffix-stripped match: 6 of 12.)
- If fusion were wired with execution-registry names, `PoolOfPools` would fall into its `"no pools have active signals"` null branch → silent zero. This is the active-memory "PoolOfPools structural bug" class.

---

## Leg 2 — Data-path completeness (NWP / METAR / GHCN / calibration)

**Healthy and current — no disabled path or stale fetch detected.**

| Source DB | Rows | Latest timestamp | Status |
|---|---|---|---|
| `nwp_forecasts.db` | 943,231 | 2026-09-20T06:00:29Z | **ACTIVE** (fetch_log shows gem/icon/ecmwf fetching 20 stations, 1540 vars, ~5h ago) |
| `gefs_archive.db` | 369,305 | 2026-09-16 | Current (GEFS cadence) |
| `era5_archive.db` | 41,380 | 2026-08-31 | Current (ERA5 has ~3wk lag) |
| `ghcn_settlements.db` | 619,428 | — | Populated (via symlink `ghcn_settlement.db`) |
| `metar_backfill.db` | 2.5 GB | 2026-09-20 22:30 | **ACTIVE** (live writes) |

**Anomalies (stale/empty paths, not active drops):**
- `ghcn_daily.db` = **0 bytes, 0 tables** (abandoned path; real GHCN lives in `ghcn_settlements.db`).
- `metar.db` = **0 tables** (abandoned; real METAR lives in `metar_backfill.db`, symlinked as `metar_archive.db`).
- Two stale orphan copies: `metar_backfill.db.pre-settlement-fix-20260917*` (2.47 GB each) sitting in `data/` — same orphan-artifact class as the PWW incident. Not git-tracked; cleanup candidate.

---

## Leg 3 — 9/4–9/7 wipe reconciliation (parity with pre-wipe?)

**Reconciled 9/17, still accurate.** `SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md` + `WIPE-RECOVERY-POOL1-POOL2-2026-09-18.md` already did the RECONCILE pass. Current live code matches the 9/17 classification:

- **LIVE** (12): gaussian, gaussian_v2, calendar_climatology, ecmwf_bias_corrected, metar_nowcast, forecast_disagreement, metar_trend, radiational_cooling, cross_model_divergence, frontal_passage_intraday, cloud_cover_index, feels_like_delta.
- **IMPL-NOT-WIRED** (18), **PARKED** (4), **KILLED** (4), **GATED** (1 = ai_composite), **ORPHANED** (6).

No silently-missing *live* inputs found. The pre-wipe live set is at parity — the 12 signals that were wired before are wired now. The open question is the 18 IMPL-NOT-WIRED, which were never wired (not wiped-and-dropped).

---

## Leg 4 — Calibration scoring completeness

**Gap confirmed:** `calibrate_all_signals.py` calibrates `registry.get_all_signals()` = **the 12 live signals only** (docstring says "23", registry returns 12). The 18 IMPL-NOT-WIRED + 4 PARKED signals are **scored nowhere** — no calibration coverage.

This is a *silent skip* in the sense that those signals are never calibrated, but it is **not** a correctness bug in the live path (those signals don't reach prediction). It matters only if/when any IMPL-NOT-WIRED signal is promoted to live — it would enter the fusion/calibration surface with no calibration history.

---

## Inventory Summary (wired vs dropped vs stale vs disabled)

| Category | Count | Names / detail |
|---|---|---|
| **WIRED (live)** | 12 | gaussian, gaussian_v2, calendar_climatology, ecmwf_bias_corrected, metar_nowcast, forecast_disagreement, metar_trend, radiational_cooling, cross_model_divergence, frontal_passage_intraday, cloud_cover_index, feels_like_delta |
| **Fusion (pool-of-pools/UWC)** | 1 engine | Implemented; **NOT invoked by live path** (latent name-mapping zero-risk if wired) |
| **DROPPED (silent, active)** | **0** | No live signal is silently dropped at runtime. The fusion simply isn't wired in. |
| **IMPL-NOT-WIRED** | 18 | Real `evaluate()` logic exists, not in `SignalRegistry` (see reconciliation doc) |
| **STALE (abandoned paths)** | 2 DBs | `ghcn_daily.db` (0B), `metar.db` (0 tables) — real data elsewhere |
| **DISABLED/GATED** | 1 | `ai_composite` (GATED, off by default); `NWP_ANALOG_ENABLED=False` (nwp_analog, KILLED) |
| **ORPHAN FILES (cleanup candidate)** | 2 | `metar_backfill.db.pre-settlement-fix-20260917*` (2×2.47 GB) |

---

## Receipt

- **Repo:** `prototypes/weather-engine-source`
- **Commit:** `98882303b` (R/R-1c: record final commit hash in receipt) — tree clean (`git status --porcelain` empty).
- **Method:** script-only (code inspection + live-DB SQLite read-only queries + empirical name-match check). No AI in the loop.
- **Key artifacts consumed:** `core/signals/__init__.py` (SignalRegistry), `core/signal_families.py` (taxonomy), `core/signal_fusion.py` (UWC), `core/modulator_stack.py`, `core/calibrate_all_signals.py`, `docs/plans/SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md`.

**Bottom line for the Gray Room synthesis:** the prediction layer's *signal inventory* is reconciled and at parity (12 live, 18 unwired, no silent live drops). The material open finding is **architectural**: the pool-of-pools/UWC fusion layer is implemented but disconnected from the live execution path, and if it were ever wired with execution-registry names it would silently zero due to the `_signal`-suffix name mismatch. This is an inventory fact, not a weighting/calibration judgment — the latter is the Gray Room expert's lane.
