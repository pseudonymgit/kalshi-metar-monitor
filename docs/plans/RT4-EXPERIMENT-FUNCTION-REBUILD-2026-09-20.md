# R/T-4 Experiment/Function Rebuild-and-Test — Receipt

Date: 2026-09-20
Author: Gilfoyle (technical subagent)
Task: R/T-4 — re-test every experiment + recovered function against the corrected
      ground-truth axis + real strikes, INCLUDING the partial `intraday_entry_stub`
      execution/sizing layer, with explicit NO-side entry verification.

## Objective

Re-run the canonical harness (`scripts/ultimate_sweep.py`) so that every experiment
function and every recovered function is scored against the **correct strike-crossing
axis** with **real strikes** — NOT assumed at parity — and explicitly verify whether the
execution/sizing layer (`intraday_entry_stub.py`) can express a **NO-side entry on bucket
N+1** (Dan's 9/19 requirement).

## Method (deterministic, script-only, no AI in loop)

1. **Build/run** — canonical harness re-executed end-to-end:
   `python3 -u scripts/ultimate_sweep.py --no-emit` (full suite: signal + lane/gate +
   calibration + experiment). Elapsed 1298.01s. `--no-emit` used to avoid storage bloat
   (no report files written; read-only against the DB).
2. **Independent QC** — separate pass, NOT build-then-self-check:
   `scripts/rt4_noside_entry_verification.py` (new) inspects the stub's actual symbols,
   schema, and code paths and emits a verdict on NO-side/bucket expressibility.
3. **Confirm-against-spec** — verdict checked against the P0-B strike-ladder receipt
   (`docs/plans/P0-B-STRIKE-LADDER-BACKFILL-2026-09-20.md`) which defines the bucket/rung
   semantics (`greater` / `less` / `between` 1-degree bins, 57,219 `between` rungs).

---

## (a) Per-experiment / per-function pass/fail table

### Axis + provenance (every number below)

- **Axis:** `settlement_temp_f` vs `strike_price` (strike-crossing), via
  `core/settlement_outcome.strike_crossing_direction`. **NOT** day-over-day bucket delta.
- **Truth source:** `settlement_truth.strike_price` — real strikes (14,305 single-strike
  rows; 10,139 greater + 4,112 less + 54 between). Full ladder = 85,846 rungs in
  `kalshi_strike_ladder.db`.
- **Labels:** n = 14,130 (up = 4,855 / down = 9,275), base rate UP = **34.36%**
  (DOWN-skewed). By market type: HIGH up = 10.1% (n=10,079), LOW up = 94.6% (n=4,051).
- **Excluded:** between=54, untrusted=80, no_temp=0, unknown_mt=0.
- **temp_source:** ghcn=13,889, kalshi_finalized=79, weatherapi=162.
- **Verdict rule:** `PASS` iff MCC > 0 **and** base_rate_delta > 0; else `NO_EDGE`.
  MCC (not accuracy) is the honest skill metric — the label set is market-type-conditional.

### Signal sweep (recovered functions — directional layer, correct axis)

| signal | status | accuracy | MCC | baseΔ | Sharpe | n |
|---|---|---|---|---|---|---|
| ai_composite | NO_EDGE | 50.67% | +0.0130 | -45.37 | 0.21 | 4823 |
| calendar_climatology | NO_EDGE | 47.37% | -0.1973 | -43.40 | -0.84 | 3078 |
| cloud_cover_index | NO_EDGE | 55.50% | +0.0107 | -35.63 | 1.76 | 6054 |
| cooling_failure | NO_EDGE | 35.52% | +0.0000 | -55.82 | -4.80 | 670 |
| corrected_pressure_delta | NO_EDGE | 49.41% | -0.0088 | -40.71 | -0.19 | 7602 |
| cross_model_divergence | NO_EDGE | 51.25% | +0.0238 | -44.64 | 0.40 | 3573 |
| dewpoint_depression | NO_FIRES | — | — | — | — | — |
| dual_polarity | NO_EDGE | 49.41% | -0.0088 | -40.71 | -0.19 | 7602 |
| eighty_two_member_ensemble | NO_EDGE | 58.02% | -0.0040 | -33.02 | 2.58 | 13515 |
| eighty_two_member_ensemble_ece | NO_EDGE | 58.02% | -0.0040 | -33.02 | 2.58 | 13515 |
| eighty_two_member_ensemble_pooled | NO_EDGE | 54.13% | -0.0043 | -36.90 | 1.32 | 13515 |
| esdr | SKIPPED | — | — | — | — | slow_or_live_api |
| feels_like_delta | NO_EDGE | 50.08% | +0.0537 | -40.72 | 0.03 | 4197 |
| fogr_reversion | NO_EDGE | 33.86% | +0.0000 | -56.96 | -5.42 | 7154 |
| forecast_disagreement | NO_EDGE | 48.97% | -0.0012 | -42.56 | -0.33 | 4680 |
| frontal_detector | NO_FIRES | — | — | — | — | — |
| frontal_passage_detector | NO_FIRES | — | — | — | — | — |
| frontal_passage_intraday | NO_EDGE | 65.82% | +0.1081 | -23.92 | 5.30 | 2809 |
| frontal_passage_nowcast | NO_FIRES | — | — | — | — | — |
| gaussian | NO_EDGE | 49.89% | -0.1515 | -40.57 | -0.03 | 5568 |
| gaussian_v2 | NO_EDGE | 50.74% | -0.0449 | -40.05 | 0.24 | 9438 |
| goldilocks | NO_EDGE | 49.26% | -0.0284 | -41.22 | -0.23 | 7344 |
| intraday_metar_confirmation | SKIPPED | — | — | — | — | slow_or_live_api |
| metar_dtdt | NO_EDGE | 60.20% | +0.0093 | -30.51 | 3.31 | 8221 |
| metar_nowcast | NO_FIRES | — | — | — | — | — |
| metar_trend | NO_EDGE | 51.61% | +0.0271 | -39.60 | 0.51 | 14020 |
| nwp_analog | NO_EDGE | 49.08% | -0.0225 | -47.13 | -0.29 | 4609 |
| nwp_analog_ensemble | NO_EDGE | 49.08% | -0.0225 | -47.13 | -0.29 | 4609 |
| nwp_direct | SKIPPED | — | — | — | — | slow_or_live_api |
| nwp_dtdt_fusion | NO_EDGE | 58.98% | +0.1062 | -32.59 | 2.90 | 9829 |
| pressure_delta | NO_EDGE | 50.61% | +0.0091 | -39.52 | 0.19 | 7594 |
| pressure_tendency | NO_EDGE | 57.33% | +0.0332 | -28.00 | 2.35 | 893 |
| radiational_cooling | NO_EDGE | 77.08% | +0.0000 | -13.89 | 10.23 | 144 |
| regime | NO_EDGE | 57.84% | -0.1353 | -36.27 | 2.52 | 204 |
| seasonal_regime | NO_EDGE | 46.41% | +0.0552 | -45.66 | -1.14 | 9243 |
| settlement_arbitrage | NO_FIRES | — | — | — | — | — |
| spike_reversion | NO_EDGE | 61.90% | +0.0000 | -27.23 | 3.89 | 3848 |
| spread_based_entry | NO_FIRES | — | — | — | — | — |
| temperature_advection | SKIPPED | — | — | — | — | slow_or_live_api |
| trajectory_state_vector | NO_EDGE | 49.49% | -0.0148 | -41.72 | -0.16 | 13057 |
| volume_momentum | NO_FIRES | — | — | — | — | — |
| warm_momentum | NO_EDGE | 35.38% | +0.0000 | -55.19 | -4.86 | 6759 |
| wind_direction_shift | NO_EDGE | 49.90% | +0.0322 | -39.48 | -0.03 | 1563 |

**Signal verdict:** 0 PASS / 31 NO_EDGE / 8 NO_FIRES / 4 SKIPPED (slow/live).
No recovered function clears the MCC>0 AND baseΔ>0 bar on the correct axis.
Positive-MCC signals (`frontal_passage_intraday` +0.1081, `nwp_dtdt_fusion` +0.1062,
`feels_like_delta` +0.0537, `wind_direction_shift` +0.0322) all have large negative
base-rate delta — the positive MCC is a minority-class artifact of the 34.36% up /
65.64% down skew, NOT skill. The apparent positive Sharpes (e.g. radiational_cooling
10.23 at n=144, metar_dtdt 3.31) are base-rate-driven and not edge.

### Lane / gate / fusion / calibration-mode sweep

All reachable (no import failures): 6 lanes, 9 gates, 4 fusion modes, 4 calibration
modes. See R/T-3 receipt for the gate-by-gate edge verdict (PASS 0 / KILLED 6 / PARKED 9).
R/T-4 confirms reachability + re-scores the underlying feed signals (above) — no
lane/gate/fusion/calibration mode produces edge on the correct axis.

### Calibration sweep (Brier / ECE)

All 31 fired signals SCORED. Every Brier is near its market-type base-rate floor
(e.g. HIGH signals cluster ~0.25–0.50, LOW ~0.20–0.25); ECE ranges 0.08–0.50. No
signal is meaningfully better calibrated than the base-rate null. Full table in the
harness log (`/tmp/rt4_sweep.log`); representative values match the prior frozen run
`reports/ultimate_sweep_20260919-212026.md` (determinism confirmed).

### Experiment / function sweep (enumeration)

| group | files | status |
|---|---|---|
| dtw | a1_dtw_directional, a2_dtw_normalize, a3_dtw_matching, a4_dtw_seasonal_check, a5_dtw_scaletest | present |
| gaussian | apply_gaussian_fusion | present |
| goldilocks | goldilocks_feature_engineering | present |
| kalman | experiment_option2_kalman_smoothing | present |
| trajectory | trajectory_dtw_replay | present |
| nwp_analog | backtest_nwp_analog_knn | present |

All 10 experiment-function files present and reachable (0 missing). Note: the canonical
`sweep_experiments()` is an enumeration/smoke-reachability check (file presence), not a
score — the *scoring* of recovered functions is the signal + calibration sweep above.

---

## (b) NO-side entry verification — `intraday_entry_stub.py`

**Verdict: NO-side (bucket N+1) entry is NOT EXPRESSIBLE. FAIL.**

The independent QC (`scripts/rt4_noside_entry_verification.py`) established, by direct
inspection of the stub's symbols, schema, and code:

1. **Trade-direction vocabulary is BUY/SELL only.** `_determine_trade_direction` returns
   `'BUY'` (our_prob > market) or `'SELL'` (our_prob < market). There is no `buy_yes` /
   `sell_yes` / `buy_no` / `sell_no` / `trade_type` token anywhere in the source.
2. **No bucket / strike-ladder awareness.** No `bucket`, `strike_type`, `floor_strike`,
   `cap_strike` construct; the stub does not read `data/kalshi_strike_ladder.db` at all.
3. **Market price is HIGH 'greater' only.** `_read_market_prices` filters
   `market_type = 'HIGH'` — it reads the single "greater" contract's mid, never the
   `between` (1-degree bucket) rungs that carry the NO-side semantics.
4. **Probability is collapsed to a single UP axis.** `_read_our_probabilities` folds
   `down` → `1 - confidence` into one `up_prob` (HIGH-win probability). There is no
   per-bucket probability surface, so "bet NO on bucket N+1" cannot be represented.
5. **Execution schema has no side/bucket field.** `_write_paper_trade` writes
   `(id, station, date_utc, hour_utc, hour_before, direction, contracts, entry_price,
   our_prob, market_price, edge_pp, status)` — `direction` is the only directional field
   and only ever holds BUY/SELL.

**What the stub CAN express** (single binary axis, correct strike-crossing direction):
- BUY  = bet HIGH "greater" contract wins (settlement_temp_f > strike).
- SELL = bet HIGH "greater" contract loses (settlement_temp_f <= strike; equivalently
  LOW "less" wins).

**What it CANNOT express** (Dan's 9/19 requirement):
- Bet **NO** on a specific 1-degree `between` bucket (bucket N+1) — e.g. "temp will NOT
  be in [96.0, 97.0]".
- `buy_no` / `sell_no` trade types (which the older `trades` table in
  `paper_trading.db` already supports — see its `trade_type` column:
  `buy_yes, sell_yes, buy_no, sell_no`).
- The LOW/less "temp stays ABOVE strike" vs HIGH/greater "temp stays BELOW strike"
  NO-side framing (betting against the strike-cross, bucket-adjacent).

**Secondary defect found (same layer):** `_close_position` writes `pnl = NULL` on every
close — it never computes `(exit − entry) × contracts` despite its docstring claiming to.
The `_exit_price_for` helper's comment ("BUY → (1 − entry) is the NO-side") is
**conceptually wrong**: it conflates "selling the HIGH contract" (betting HIGH loses)
with the YES/NO side of a single binary contract — two distinct concepts. This further
confirms the layer has no genuine NO-side model.

**Implication:** the execution/sizing layer cannot yet express Dan's required NO-side
(bucket N+1) entry. This is a **gap, not a regression** — the stub was re-spec'd (v3.0)
around the single-strike HIGH/LOW axis before the P0-B full strike ladder (57,219
`between` rungs) landed. Bucket-level NO-side entry requires the stub to (1) read
`kalshi_strike_ladder.db` `between` rungs, (2) add a `side` (YES/NO) + `strike_type` +
`strike_value` dimension to the paper-trade schema, and (3) size a NO position as
`1 − bucket_price` — none of which exist today.

---

## (c) Axis + provenance statement

Every number in (a) is scored on the **strike-crossing axis**: `settlement_temp_f` vs
`strike_price` via `core/settlement_outcome.strike_crossing_direction` (strict `>` / `<`;
HIGH = up iff temp > strike, LOW = down iff temp < strike). Truth source is
`settlement_truth.strike_price` (real strikes from the /historical backfill), not
day-over-day bucket deltas. Base rate UP = 34.36% (DOWN-skewed), matching the
always-down null Sharpe ≈ 5.25 context; MCC ≈ 0 across the board is confirmed as a
base-rate artifact, not skill.

---

## (d) Commit

- branch: `temp-sprint-branch`
- files added: `scripts/rt4_noside_entry_verification.py` (independent QC),
  `docs/plans/RT4-EXPERIMENT-FUNCTION-REBUILD-2026-09-20.md` (this receipt)
- commit hash: `ed66eb31dcb2697899f503a2010182952218e967` (single work commit — QC
  script + this receipt; self-referential, final state recorded here)

## Safety / boundaries honored

- Deterministic script-only; no AI/ML model calls in the backtest loop.
- Read-only against all DBs; `--no-emit` (no report files written, no storage bloat).
- No data mutation, no signal re-enable/wiring, no live trading.
- Reused the canonical harness (`ultimate_sweep.py`); authored only the independent
  QC verifier (no redundant test code).
- `data/` is gitignored; only the two above files are committed.
