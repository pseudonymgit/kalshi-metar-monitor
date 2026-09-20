# R/R-1a Post-Review Fix + Re-Score — Settlement-Day Lookahead, Threshold Fabrication, Execution-Layer As-Of, Strike-Row Drift

Date: 2026-09-20 (UTC)
Author: Gilfoyle (technical subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
Authorized: Gerri (COO/QA) signed off R/R-1 and expanded R/R-1a scope.
Predecessor: `RR1-DEEP-CODE-REVIEW-2026-09-20.md` (R/R-1).

**Three tightly-coupled parts folded into one commit — one receipt, one commit.**

---

## 0. TL;DR

- **BLOCKER 1 (lookahead) — FIXED + CONFIRMED.** The two "genuine edge" candidate
  signals read the settlement day's own intraday observations. A shared as-of
  predicate (`timestamp_utc < settlement_window_open`) is now applied at READ TIME
  across the observational-intraday family. Re-scored post-gate, **the edge
  collapses**: `frontal_passage_intraday` MCC +0.108 → **0.000 (n=0, no fires)**;
  `nwp_dtdt_fusion` MCC +0.106 → **+0.013 (noise)**. The R/R-1 edge-collapse
  hypothesis is **CONFIRMED** — not merely assumed, measured.
- **BLOCKER 2 (fabricated thresholds) — FIXED.** `adaptive_thresholds.SIGNAL_CONFIGS`
  description strings relabeled "TARGET (not measured)"; `tau_min/tau_max`
  documented as explicit policy targets with provenance.
- **PART 3 (execution layer) — AS-OF SAFE.** The v4.0 `intraday_entry_stub.py` reads
  only `signal_cache.db`, `kalshi_market_snapshots.db`, `kalshi_strike_ladder.db`,
  and `paper_trading.db`. It does NOT read `metar_observations`/`daily_stats`/
  `settlement_truth`. No settlement-day lookahead in its own read path. No fix needed.
- **PART 4 (strike-row drift) — FIXED.** R/T-3 §2/§5 corrected to "10,139 greater +
  4,112 less".

---

## 1. As-of predicate design + application map

The settlement outcome a signal is scored against is the settlement day's FULL-DAY
extremum (`settlement_temp_f` = the day's high for HIGH markets, low for LOW). Any
observation with `timestamp_utc >= settlement_day_start` is information from the
future relative to the prediction — it already contains part of the outcome.

New shared gate: `core/observational_asof_gate.py` (observational analog of
`core/nwp_lookahead_gate.py`, whose `fetch_date <= target_date` predicate protects
only the NWP DB path).

**Predicate:** `timestamp_utc < settlement_window_open`, where
`settlement_window_open(date) = "<date>T00:00:00"` (the settlement day's 00:00 UTC).
Strictly-before, mirroring the NWP gate's contemporaneous semantics. Gate at READ
TIME only — settlement-day rows are NOT deleted (needed by retrospective consumers).

**Application map:**

| Target | Read path gated | Method |
|---|---|---|
| `core/signals/frontal_passage_intraday_signal.py` | `_load_intraday_obs` — settlement-day METAR | `AND timestamp_utc < ?` (bound to `settlement_window_open(date)`) + belt-and-suspenders `is_asof_safe(ts, date)` |
| `core/signals/metar_dtdt_signal.py` (METAR leg of `nwp_dtdt_fusion`) | `evaluate_for_station` — `ORDER BY timestamp_utc DESC LIMIT 20` | `AND timestamp_utc < ?` |
| `core/trajectory_confirmation_gate.py` | `EpochBuilder.build_epoch` — settlement-day METAR rows | `AND mo.timestamp_utc < ?` |

**Residual note (documented, non-blocking):** `build_epoch` still reads the
settlement day's `daily_stats.max_temp_f/min_temp_f/avg_temp_f` (already-aggregated
daily extremum). The `trajectory_confirmation` gate is already KILLED (R/T-3 MCC ≤ 0)
and not a candidate, so this is flagged rather than re-scored. Any future revival
must gate the daily_stats read too.

---

## 2. Re-scored candidate table (pre-gate vs post-gate)

Harness: canonical `ultimate_sweep._score_signal` scorer, driven read-only by
`scripts/rr1a_rescore.py`. PRE-GATE = as-of predicate neutralized (reproduces the
R/R-1 leak); POST-GATE = as-of predicate active (fixed code). Pre-gate values
reproduce R/T-2/3/R-1 exactly (provenance check passes).

| signal | mode | MCC | Sharpe | baseΔ | acc | n |
|---|---|---|---|---|---|---|
| frontal_passage_intraday | pre-gate | **+0.1081** | 5.30 | −23.92% | 65.82% | 2809 |
| frontal_passage_intraday | post-gate | **0.0000** | n/a | +0.00% | 0.00% | **0** |
| nwp_dtdt_fusion | pre-gate | **+0.1062** | 2.90 | −32.59% | 58.98% | 9829 |
| nwp_dtdt_fusion | post-gate | **+0.0130** | 0.21 | −45.37% | 50.67% | 4823 |

Receipt JSON: `reports/rr1a_rescore_20260920-142905.json`.

---

## 3. Edge-collapse verdict — CONFIRMED (measured, not assumed)

- `frontal_passage_intraday`: MCC +0.108 → **0.000 with n=0**. The signal produces
  **no predictions at all** once settlement-day observations are excluded — its
  entire signal was settlement-day lookahead. 100% collapse.
- `nwp_dtdt_fusion`: MCC +0.106 → **+0.013** (MCC ≈ 0 = artifact per the down-skew
  regime), Sharpe 0.21 (vs null ~5.25), baseΔ −45%, n halved (9829 → 4823). The
  NWP leg remains gated-correct but contributes no directional edge; the leaked
  METAR leg was the entire +0.106.

**The R/T-2b "candidate-selective OR → MCC +0.124" edge claim COLLAPSES to ~0.**
The two candidates have **no genuine edge** post-gate. The Gray Room R/R-2 must
treat "0 genuine-edge signals" as the conservative baseline.

---

## 4. adaptive_thresholds fix

`core/adaptive_thresholds.py`:
- `SignalConfig` docstring now states `tau_min/tau_max` are **explicit policy
  targets** (desired controller floor/ceiling), chosen by design
  (FP-ADAPTIVE-THRESHOLDS.md), NOT measured empirical accuracy.
- All `SIGNAL_CONFIGS` / `DEFAULT_CONFIG` `description=` strings relabeled
  `"TARGET (not measured): …"` (the fabricated "70-72%"/"71-73%"/"65-68%"/etc.
  accuracy claims are removed). R/T-3 measured corrected-axis accuracies are
  47–66% with MCC ≤ 0 — the strings are now honest about being aspirational.

---

## 5. Execution-layer as-of verdict — AS-OF SAFE (no fix)

`scripts/intraday_entry_stub.py` (v4.0 NO-side rebuild) was NOT reviewed by R/R-1.
Verified now: its read path touches only:
- `signal_cache.db` — latest directional signal + confidence (upstream pipeline output)
- `kalshi_market_snapshots.db` — market-implied prices
- `kalshi_strike_ladder.db` — authoritative bucket/rung + settlement semantics
- `paper_trading.db` — its own persistence

It does **not** read `metar_observations`, `daily_stats`, or `settlement_truth`.
`_settle_value_for` reads the strike ladder's `settlement_value_dollars` only at
close time (post-settlement resolution), which is legitimate settlement accounting,
not lookahead. `--self-test` passes (deterministic). **Verdict: as-of-safe; review
boundary closed.**

---

## 6. Strike-row drift correction

R/T-3 receipt `docs/plans/RT3-LANE-GATE-REBUILD-2026-09-20.md` §2 and §5 corrected:
"10,079 greater + 4,051 less" → **"10,139 greater + 4,112 less"** (14,305 rows).
Matches the DB and R/T-4.

---

## 7. Axis + provenance statement

Every number in this receipt is on the **strike-crossing axis**:
`settlement_temp_f` vs `strike_price` via
`core/settlement_outcome.strike_crossing_direction` (single source of truth,
strict `>`/`<`, tie = loss). Real strikes from `settlement_truth.strike_price`
(14,305 rows: 10,139 greater + 4,112 less + 54 between). Base rate up = 34.36%
(down-skewed); always-down null Sharpe ≈ 5.25. MCC is the honest skill metric;
MCC ≈ 0 = artifact, not skill. NOT day-over-day temp.

Method: expert-spec → build → independent re-score (separate pass, canonical scorer
driven read-only) → confirm-against-spec. Deterministic/script-only in the scoring
loop; no AI calls inside the loop.

---

## 8. Receipt

- New gate module: `core/observational_asof_gate.py`
- Gated targets: `core/signals/frontal_passage_intraday_signal.py`,
  `core/signals/metar_dtdt_signal.py`, `core/trajectory_confirmation_gate.py`
- Threshold fix: `core/adaptive_thresholds.py`
- Re-score driver (read-only): `scripts/rr1a_rescore.py`
- Re-score JSON: `reports/rr1a_rescore_20260920-142905.json`
- R/T-3 strike-row correction: `docs/plans/RT3-LANE-GATE-REBUILD-2026-09-20.md`
- This doc: `docs/plans/RR1A-POST-REVIEW-FIX-2026-09-20.md`

**Commit:** `6cbc97e84825ee573c751dfedd526bedd65476ca`
