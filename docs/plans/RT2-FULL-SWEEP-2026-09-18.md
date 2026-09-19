# R/T-2 Full-History Signal Re-Sweep — Honest Edge vs Base-Rate Artifact

Date: 2026-09-18/19 (UTC)
Author: Gilfoyle (subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved)
Suite: `scripts/ultimate_suite.py` (R/T-1) + `scripts/rt2_full_sweep.py` (R/T-2 driver, read-only)

**This is the evidence for the Gray Room gate review.** Read §6 (the verdict) first.

---

## 0. TL;DR

- **Truth axis is now correct and complete**: 14,130 strike-crossing labels (14,265-row
  Kalshi `/historical/markets` backfill joined to date-tiered `settlement_truth` temps,
  minus 54 `between` bins and 80 untrusted rows). Range 2022-12-22 → 2026-09-18, 20 stations.
- **The label set is DOWN-skewed, not up-skewed.** Base rate `up` = 34.3%. The null
  "always predict DOWN" wins 65.7% — and already carries an annualized Sharpe ≈ **5.25**
  purely from skew. This inverts the R/T-1 assumption ("82% up-skewed").
- **The skew is market-type-conditional and extreme**: HIGH (`greater`) resolves `up`
  only **10.0%** of the time; LOW (`less`) resolves `up` **94.6%** of the time. A signal
  that mirrors market type trivially hits ~91.4% accuracy — pure base rate, zero skill.
- **Two signals show genuine minority-side discrimination**: `nwp_dtdt_fusion`
  (MCC +0.105, up-call edge +13.0pt) and `frontal_passage_intraday` (MCC +0.109, up-call
  edge +8.9pt). These are the only candidates that beat the skew on the *skilled* side.
- **Every "high Sharpe" signal with MCC ≈ 0 is a base-rate artifact** — most famously
  `spike_reversion` (Sharpe 3.93) and `radiational_cooling` (Sharpe 10.23), which simply
  always predict "down" and ride the HIGH-market down-skew. `spike_reversion`'s Sharpe is
  *below* the always-down null (5.25), so it is actively worse than doing nothing.

---

## 1. What was re-swept (scope)

1. **Full signal sweep** — all 43 registered signals (9 active + killed + recovered) on the
   correct strike-crossing axis.
2. **Lane / gate / fusion / calibration-mode availability** — enumerated.
3. **Per-signal calibration** — Brier + ECE (within-market-type).
4. **Independent QC** — axis spot-check (10 labels recomputed by hand), base-rate null
   derivation, and minority-side precision analysis to separate skill from skew.

**Boundaries held:** read-only, no signal re-enable/wiring, no commit of signal changes,
git-clean (new files only: `scripts/rt2_full_sweep.py`, the two receipt artifacts).
Dan holds wire-in.

---

## 2. The correct axis (and why it matters)

Ground truth is **strike-crossing**, per `core/settlement_outcome.strike_crossing_direction`:

- HIGH (`greater`): `up` iff `settlement_temp > strike` (strict `>`; tie = loss).
- LOW (`less`): `down` iff `settlement_temp < strike` (strict `<`; tie = loss).

Independently verified against raw rows (10/10 spot-checks matched). The prior
day-over-day ("warmer than yesterday") target is **wrong** for Kalshi HIGH/LOW settlement.

**The backfill is authoritative**: `settlement_epochs.strike_price` / `strike_type` are
read directly from Kalshi `/historical/markets` market objects (`floor_strike`/`cap_strike`),
not reconstructed. `strike_type` is *independent* of `market_type`: HIGH series → `greater`
(10,119 rows), LOW series → `less` (4,092), plus 54 HIGH `between` bins (excluded — no
single-strike semantics).

---

## 3. The base-rate trap (the whole point of R/T-2)

| Null model | Accuracy | Annualized Sharpe |
|---|---|---|
| Always "down" (global majority) | 65.71% | **5.25** |
| Market-type-conditional majority (down in HIGH, up in LOW) | **91.35%** | — (no position risk, but unattainable without knowing market type) |

Two distinct nulls:

1. **Global-majority null** (always "down") = 65.71% acc, Sharpe 5.25. This is the honest
   floor for a *generic directional* signal that does not know the market type.
2. **Market-type-conditional null** = 91.35% acc. This is what the *engine* could achieve
   if it simply bet the majority class of whichever market it is trading — but a temperature
   signal has no such input, and exploiting it is not "signal skill".

**The honest skill bar is MCC**, which is imbalance-immune and correct regardless of skew.
A signal with MCC ≈ 0 is adding no discrimination — its accuracy/Sharpe is entirely skew.
Positive MCC means the signal's predictions are *informative*, specifically on the
minority (skilled) side.

**Sharpe is a trap here.** Because the label set is down-skewed, "always down" already
yields Sharpe ≈ 5.25. Any signal whose Sharpe is *below* 5.25 and whose MCC ≈ 0 is
**worse than the trivial null**, even if it "looks" profitable.

---

## 4. Full per-signal results (correct axis)

Legend: `acc` = directional accuracy; `MCC` = Matthews (imbalance-immune skill); `up-prec`
= precision of "up" calls (base = 34.3%); `up-edge` = up-prec − 34.3% (positive = real
minority-side skill); `Sharpe` = annualized unit-bet; `ECE` = within-market-type expected
calibration error.

### (a) Genuine edge — real minority-side discrimination

| signal | acc | MCC | up-prec | up-edge | Sharpe | n | ECE |
|---|---|---|---|---|---|---|---|
| **nwp_dtdt_fusion** | 59.0% | **+0.105** | 47.3% | **+13.0pt** | 2.90 | 9827 | 0.205 |
| **frontal_passage_intraday** | 66.1% | **+0.109** | 43.2% | **+8.9pt** | 5.39 | 2809 | 0.145 |

Both have MCC meaningfully > 0 **and** positive up-call edge. `frontal_passage_intraday`
also beats the always-down null on Sharpe (5.39 > 5.25). These are the two signals worth
taking to the Gray Room as *candidates* — subject to out-of-sample validation (see §7).

### (b) Marginal / weak (positive but small or mixed)

| signal | acc | MCC | up-prec | up-edge | Sharpe | n | note |
|---|---|---|---|---|---|---|---|
| cross_model_divergence | 51.3% | +0.025 | 51.9% | **+17.6pt** | 0.42 | 3611 | strong up-side, weak down-side (down-prec 50.6% < 65.7%) |
| nwp_analog / _ensemble | 52.6% | +0.035 | 40.0% | +5.7pt | 0.82 | 10861 | identical (same impl) |
| pressure_tendency | 57.3% | +0.033 | 35.5% | +1.2pt | 2.35 | 893 | small n |
| metar_trend | 51.6% | +0.027 | 35.5% | +1.2pt | 0.51 | 14020 | large n, thin edge |

`cross_model_divergence` is notable: its *up*-calls are strong (51.9% precision vs 34.3%
base) but it under-calls "down" (50.6% vs 65.7% base), so net MCC is modest. Worth a
look as an up-only / confirmation signal, not a standalone directional.

### (c) Base-rate artifacts (MCC ≈ 0, "looks good" only from skew)

| signal | acc | MCC | Sharpe | n | why it's an artifact |
|---|---|---|---|---|---|
| radiational_cooling | 77.1% | 0.000 | 10.23 | 144 | always "down"; n tiny & HIGH-dominated |
| spike_reversion | 62.0% | 0.000 | 3.93 | 3848 | always "down"; Sharpe **< null 5.25** |
| metar_dtdt | 60.2% | +0.008 | 3.32 | 8221 | 89% down-calls; Sharpe < null |
| warm_momentum | 35.3% | 0.000 | −4.88 | 6759 | no discrimination |
| fogr_reversion | 33.8% | 0.000 | −5.44 | 7154 | no discrimination |
| cooling_failure | 35.4% | 0.000 | −4.86 | 670 | no discrimination |

These are the signals the R/T-1 "82% up-skew" framing would have *wrongly credited*.
On the correct axis they add nothing.

### (d) Dead / negative (MCC ≤ ~0, or negative up-edge)

| signal | acc | MCC | Sharpe | n |
|---|---|---|---|---|
| calendar_climatology | 47.4% | −0.197 | −0.84 | 3078 |
| gaussian | 50.0% | −0.151 | −0.02 | 5568 |
| regime | 57.8% | −0.135 | 2.52 | 204 |
| gaussian_v2 | 50.7% | −0.046 | 0.23 | 9438 |
| goldilocks | 49.3% | −0.028 | −0.23 | 7344 |
| wind_direction_shift | 49.9% | +0.032 | −0.03 | 1563 (up-edge −5.0pt) |
| corrected_pressure_delta | 49.4% | −0.009 | −0.19 | 7602 |
| forecast_disagreement | 48.9% | −0.002 | −0.34 | 4680 |
| pressure_delta | 50.6% | +0.009 | 0.19 | 7594 |

### (e) No output (n=0) — structurally broken/dead against this schema

`ai_composite`, `cloud_cover_index`, `dewpoint_depression`, `dual_polarity`,
`eighty_two_member_ensemble` (+ `_ece`, `_pooled`), `feels_like_delta`, `frontal_detector`,
`frontal_passage_detector`, `frontal_passage_nowcast`, `metar_nowcast`,
`settlement_arbitrage`, `spread_based_entry`, `trajectory_state_vector`, `volume_momentum`.

(Note: several of these are gate/modulator functions, not standalone directional signals —
e.g. `dewpoint_depression` is a `modulate_confidence` function, `spread_based_entry` is an
entry detector. "No output" is expected for those; they are not dead *signals*, just not
directional-signal-shaped.)

### (f) Skipped / failed

- **Slow or live-API** (skipped by design): `esdr`, `intraday_metar_confirmation`,
  `nwp_direct`, `temperature_advection`.
- **Import failed**: `seasonal_regime`.

---

## 5. Lane / gate / fusion / calibration-mode availability

All lanes, gates, fusion modes, and calibration modes import cleanly:

- **Lanes** (6/6): forecasting_ensemble, sure_thing, goldilocks, trajectory, nowcasting, radiational_cooling.
- **Gates** (9/9): agreement, settlement_execution, production, station_skill, liquidity, trajectory_confirmation, spatial_coherence, adaptive_thresholds, crossfeed.
- **Fusion modes** (4/4): none, uwc, majority, weighted.
- **Calibration modes** (4/4): platt, bma, emos, both.

Availability ≠ accuracy; this only proves the code paths are wired. The *directional*
evidence above is what governs the gate decision.

---

## 6. Verdict — "does anything have genuine edge?"

**Yes, weakly, and narrowly — two candidates, neither yet proven out-of-sample.**

1. **`nwp_dtdt_fusion`** — the strongest *candidate*. MCC +0.105, up-call edge +13.0pt on
   n=9,827. Real minority-side discrimination. But Sharpe 2.90 is *below* the always-down
   null (5.25), meaning its edge is real-but-thin and would need sizing/gating to monetize
   against the skew. **Status: genuine-edge candidate → out-of-sample validation.**

2. **`frontal_passage_intraday`** — MCC +0.109, up-call edge +8.9pt, Sharpe 5.39 (beats
   null). Smaller sample (n=2,809). **Status: genuine-edge candidate → out-of-sample
   validation (watch n).**

**Everything else is dead or base-rate artifact.** Specifically:

- **Base-rate artifacts (do NOT wire):** `spike_reversion`, `radiational_cooling`,
  `metar_dtdt`, `warm_momentum`, `fogr_reversion`, `cooling_failure` — all MCC ≈ 0,
  all "profit" from down-skew. `spike_reversion` and `radiational_cooling` in particular
  are the textbook trap: high Sharpe, zero skill.
- **Dead/negative (do NOT wire):** `calendar_climatology`, `gaussian`, `regime`,
  `gaussian_v2`, `goldilocks`, and the near-zero-MCC remainder.

**The Gray Room gate review should (a) greenlight out-of-sample validation for the two
genuine candidates, (b) formally kill the base-rate-artifact set, and (c) note that
`cross_model_divergence` may have value as an up-only confirmation signal, not a standalone.**

---

## 7. QC summary (independent checks performed)

1. **Axis** — 10 labels recomputed by hand from raw `temp` vs `strike` vs `market_type`;
   10/10 matched `strike_crossing_direction`. Strict `>`/`<` (tie = loss) confirmed.
2. **Base rate** — independently recomputed: global up = 34.29%; HIGH up = 10.0%;
   LOW up = 94.6%. The R/T-1 "82% up-skewed" claim is **wrong** — the correct-axis label
   set is down-skewed, and the skew is market-type-conditional and extreme.
3. **Null Sharpe** — the always-down null carries annualized Sharpe ≈ 5.25; any signal
   with MCC ≈ 0 and Sharpe < 5.25 is worse than doing nothing.
4. **Skill vs skew** — minority-side ("up") precision vs base rate is the discriminating
   test; only `nwp_dtdt_fusion` and `frontal_passage_intraday` clear a positive up-edge
   with meaningful MCC.

**Residual caveats (needs-more-data):**
- `frontal_passage_intraday` n=2,809 is thin relative to the full 14,130 labels — its
  edge could be regime-specific (frontal passages cluster in transition seasons).
- `pressure_tendency` (n=893) and `regime` (n=204) are too small to judge.
- No temporal train/test split was run here (R/T-2 is a full-history point estimate);
  the two candidates **must** pass an out-of-sample walk-forward before wire-in.

---

## 8. Receipts

- Structured JSON: `reports/rt2_full_sweep_20260919-031822.json` (raw sweep)
- Structured JSON: `reports/rt2_full_sweep_verdict.json` (QC'd verdict, this doc's source)
- Driver (read-only, new file): `scripts/rt2_full_sweep.py`
- This doc: `docs/plans/RT2-FULL-SWEEP-2026-09-18.md`

**Boundary note:** the committed `scripts/ultimate_suite.py` was *not* modified; the
R/T-2 truth source (full backfill) is injected by the standalone driver. No signal wiring
was touched. Git tree remains dirty (preserved as instructed); no commit performed.
