# R/R-1c — NWP Strict As-Of Re-Score (fetch_timestamp gate) + Mixed-Units Hygiene Fix

Date: 2026-09-20 (UTC)
Author: Gilfoyle (technical subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, HEAD `ed6320379`)
Authorized: Gerri (COO/QA) — REJECTED R/R-1b's +0.904 re-score as a same-day-lookahead artifact (corrupted `fetch_date` as-of key). This receipt supersedes `RR1B-NWP-AXIS-RESPEC-2026-09-20.md`.

---

## 0. TL;DR — the +0.904 does NOT collapse to 0; it goes UP (but the edge still dies)

Gerri's expectation was that the +0.904 would collapse toward the "0 genuine-edge"
baseline under the strict `fetch_timestamp` gate. **Measured result: it did not
collapse — it rose slightly to +0.922 — but the honest verdict is unchanged: NO
tradeable edge.**

| signal | R/R-1b (loose `fetch_date<=target`) | **R/R-1c (strict `fetch_timestamp<target`)** | verdict |
|---|---|---|---|
| `nwp_direct` | MCC +0.904, n=5,241 | **MCC +0.922, baseΔ −1.08%, n=2,045** | NO_EDGE |
| `nwp_dtdt_fusion` | MCC +0.639, n=9,990 | **MCC +0.339, baseΔ −21.86%, n=9,259** | NO_EDGE |
| `metar_dtdt` (isolated) | +0.021 | **+0.021, baseΔ −30.66%, n=8,398** | NO_EDGE |

**Why the MCC rose instead of collapsing:** the strict gate *removed* the noisy
same-day late-nowcasts (1,360 @ 22:xx + 80 @ 16:xx + 20 @ 19:xx) and the entire
contaminated backfill, leaving only clean live-collected forecasts. On that clean
subset, the NWP forecast's correlation with the (same-question) strike-crossing
outcome is *tighter*, so MCC rose 0.904 → 0.922.

**But the edge is still dead — for a sharper reason than Gerri flagged:**
- The surviving subset (n=2,045) is confined to **2026-07-23 → 2026-09-18** (the
  live-collection window). The backfill (2022-12 → 2026-07-22, all fetched in a
  July-2026 batch *after* their own target dates) has **zero** trustworthy as-of
  rows and is correctly excluded.
- On that summer window the base rate is **market-type-conditionally extreme**:
  HIGH is 95% down, LOW is 98.6% "up" (= LOW loses). The per-market-type
  majority-class null is **97.16% accurate**.
- The signal's raw accuracy is **96.09%** — i.e. **1.08% WORSE than the null**
  (`baseΔ = −1.08%`). The positive MCC is real *correlation* (a weather forecast of
  the day's high genuinely predicts whether the high crosses a strike — that is
  literally the same question), but it is **not** a tradeable edge: it
  underperforms "always predict the majority class."

**Honest disposition:** `nwp_direct`'s strike-crossing vote has genuine predictive
correlation (MCC +0.92) but **negative accuracy-delta vs the null** (−1.08%) and
**no usable as-of data before 2026-07-23** (the backfill is not trustworthy as-of
data). The fusion's +0.639 collapses to +0.339 and remains deep negative baseΔ
(−21.86%). **Neither signal is a directional edge.** R/R-2 Gray Room should treat
this as **NO-EDGE**, not as the +0.904 rare-event "up" detector R/R-1b presented.

---

## 1. The defect (Gerri-verified, data-level, reproduced exactly)

`core/signals/nwp_direct_signal.py::_get_temp` gated on
`fetch_date <= target_date` via `core/nwp_lookahead_gate.contemporaneous_where`.
`fetch_date` is an **untrustworthy as-of key**. Reproduced against
`data/nwp_forecasts.db`:

| measure | value |
|---|---|
| `temperature_2m_max` total rows | 63,665 |
| `fetch_timestamp` date ≠ `fetch_date` | **29,900 (47.0%)** |
| `fetch_date == target_date` (same-day nowcasts) | 17,835 (28.0%) |
| old gate `fetch_date <= target_date` survives | 62,465 (98.1%) |
| **strict `fetch_timestamp < target_date T00:00` survives** | **28,390 (44.6%)** |

**Root cause (code-level):** the two backfill scripts wrote contradictory values
into `fetch_date`, both of which make *historical* rows look contemporaneous when
the forecast was actually fetched (backfilled) months later:

- `scripts/nwp_backfill_max.py:78` → `fetch_date = target_date` ("the forecast was
  'made' on that date" — same-day semantics).
- `scripts/nwp_backfill_30d.py:367` → `fetch_date = daily.time[0]` (the first
  horizon date; one `fetch_date` spans a ~30-day target range).

`fetch_timestamp` is the wall-clock time the row was actually written (the ingestion
job), so it is the correct as-of key: a row is trustworthy as-of data for target day
`T` only if it was fetched strictly BEFORE `T` began. The historical backfill was all
fetched in a 2026-07-22/25 batch (and daily thereafter through 2026-09-20), so
`fetch_timestamp < target_date` correctly excludes every backfilled historical row
and admits only the live-collected window (target dates 2026-07-23 → 2026-09-26).

---

## 2. The fix (what changed)

### 2a. Strict as-of gate — `core/nwp_lookahead_gate.py`

Added (leaving the existing `contemporaneous_where` untouched for other consumers):

```python
def strict_asof_predicate() -> str:
    return "fetch_timestamp < ?"        # bound = "<target_date>T00:00:00"

def strict_asof_bound(target_date: str) -> str:
    return f"{target_date}T00:00:00"
```

`fetch_timestamp` is an ISO-8601 string with a `+00:00` UTC suffix; lexicographic
comparison against the target-day-midnight bound sorts the entire target day AFTER
the bound, so only strictly-prior-day fetches survive. Gate-at-read; no data deleted.

### 2b. `_get_temp` — `core/signals/nwp_direct_signal.py`

- Switched the WHERE clause from `contemporaneous_where` (fetch_date) to
  `strict_asof_predicate()` + `strict_asof_bound(target_date)`, bound as a 5th param.
- Added `_apply_unit_hygiene` (see 2c).

### 2c. Mixed-units hygiene — `_apply_unit_hygiene`

The `temperature_2m_max`/`_min` field is nominally °C but carries **1,340 °F values**
(range +62.0 → +115.3), all from `nws_mos` (a model NOT in the signal's
`MODELS = [GFS, ECMWF, ICON, GEM]` — those four are clean °C, max ~47.4°C). A
legitimate °C daily extremum is ~−40..+55; any value > +50 in the °C field is °F
mis-stored. Fix: normalize °F → °C before the °C → °F signal conversion.

```python
@staticmethod
def _apply_unit_hygiene(val_c_or_f: float) -> float:
    if val_c_or_f > 50.0:
        return (val_c_or_f - 32.0) * 5.0 / 9.0   # °F -> °C
    return val_c_or_f
```

**Before/after distribution (1,340 rows):** raw °F min 62.0 / max 115.3 → normalized
°C min 16.7 / max 46.3. Example: 70.0°F → 21.1°C; 100.0°F → 37.8°C. This is
**defense-in-depth** — the four signal models are already clean °C — but it guards any
broadening of `MODELS` and makes the honest re-score robust to unit contamination.

---

## 3. Re-scored table (strict gate + units fix)

Harness: `scripts/rr1c_strict_asof_rescore.py` (deterministic, script-only, read-only;
NO AI calls in the loop). The strict gate + units fix are inside `_get_temp`, so the
scoring enforces them automatically. Axis: strike-crossing via
`core/settlement_outcome.strike_crossing_direction`. Label set: 14,305 strike rows
(exclusions: 54 between, 80 untrusted, 0 no-temp, 0 unknown-mt).

| signal | MCC | Sharpe | baseΔ | acc | n | tp | fp | tn | fn | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| nwp_direct | **+0.9217** | 37.74 | **−1.08%** | 96.09% | 2,045 | 1,041 | 51 | 924 | 29 | NO_EDGE |
| nwp_dtdt_fusion | **+0.3389** | 7.03 | **−21.86%** | 70.25% | 9,259 | 1,212 | 438 | 5,292 | 2,317 | NO_EDGE |
| metar_dtdt (isolated) | +0.0214 | 3.56 | −30.66% | 60.96% | 8,398 | 379 | 580 | 4,740 | 2,699 | NO_EDGE |

Raw JSON: `reports/rr1c_strict_asof_rescore_20260920-151235.json`.

**Reading the table (honest interpretation):**
- `nwp_direct`'s **n collapses 5,241 → 2,045** (39% of labels retain trustworthy
  as-of data; all in the 2026-07-23 → 2026-09-18 live window).
- **baseΔ is negative on every leg.** The per-market-type majority null is 97.16%
  accurate on the surviving subset (HIGH 95.7% down, LOW 98.6% "up"); the signal's
  96.09% is **1.08% below it**. The +0.922 MCC is genuine correlation with the
  same-question outcome, but does **not** beat the null — a weather forecast of the
  day's high is a *solved problem*, not a tradeable edge.
- `nwp_dtdt_fusion`'s +0.639 → +0.339 collapse: the fusion fires on METAR-only labels
  too (n=9,259 ≫ nwp_direct's 2,045), diluting the NWP leg's high MCC with the
  axis-mismatched METAR dT/dt leg (which remains +0.021 noise). baseΔ −21.86%.

---

## 4. Explicit "no usable data" vs "residual skill" distinction

| Case | Definition | This finding |
|---|---|---|
| **No usable as-of data** | The strict gate admits a non-empty (or empty) contemporaneous set; the signal fires but has no directional *edge* over the null. | `nwp_direct`: usable data exists ONLY for 2026-07-23 → 2026-09-18 (2,045 labels). Before that, **zero** trustworthy as-of rows (backfill is lookahead by construction). MCC +0.92 but baseΔ −1.08% → **legitimate NO-EDGE**. |
| **Residual skill survives** | A signal beats the majority-class null after the strict gate. | **None.** No signal has positive baseΔ. The +0.904 → +0.922 is *correlation*, not *edge* (negative accuracy-delta). |
| **Broken/contaminated query** | Query returns wrong rows or wrong units. | Fixed in R/R-1c (fetch_timestamp strict gate + °F→°C hygiene). Not present in the final score. |

**Verdict:** the +0.904 is **not** a genuine tradeable edge. It is the correlation
of a weather forecast with the outcome it literally predicts (day's high vs strike),
measured on a clean-but-short live window, and it **underperforms the majority-class
null by 1.08%**. R/R-2 Gray Room: carry as **NO-EDGE**.

---

## 5. Axis + provenance

Every number is on the **strike-crossing axis**: `settlement_temp_f` vs `strike_price`
via `core/settlement_outcome.strike_crossing_direction` (single source of truth, strict
`>`/`<`, tie = loss). Real strikes from `settlement_truth.strike_price` (14,305 rows).
**NOT day-over-day temp.** Global base rate up = 34.36% (down-skewed); **the surviving
strict-gate subset has a DIFFERENT, market-type-conditional base rate** (HIGH 4.3% up,
LOW 98.6% up in the summer window), so the always-down null is NOT 96.4% on the subset —
the correct null is per-market-type majority (97.16%). MCC is the honest skill metric;
MCC ≈ 0 = artifact.

Method: expert-spec → build → **independent QC** (`scripts/rr1c_qc_gate_units.py`, a
separate pass that reads the DB directly and verifies the query filters on
`fetch_timestamp` not `fetch_date`, reproduces Gerri's 28,390/29,900/1,340 numbers,
and shows the °F→°C before/after distribution) → confirm-against-spec.
Deterministic/script-only in the scoring loop; no AI calls inside the loop.

---

## 6. Receipt

- Strict-gate helper: `core/nwp_lookahead_gate.py` (`strict_asof_predicate` / `strict_asof_bound`)
- Fixed read site + units hygiene: `core/signals/nwp_direct_signal.py` (`_get_temp`, `_apply_unit_hygiene`)
- Independent QC: `scripts/rr1c_qc_gate_units.py`
- Re-score driver (read-only): `scripts/rr1c_strict_asof_rescore.py`
- Re-score JSON: `reports/rr1c_strict_asof_rescore_20260920-151235.json`
- This doc: `docs/plans/RR1C-NWP-STRICT-ASOF-2026-09-20.md`

**Commit:** `<recorded-below>`

---

## 7. Stop conditions met

1. ✅ `_get_temp` gates on `fetch_timestamp < target_date T00:00` (strict), not `fetch_date`.
2. ✅ Mixed-units (°F-in-°C-field, 1,340 rows) hygiene fixed via `_apply_unit_hygiene` with documented provenance.
3. ✅ `nwp_direct` + `nwp_dtdt_fusion` re-scored under the strict gate; honest MCC/Sharpe/baseΔ/n emitted.
4. ✅ Receipt doc + commit hash.
