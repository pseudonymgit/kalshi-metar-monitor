# Temperature-Prediction Shadow Track — Expert Review

**Date:** 2026-08-15 UTC  
**Status:** design/review only; shadow accuracy work; no production, signal, sweep, or trading changes  
**Decision boundary:** HMM/regime output is diagnostic only unless separately approved.

## Executive recommendation

Build one vintage-aware, common evaluator before comparing models. Compare **GEFS-only**, **ECMWF IFS-only**, and a **model-balanced GEFS+ECMWF mixture** on exactly the same station/date/lead rows. Do not treat the existing archive `fetch_timestamp` as issue time, do not infer a forecast vintage from a target date, and do not silently repair missing or malformed member blobs. The current archive inventory is useful for planning but is not yet sufficient evidence for an accuracy claim.

The primary accuracy target is the **NWS settlement station's official daily high and daily low for local calendar date D**. A Kalshi value or bucket is not an interchangeable label. Any settlement-to-NWS equivalence must be demonstrated separately and recorded as provenance.

## 1. Exact forecast target and label contract

### Target

For each station `s` and local calendar date `D`:

* `y_high(s,D)`: official NWS daily climate-report maximum temperature for the settlement station, in °F.
* `y_low(s,D)`: official NWS daily climate-report minimum temperature for the same station/date, in °F.

Evaluate HIGH and LOW as separate targets. Preserve the NWS raw value, units, report/PIL/date provenance, and any NWS rounding convention. The label is the station's official settlement definition, not the nearest grid-cell value and not a METAR-derived substitute unless an explicit, reviewed equivalence contract exists.

The NWS report's local calendar day is the authoritative day boundary. Store the station timezone and UTC interval used to translate the day (including DST). Do not use a fixed UTC 00:00–24:00 interval for every station.

### Forecast object

A forecast row must be keyed by:

`(station, target_local_date, target_kind ∈ {HIGH, LOW}, source, issue_time_utc, cutoff_time_utc, lead_hours, valid_window, member_id)`.

For each source/member, derive the daily extreme over the target station-local day from forecast values whose valid times are inside that day. If a source supplies an official daily-extreme field, retain it as a separate field and compare it with the time-series-derived extreme; never mix them silently. GEFS and TIGGE archive rows are presently step samples (GEFS 3-hourly rows; TIGGE 6-hourly rows in the populated archive), so extrema derived from them are temporal-grid approximations unless a daily-extreme product is obtained. This limitation must appear in every result.

The evaluator must report whether every member has complete required valid times. A partially sampled member is missing for the row, not a reason to impute an extreme.

## 2. Issue cycle, cutoff, and lead time

### Frozen comparison proposal

Use a **12Z UTC D-1 decision cutoff** for a target local date D. The eligible forecast vintage is the latest run whose *actual issuance/initialization time* is no later than `12:00 UTC D-1`; use the same cutoff for every source and station. This is a forecast available before the target local day begins for US stations and gives a practical medium-range comparison.

For each row report:

* `issue_time_utc` (actual model initialization time, not ingestion time),
* `cutoff_time_utc = 12:00 UTC D-1`,
* first and last valid times used,
* lead to local-day start and lead to local-day end,
* model cycle and archive provenance.

A source may use an earlier run when its latest eligible run is unavailable; that is a **different lead stratum**, not an invisible substitution. The primary comparison should be a common-vintage/common-lead subset. Secondary results may use all eligible vintages stratified by lead.

### Important archive caveat

The current GEFS table has `init_cycle='12Z'` but no run date/initialization timestamp; its `fetch_timestamp` records ingestion/population time. The current TIGGE table was populated by scripts requesting ECMWF `time='00:00:00'`, while storing a `target_date` that is also used as the request date in those scripts. Neither table, as currently shaped, proves a 12Z D-1 vintage or a valid date/step mapping. Before evaluation, create a read-only manifest that reconstructs or verifies `(issue_time, valid_time, lead)` from source metadata. Rows that cannot pass this check are excluded and counted as unavailable. Do not backfill metadata with evaluation-time assumptions.

## 3. Station and date universe

### Station universe

Start with the 20 stations already used by the weather engine and archive scripts:

`KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`.

Freeze one station registry containing ICAO, exact NWS climate/settlement station identifier, latitude/longitude, elevation if available, timezone, DST rules, and valid-from/to metadata. Resolve the existing coordinate inconsistencies (for example KNYC appears with different coordinates in scripts) before scoring. A grid point nearest to an airport coordinate is not itself proof of NWS station identity.

### Date universe

Use the intersection of:

1. NWS HIGH and/or LOW labels passing the provenance/QC contract;
2. a verified eligible GEFS vintage and complete valid-time/member data;
3. a verified eligible ECMWF IFS vintage and complete valid-time/member data;
4. any other required baseline inputs.

The primary GEFS-vs-ECMWF comparison must use the **same station/date/target-kind rows for all three candidates**. Report source-specific coverage separately so the intersection does not hide availability loss. Exclude target dates whose official NWS report was not final at the label freeze, and exclude dates after the chosen evaluation cutoff; do not use the current/future partial day.

Inventory observed on 2026-08-15: `gefs_archive.db` spans 2021-01-02 through 2026-08-14 (2,050 target-date labels, 20 stations); `tigge_archive.db` spans 2021-08-19 through 2026-08-06 (1,814 labels, 20 stations). These are archive ranges, not verified evaluation ranges until vintage and label alignment pass.

## 4. Member construction

### GEFS-only

Use the raw, independently identified GEFS members for each eligible vintage. Preserve control and perturbed-member identity in metadata. The expected operational ensemble is commonly 31 members, but the local archive is not consistently complete: inventory shows 89,933 rows with 31 members, 275,405 with 6, and additional rows with 1–5 or 30 members. Apply an explicit minimum (recommended primary: `n_members >= 20`; sensitivity: 10–19) and report the distribution. No duplication, synthetic member generation, or member-count imputation.

The existing GEFS reader in `scripts/gefs_walk_forward_backtest.py` assumes a one-byte signed offset blob. Verify this against the writer and row lengths before use; malformed or incompatible encodings are unavailable, not zero-spread forecasts.

### ECMWF IFS/TIGGE-only

Use the 50 perturbed members present in the populated TIGGE archive when all are valid. The archive inventory reports 181,400 rows, all `n_members=50`, over 1,814 dates and five steps (0, 6, 12, 18, 24). The request scripts identify ECMWF TIGGE perturbed members `1/to/50`, not the control; retain that fact.

The local TIGGE blobs appear to have more than one encoding lineage: the current sample is consistent with 50 float32 values, while other writer code describes compressed int16 offsets. Require a version/encoding discriminator or a verified decoder and quarantine rows that fail round-trip checks. Never score a decoded blob merely because its length looks plausible.

### Combined GEFS + ECMWF

Report three constructions, in this order:

1. **GEFS-only baseline** and **ECMWF-only baseline** (mandatory).
2. **Naive pooled members**: concatenate all available members (up to 31 + 50), clearly labeled as a diagnostic only. This shows the consequence of member-count weighting but is not the recommended ensemble.
3. **Recommended model-balanced mixture**: assign 0.5 total probability mass to GEFS and 0.5 to ECMWF, with equal mass among valid members within each source. Thus source member count does not give ECMWF an automatic 50/81 or GEFS a 31/81 advantage. Re-normalize only over available source members and mark a row ineligible when the required source/minimum is absent.

For a point forecast, use the mixture median as the primary estimate and retain the mean as a sensitivity. For probabilities/intervals, use the weighted empirical CDF. A further sensitivity may use training-only source weights by station, season, and lead, but weights must be frozen before each test fold.

### Correlation and effective availability

GEFS and IFS members are not independent observations. They share observations, large-scale dynamics, and often similar initial-condition information. There is no member identity across models, so member-to-member correlation cannot be claimed from labels alone. Estimate, on training data only:

* within-source member anomaly correlation;
* cross-source correlation of ensemble-mean errors and spread/mean anomalies by station, season, target kind, and lead;
* effective sample size/effective ensemble spread for the combined CDF.

Use block bootstrap confidence intervals by target date (not independent-member bootstrap) and report the number of valid member pairs. Do not turn a correlation estimate into a hidden production weight. A model-balanced mixture is the default precisely because the 31-vs-50 count is not evidence of independent information.

## 5. Existing forecast signals and modules

* `data/gefs_archive.db` contains GEFS step rows and member blobs; `scripts/gefs_walk_forward_backtest.py` reads step 24 and currently uses a placeholder signal path. It also explicitly notes global calibration/lookahead concerns. It is not a frozen evaluator.
* `data/tigge_archive.db` contains the ECMWF TIGGE inventory described above. The ECDS scripts request 00Z, 6-hourly steps in the populated lineage; issue/valid semantics need reconstruction.
* `data/nwp_forecasts.db` contains point/daily fields for `ecmwf`, `gfs`, `icon`, `gem`, `gefs_ens`, and `era5`, but its rows mix fetch date, target date, and forecast horizons. `gefs_ens` includes suspicious historical target dates fetched in 2026 and is not a vintage-safe member archive by itself.
* `core/signals/ecmwf_bias_corrected_signal.py` uses Open-Meteo live forecasts and a wall-clock rolling bias DB. It is not suitable as a historical shadow feature without frozen-vintage inputs and training-only bias windows.
* `core/multi_model_ensemble.py` contains static MAE/bias weights and a simulated-noise backtest mode. Simulation validates code shape, not model skill; static historical weights must not be treated as out-of-sample evidence.
* METAR, Kalman, analog, disagreement, and other current signals may be useful baselines or later covariates, but any observation used must be timestamped and strictly before the cutoff. The shadow track does not change their production behavior.

## 6. HMM/regime role

`core/regime_switching_hmm.py` is a Gaussian HMM over rolling METAR features (temperature volatility, pressure, humidity, wind, and cloud variables). It has useful diagnostic potential but is **not an approved forecast member, probability source, or B-mode signal input**.

Diagnostic experiment only:

1. At each cutoff, construct features from observations available by the cutoff.
2. Fit the HMM on an earlier training interval only; do not fit on the complete historical sample and then score it in-sample.
3. Use filtered (past-only) regime probabilities, not smoothed probabilities that use future observations.
4. Compare MAE/bias/coverage and forecast-error distributions by regime and regime-transition status. Test whether regime probability adds information beyond model mean/spread.
5. If converting regime information to event probabilities or calibration, freeze the mapping inside each training fold. No confidence multiplier may enter the deterministic loop without separate approval.

Current implementation risks: `train(walk_forward=True)` fits one model on the first 70% rather than performing true rolling refits; feature extraction can be run over a full frame; `predict_proba` semantics need verification for filtered versus smoothed use; and `get_confidence_multiplier` maps maximum regime probability directly to 0.7–1.3 without calibration. The HMM specification's proposed confidence modulation and regime-weighted DTW are hypotheses, not accepted improvements.

## 7. Leakage and validity risks

Hard gates for every evaluator run:

* **Vintage leakage:** select by actual model initialization/issue time, never by `fetch_timestamp`, file mtime, or latest database row.
* **Target-date leakage:** do not query a daily aggregate whose window includes any target-day observation when constructing a forecast issued before that day.
* **Settlement leakage:** use NWS labels only after the report is final; never use `kalshi_temp`, settlement bucket, or later revisions as a feature.
* **METAR leakage:** enforce `observation_timestamp_utc <= cutoff`; daily max/min for D is label-only.
* **Bias/calibration leakage:** estimate station/season/lead bias, quantile maps, thresholds, and source weights only on dates before the test fold. The current wall-clock ECMWF bias table is not acceptable for retrospective scoring.
* **HMM leakage:** no full-sample fit, future-smoothed state, target-day feature, or post-cutoff regime label.
* **Member leakage:** do not fill missing members from another cycle, another model, or a future reforecast; do not use target labels to reject inconvenient members.
* **Selection leakage:** rank candidates on a development interval and use a final untouched holdout. Do not select a model, lead, station subset, or calibration curve on the reported test set.
* **Temporal dependence:** overlapping forecast vintages and adjacent station/date rows are correlated. Use date-blocked or rolling-origin intervals, and disclose the unit of resampling.
* **Semantic leakage:** do not claim NWS daily high/low accuracy from METAR extrema or grid-cell extrema until station/day and rounding semantics are audited.

## 8. Metrics and reporting contract

Report HIGH and LOW separately, then a clearly labeled aggregate. Every table includes row count, station/date coverage, valid-member counts, issue/lead stratum, and missingness.

### Point accuracy

* MAE in °F (primary, interpretable);
* RMSE in °F (tail sensitivity);
* signed mean error/bias in °F (warm/cool direction);
* median absolute error and 90th-percentile absolute error as robustness diagnostics;
* mean and median forecast spread, plus correlation of spread with absolute error.

Compare at minimum against persistence (previous same target-kind settlement/day label where legally and temporally available), seasonal station climatology, raw GEFS, and raw ECMWF. Persistence/climatology must also obey the cutoff and training rules.

### Directional accuracy

Define in advance using the previous finalized value of the same target kind: `up` if `forecast_D - y_{D-1} > 0.5°F`, `down` if `< -0.5°F`, and `flat` otherwise. Report three-class accuracy and a binary up/down accuracy on non-flat rows, with coverage (fraction of rows on which a direction is issued). Also report the confusion matrix and per-station/season breakdown. Do not use a 2°F trade trigger as the forecast metric; that is a separate decision rule.

### Probabilistic metrics

Brier score and reliability/ECE are valid only for a predeclared event, such as `P(y_high >= threshold)` or `P(y_low <= threshold)`, where the threshold is fixed before evaluation (for example a published settlement threshold) and the ensemble CDF is converted consistently. For a directional event, define the flat policy before scoring. Report Brier decomposition where sample size permits, reliability diagrams, bin counts, and ECE binning rule; do not report Brier/ECE for an uncalibrated continuous value without an event definition.

For intervals, report empirical coverage at 50%, 80%, and 90% nominal levels, mean interval width, and interval score/Winkler score. Include conditional coverage by station, season, target kind, and lead. A forecast that widens intervals to obtain coverage must show the width tradeoff. CRPS is recommended if an evaluator can preserve a valid weighted ensemble CDF; it is preferable to reducing members to a single probability.

Use paired date-block bootstrap or rolling-origin confidence intervals for model deltas. A point estimate without uncertainty is insufficient for an ADVANCE decision.

## 9. Ranked experiment plan

### P0 — Freeze QC and vintage manifest (blocking)

Create a read-only manifest for source files/DB hashes, station mapping, NWS label provenance, model issue/valid times, blob encoding, and row-level member availability. Produce a small hand-audited set of station/day/vintage examples. **Gate:** no score until issue time, local-day window, and label semantics are proven.

### P1 — Common evaluator and baselines

Implement one evaluator that consumes immutable forecast rows and NWS labels, supports HIGH/LOW, exact cutoff/lead strata, no-imputation missingness, and the metrics above. Run persistence, climatology, GEFS-only, and ECMWF-only on the strict common subset. **Gate:** reproducible artifact/hash, temporal split, and coverage report.

### P2 — GEFS-only skill

Score complete GEFS vintages at the proposed cutoff; run sensitivities for member minimum, control inclusion, temporal-grid handling, and lead. Report whether the 6-member-heavy archive changes conclusions. **Disposition:** PARK if vintage/member QC cannot be satisfied; otherwise retain as baseline.

### P3 — ECMWF-only skill

Score the verified TIGGE 50-member ensemble on the same rows and lead strata. Resolve the mixed blob encoding before any result. Compare median/mean and empirical intervals. **Disposition:** PARK until 00Z request-date versus valid-date semantics are resolved.

### P4 — Model-balanced combined ensemble

Run naive concatenation only as a diagnostic, then the recommended 50/50 source-mixture CDF. Add training-only correlation/effective-spread analysis and model-weight sensitivity. Advance only if the combined model improves out-of-sample MAE/RMSE or calibration without unacceptable coverage loss and the gain survives station/season/lead strata.

### P5 — Accuracy post-processing

Test a strictly walk-forward, station/target-kind/lead-specific additive bias correction and spread/quantile calibration against raw ensembles. Freeze parameters per fold; compare against no-calibration baselines. This is the first place to test a practical accuracy improvement, not a production change.

### P6 — HMM diagnostic

After P1–P5, test whether past-only HMM regimes explain residual error or conditional coverage. No HMM-generated probability, confidence multiplier, DTW weighting, signal, or trading evaluation is allowed in this experiment. **Disposition:** ADVANCE only to a separately approved calibration study; otherwise PARK/KILL based on stability and incremental out-of-sample evidence.

### P7 — Holdout confirmation

Lock the candidate and all parameters, score one untouched temporal holdout, publish confidence intervals and missingness, and make an accuracy-only ADVANCE/PARK/KILL decision. A market-impact or P&L study requires a new approval and a separate design.

## 10. Accuracy-focused improvement suggestion

**Recommended improvement: lead-specific, station-specific rolling bias correction with spread-preserving calibration, trained only on prior finalized NWS labels.**

For each source, target kind, station, season/month, and lead stratum, estimate the recent forecast error from vintages that were available before the current cutoff. Apply the additive correction to the ensemble member values (or the ensemble location) and calibrate spread/quantiles separately; do not collapse the ensemble to a point forecast. Use a minimum history, shrink small samples toward a station/season prior, and cap corrections only by a predeclared training rule.

Why this is the best first accuracy intervention: daily extrema at airport/grid points have persistent local elevation, urban-heat, coast, and diurnal-extreme biases. A source-balanced ensemble addresses model-count overweighting and correlated members, but it cannot reliably remove a station-specific warm/cool offset. Lead-specific correction also avoids assuming that a 12-hour and 48-hour forecast have the same error. The existing ECMWF bias module demonstrates the idea but uses wall-clock rolling state and hourly records, which can leak in retrospective work; the shadow implementation must use immutable vintage joins and training-only windows.

## Required non-claims

This review does not establish model superiority, NWS/Kalshi equivalence, profitable trading edge, or permission to modify production signals. Any result that fails vintage, label, or member-integrity gates is **[blocked]**, not a negative accuracy result. Phase 1 production semantics remain untouched.
