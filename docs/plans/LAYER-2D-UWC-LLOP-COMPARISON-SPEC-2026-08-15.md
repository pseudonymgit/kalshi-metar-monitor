# Layer 2D UWC/LLOP Comparison Specification — 2026-08-15

**Status:** preregistered research-only design; **BLOCKED before implementation** by Phase 1 target-day leakage and non-comparable UWC semantics. No production, deployment, shadow/paper promotion, or live trading action is authorized.

## 1. Objective and fixed population

Compare the locked LLOP and UWC fusion paths on the exact same station/date cells. The canonical station universe is exactly: `KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO` (20 stations). The holdout is inclusive `2026-05-15..2026-08-13` UTC/date strings. The calibration cutoff and fold are fixed to `2026-02-28` and `fold_4_holdout`, respectively, matching the existing locked matched-comparison geometry.

The eligible cell universe is constructed once, independent of either engine: deduplicated settlement for the station/date, the prior chronological settlement, a non-flat epsilon-0.5°F label, and the same station/date daily METAR aggregate. Settlement is label-only. No target-day label or settlement may be passed into signal evaluation, calibration fitting, fusion, thresholding, or rejection logic.

## 2. Shared signal set (locked; no tuning)

Primary shared set: exactly `[pressure_delta, seasonal_regime]`. This is the only inferential signal set. The full registered 17-signal set is permitted only as a descriptive appendix, never for selection, tuning, or the primary endpoint. The full-17 set is the registry set as of this run: `gaussian, gaussian_v2, pressure_delta, forecast_disagreement, calendar_climatology, corrected_pressure_delta, seasonal_regime, ecmwf_bias_corrected, metar_trend, wind_direction_shift, eighty_two_member_ensemble, spike_reversion, radiational_cooling, warm_momentum, cooling_failure, asos_microstructure, temperature_trend`.

Both engines must receive the same signal names, same raw signal outputs, same confidence floor (`0.50`), same station/date input rows, same label-safe feature snapshot, and same calibration training population. No extra signal, threshold, station, fold, or population scan is allowed.

## 3. Label-safe feature rules

A scored target date `D` may use only immutable metadata for `D` (station/date) and observations/forecasts that were available no later than the declared prediction cutoff immediately before `D`; the deterministic offline implementation uses the prior chronological METAR index (`D-1`/prior available record), never the target-day METAR record. Target-day settlement temperature, target-day label, and any target-day observed weather value are forbidden features. Calibration records must be generated under the same rule and must use only dates `<= 2026-02-28`; holdout labels are never used to fit calibration.

Static audit must reject calls equivalent to `metar_date_to_idx[today]` for signal evaluation. The existing evaluator currently does exactly this in both calibration and scoring passes (`scripts/ws5_common.py`), so the comparison cannot proceed until a label-safe evaluator contract exists and is independently tested.

## 4. Engine configurations and adapter contracts

**LLOP:** existing `SignalFusionEngine` log-odds linear opinion pool, isotonic calibration, locked `pressure_delta + seasonal_regime`, confidence floor 0.50, DS conflict and diversity settings as explicitly fixed by the evaluator. Output contract must be `(direction in {up,down}, confidence/probability in [0,1], fired/rejected reason)`.

**UWC:** existing `UncertaintyWeightedCascade`, locked same two signals and calibration/floor inputs, with its configuration version recorded. UWC must expose a semantically comparable adapter returning direction (`up`/`down`), confidence in `[0,1]`, fired/abstain, and deterministic rejection reason for every eligible key. It must not silently require a different minimum signal count, pool universe, or output meaning. A UWC confidence that is a posterior/edge/bet-sizing quantity rather than comparable directional confidence must be explicitly adapted and validated before comparison; it may not be fabricated.

Current audit finding: UWC's default contract requires `min_signals_for_fusion=2`, while `compute_fusion_accuracy` passes a signal dictionary and records only a direction/confidence result. Its rejection path is `"Insufficient signals for fusion"`; its output is a full cascade diagnostic rather than the LLOP four-tuple. This requires a reviewed adapter contract before implementation. If it cannot be made semantically comparable, disposition is BLOCKED.

## 5. Per-key output and abstention semantics

The eventual standalone artifact must contain exactly one row per eligible `(station,date,engine)` for both engines, including abstentions. Each row must include: station, date, label, prediction, confidence, fired, abstain, rejection reason, engine (`llop`/`uwc`), engine/config version, signal config, calibration cutoff, fold assignment, and deterministic correctness (`null` for abstention). Duplicate keys or unequal key sets are hard failures. Abstention is not an incorrect prediction and is never imputed.

## 6. Endpoints and coverage context

Report all-eligible coverage and fired counts for each engine (`fired/eligible`) and fired-only contextual accuracy. The primary endpoint is accuracy difference on the matched fired-cell population: UWC minus LLOP, retaining only keys where both engines fired. Report matched `n`, correct counts, each accuracy, delta and percentage-point delta. This matched endpoint prevents coverage differences from becoming the primary accuracy claim; all-eligible coverage remains context.

## 7. Exact statistical tests

Use exact two-sided McNemar with discordances `b = LLOP correct/UWC incorrect`, `c = LLOP incorrect/UWC correct`; binomial p-value with success probability 0.5 and p=1 when `b+c=0`. Report exact two-sided 95% Clopper–Pearson intervals for each matched fired accuracy. Report deterministic paired bootstrap 95% CI for UWC−LLOP: station/date matched rows sampled with replacement, seed `20260815`, `10,000` replicates, percentile indices fixed as `int(0.025*(reps-1))` and `int(0.975*(reps-1))`. Empty matched population is a hard failure, not a fabricated CI.

## 8. 58% research gate and disposition

The 58% threshold is a research gate only: the selected engine must have matched fired accuracy `>= 0.58` at the preregistered review. It is not permission to promote, deploy, shadow trade, paper trade, or trade live. Advance requires: no leakage; validated comparable adapters; identical key sets; deterministic byte-identical rerun; artifact/hash/receipt; and independent review. Kill/stop if any target-day access, mismatched semantics, missing UWC adapter contract, missing identical station/date reconstruction, nondeterminism, or production/trading action occurs. A result below 58%, or a CI/diagnostic that fails review, is kill/no-advance for this line; no tuning follows.

## 9. Historical UWC evidence reconciliation

`docs/plans/PIPELINE-TRACE-2026-08-13.md` is an audit input, not current truth. It records (a) a confirmed target-day leakage warning in `scripts/ws5_common.py` and signal paths, and (b) an old UWC measurement of `48.39%` on 279 trades. Those facts are preserved as historical evidence and are not treated as a valid current comparison result. The leakage warning blocks this run until corrected; the old 48.39% run cannot be reconciled into a current claim without the locked geometry and a label-safe evaluator.

## 10. QC gates before any build/run

1. Static source audit of both fusion paths and evaluator for target-day label/feature access.
2. Runtime mutation test: changing target-day weather/settlement values must not change a pre-target feature, signal, calibration fit, or prediction (labels may change only the scored correctness field).
3. Verify identical station/date keys, fold, cutoff, label, signal set, and evaluator semantics.
4. Verify UWC adapter direction/confidence/rejection contract and confidence meaning.
5. Run focused tests, `py_compile`, then run the standalone script twice and require byte-identical JSON.
6. Write JSON artifact, metadata/hash, and continuity receipt only after all gates pass.

**Current disposition:** BLOCKED in Phase 1. Do not implement `scripts/layer2d_uwc_llop_comparison.py` or write `data/layer2d_uwc_llop_comparison_20260815.json` until the evaluator is made label-safe and the UWC adapter contract is validated.

## Accuracy-focused suggestion

Re-anchor signal evaluation to the prior available METAR index and add a runtime guard that rejects target-day weather reads except immutable date/station metadata. This directly removes the same-day observation artifact documented in the trace and is more likely to improve honest held-out accuracy than changing fusion weights or thresholds.
