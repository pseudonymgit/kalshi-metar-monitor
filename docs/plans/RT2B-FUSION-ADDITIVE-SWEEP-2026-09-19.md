# R/T-2b Fusion + Additive Re-Sweep — Does the Fused System Have Edge?

Date: 2026-09-19 (UTC)
Author: Gilfoyle (subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved)
Suite: `scripts/rt2b_fusion_additive_sweep.py` (read-only) + `scripts/rt2_full_sweep.py` (R/T-2, for the standalone baseline)

**This is the missing Gray Room evidence.** R/T-2 measured 43 STANDALONE signals and
found 2 thin candidates. This sweep measures (not availability-checks) the **fusion layer
and additive stack** on the same 14,130 correct-axis labels, and answers the actual
question: *does the fused system have real edge that individual signals lack?*

**Read the verdict (§6) first.**

---

## 0. TL;DR

- **The canonical fusion layer does NOT create edge — it destroys it.** The LOOP
  (log-odds opinion pool) over all 22 firing signals yields MCC **−0.0008**; the UWC
  cascade and Beta pool-of-pools yield MCC **0.0000** (they collapse to "always down" —
  pure base rate, zero discrimination). Pooling all signals dilutes the 2 genuine
  discriminators with 20 base-rate-artifact / negative-MCC signals.
- **The one fusion that DOES concentrate edge is candidate-selective union (OR), not
  log-odds pooling.** A simple "either candidate says *up* → up" over the two genuine
  candidates (`nwp_dtdt_fusion` ∪ `frontal_passage_intraday`) yields **MCC +0.124** —
  *beating* both individuals (+0.106 / +0.108) on 10,719 rows (their union coverage).
  AND (both-agree) is even sharper on up-precision (50.6%, up-edge +14.1pt) but covers
  only 1,192 rows.
- **The additive stack is structurally non-directional on this axis.** The three frontal
  detectors + six Gamma signals return temperature *offsets* (degF), not up/down votes;
  they produced zero directional output in both R/T-2 and this sweep. Their marginal
  contribution to a *directional* fusion is zero by construction — they were never
  directional signals, and treating them as such is a category error.
- **Net answer: No — the fused system as currently wired has no edge that individual
  signals lack.** Edge lives in exactly two signals; naive pooling of the whole registry
  dilutes them. The only "fusion" with incremental value is a candidate-selective OR,
  which is a *confirmation logic*, not the LIOP/LOOP pool-of-pools that the historical
  "LIOP 59.86% / UWC cascade" folklore credited.

---

## 1. Scope and method

Measured (not availability-checked) on the **14,130 correct-axis labels** (full Kalshi
strike backfill, 2022-12 → 2026-09, 20 stations, up-base-rate 34.36%):

1. **LOOP/LIOP pool-of-pools** (log-odds opinion pool) — directional accuracy + MCC +
   up-edge + ECE.
2. **UWC cascade** — same.
3. **Weighted / majority fusion modes** — same.
4. **Additive stack** — frontal detectors + Gamma signals + co-occurrence + context.
5. **Combination test** — the 2 thin candidates ± the pool.

**Metric discipline held:** MCC (imbalance-immune) leads; accuracy/Sharpe reported but
flagged. The down-skew null = always-down = 65.64% acc / Sharpe 5.23. Sharpe is NOT edge
here. Every number's axis is the strike-crossing axis.

**Boundaries held:** read-only, no signal re-enable/wiring/kill, no commit, git-clean.
Kill list PARKED per Dan.

---

## 2. Truth axis (unchanged from R/T-2)

Ground truth = strike-crossing, `settlement_temp_f` vs `strike_price`, HIGH→`greater`
(up iff temp > strike), LOW→`less` (down iff temp < strike), tie = loss. 14,130 labels:
down=9,275 (65.64%), up=4,855 (34.36%). Market-type-conditional base rate: HIGH up=10.0%,
LOW up=94.6%.

**⚠️ Data-integrity note (material, surfaced to Dan):** the strike backfill that R/T-2
swept at 03:18 UTC had regressed to 110 rows by 04:05 UTC. Root cause identified:
`scripts/migrate_add_strike_price.py` (line 227 `DROP TABLE IF EXISTS settlement_truth` +
re-backfill from stale `strike_map.json`) re-runs and wipes the 14k-row Kalshi backfill.
This sweep re-ran `kalshi_historical_strike_backfill.py --commit` (Dan-approved, read-only
on existing data) to restore 14,305 strike rows → 14,130 labels. **The re-migration is a
standing data-integrity hazard and must be gated before any future sweep.**

---

## 3. Fusion-layer results (correct axis)

Legend: `acc` = directional accuracy; `MCC` = Matthews; `up-prec` = precision of "up"
calls (base 34.36%); `up-edge` = up-prec − base; `ECE` = expected calibration error;
`Sharpe` = annualized unit-bet (NOT edge here).

### (a) Fusion over ALL 22 firing signals

| fusion mode | n | acc | MCC | up-prec | up-edge | ECE | Sharpe | beat-null? |
|---|---|---|---|---|---|---|---|---|
| **loop (log-odds opinion pool)** | 14020 | 54.76% | **−0.0008** | 34.2% | −0.0pt | 0.289 | 1.52 | ❌ (MCC ≈ 0) |
| pool_of_pools (Beta) | 14020 | 34.24% | 0.0000 | 34.2% | +0.0pt | 0.158 | −5.27 | ❌ (collapsed to "down") |
| uwc cascade | 14020 | 34.24% | 0.0000 | 34.2% | +0.0pt | 0.332 | −5.27 | ❌ (collapsed to "down") |
| majority | 12266 | 52.61% | −0.0030 | 34.5% | −0.2pt | 0.172 | 0.83 | ❌ |
| weighted | 14020 | 53.64% | −0.0066 | 33.8% | −0.4pt | 0.170 | 1.16 | ❌ |

Every fusion mode has MCC ≈ 0. The LOOP (the canonical LIOP) is **not** informative on
this axis. The Beta pool-of-pools and UWC cascade collapse to "always down" — their
agreement gates default to no-trade/0.5 and the direction resolves to the majority class.

### (b) Fusion over the "honest" signal subset (excludes base-rate artifacts)

The 10 signals with standalone MCC > 0.01 or up-edge > 0 (the non-artifact set):
`cross_model_divergence, frontal_passage_intraday, metar_dtdt, metar_trend, nwp_analog,
nwp_analog_ensemble, nwp_dtdt_fusion, pressure_delta, pressure_tendency,
wind_direction_shift`.

| fusion mode | n | acc | MCC | up-prec | up-edge | Sharpe |
|---|---|---|---|---|---|---|
| loop | 14020 | 54.24% | −0.0088 | 33.7% | −0.6pt | 1.35 |
| **majority** | 12018 | 58.01% | **+0.0588** | 38.3% | **+4.0pt** | 2.58 |
| weighted | 14020 | 56.86% | +0.0383 | 36.8% | +2.5pt | 2.20 |
| uwc / pool-of-pools | 14020 | 34.24% | 0.0000 | 34.2% | +0.0pt | −5.27 |

The best "honest" fusion is **majority vote at MCC +0.059** — real but **weaker than the
standalone `nwp_dtdt_fusion` (+0.106) and `frontal_passage_intraday` (+0.108)**. Even
restricting to non-artifact signals, pooling dilutes rather than concentrates.

---

## 4. Additive-stack results (marginal contribution)

| additive | directional output on axis | marginal effect on fusion |
|---|---|---|
| frontal_detector | 0 rows (dead) | none — not a directional voter |
| frontal_passage_detector | 0 rows (dead) | none |
| frontal_passage_nowcast | 0 rows (dead) | none |
| six Gamma signals (g1–g6) | not in directional registry | none — return degF offsets |
| co-occurrence / context modules | n/a (stacking logic) | none — cap offsets, not directions |

The additive stack (`core/additive/*`) produces **temperature offsets in degF**, applied
as an L3 mean-adjustment to the LOOP output — not directional votes. On the strike-crossing
*directional* axis their marginal contribution is zero by construction. They are a
*separate concern* (point/offset accuracy), not a directional fusion input. R/T-2 §5's
"import cleanly" was correct; the additive layer was never meant to be a directional edge
source, and this sweep confirms it contributes nothing directional.

---

## 5. Combination test (the actual edge question)

Two thin candidates: `nwp_dtdt_fusion` (MCC +0.106, n=9,827) and
`frontal_passage_intraday` (MCC +0.108, n=2,809). Coverage union = 10,719 rows;
overlap = 1,917 rows.

| combination | n | acc | MCC | up-prec | up-edge | note |
|---|---|---|---|---|---|---|
| nwp_dtdt_fusion (alone) | 9827 | 58.98% | +0.106 | 47.5% | +8.0pt | R/T-2 candidate |
| frontal_passage_intraday (alone) | 2809 | 65.82% | +0.108 | 43.4% | +11.4pt | R/T-2 candidate |
| log-odds MEAN pool (naive LOOP) | 10719 | 54.81% | −0.0081 | 37.0% | −0.6pt | ❌ pooling dilutes |
| **OR (either says "up" → up)** | 10719 | 60.41% | **+0.124** | 46.7% | +9.1pt | ✅ **best** — beats both individuals |
| AND (both must agree) | 1192 | 63.59% | +0.113 | 50.6% | +14.1pt | ✅ sharpest up-prec, tiny coverage |

**The combination that beats either individual is a candidate-selective OR (union), not
log-odds pooling.** OR over the two candidates reaches MCC +0.124 — a genuine improvement
over both +0.106 and +0.108 — because it broadens the up-call surface without importing
the 20 diluting signals. AND is even sharper on the skilled (up) side (up-prec 50.6% vs
34.4% base) but covers only 1,192 rows (both must fire *and* agree).

---

## 6. Verdict — does the fused system have edge individual signals lack?

**No — not as wired. The LIOP/LOOP pool-of-pools and UWC cascade are base-rate artifacts
on this axis, not edge concentrators.** The historical "LIOP 59.86% / UWC cascade" edge
folklore does not reproduce on the correct strike-crossing axis: the canonical fusion
produces MCC ≈ 0 (−0.0008 to 0.0000), while the *only* edge lives in two individual
signals that pooling actively dilutes.

Three concrete conclusions for the Gray Room:

1. **Do NOT greenlight the fusion layer as an edge source.** LOOP, Beta pool-of-pools,
   UWC cascade, weighted — all MCC ≈ 0 on 14,130 labels. The fusion layer is currently a
   *dilution* layer, not a *concentration* layer.

2. **The only incremental "fusion" that works is candidate-selective OR**, reaching MCC
   **+0.124** (up-edge +9.1pt, n=10,719) over the two genuine candidates. This is
   confirmation/union logic, not the pool-of-pools. It is a *different architecture* than
   the LIOP/UWC that was historically credited.

3. **The additive stack is out of scope for directional edge** — it is a degF-offset /
   point-accuracy layer, not a directional voter. Any prior claim that additive signals
   "compound" directional edge is a category error.

**Net recommendation to Dan:** the two genuine candidates (`nwp_dtdt_fusion`,
`frontal_passage_intraday`) remain the only directional edge. Their OR-union (MCC +0.124)
is worth out-of-sample walk-forward validation as the *only* fusion configuration that
adds value. The LIOP/LOOP pool-of-pools and UWC cascade should be treated as
**no-edge on the directional axis** until proven otherwise. Kill list stays PARKED.

---

## 7. Receipts

- Structured JSON (fusion + additive + combination): `reports/rt2b_fusion_additive_sweep_20260919-045950.json`
- Prediction-matrix cache (14,130 rows × 22 firing signals): `reports/rt2b_prediction_matrix_cache.json`
- Driver (read-only, new file): `scripts/rt2b_fusion_additive_sweep.py`
- This doc: `docs/plans/RT2B-FUSION-ADDITIVE-SWEEP-2026-09-19.md`

**Boundary note:** the strike backfill was re-run (`--commit`) to restore the regressed
truth axis — this is the Dan-approved backfill script, read-only on existing data (updates
2 strike columns only), no signal wiring touched, no git commit performed. Kill list PARKED.
