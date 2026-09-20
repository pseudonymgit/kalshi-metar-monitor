# R/R-1 Deep Code Review — Signals / Lanes / Gates / Fusion / Calibration

Date: 2026-09-20 (UTC)
Reviewer: Gilfoyle (independent QC subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, HEAD `8ce90ce40`)
Scope: evidence-ready slice only (signals, lanes, gates, fusion, calibration).
DEFERRED: execution layer (`scripts/intraday_entry_stub.py`) — R/T-4a target (running in parallel).

**Method:** independent code inspection + deterministic read-only re-derivation of the
ground-truth axis / provenance / lookahead numbers. NOT a re-run of the build's own
assertions. No data mutation, no signal re-enable/wiring. New files: this receipt + one
read-only QC script (`scripts/rr1_review_qc.py`).

---

## 0. TL;DR

- **Axis correctness: PASS.** `core/settlement_outcome.strike_crossing_direction` is a
  clean single source of truth; strict `>`/`<`, tie = loss, `between` excluded. I
  independently re-derived the full label set from raw `settlement_temp_f` vs
  `strike_price` vs `market_type` and reproduced n=14,130 / up=4,855 (34.36%) /
  down=9,275 (65.64%), HIGH up=10.13%, LOW up=94.64%. No day-over-day temp anywhere in
  the scoring path.
- **Number provenance: PASS with one documentation drift.** `settlement_truth.strike_price`
  = 14,305 rows (10,139 greater + 4,112 less + 54 between), temp_source ghcn=13,889 /
  kalshi_finalized=79 / weatherapi=162. Matches the DB. BUT R/T-3's receipt quotes
  "10,079 greater + 4,051 less" — WRONG (off by 60/61) vs R/T-4's "10,139 + 4,112"
  (correct). Cross-receipt drift, cosmetic not functional.
- **Lookahead: BLOCKER.** The two "genuine edge" candidate signals AND the
  `trajectory_confirmation` gate read the **settlement day's own intraday observations**
  to predict that same day's strike-crossing outcome. This is contemporaneous-future
  leakage, not gated by `nwp_lookahead_gate` (which only protects the NWP DB path).
- **Branch/edge-case fidelity: mixed.** The harness routing and metric math are sound;
  the signal-layer edge cases (confidence contract, lookahead windows, axis-internal
  mismatch) have material defects.

---

## 1. Per-item review table

Legend: ✅ clean · ⚠️ defect (non-blocker) · 🔴 blocker · ➖ not applicable (modulator/reachability-only).

| # | Item | Branch/edge fidelity | Axis correctness | Provenance | Lookahead |
|---|---|---|---|---|---|
| S1 | `settlement_outcome.strike_crossing_direction` | ✅ strict `>`/`<`, tie=loss, None-on-missing | ✅ correct axis | ✅ single source of truth | ➖ (label fn) |
| S2 | `nwp_lookahead_gate` | ✅ `fetch_date > target_date`, ISO-lexicographic | ➖ | ✅ | ✅ correct predicate |
| S3 | `ultimate_sweep.load_labels` | ✅ excludes between/untrusted/no-temp | ✅ | ✅ reads `strike_price`/`strike_type` | ➖ |
| S4 | `ultimate_sweep._score_signal` / `_mcc` / `_sharpe` / `_ece_by_market_type` | ✅ correct confusion matrix; MCC denom-safe; ECE within-market-type | ✅ | ✅ | ⚠️ delegates lookahead to signal impl |
| S5 | `nwp_dtdt_fusion` (candidate 1) | ⚠️ see L2/L3 | ⚠️ **internal day-over-day axis** (tomorrow>today) scored vs strike-crossing | ✅ | 🔴 **METAR leg leaks settlement day** (via `metar_dtdt`) |
| S6 | `frontal_passage_intraday` (candidate 2) | ⚠️ `hours=24` fallback | ⚠️ direction semantics = front type, not strike-crossing | ✅ | 🔴 **reads full settlement day intraday** |
| S7 | `nwp_direct_signal` | ✅ | ✅ applies `contemporaneous_where` | ✅ | ✅ NWP gated correctly |
| S8 | `metar_dtdt_signal` | ⚠️ `LIMIT 20` + 3h window | ✅ | ✅ | 🔴 **reads settlement day `date_utc`** |
| S9 | `signal_fusion` (LOOP/UWC/majority/weighted) | ⚠️ dead code block + `n_eff` collapse | ✅ | ✅ | ➖ (consumes already-computed signals) |
| S10 | `adaptive_thresholds` | ⚠️ hardcoded priors | ✅ | 🔴 **fabricated accuracy floors** (55–90% descriptions) | ➖ |
| L1 | 6 lanes (`ultimate_sweep.LANES`) | ➖ reachability-only in harness | ➖ | ⚠️ disposition mapped by hand, not scored | ➖ |
| G1 | 9 gates (`ultimate_sweep.GATES`) | ➖ reachability-only | ➖ | ⚠️ same | ➖ |
| G2 | `trajectory_confirmation_gate` | ⚠️ | ✅ | ✅ | 🔴 **build_epoch reads settlement-day obs + settlement_temp** |
| C1 | calibration sweep (Brier/ECE) | ✅ | ✅ | ✅ | ⚠️ inherits signal lookahead |

---

## 2. Detailed findings

### 🔴 BLOCKER 1 — Settlement-day lookahead in the two candidate signals

The two signals R/T-2/2b/3/4 flag as the *only* genuine edge are both contaminated by
contemporaneous-future leakage:

**`frontal_passage_intraday`** (`core/signals/frontal_passage_intraday_signal.py`):
- `evaluate(idx, days)` → `evaluate_for_station(station, date)` → `_load_intraday_obs(station, date, hours=3)`.
- `_load_intraday_obs` queries `metar_observations WHERE date_utc = ?` (the settlement
  date) with `timestamp_utc` window `T00:00:00 .. T03:59:59`, and **falls back to
  `hours=24`** (the full day `00:00 .. 23:59`) when fewer than 4 observations appear in
  the 3h window.
- The label it is scored against is `settlement_temp_f` = the settlement day's GHCN daily
  **high** (HIGH) / **low** (LOW) — i.e. the day's full-day extremum.
- Net: the signal observes the settlement day's actual temperature trajectory (including
  the afternoon max that determines the outcome) and predicts whether that same day's
  temp crosses the strike. This is textbook lookahead. The `hours=24` fallback is the
  worst case — it consumes the entire outcome window.

**`nwp_dtdt_fusion`** (`core/signals/nwp_dtdt_fusion_signal.py`):
- NWP leg (`nwp_direct_signal`) is correctly gated (`contemporaneous_where` → `fetch_date <=
  target_date`). ✅
- METAR leg (`metar_dtdt_signal`) reads `metar_observations WHERE date_utc = ?` (settlement
  date), `ORDER BY timestamp_utc DESC LIMIT 20` — the **most recent** observations of the
  settlement day — to compute dT/dt and vote up/down. This is the same settlement-day leak.
- Net: half of this "fusion" is leaked. Its MCC +0.106 is not trustworthy evidence of
  skill until the METAR leg is re-gated to `timestamp_utc < settlement_window_open`.

**`trajectory_confirmation_gate`** (`core/trajectory_confirmation_gate.py`) — same pattern:
`EpochBuilder.build_epoch` reads `daily_stats` (max/min/avg) + all `metar_observations`
for `date_utc = date`, and constructs `settlement_temp` in the feature vector. (Already
KILLED by R/T-3 on MCC grounds, but the leak is systemic across the "observational
intraday" family.)

**Why this matters for the Gray Room:** the two candidate signals are the *entire* edge
story. If their edge is partly/wholly leakage, the "candidate-selective OR → MCC +0.124"
conclusion (R/T-2b §5) collapses. This must be re-scored with a proper
`as-of <= trade_time` gate before any out-of-sample claim.

### 🔴 BLOCKER 2 — `adaptive_thresholds` accuracy floors are fabricated (confirmed by code)

`core/adaptive_thresholds.py` `SIGNAL_CONFIGS` hardcodes `description=` strings of
"70-72%", "71-73%", "65-68%", "62-65%", "60-65%", "58-63%" accuracy. These are priors
written into the dataclass, never derived from any corrected-axis measurement. The
`tau_min=0.55` / `tau_max=0.90` bounds are likewise hardcoded constants. R/T-3's finding
(claimed 55–90% vs actual 47–66% with MCC ≤ 0) is **confirmed at the source**. The gate's
description strings are mislabeled as empirical accuracy when they are unvalidated
assumptions. → R/R-2 should require either (a) deleting the description strings, or (b)
re-deriving them from the corrected-axis sweep and labeling them as "targets, not
measured."

### ⚠️ NON-BLOCKER 1 — `nwp_dtdt_fusion` has an internal axis mismatch

`nwp_direct_signal.evaluate_for_station` predicts `tomorrow_temp > today_temp`
(day-over-day temperature *change*), then `nwp_dtdt_fusion` fuses this with the METAR
dT/dt and emits `direction`. This `direction` is scored against `settlement_temp_f >
strike` (strike-crossing). The signal's *internal* semantics (warming vs cooling) is a
different target than the score axis (crossing the strike). This is not a scoring bug
(the scorer correctly applies strike-crossing), but it means the signal was *designed*
for a different target and is being force-fit — a category mismatch that explains why it
only shows thin MCC even before the lookahead is stripped out.

### ⚠️ NON-BLOCKER 2 — `signal_fusion.PoolOfPools.evaluate` has dead code

`core/signal_fusion.py` `PoolOfPools.evaluate` contains a large **stale docstring/block**
that is actually a second, orphaned copy of the method body (the real body follows after
the triple-quoted string). The dead block references `combined["mean"]` before
`combined` is defined. Not executed, but it is misleading and a maintenance hazard. The
real body is correct. (No functional impact on the sweep, which uses
`fuse_majority_vote`/`fuse_weighted_vote`/`UncertaintyWeightedCascade`.)

### ⚠️ NON-BLOCKER 3 — R/T-3 strike-row provenance drift

R/T-3 §2/§5 quotes "10,079 greater + 4,051 less" (14,305 rows). The DB and R/T-4 both show
**10,139 greater + 4,112 less**. The R/T-3 numbers are off by 60/61 rows (likely a stale
snapshot pre-P0-B-final). Cosmetic — no functional consequence — but receipts should be
internally consistent for audit.

### ⚠️ NON-BLOCKER 4 — lane/gate "disposition" is a hand-mapped readback, not scored code

`ultimate_sweep.sweep_lanes_gates()` only performs `_module_reachable()` (import check).
The R/T-3 KILLED/PARKED table is derived by hand-mapping each gate's *feed-signal* MCC
(from the signal sweep) onto the gate constant. That is a legitimate readback but it is
**not** a run of the gate logic against the axis. The gate *constants* themselves
(e.g. `production.min_acc 0.58`, `agreement.n_required 3`, `adaptive_thresholds.tau_min
0.55`) were never executed on corrected-axis data in this pass. R/R-2 should treat the
gate disposition as "feed-signal evidence," not "gate-tested."

### ✅ CLEAN 1 — Axis / ground-truth seam

`core/settlement_outcome.py` is correct and well-documented. The strict `>`/`<` (tie=loss)
matches Kalshi's verified rule. `None` returns are correctly propagated (no silent
day-over-day fallback). The `between` exclusion is correct (no single-strike semantics).

### ✅ CLEAN 2 — NWP lookahead gate is real and wired

`nwp_lookahead_gate.CONTEMPORANEOUS_PREDICATE` is applied inside `nwp_direct_signal._get_temp`
(the only NWP consumer on the directional path). Independent DB check: 317,669/943,231
(33.68%) NWP rows are `fetch_date > target_date` — the gate is load-bearing, not cosmetic.

### ✅ CLEAN 3 — Metric math (MCC / Sharpe / ECE / Brier)

`_mcc` is denom-safe (returns 0 on zero denominator). `_ece_by_market_type` correctly
bins *within* market type (necessary given the 10%/95% conditional base rates). `_sharpe`
uses population variance and ×√252 annualization. `_brier` uses `1-conf` for down-calls.
All correct for the skew regime.

---

## 3. Blockers vs non-blockers

### Blockers (must fix / re-score before wire-in or before R/R-2 relies on the edge claim)

1. **Settlement-day lookahead in `frontal_passage_intraday`, `nwp_dtdt_fusion` (METAR leg),
   and `trajectory_confirmation_gate`.** The only two "edge" signals are contaminated.
   Re-score with `timestamp_utc < settlement_window_open` (or equivalent as-of gate).
2. **`adaptive_thresholds` fabricated accuracy floors.** Remove or re-derive the
   `description` accuracy strings and `tau_min/tau_max` constants.

### Non-blockers (note, but do not block)

1. `nwp_dtdt_fusion` internal day-over-day axis vs strike-crossing score axis (design mismatch).
2. `signal_fusion.PoolOfPools.evaluate` orphaned dead-code block.
3. R/T-3 strike-row count drift (10,079/4,051 vs 10,139/4,112).
4. Lane/gate dispositions are hand-mapped feed-signal readbacks, not executed gate logic.

---

## 4. Findings that MUST feed R/R-2 (Gray Room)

1. **The edge claim is unproven pending lookahead fix.** R/T-2b's "candidate-selective OR
   → MCC +0.124" and R/T-2/4's two-candidate edge both depend on signals that read the
   settlement day. Until re-scored clean, the Gray Room should treat "0 genuine-edge
   signals" as the conservative baseline, not "2 candidates."
2. **`adaptive_thresholds` descriptions are not evidence.** Any gate-review framing that
   cites "55–90% accuracy floors" is citing fabricated priors.
3. **The fusion layer is still a dilution layer** (R/T-2b MCC ≈ 0) — this review found no
   code defect that would reverse that; the dilution is a genuine architectural property,
   not a bug.
4. **Systemic lookahead pattern**: the "observational intraday" signal family
   (`metar_dtdt`, `frontal_passage_intraday`, `trajectory_confirmation_gate`,
   `subhourly_trajectory`) uniformly reads `date_utc = settlement_date` with no
   as-of/`timestamp_utc` bound. A single shared "settlement-window open time" constant +
   predicate is needed, analogous to `nwp_lookahead_gate`, applied at read time across
   the family.

---

## 5. Receipt

- Review doc: `docs/plans/RR1-DEEP-CODE-REVIEW-2026-09-20.md` (this file)
- QC script (read-only, deterministic): `scripts/rr1_review_qc.py`
- Commit: `<hash-filled-after-commit>`
