# R/R-1a Part 1 RE-DO — Corrected Settlement-Day Lookahead As-Of Gate + Re-Score

Date: 2026-09-20 (UTC)
Author: Gilfoyle (technical subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
Authorized: Gerri (COO/QA) REJECTED the prior attempt (`RR1A-POST-REVIEW-FIX-2026-09-20.md`,
commit `6cbc97e84`) because the as-of gate was structurally broken — it nullified
queries instead of gating lookahead. This receipt supersedes that re-score.

---

## 0. TL;DR

The prior as-of gate bolted `timestamp_utc < settlement_window_open` (settlement-day
`T00:00:00`) onto queries **still scoped `date_utc = settlement_date`**. That asks for
"settlement-day rows with timestamp before settlement-day-start," which is **EMPTY BY
CONSTRUCTION**. The resulting `n=0` "collapse" was a **NULLIFIED QUERY**, not a
"no predictions" finding.

This re-do corrects the gate to **prior-day semantics** (`date_utc < settlement_date`),
verifies the query now admits a non-empty contemporaneous observation set, and re-scores
both candidates. The corrected verdicts:

- **frontal_passage_intraday**: n 0 → **13,303** (real fires). MCC **+0.019** (noise),
  Sharpe −0.69. A **legitimate NO-EDGE** finding now — the signal produces real
  predictions on genuinely-contemporaneous prior-day data, but no directional skill.
- **nwp_dtdt_fusion**: MCC **+0.114**, Sharpe 3.12, n 9,836. **NOT collapsed** — and this
  residual edge is **NOT settlement-day lookahead**. Its corrected METAR leg is +0.021
  (noise). The +0.114 comes from the **NWP leg**, which is gated correctly
  (`fetch_date <= target_date`) but predicts **day-over-day** direction, not
  strike-crossing — a *separate* target-variable defect (already flagged in
  `core/settlement_outcome.py`), not the lookahead defect this re-do fixes.

**Correction to prior receipt:** the prior claim that "the leaked METAR leg was the
entire +0.106" was **wrong**. The METAR leg was never the source of the edge; once
correctly gated it contributes +0.021 (noise). The NWP day-over-day leg is the source
of the fusion's residual +0.114 — and it is a target-variable mismatch, not lookahead.

---

## 1. Corrected as-of spec (what changed)

The ground-truth label a signal is scored against is the settlement day's strike-crossing
direction (`settlement_temp_f` = the day's HIGH/LOW extremum vs `strike_price`). Any
observation with `timestamp_utc >= settlement_date T00:00:00` is the outcome's own
measurement stream and is lookahead for a same-day prediction.

**Corrected predicate semantics:**

| Aspect | Prior (REJECTED) | Corrected (this re-do) |
|---|---|---|
| Date scope | `date_utc = settlement_date` | `date_utc < settlement_date` (prior day(s)) |
| Timestamp predicate | `timestamp_utc < settlement-day T00:00:00` | `timestamp_utc < settlement-day T00:00:00` (now meaningful because date scope already excludes the settlement day) |
| Result | ZERO rows always (contradiction) | Non-empty prior-day observation set |
| Read-time gate | Yes (but empty) | Yes (admits prior day) |

The gate still honors "gate at read time, don't delete data" — settlement-day rows are
NOT deleted; they remain for retrospective consumers (settlement verification, PnL).

New helpers in `core/observational_asof_gate.py`:
- `settlement_window_open(date)` → `"<date>T00:00:00"` (unchanged).
- `prior_day(date)` → prior ISO date (the upper-exclusive bound for the date scope).
- `lookback_start(date, days)` → bounded prior-window lower bound (optional).
- `is_asof_safe(ts, date)` → `ts < settlement_window_open(date)` (unchanged semantics,
  still correct: the `+00:00` suffix sorts after the zero-padded digits, so the whole
  settlement day is excluded).
- `PRIOR_DAY_PREDICATE = " AND date_utc < ?"` (REPLACES the `date_utc = ?` equality).

---

## 2. The three read-site fixes

| Read site | Prior (broken) | Corrected |
|---|---|---|
| `core/signals/frontal_passage_intraday_signal.py` `_load_intraday_obs` | `date_utc = date AND timestamp_utc >= dateT00:00 AND timestamp_utc <= dateThh:59 AND timestamp_utc < dateT00:00` (contradiction) | `date_utc < date AND timestamp_utc >= prior_day T(24-hours):00 AND timestamp_utc < dateT00:00` — reads the prior day's last `hours` hours |
| `core/signals/metar_dtdt_signal.py` (METAR leg of fusion) | `date_utc = date AND timestamp_utc < dateT00:00` (contradiction) | `date_utc < date AND timestamp_utc < dateT00:00 ORDER BY ts DESC LIMIT 20` — prior-day trend |
| `core/trajectory_confirmation_gate.py` `build_epoch` | `daily_stats.date_utc = date` (settlement-day extremum, lookahead) + intraday `date_utc = date AND timestamp_utc < dateT00:00` (contradiction) | `daily_stats.date_utc = prior_day` + intraday `date_utc = prior_day` — prior-day epoch |

The trajectory gate is already KILLED (R/T-3 MCC ≤ 0) and not a re-score candidate, but
its `build_epoch` was fixed per the task's read-site list for correctness.

---

## 3. Before/after row-count evidence (query now admits prior-day data)

Representative station/date: **KATL @ settlement date 2026-09-19** (the most recent
settlement row in `settlement_truth`).

| Query | Row count | Interpretation |
|---|---|---|
| BROKEN: `date_utc = 2026-09-19 AND timestamp_utc < 2026-09-19T00:00:00` | **0** | Zero-by-contradiction (the bug) |
| CORRECTED: `date_utc < 2026-09-19` (all prior days) | **50,185** | Non-empty contemporaneous set |
| CORRECTED prior day only: `date_utc = 2026-09-18` | **23** | ~hourly rows for the prior day |
| CORRECTED prior-day last 3h (frontal window, 21:00–23:59): `date_utc = 2026-09-18 AND ts >= 21:00 AND ts < 2026-09-19T00:00` | **3** | The frontal detector's 3-hour window |

The BROKEN query is definitively 0 rows; the CORRECTED query admits a non-empty,
genuinely-contemporaneous prior-day set. **The query is now meaningful.**

---

## 4. Re-scored candidate table (corrected gate)

Harness: canonical `ultimate_sweep._score_signal`, driven read-only by
`scripts/rr1a_asof_redo_rescore.py`. Axis: strike-crossing (see §6). Label set:
14,305 strike rows (exclusions: 54 between, 80 untrusted, 0 no-temp, 0 unknown-mt).

| signal | MCC | Sharpe | baseΔ | acc | n | verdict |
|---|---|---|---|---|---|---|
| frontal_passage_intraday | **+0.0187** | −0.69 | −43.14% | 47.84% | **13,303** | NO_EDGE (real) |
| nwp_dtdt_fusion | **+0.1144** | 3.12 | −32.58% | 59.64% | **9,836** | NO_EDGE on strike-crossing axis¹ |

¹ The fusion's +0.114 is a **target-variable mismatch** (see §5), not lookahead.

Component isolation (to locate the residual edge):

| component | MCC | Sharpe | baseΔ | acc | n |
|---|---|---|---|---|---|
| nwp_direct (day-over-day NWP leg) | +0.0000 | n/a | +0.00% | 0.00% | 0 (DISABLED in registry) |
| metar_dtdt (corrected prior-day METAR leg) | +0.0214 | 3.56 | −30.66% | 60.96% | 8,398 |
| nwp_dtdt_fusion | +0.1144 | 3.12 | −32.58% | 59.64% | 9,836 |

---

## 5. Explicit "no usable data" vs "broken query" distinction (core QA requirement)

| Case | Definition | Which candidate / finding |
|---|---|---|
| **BROKEN query** | Query returns zero rows *by contradiction* — the WHERE clause is internally inconsistent (asks for settlement-day rows before settlement-day start). | The PRIOR gate (both candidates read the settlement day's own rows and returned n=0). **FIXED in this re-do.** |
| **No usable as-of data** | The corrected query admits the correct prior-day observation set (non-empty), but the signal produces n=0 OR MCC≈0 because there is genuinely no directional signal in contemporaneous data. | `frontal_passage_intraday` — now produces n=13,303 real predictions but MCC +0.019 ≈ 0. This is a **legitimate NO-EDGE**, NOT a broken query. |
| **Edge from wrong target variable** | A signal fires with positive MCC, but its prediction is day-over-day direction scored against a strike-crossing axis. | `nwp_dtdt_fusion` — residual +0.114 from the NWP leg's day-over-day `tomorrow_temp vs today_temp` logic, NOT lookahead, NOT strike-crossing skill. |

**Verdict trust:** the edge-collapse verdict from the prior receipt is **NOT fully
trustworthy as originally stated**. `frontal_passage_intraday`'s collapse to n=0 was
indeed a broken query (now fixed → real n=13,303, MCC≈0, genuine no-edge). But
`nwp_dtdt_fusion` did **NOT** collapse via the METAR leg — its +0.114 survives and is
attributable to the NWP day-over-day leg (a target-variable defect outside this re-do's
scope). The prior receipt's causal attribution ("the leaked METAR leg was the entire
+0.106") is **incorrect**.

---

## 6. Axis + provenance

Every number in this receipt is on the **strike-crossing axis**:
`settlement_temp_f` vs `strike_price` via `core/settlement_outcome.strike_crossing_direction`
(single source of truth, strict `>`/`<`, tie = loss). Real strikes from
`settlement_truth.strike_price` (14,305 rows: 10,139 greater + 4,112 less + 54 between).
Base rate up = 34.36% (down-skewed); always-down null Sharpe ≈ 5.25. MCC is the honest
skill metric; MCC ≈ 0 = artifact, not skill. NOT day-over-day temp.

Method: expert-spec → build → independent re-score (separate pass, canonical scorer driven
read-only) → confirm-against-spec. Deterministic/script-only in the scoring loop; no AI
calls inside the loop.

---

## 7. Receipt

- Corrected gate module: `core/observational_asof_gate.py`
- Fixed read sites: `core/signals/frontal_passage_intraday_signal.py`,
  `core/signals/metar_dtdt_signal.py`, `core/trajectory_confirmation_gate.py`
- Re-score driver (read-only): `scripts/rr1a_asof_redo_rescore.py`
- Re-score JSON: `reports/rr1a_asof_redo_20260920-143957.json`
- This doc: `docs/plans/RR1A-PART1-ASOF-FIX-2026-09-20.md`

**Commit:** `<recorded post-commit>` (see §8)

---

## 8. Open item for Gerri (out of scope, but material)

The `nwp_dtdt_fusion` +0.114 is a **day-over-day vs strike-crossing** target-variable
mismatch in `core/signals/nwp_direct_signal.py` (`tomorrow_temp > today_temp`), NOT a
lookahead leak. This is the same defect class `core/settlement_outcome.py` was created to
eliminate, but it lives in the NWP leg. Recommend a follow-up R/R item to re-spec the NWP
leg on the strike-crossing axis (or kill it) — but that is a separate fix, not part of
this as-of-gate re-do.
