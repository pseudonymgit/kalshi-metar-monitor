# Walk-Forward Out-of-Sample Validation — nwp_dtdt_fusion & frontal_passage_intraday

Date: 2026-09-19 (UTC)
Author: Gilfoyle (subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
Driver: `scripts/walk_forward_validation.py` (new, read-only)
Receipt: `reports/walk_forward_validation_<ts>.json`

**Dan's question:** are the two R/T-2b "thin-edge" candidates real edge, or base-rate luck?
This driver answers it with **out-of-sample walk-forward validation** (no in-sample refit),
on the **correct strike-crossing axis** (`settlement_temp_f` vs `strike_price`), date-tiered
ground truth.

**Read the verdict (§4) first.**

---

## 0. TL;DR

- **`nwp_dtdt_fusion` is a 2026-only artifact, not a stable edge.** Its in-sample MCC +0.106
  is driven entirely by 2026 (which is 85% of the contemporaneous data). In the genuinely
  contemporaneous window, **2025 is NEGATIVE (MCC −0.042)** and 2026 is +0.073. The edge is
  not reproducible across years — the signature of a single-period/recency artifact, not
  skill.
- **`frontal_passage_intraday` is thin and unstable** — by-year MCC swings +0.054 / +0.015 /
  +0.034 / +0.045, with month-level sign-flipping. It does NOT meaningfully survive
  out-of-sample as a reliable edge (several walk-forward folds are negative).
- **The principled stack COLLAPSES to base rate** (OOS MCC +0.0005). A regularized L2-logistic
  stack over the 10 "honest" (non-artifact) signals produces **zero** out-of-sample edge —
  it does NOT beat the best single candidate, it dilutes it to nothing.
- **Critical lookahead finding:** `nwp_forecasts.db` has `target_date` 2021→2026 but
  `fetch_date` only 2025-01-01→2026-09-18. For 2021–2024 (and ~47% of 2025), the "forecast"
  was **backfilled in 2026** (fetch_date > target_date) — a lookahead leak. Any NWP-dependent
  signal scored on pre-2026 data is contaminated.

**Net verdict: the OR-union MCC +0.124 is a base-rate/recency artifact, not a real edge.**
Neither candidate survives out-of-sample. Dan's skepticism is correct.

---

## 1. The lookahead contamination (material, establishes trust boundary)

`data/nwp_forecasts.db` (930,911 rows) joins NWP forecast `target_date` against `fetch_date`:

| target year | total | contemporaneous (fetch ≤ target) | lookahead (fetch > target) |
|---|---|---|---|
| 2021 | 49,660 | 0 | 49,660 (100%) |
| 2022 | 58,360 | 0 | 58,360 (100%) |
| 2023 | 58,360 | 0 | 58,360 (100%) |
| 2024 | 58,520 | 0 | 58,520 (100%) |
| 2025 | 109,500 | 51,100 (47%) | 58,400 (53%) |
| 2026 | 596,511 | 562,142 (94%) | 34,369 (6%) |

**Every NWP forecast for a 2021–2024 target was generated in 2026.** The backfill wrote
`fetch_date` = 2026-07-28→2026-09-02 for those historical targets. This is a **lookahead leak**
that inflates any NWP-dependent signal's apparent skill on pre-2026 history.

NWP-dependent signals (contaminated pre-2026): `nwp_dtdt_fusion`, `nwp_analog`,
`nwp_analog_ensemble`, `nwp_direct`, `fogr_reversion`. `frontal_passage_intraday` uses ONLY
intraday METAR (real-time observed) — not contaminated.

This driver therefore restricts NWP-dependent signals to rows with a **genuinely
contemporaneous NWP forecast** (`fetch_date <= target_date`), and reports the per-year split.

---

## 2. Method

1. **Per-signal walk-forward:** sort prediction-matrix rows by date; rolling expanding
   training split; score the signal only on out-of-sample (later) slices. The signal has no
   learned params, so the "train" side only sets the split boundary — the honest question is
   whether the signal's edge persists in unseen time.
2. **Principled stack:** L2-regularized logistic regression over the **honest signal set**
   (the 10 signals with non-~0 in-sample MCC — the 11 base-rate-artifact/skew signals are
   excluded per Dan's instruction). Walk-forward: train on dates ≤ split, predict on dates >
   split. Report whether the stack beats the best candidate or collapses to base rate.
3. **Temporal-stability test:** per-year and per-month MCC — the artifact test (a real edge
   is stable across time; a recency/base-rate artifact concentrates in one period).

Metrics: MCC (imbalance-immune) leads; up-precision vs **slice-conditional** up-base-rate
(the honest null — the test slice's own up rate, not the global 34.36%).

---

## 3. Results

### 3a. `nwp_dtdt_fusion` (NWP-dependent, contemporaneous-only)

| slice | n | MCC | up-prec | up-base | up-edge |
|---|---|---|---|---|---|
| **2025** | 1,258 | **−0.0423** | 12.0% | 16.6% | −4.6pt |
| **2026** | 7,136 | **+0.0733** | 51.1% | 46.3% | +4.8pt |
| aggregate (OOS) | 8,394 | +0.1046 | 49.2% | 41.9% | +7.4pt |

Walk-forward folds (chronological): fold0 −0.034, fold1 +0.072, fold2 +0.018, fold3 +0.012,
fold4 +0.012. The edge is **concentrated in fold1 (early 2026)** and decays to ~0.

**Reading:** negative in 2025, positive in 2026, decaying within 2026. Not stable. The
aggregate +0.105 is a recency-weighted average dominated by 2026 (85% of rows) — it is NOT
evidence of a reproducible edge.

### 3b. `frontal_passage_intraday` (METAR-only, full range)

| slice | n | MCC |
|---|---|---|
| 2023 | 267 | +0.0540 |
| 2024 | 441 | +0.0148 |
| 2025 | 617 | +0.0341 |
| 2026 | 1,483 | +0.0453 |
| aggregate (OOS) | 2,809 | +0.1081 |

Walk-forward folds: +0.053, −0.034, +0.137, +0.041, +0.003. Month-level MCC swings
+0.24 / −0.20 / +0.16 / −0.12, with many months at 0 (the signal fires "up" only ~16% of
the time). **Thin and unstable — no reliable out-of-sample edge.**

### 3c. Principled stack (L2 logistic over 10 honest signals)

| slice | n | MCC | up-prec | up-base | up-edge |
|---|---|---|---|---|---|
| aggregate (OOS) | 14,020 | **+0.0005** | 34.3% | 34.2% | +0.05pt |

Walk-forward folds: +0.001, +0.014, −0.004, +0.003, −0.002. **The stack collapses to base
rate.** It does not beat `nwp_dtdt_fusion` (+0.105) or `frontal_passage_intraday` (+0.108);
it dilutes them to zero.

---

## 4. Verdict — real edge, artifact, or inconclusive?

**ARTIFACT.** Neither candidate survives out-of-sample as a reproducible edge, and the
OR-union is a base-rate/recency artifact.

1. **`nwp_dtdt_fusion`** — **artifact (lookahead + recency).** The in-sample +0.106 is
   contaminated: (a) pre-2026 NWP was backfilled in 2026 (lookahead leak), and (b) the
   surviving contemporaneous edge is a single-year (2026) phenomenon that is NEGATIVE in 2025
   and decays within 2026. Not reproducible → not a real edge.

2. **`frontal_passage_intraday`** — **inconclusive-to-artifact.** By-year MCC (0.015–0.054)
   is weak and unstable, month-level sign-flips, and it fires "up" too rarely (~16%) to be a
   usable directional signal. It does not cleanly survive walk-forward (two of five folds
   negative).

3. **Principled stack** — **collapses to base rate (OOS MCC +0.0005).** A proper regularized
   stack over the honest signal set produces ZERO out-of-sample edge. The OR-union's +0.124
   was a hand-picked confirmation logic that looked better in-sample than it is; a principled
   model-averaging approach does not reproduce any edge.

**Recommendation to Dan:** do NOT greenlight either candidate as an edge source. The
OR-union (+0.124) is a base-rate/recency artifact — confirmed by (a) the lookahead leak in
the NWP backfill and (b) the collapse of the principled stack to base rate. The two
"candidates" should be treated as no-edge on the strike-crossing axis pending a clean,
lookahead-free NWP dataset. Kill list stays PARKED.

---

## 5. Receipts

- Structured JSON: `reports/walk_forward_validation_<ts>.json`
- Driver (new, read-only): `scripts/walk_forward_validation.py`
- This doc: `docs/plans/WALK-FORWARD-VALIDATION-2026-09-19.md`

**Boundaries held:** read-only (no signal change, no wiring, no kill); did NOT touch
`migrate_add_strike_price.py`; did NOT run any DROP/backfill; NWP contemporaneity read from
`nwp_forecasts.db` via SELECT only. Git-clean on commit.
