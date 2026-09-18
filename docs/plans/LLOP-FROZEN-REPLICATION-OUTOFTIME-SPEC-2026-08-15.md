# LLOP Frozen Replication + Predeclared Out-of-Time Seasonal/Regime Extension — 2026-08-15

**Status:** preregistered, research-only evidence gate. No promotion, tuning, calibration fitting, deployment, production/shadow/paper/live-trading change, or capital action.

## Frozen question and inputs

The locked pair is exactly `[pressure_delta, seasonal_regime]`; Arm A is the frozen baseline `pressure_delta` and Arm B is the frozen pair. Evaluator is `scripts/ws5_common.py::compute_fusion_accuracy`, version `matched-comparison-v1`, LLOP fusion, fold `fold_4_holdout`, raw confidence floor `0.50`, epsilon `0.5°F`, Dempster-Shafer conflict and diversity behavior unchanged, and `calibration_label_source='disabled'`. No calibration fitting is permitted. The 20 stations are exactly: KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO.

Features use only history. The prior-METAR rule is strict: for target date `today`, use the latest METAR daily row with date strictly earlier than `today` (`prior_metar_index`); same-day/target-day METAR is forbidden. Settlement is used only to construct the directional outcome and the prior chronological settlement; it is never passed as a feature or calibration label. The frozen current matched holdout geometry is inclusive `2026-05-15..2026-08-13`, with one shared eligible station/date universe and both arms scored identically.

## Predeclared extension (locked before scoring)

The out-of-time extension is inclusive **2026-03-01..2026-05-14**, selected solely from repository availability before any extension score was read: all 20 stations have settlement + prior settlement + non-flat labels and daily METAR coverage in this window (availability QC counts are recorded in the receipt). This is an earlier cold/transition regime and does not overlap the current holdout. Dates, stations, signals, evaluator, thresholds, and tests are locked here; extension results cannot alter them.

## Rows, strata, and endpoints

For each period, emit one row per eligible `(station,date,arm)`, including explicit abstentions. The matched-fired endpoint is the inner intersection where both arms fired, paired by exact station/date. Also report all-eligible coverage (`fired/eligible`) for each arm. Report station strata and seasonal/regime strata (regime is the deterministic evaluator's frozen `seasonal_regime` output; no post-hoc regime selection), with eligible/fired/matched counts and accuracy where defined. No row or stratum may be dropped to improve results.

Primary endpoint is pair minus baseline matched accuracy. Report exact two-sided McNemar binomial p-value, CP95 intervals for each matched accuracy, and deterministic paired bootstrap 95% CI for the delta (seed `20260815`, 10,000 resamples, station/date pair rows sampled with replacement). If matched n is zero, the gate is blocked and no fabricated interval is allowed. Coverage floors are preregistered: each arm must have `>=100` eligible cells, `>=20%` all-eligible fired coverage, and the matched-fired population must be `>=100`; every station must contribute `>=1` eligible cell and `>=1` matched-fired cell, otherwise PARK/BLOCKED. These are evidence-quality floors, not promotion criteria.

## Decision criteria

**ADVANCE** only if label-safe construction, byte-identical rerun, all QC gates, both period coverage floors, and extension matched pair pass with no scope violation; this records evidence only and never authorizes promotion. **PARK** if coverage floors fail, one or more stations lacks matched support, or results are too sparse/ambiguous. **KILL/BLOCKED** if prior-METAR indexing, label safety, frozen hashes/config, shared geometry, deterministic rerun, or artifact integrity fails. No tuning, signal search, threshold selection, calibration re-enable, or production change is allowed.

## QC and reproducibility gates

Before scoring: verify the current artifact/source hashes, exact frozen config, `calibration_label_source='disabled'`, prior-METAR indexing, extension availability, and that the current artifact is reproducible without modifying source/data. The standalone script is read-only with no network/model/scheduler/trading calls. Run the identical command twice; require byte-identical stdout and JSON plus recorded SHA-256. Preserve `data/pressure_delta_matched_comparison_20260815.json` and its hash. Record Layer 5 as **69/69 adjacent pass**, full collection as **912 collected / 8 stale import errors**, and the paper-trading datetime traceback as out of scope.

## Stop conditions and limitations

Stop as PARK/BLOCKED rather than fabricate if the extension cannot be label-safe, exact dates cannot be reconstructed, data are missing, or floors fail. This is directional-accuracy evidence, not P&L or live-trading evidence; conditioning on both arms firing leaves coverage confounding. One accuracy-focused improvement suggestion is to add a source/runtime guard rejecting signal reads from target-day rows except immutable date/station metadata, because this mechanically prevents future target-day contamination while preserving current frozen signals.
