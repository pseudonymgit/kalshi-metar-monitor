# H1 + H2 — Research Hypothesis Preregistration

**Doc ID:** H1-H2-PREREGISTRATION-2026-09-17
**Status:** PREREGISTERED — documentation only, no execution
**Owner:** Marty Byrde (research lane) — hypothesis framing
**Date:** 2026-09-17 UTC
**Workstream:** H — parked research items returned to scope for preregistration
**Branch context:** `temp-sprint-branch` @ `04be98a04` (lineage freeze — see `P1-LINEAGE-FREEZE-2026-09-17.md`)
**Execution gate:** CLOSED — data preconditions not yet met (see §5, §10)

---

## 0. Purpose and discipline

This document preregisters two research hypotheses — **H1 (settlement arbitrage)** and
**H2 (volume momentum)** — that were previously **PARKED** and are now being brought back into
scope for **preregistration only**. No backtest, no trading, no database write, and no code,
config, or data mutation is performed by this artifact.

The value of a preregistration is that the hypothesis, data requirement, test design, and
success threshold are fixed **before** any result is observed. Once a hypothesis executes, it is
evaluated against these frozen criteria. A miss is a **fail-and-iterate** outcome, never a
**tune-until-green** outcome.

**Freeze contract (identical in spirit to `B2-PARAM-GRID-FREEZE-2026-09-17.md`):**

1. The hypothesis text, data requirements, test designs, and thresholds below are immutable.
2. Any change requires a new `H1-H2-PREREGISTRATION-<date>-REV2.md` that (a) records the change,
   (b) states the reason, and (c) re-preregisters the full hypothesis — it does **not** silently
   amend this document.
3. Neither hypothesis may be evaluated against live or historical data until its explicit
   precondition (§5, §10) is independently confirmed and recorded.

---

## 1. Shared dependency: the `z8` stability/provenance tier

Both hypotheses name a data dependency called **`z8`** in their preconditions. At the time of
this preregistration, `z8` is **not yet a defined, verified artifact** in the repository — it is a
forward reference to a stability/provenance tier that must be established before either
hypothesis is executable.

> **Naming disambiguation:** this `z8` (an abstract, hash-pinned, frozen provenance tier) is
> **NOT** `scripts/z8_kalshi_market_cron.py`. That script is a *live* Kalshi market snapshot
> collector (labeled "Z.8") producing a mutating snapshot DB, and it does **not** satisfy this
> tier's frozen/hash-pinned requirement. Do not conflate the two labels.

For the purposes of this preregistration, `z8` means:

> A point-in-time-frozen, provenance-tagged, corruption-audited dataset whose identity is
> pinned by content hash (sha256) and whose truth precedence is explicitly declared, such that a
> result consuming it is reproducible against the lineage freeze (`P1-LINEAGE-FREEZE-2026-09-17.md`).

The preconditions in §5 and §10 are deliberately stated as requirements **on `z8`** — they gate
execution on the establishment of that tier, not on the present (unstable, rapidly-mutating)
live DBs. Until a `z8` artifact is created, hashed, and recorded, both hypotheses remain **PARKED
at the execution layer**.

---

# H1 — Settlement Arbitrage

## 2. Hypothesis

**H1 (formal):** A systematic, executable edge exists between the **provenanced settlement
ground-truth price** and the **market's pre-settlement pricing** for weather markets, such that
a rule-based position taken against a **stable, provenance-tagged settlement source** yields a
positive risk-adjusted return after fees and the sanity-band offset correction.

**Directional claim:** Market pricing persistently deviates from the settleable ground truth by
more than the round-trip cost of trading that deviation, on a predictable schedule (settlement
approach).

**What H1 is NOT:** H1 does **not** claim an edge from *predicting weather better*. It claims an
edge from *settlement-source arbitrage* — the gap between what the market prices and what the
authoritative settlement source will resolve to. Weather forecasting accuracy is out of scope.

## 3. Data requirement

To test H1, the following must be available **as `z8`** (see §1):

1. **Stable settlement source** — a frozen, hash-pinned settlement-truth dataset with declared
   precedence. Per `S5-SOURCE-RECONCILIATION-2026-09-17.md`, precedence is:
   - **Primary:** Kalshi `source_type='finalized'` (1,641 rows, the economic truth).
   - **Fallback (weather truth):** GHCN daily high (`ghcn_settlements.db`, `ghcn_daily`
     provenance tag).
   - **Never authoritative:** `settlement_epochs.settlement_bucket` (METAR-derived proxy, carries
     a −1 °F convention offset **and** 424 garbage rows at 0.36% — see S5 §3) and Kalshi
     `source_type='historical_api'` (proxy/reconstructed).
2. **Provenance tags** on every settlement row (source + finalized vs. reconstructed).
3. **Corruption audit applied** — sanity band `[-60, 135] °F` enforced before any row enters the
   test set; garbage rows (e.g. `54082`, `30679`) excluded and counted.
4. **Market pre-settlement pricing** — bid/ask or last-trade series at defined times-to-settlement
   (T−24h, T−12h, T−6h, T−1h) for the same markets, hash-pinned.
5. **Fee/slippage schedule** — the round-trip cost model to apply to any candidate edge.

## 4. Test design

1. **Universe:** all weather markets with a matching finalized settlement and a pre-settlement
   price series in `z8`.
2. **Signal:** `edge = (market_price − settlement_truth) − round_trip_cost`, computed at each
   time-to-settlement checkpoint. A candidate position exists when `edge` exceeds a frozen
   threshold (default entry-price bounds from `B2-PARAM-GRID-FREEZE-2026-09-17.md` §3.2:
   `entry_price_min = 0.08`, `entry_price_max = 0.50`).
3. **Sanity-band gate:** any settlement row outside `[-60, 135] °F` is dropped and tallied as a
   data-quality count (reported separately, never silently imputed).
4. **Evaluation:** label-permutation / shuffle test against the null of "no persistent edge"
   (mirrors `FORMAL-SUCCESS-CRITERIA.md` Phase 9 criterion 1), plus a fee-adjusted directional
   accuracy metric.
5. **No post-hoc tuning:** all thresholds are fixed from the canonical config before any run.
   A parameter change requires a REV2 preregistration.

## 5. Explicit precondition (must be met before execution)

**H1 may execute only when ALL of the following are true and recorded:**

1. A **stable `z8` settlement source** exists — hash-pinned, provenance-tagged, with Kalshi
   `finalized` as primary and GHCN daily as the declared weather fallback.
2. The **corruption audit** (S5 disagreement A: 424 garbage rows) is applied and the affected
   rows are excluded/counted, not ingested.
3. The **−1 °F convention offset** (S5 disagreement C) is resolved in the settlement source or
   explicitly modeled — `settlement_epochs` is never used as truth without the offset correction.
4. The **epoch-boundary definitional mismatch** (S5 disagreement B, 2026-05-28 and 2026-08-10)
   is resolved or those dates are explicitly excluded with a recorded rationale.
5. A **provenance comment / lineage record** ties the `z8` dataset to the lineage freeze
   (`04be98a04`) so the result is reproducible.

**Blocked-on:** establishment of the `z8` tier and settlement-source reconciliation completion
(S5/S6 disposition carried into a frozen artifact).

---

# H2 — Volume Momentum

## 6. Hypothesis

**H2 (formal):** In weather markets, **volume momentum** — a sustained, direction-consistent
increase in traded volume over a defined lookback window — is a **statistically significant**
predictor of the direction of the next settlement-price move, over and above a naive persistence
baseline.

**Directional claim:** When volume rises persistently ahead of settlement while price moves in a
consistent direction, that momentum carries information about the settlement outcome that
persistence alone does not capture.

**What H2 is NOT:** H2 is a *predictive-signal* hypothesis (does volume momentum add predictive
information), not an *arbitrage* hypothesis and not a claim of standalone profitability. A
successful H2 establishes the signal's incremental predictive value only.

## 7. Data requirement

To test H2, the following must be available **as `z8`** (see §1), with a **72-hour trustworthy
history**:

1. **72h trustworthy `z8` volume series** — hourly (or finer) traded-volume observations for each
   weather market, with at least 72 consecutive hours of clean, provenance-tagged, corruption-free
   data per market before any test window is opened.
2. **Volume provenance** — a declared source and schema for the volume field (which DB, which
   table, which column), hash-pinned, so volume is not conflated with a proxy or reconstructed
   series.
3. **Paired settlement-direction labels** — the realized direction of the settlement move
   (up/down) for each test market, from the primary settlement source, sanity-band-applied.
4. **Persistence baseline inputs** — the prior-period direction needed to build the null
   persistence model.
5. **Fixed lookback window definition** — the momentum window (e.g. 6h/12h/24h) frozen before
   any run.

## 8. Test design

1. **Feature:** `volume_momentum = (rolling volume sum over lookback window) − (baseline volume)`,
   normalized, with a direction-consistency qualifier (price move direction over the same window).
2. **Baseline:** a naive **persistence** predictor (next-period direction = current-period
   direction). H2 is a **strictly incremental** test — it must beat persistence, not just beat
   chance.
3. **Evaluation:** nested cross-validation / walk-forward with an out-of-sample holdout, following
   the `nested_cv_persistence_test` pattern already present in `reports/`. Directional accuracy of
   the volume-momentum-augmented predictor vs. the persistence baseline.
4. **Significance:** the incremental accuracy must clear a preregistered significance threshold
   (§9) — no threshold shopping after results.
5. **No leakage:** the 72h `z8` history must be fully finalized before the test window; no
   in-window data may be used to build the feature for that same window.

## 9. Success threshold

**H2 is confirmed only when BOTH hold:**

1. The volume-momentum-augmented predictor's directional accuracy exceeds the persistence baseline
   by a **preregistered minimum lift** (frozen at **+5 percentage points** of directional
   accuracy), on the out-of-sample holdout.
2. The improvement is **statistically significant** at the frozen threshold
   (**p < 0.01** on the label-permutation / paired comparison), consistent with
   `FORMAL-SUCCESS-CRITERIA.md` Phase 9 criterion 1.

A result that clears the lift but not significance, or significance but not lift, is a **fail** —
recorded as such, not re-tuned.

## 10. Explicit precondition (must be met before execution)

**H2 may execute only when ALL of the following are true and recorded:**

1. A **72-hour trustworthy `z8` volume series** exists per market — clean, provenance-tagged,
   hash-pinned — with no gaps, reconstruction, or proxy substitution in the 72h window.
2. The **volume provenance** (source DB/table/column) is declared and tied to the lineage freeze
   (`04be98a04`).
3. The **settlement-direction labels** are sourced from the primary settlement source
   (Kalshi `finalized` / GHCN fallback), with the sanity band `[-60, 135] °F` applied.
4. The **lookback window** and the **+5pp lift / p < 0.01** thresholds are frozen (they are —
   this document) and no result has yet been observed.

**Blocked-on:** establishment of a trustworthy 72-hour `z8` volume series with declared
provenance.

---

## 11. Verification / no-mutation confirmation

- **Scope executed:** documentation only. One file created under `docs/plans/`.
- **Files written:** `docs/plans/H1-H2-PREREGISTRATION-2026-09-17.md` (this document).
- **Code / config / data / DB:** **not** modified, added, deleted, or written.
- **No backtests, no trading, no DB writes** were performed.
- **Stop conditions honored:** execution gate left CLOSED; both hypotheses remain PARKED at the
  execution layer pending their preconditions (§5, §10).
