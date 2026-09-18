<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Expert 5 — Adversarial / anti-overfitting validation

**Date:** 2026-08-19  
**Disposition:** Treat the reported **45.95% holdout versus 47.34% persistence** result as overfit / non-evidence until disproven.

## Executive judgment

The comparison is not a demonstrated edge: the candidate is below the persistence comparator, and the apparent difference is small enough to be sampling noise, selection noise, or a data/label-timing artifact. A holdout is only independent if it was sealed before signal killing, feature choice, calibration, fusion weights, thresholds, station eligibility, and report selection. Any reuse of that window—even for diagnosing a bad signal—turns it into development data. The next run must be a preregistered falsification exercise, not another sweep for a better number.

## Threats requiring explicit audit

### 1. Signal selection and killing

- Build a complete inventory of every signal proposed, tested, rejected, transformed, or omitted. Include dates tested, stations, sample count, direction, threshold, metric, and reason for kill. “Not in the final set” is not evidence of independent selection.
- Recompute all candidate-level results from immutable raw inputs, not a curated winner table. Report the full distribution (median, quartiles, max, standard deviation), selection count, and effective number of looks.
- A signal killed after seeing holdout outcomes must be marked holdout-contaminated; it cannot be used in a new claim without a fresh untouched holdout.
- Use nested selection: inner folds choose/kill signals; only the outer test scores the locked resulting rule. Never choose the signal with best outer accuracy.
- Require a fixed minimum utility and minimum observations before a signal can survive (suggested: at least 50 independent eligible decisions per fold and at least 30 outer-test decisions). Otherwise label it exploratory, not positive or negative evidence.

### 2. Calibration and fusion reuse

Calibration is model fitting. Platt/isotonic maps, confidence bins, priors, station weights, fusion coefficients, tie rules, abstention thresholds, and Kelly/trade thresholds must be fitted strictly before the scored period. Re-fitting after inspecting holdout Brier, ECE, accuracy, or reliability plots is leakage. The audit should log calibration-training row IDs and assert zero intersection with every outer-test row.

Fusion needs a locked ablation: each component alone, persistence alone, equal-weight fusion, and the proposed weighted fusion, all scored on the same untouched rows. We must report whether weights were chosen by accuracy, Brier, P&L, coverage, or any composite; each is a selection criterion. A fusion that only wins after tuning is not independent evidence. Calibrate the *final fused score* in inner folds, never calibrate components on one partition and tune their combination on the outer partition.

### 3. Zero-fire and one-direction signals

A signal that never fires, fires once, or always predicts one direction can look stable while contributing no information. Every report must include:

- coverage / fire rate overall and by station, direction, and fold;
- number of UP and DOWN fires, unique dates, and unique market events;
- prediction entropy and proportion of the majority direction;
- accuracy conditional on firing **and** unconditional accuracy with abstentions handled explicitly;
- comparison with always-UP, always-DOWN, majority-class, and persistence baselines on exactly the same eligible rows.

Zero-fire signals are ineligible for positive claims and must not disappear silently from denominators. One-direction signals must beat the corresponding one-direction baseline with a predeclared margin and confidence interval; otherwise kill or park them. “No prediction” must not be counted as a correct prediction, and coverage must not be optimized after seeing labels.

### 4. Tiny samples and dependence

Raw row counts overstate evidence when observations share a station, forecast cycle, market, or weather regime. Report effective sample size and cluster/block bootstrap intervals (cluster by station-day or market event; block by date/forecast cycle). Include Wilson or exact binomial intervals for directional accuracy, but do not pretend individual rows are independent. A 50% result from a tiny or concentrated sample is inconclusive, not confirmation. Stratify by station, horizon, season/regime, and direction; show worst-fold and leave-one-station-out results, not only pooled accuracy.

## Required falsification battery

The runner should emit a machine-readable, immutable manifest containing commit, config, random seeds, raw-input hashes, station/event IDs, label cutoff, selection rules, calibration rows, fusion weights, and every attempted configuration. The report must contain all predeclared tests, including failures.

1. **Shuffled-label placebo:** permute settlement labels within permissible exchangeability blocks (at minimum date/event blocks; preserve class balance and missingness). Run the complete selection pipeline, including signal killing, calibration, and fusion. Repeat at least 1,000 times or until Monte Carlo standard error is <0.1pp. The observed statistic must be compared with the *maximum selected placebo statistic*, not a single placebo. A real edge should be rare after the full search.
2. **Time-shift placebo:** shift labels/features by predeclared offsets (e.g. ±1, ±2, ±7 days or forecast cycles), with no post-hoc choice of the winning offset. Any similar “edge” at many shifts indicates persistence/calendar leakage or a broad artifact.
3. **Negative controls:** use features that cannot causally contain forecast information (future-independent random seeds, unrelated station/weather fields, permuted station IDs, and deliberately stale or wrong-lead forecasts). They should be null after the same pipeline. A negative control that performs similarly is a kill signal for the pipeline, not a reason to add it.
4. **Persistence and trivial baselines:** persistence, climatological/majority direction, always-UP, always-DOWN, and a no-trade/abstain rule must be evaluated before selection. Compare paired per-event outcomes and confidence intervals; do not compare independently filtered populations.
5. **Untouched holdout:** create a new sealed period or event set that has never appeared in any report, debugging query, threshold choice, calibration fit, or signal-kill decision. Hash and access-control it before rerunning the locked pipeline. One pass only; no repair-and-rerun on that same set. If the current 45.95% period has been inspected, it is no longer untouched.
6. **Temporal and geographic stress:** expanding walk-forward folds, non-overlapping test windows, and leave-one-station/region-out tests. The final outer test must not be pooled with inner holdouts reused later for selection. Record fold-level coverage and trade counts.
7. **Sensitivity / researcher degrees of freedom:** rerun with predeclared reasonable label windows, horizons, tie handling, missing-data policy, station universe, and threshold perturbations. Report the whole surface and multiplicity-adjusted uncertainty (permutation max-statistic or Holm correction), not the most favorable slice.
8. **Integrity checks:** assert no duplicate event IDs, no forecast issued after the information cutoff, no settlement lookup after label availability, no row overlap between train/calibration/selection/test, and no use of post-settlement revisions. Fail closed on any violation.

## Acceptance rule

Do not promote any signal, calibration scheme, or fusion result on accuracy alone. Advancement requires: (a) positive paired improvement over persistence and majority baselines on the untouched outer test; (b) lower or materially non-worse Brier/ECE; (c) predeclared coverage and minimum effective sample size; (d) success across folds/stations rather than one pooled win; (e) placebo/negative-control nulls; and (f) an interval or permutation result that survives multiplicity. Otherwise the correct conclusion is “not demonstrated.”

## Accuracy improvement (safe, because it reduces false positives)

**Adopt an inner-fold, predeclared abstention/coverage gate and freeze it before the outer test**: issue a directional prediction only when the calibrated score clears a fixed margin above the baseline (for example, calibrated probability ≥0.60, with the exact threshold selected only inside inner folds); otherwise abstain. Report unconditional accuracy, conditional accuracy, coverage, and accuracy-versus-coverage curves. This can improve realized directional accuracy by removing low-confidence coin-flip decisions, while preventing post-hoc threshold hunting and making the tradeoff visible. It is not evidence of an edge unless it also beats persistence on the untouched set at a predeclared minimum coverage and passes the falsification battery.

## Disposition

- **ADVANCE:** the validation harness only: immutable manifests, nested selection, row-overlap assertions, full-distribution reporting, baselines, placebo/time-shift/negative-control tests, and one sealed untouched holdout. These improve epistemic accuracy without adding a trading signal.
- **PARK:** all candidate signals, calibration variants, fusion weights, and abstention thresholds until they pass the above locked protocol with adequate effective sample size and replication. Keep exploratory results clearly labeled.
- **KILL:** any result relying on holdout-informed signal killing, calibration/fusion reuse, selective reporting, zero-fire/one-direction denominators, tiny correlated samples presented as independent, or a candidate that fails to beat persistence on the untouched test. The current 45.95% result is **KILL as evidence of an edge**, not a claim that the underlying idea can never be revisited.
