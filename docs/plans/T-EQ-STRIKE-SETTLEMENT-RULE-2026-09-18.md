# T == Strike Settlement-Direction Rule — Verification Report

**Date:** 2026-09-18
**Owner:** Gilfoyle (subagent)
**Trigger:** Dan surfaced a suspected ground-truth correctness bug in the Weather Engine's settlement-direction labeling.
**Status:** VERIFIED. Strike-crossing rule is **strict** (confirmed against live Kalshi API). The tie-operator itself is CORRECT — no `>=`/`<=` fix needed. The REAL bug is a **day-over-day vs strike-crossing conflation** (see §4), which does cause mislabels in calibration/backtest.

---

## 0. Dan's Refined Model (2026-09-18 11:16 — incorporated)

Dan clarified the actual concern: the thing that pays out is **MOVEMENT across the strike**, not day-over-day change, and not a three-way up/down/flat vs yesterday. Two concepts are conflated and must be separated:

1. **`paper_test_controller.py`** labels up/down/**flat** by **day-over-day** change (settlement vs prior settlement). Its "flat" = "same temp as yesterday" — **WRONG concept**.
2. **`pnl_tracking.py` + `calibrate_all_signals.py`** label by **strike-crossing** (settlement > strike) — **RIGHT concept**.

Dan's target model (market-relative strike-crossing):
- HIGH market: did temp tick UP over the strike (cross above)?
- LOW market: did temp tick DOWN under the strike (cross below)?
- "flat" = no movement across the strike (didn't reach/cross threshold) = no directional outcome.

This section (a/b/c below) answers Dan's three sub-questions directly.

---

## 1. The Question

The codebase labels settlement direction using **strict** comparison, assumed since Phase A-1 and never verified against Kalshi's actual rule:

- HIGH: `'up' if settlement_temp > strike else 'down'` (strict `>`)
- LOW:  `'down' if settlement_temp < strike else 'up'` (strict `<`)

Under this rule, **T == strike settles as a LOSS on both sides** (the tie is excluded from win on both HIGH and LOW).

**Hypothesis to verify:** Is Kalshi's actual rule **inclusive** (`>=` for HIGH / `<=` for LOW), which would mean every tie in our history is mislabeled as a loss, corrupting direction accuracy + calibration at the boundary?

---

## 2. The Authoritative Rule (verified against live Kalshi API)

I pulled the actual settlement rule text from Kalshi's live trade API (`api.elections.kalshi.com/trade-api/v2/markets/{ticker}`), using the `rules_primary` and `title` fields — the authoritative, contract-level settlement language. I did **not** rely on the codebase's assumptions or internal specs.

### Finding: Kalshi does NOT use a two-sided "HIGH = up / LOW = down" binary at all.

Kalshi's weather markets are a **temperature ladder**, where each series (`KXHIGH*` = daily max, `KXLOW*` = daily min) contains multiple markets with **three distinct strike types**:

| `strike_type` | Ticker suffix | `rules_primary` language | Settles to Yes when | T == boundary |
|---|---|---|---|---|
| `greater` | `-T##` | "**greater than** X°" | `temp > X` (strict) | **LOSS** |
| `less` | `-T##` (bottom rung) | "**less than** X°" | `temp < X` (strict) | **LOSS** |
| `between` | `-B##.#` | "**between** X–Y°" | `X ≤ temp ≤ Y` (inclusive) | **WIN** (both ends) |

### Concrete evidence (live API, finalized markets):

**HIGH / `greater` — `KXHIGHTATL-26SEP08-T91`:**
```
title:         "Will the maximum temperature be >91° on Sep 8, 2026?"
rules_primary: "If the maximum temperature recorded at Atlanta (CLIATL) for Sep 8, 2026,
                is greater than 91° fahrenheit according to The Weather Company,
                then the market resolves to Yes."
strike_type:   greater, floor_strike: 91
yes_sub_title: "92° or above"   (integer settlement: ">91°" ≡ "≥92°")
result:        no                (expiration_value 88.0, which is < 91 → correctly "no")
```

**HIGH / `greater` — `KXHIGHLAX-26SEP08-T89` (a tie-adjacent example):**
```
title:         "Will the maximum temperature be >89° on Sep 8, 2026?"
rules_primary: "...greater than 89°..."
strike_type:   greater, floor_strike: 89
yes_sub_title: "90° or above"
result:        yes               (expiration_value 92.0 → 92 > 89 → correctly "yes")
```

**LOW / `less` — `KXLOWTATL-26SEP19-T68`:**
```
title:         "Will the minimum temperature be <68° on Sep 19, 2026?"
rules_primary: "If the minimum temperature ... is less than 68° fahrenheit ... resolves to Yes."
strike_type:   less, cap_strike: 68
```

**LOW / `between` — `KXLOWTATL-26SEP08-B71.5`:**
```
title:         "Will the minimum temperature be 71-72° on Sep 8, 2026?"
rules_primary: "...between 71-72° fahrenheit ... resolves to Yes."
strike_type:   between, floor_strike: 71, cap_strike: 72
result:        yes               (expiration_value 71.0 → 71 is in [71,72] → "yes", inclusive)
```

### Conclusion on the tie question

**The hypothesis is NOT confirmed.** Kalshi's `greater` and `less` strike types use **strict** comparison (`>` / `<`), not inclusive. Therefore:

- For HIGH (`greater`), **T == strike is a LOSS** — and the code's `>` is correct.
- For LOW (`less`), **T == strike is a LOSS** — and the code's `<` is correct.

**No fix is required to the strict `>` / `<` comparison operators.** The existing code matches Kalshi's authoritative rule. The only place inclusivity applies is the `between` strike type, which the codebase does not currently model at all (see §4).

---

## 3. Tie Materiality

Because Kalshi strikes are integer °F (for `greater`/`less`) and the settlement temperature (`settlement_bucket`) is integer °F, ties (T == strike) are **real and non-negligible**, not vanishingly rare. However, since the correct resolution is LOSS (strict rule), a tie is **not a corruption** — it is a correctly-labeled loss.

Quantification attempted:

- `settlement_epochs` (in `data/metar_backfill.db`) has **117,557 closed rows**, `settlement_bucket` is integer °F spanning ~[-22, 119].
- There is **no `strike_price` column** in `settlement_epochs`. The strike is fetched separately from Kalshi (`_get_strike_price` in `core/pnl_tracking.py`) and is **not persisted** alongside settlement epochs, so a precise per-trade tie count cannot be derived from stored data.
- `market_snapshots` (`data/kalshi_market_snapshots.db`) holds point-in-time strikes but only for 20 stations over 2026-09-07 → 2026-09-18 (1,008 HIGH/LOW rows), too narrow for historical tie quantification.
- The integer strike ladder spans ~47–112°F across the HIGH/LOW series, so the raw fraction of settlement buckets that happen to equal *some* integer strike is ~72% — but this is **not** the per-market tie rate; it merely reflects that integer strikes densely tile the temperature range.

**Honest impact statement:** Per-market ties occur at roughly the frequency of "temp exactly equals the strike" — on the order of ~1-in-50 to ~1-in-100 of trades for a given strike, depending on the distribution. They resolve to LOSS under Kalshi's rule, which is **correctly implemented**, so they do not corrupt direction accuracy or calibration.

---

## 4. Answering Dan's Three Questions (a / b / c)

### (a) Confirm the Kalshi strike-crossing rule — inclusive `>=` vs strict `>`

**STRICT, not inclusive.** Confirmed against live API `rules_primary` contract text on finalized markets:

- `greater` (top-tail): "**greater than** X°" → Yes if `temp > X`. **T == X → LOSS.**
- `less` (bottom-tail): "**less than** X°" → Yes if `temp < X`. **T == X → LOSS.**
- `between` (middle bins): "**between** X–Y°" → Yes if `X ≤ temp ≤ Y`. **Inclusive** — but this is a *bin*, not a "cross above/below a single strike" event.

So for the strike-crossing concept Dan cares about (HIGH crossing above a `greater` strike, LOW crossing below a `less` strike), the boundary is **strict**. The existing `>` / `<` operators in `pnl_tracking.py` are **correct** and must NOT be changed to `>=`/`<=` (that would wrongly credit exact-ties as wins).

### (b) Reconcile `paper_test_controller.py` day-over-day "flat" with the strike-crossing model

**The "flat" in `paper_test_controller.py:588-593` is the wrong concept.** It means "same temp as yesterday" (`settlement_value == prev_settlement_value`). Under Dan's model, "flat" should mean "**did not cross the strike**" (no directional outcome). These are different events:

- Day-over-day "flat" (current): `settlement == prior` — a coincidence of two consecutive days having the same temperature.
- Strike-crossing "flat" (correct): `settlement` did not cross the `greater`/`less` strike boundary — e.g. HIGH with `settlement <= strike` never ticked up over the strike.

**But** `record_settlement()` does not receive a `strike` parameter at all — its signature is `(settlement_value, prev_settlement_value, ...)`. It *cannot* compute strike-crossing without being passed the strike (and the correct strike type `greater`/`less`/`between`). So this is not just an operator fix — the function's **input contract is missing the strike**.

**Impact note:** `record_settlement` has exactly one caller, and it is a **test stub** (Test 7 in `core/paper_test_controller.py:811`), hardcoded with `settlement_value=88.0, prev_settlement_value=85.0`. It is **not wired into the live settlement path** — the live path is `pnl_tracking.py`, which *does* use strike-crossing correctly. So the `paper_test_controller` conflation is real but currently **latent** (test-only). The fix is to (1) add `strike` + `strike_type` to `record_settlement`, and (2) relabel `actual_direction` as strike-crossing (`up` = crossed above strike, `down` = crossed below strike, `flat` = did not cross), not day-over-day.

### (c) Is the day-over-day vs strike-crossing conflation causing mislabels in calibration/backtest?

**YES — this is the real, material bug.** The day-over-day (`settlement > prior` / `settlement_bucket > prior_settlement_bucket`) pattern is pervasive across the calibration/backtest/signal-pipeline paths, and it is labeling the **wrong target** (warming-vs-yesterday) where the code intends strike-crossing. The affected files (non-exhaustive):

| File | Line(s) | Label target | Conflation |
|---|---|---|---|
| `core/paper_test_controller.py` | 588-593 | `settlement_value` vs `prev_settlement_value` | day-over-day "flat" |
| `core/unified_backtest.py` | 276 | `settlement_bucket` vs `prev_bucket` | day-over-day (2-way, flat→down) |
| `core/decision_output.py` | 130, 134 | `settlement` vs `prior` | day-over-day |
| `core/paper_trading_engine.py` | 795, 812, 828 | `settlement_bucket > prior_settlement_bucket` (SQL) | day-over-day |
| `core/signal_pipeline.py` | 86, 103, 119 | same SQL | day-over-day |
| `core/climatology_pillar.py` | 290 | `settlement > prior` | day-over-day |
| `core/order_manager.py` | 1144 | `settlement_bucket > prior_settlement_bucket` | day-over-day |
| `core/kalshi_monitor.py` | 791 | `settlement_bucket > prior_settlement_bucket` | day-over-day |
| `scripts/phaseB_calibration_pipeline.py` | 167 | `settlement_bucket > prev_bucket` | day-over-day |
| `scripts/phaseB_signal_benchmarks.py` | 107, 211 | `settlement_bucket > prev_bucket` | day-over-day |
| `scripts/split_backtest.py` / `_hourly.py` | 427/431/514/559 | `settlement > prior` | day-over-day |
| `scripts/basic_backtest.py` | 88 | `settlement_bucket > prior_settlement_bucket` (SQL) | day-over-day |
| `scripts/backtest_low_markets.py` | 29 | same SQL | day-over-day |
| `scripts/nwp_analog_ensemble.py` | 122, 137 | `settlement_bucket > prior_settlement_bucket` | day-over-day |

**Two distinct failure modes in this conflation:**

1. **Wrong target variable:** A signal that predicts "will temp cross above the strike" is being scored against "did temp rise vs yesterday". These are anti-correlatable (temp can rise vs yesterday yet stay below strike → signal says WIN, label says "up" → counted correct, but the market LOST). This corrupts **direction accuracy** and **calibration** for any signal whose `direction` output is intended to map to strike-crossing.

2. **Running-max artifact (deeper):** `core/p3_backtest_engine_v2.py:11` documents that its predecessor compared `settlement_bucket > prior_settlement_bucket`, which is "always 'up' since settlement_bucket is the running daily max". This means in some data models `settlement_bucket` is the **running** max/min, not the final settled value — so `settlement_bucket > prior_settlement_bucket` is trivially true (a running max only increases). That engine was already identified as "fundamentally broken" for this reason.

**The strike-crossing fix is NOT an operator change (`>` → `>=`). It is a target-variable change** across every file in the table above: replace the day-over-day comparator with a strike-crossing comparator (`settlement > strike` for `greater`, `settlement < strike` for `less`, `floor ≤ settlement ≤ cap` for `between`), and supply the actual strike (from Kalshi `floor_strike`/`cap_strike`) rather than `prior_settlement_bucket`.

---

## 5. Files & Lines Reviewed (two non-interchangeable concepts)

### Concept 1 — Strike-crossing settlement (the CORRECT target)
These compare settlement against a **strike**. The strict `>`/`<` operator is **correct** (Kalshi `greater`/`less` rule). **No operator change needed.**

| File | Line | Current | Verdict |
|---|---|---|---|
| `core/pnl_tracking.py` | 81 | `1.0 if settlement_temp > strike_price else 0.0` (HIGH) | CORRECT (strict, matches Kalshi) |
| `core/pnl_tracking.py` | 83 | `1.0 if settlement_temp < strike_price else 0.0` (LOW) | CORRECT |
| `core/calibrate_all_signals.py` | 150 | `'up' if settlement_bucket > strike else 'down'` | Operator correct; but `strike` = **median-temperature proxy**, not a real Kalshi strike → still wrong target (see §4c) |

### Concept 2 — Day-over-day change (the WRONG target — needs strike-crossing relabel)
These compare settlement against **yesterday's** value. They label warming/cooling, NOT strike-crossing. This is the conflation Dan flagged; see §4c for the full list and fix direction.

---

## 6. Deliverable Summary

1. **Verified rule (authoritative, live API):** Kalshi `greater`/`less` strike-crossing is **strict** (`>` / `<`); `between` is inclusive. T == strike on a `greater`/`less` boundary is a **LOSS**. The code's strict operator is **correct** — do NOT change to `>=`/`<=`.
2. **The real bug (per Dan's refinement):** day-over-day labeling is conflated with strike-crossing labeling across the calibration/backtest/signal-pipeline paths (§4c). This **does** cause mislabels — the wrong target variable — and must be fixed by switching to strike-crossing comparators + supplying the real Kalshi strike, not by changing the tie operator.
3. **`paper_test_controller.py` "flat":** means "same as yesterday" and should mean "didn't cross strike" (§4b). Fix requires adding `strike` + `strike_type` to `record_settlement` (currently missing; function is test-stub-only).
4. **Files changed:** **None** in this pass (tree preserved; no commits, per boundary). The fix is a multi-file target-variable change across the §4c table — staged for Dan's review/merge decision.
5. **Tie materiality:** Strikes are integer °F, settlement is integer °F, so per-market ties are real (~1-in-50 to ~1-in-100 trades) but correctly resolve to LOSS under strict rule — they are **not** the corruption vector. The corruption vector is the day-over-day conflation.

### Recommended next action
Two distinct work items, both requiring Dan/Gerri disposition:

1. **Strike-crossing relabel (the bug):** Replace day-over-day comparators with strike-crossing comparators across the §4c file list, and persist/retrieve the real Kalshi `strike` (currently not stored in `settlement_epochs` — no `strike_price` column exists). This is the correctness fix.
2. **Ladder-vs-binary mis-model (structural):** The code models HIGH/LOW as single-strike binaries, but Kalshi is a `greater`/`less`/`between` ladder. `between` markets (where T == boundary is a WIN) are entirely unmodeled, and `_get_strike_price` collapses the ladder to one strike. This needs a follow-up decision on whether to model the full ladder.
