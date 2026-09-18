# Pressure Delta Reliability Review Specification — 2026-08-15

## Status and objective

Frozen, research-only reliability review of `pressure_delta` alone under the corrected strict prior-METAR evaluator with `calibration_label_source='disabled'`. This is not a sweep, selection, calibration, promotion, deployment, paper, shadow, or live-trading exercise. All signal/config/evaluator choices below are frozen before scoring; no thresholds or stations may be selected.

## Frozen population and evaluator

- Current holdout (inclusive): `2026-05-15..2026-08-13`.
- Predeclared extension (inclusive): `2026-03-01..2026-05-14`; it is earlier and non-overlapping.
- Canonical stations, exactly 20: `KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`.
- Same eligible universe for all summaries: one deduplicated `(station,date)` key, prior chronological settlement, non-flat directional label with epsilon `0.5°F`, and an available strictly prior METAR daily snapshot.
- Prior-METAR rule: for target date `d`, feature data may use only the latest daily METAR row with date `< d`; same-day/target-day rows are forbidden.
- Evaluator: `scripts/ws5_common.py::compute_fusion_accuracy`, LLOP, fold `fold_4_holdout`, confidence floor `0.50`, existing DS conflict/diversity behavior, signal list exactly `["pressure_delta"]`, `calibration_method="isotonic"` as an inert frozen label (no fit), `calibration_cutoff_date="2026-02-28"`, and `calibration_label_source="disabled"`.
- No alternative arm is scored; there is no comparator endpoint. Existing matched-comparison artifacts are descriptive context only.

## Required outputs and strata

For each period and each canonical station, plus seasonal strata `DJF/MAM/JJA/SON`, report: eligible, fired, coverage, correct, fired accuracy, exact CP95 interval, mean confidence, confidence bins `[0.50,0.60)`, `[0.60,0.70)`, `[0.70,0.80)`, `[0.80,0.90)`, `[0.90,1.00]`, directional balance, abstention/rejection counts, and rejection reason counts. Report period aggregates and current/extension summaries. Include eligible key hashes and exact key counts. Matched comparison metadata is explicitly `none`; no alternative is evaluated.

Confidence diagnostics are descriptive only. CP95 is exact two-sided Clopper–Pearson on fired correctness. No threshold search, bootstrap ranking, McNemar test, or calibration is used for this single-arm review; the artifact records why those endpoints are not applicable. Evidence limitations include abstention conditioning, directional-label dependence, finite coverage, and no causal/P&L/live evidence.

## Preregistered decision criteria

- **VIABLE RESEARCH BASELINE** only if both periods pass label safety, exact key equality, deterministic rerun/hash, all 20 station keys, `eligible >= 100`, coverage `>= 20%`, and CP95 lower bound `>= 0.55` in each period; this is evidence only and never authorizes promotion.
- **PARK** if integrity is valid but coverage/precision floors fail, evidence is sparse, or the result is ambiguous.
- **KILL** if any integrity, key equality, deterministic, prior-METAR, or label-safety gate fails. Stop immediately on such failure.

No inference of production/promotion follows this review. Live/shadow/paper changes remain prohibited.

## QC and execution contract

Before scoring verify frozen source/artifact hashes, exact station/date keys, target-day access exclusion, no calibration fit, and deterministic data availability. The standalone script is read-only apart from its designated JSON output, has no network/model/scheduler/trading calls, and must be run twice with the identical command. The output JSON must be byte-identical and have identical SHA-256. Receipt must include commands, counts, results, hashes, and one accuracy-focused improvement suggestion. Preserve all prior artifacts.
