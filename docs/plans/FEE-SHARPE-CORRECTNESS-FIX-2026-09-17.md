# Fee + Sharpe Correctness Fix — Receipt

**Date:** 2026-09-17 21:40 UTC
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Type:** Correctness fix. Staged only — **not committed** (per task).
**Source of truth:** `docs/plans/WE-COMPLETENESS-AUDIT-2026-09-17.md` (sections 3a, 3b).

---

## Summary

Reconciled two material bugs flagged by the completeness audit:

- **TASK A — FEES:** two call sites returned fee in **cents** (off 100×) instead of
  **dollars**, diverging from the canonical `core/fees.py::kalshi_fee()`
  (order-level `ceil(0.07·C·P·(1−P))/100` dollars).
- **TASK B — SHARPE:** one call site defaulted cold-start Sharpe to **1.0**
  (falsely passing `MIN_SHARPE = 1.0`) instead of the canonical **0.0**.

Authoritative files **untouched** (`core/fees.py`, `core/pnl_tracking.py`).

---

## Files Changed

| File | Change |
|---|---|
| `core/continuous_kelly.py` | `kalshi_fee()` now `/100.0` → dollars (was cents) |
| `core/paper_trading_engine.py` | `per_side_fee`/`round_trip_fee` now `/100.0` → dollars (was cents) |
| `core/paper_trading_engine.py` | `compute_sharpe()` cold-start default `1.0` → `0.0` |
| `tests/test_fee_sharpe_correctness.py` | **NEW** — 6 regression tests |

No data/DB changes. No changes to `core/fees.py` or `core/pnl_tracking.py`.

---

## Before / After

### TASK A — FEES (cents → dollars)

**`core/continuous_kelly.py::kalshi_fee()`**
```python
# BEFORE (cents — off 100×)
return math.ceil(KALSHI_FEE_RATE * contracts * price * (1.0 - price))

# AFTER (dollars — matches core/fees.py)
return math.ceil(KALSHI_FEE_RATE * contracts * price * (1.0 - price)) / 100.0
```
Runtime check: `kalshi_fee(1000, 0.50)` → **was 18.0 (cents), now 0.18 (dollars)** = canonical.

**`core/paper_trading_engine.py` (~line 1924)**
```python
# BEFORE (cents)
per_side_fee = math.ceil(0.07 * quantity * effective_price * (1.0 - effective_price))
round_trip_fee = per_side_fee * 2

# AFTER (dollars)
per_side_fee = math.ceil(0.07 * quantity * effective_price * (1.0 - effective_price)) / 100.0
round_trip_fee = per_side_fee * 2
```
Runtime check: `per_side_fee(1000, 0.50)` → **was 18.0, now 0.18**; round-trip → **0.36 dollars**.

### TASK B — SHARPE (1.0 → 0.0)

**`core/paper_trading_engine.py::compute_sharpe()` (~line 2969)**
```python
# BEFORE (falsely passes MIN_SHARPE=1.0 on cold start)
if len(trades) < 2:
    return 1.0

# AFTER (matches pnl_tracking.compute_sharpe)
if len(trades) < 2:
    return 0.0
```

---

## Regression Tests

`tests/test_fee_sharpe_correctness.py` — **6 passed, 0 failed** (pytest 9.1.1).

| Test | Asserts |
|---|---|
| `test_continuous_kelly_fee_is_dollars` | `continuous_kelly.kalshi_fee` == `core.fees.kalshi_fee` across 5 cases |
| `test_continuous_kelly_fee_not_cents` | `kalshi_fee(1000, 0.50) == 0.18` dollars (not 18.0) |
| `test_paper_trading_engine_fee_is_dollars` | source contains `/ 100.0` on `per_side_fee` |
| `test_paper_trading_engine_fee_formula_dollars_value` | per-side = 0.18, round-trip = 0.36 dollars |
| `test_paper_trading_engine_compute_sharpe_cold_start_zero` | `pnl_tracking.compute_sharpe([], [1]) == 0.0` + source guard returns 0.0 |
| `test_paper_trading_engine_compute_sharpe_source_uses_zero_default` | source no longer contains `return 1.0` cold-start default |

Pre-existing `tests/test_fee_consistency.py` still passes 11/11 (its source-substring
assertion still matches the corrected line, since `/100.0` is appended after the paren).

---

## Authoritative Files — Confirmed Untouched

- `core/fees.py` — **unchanged** (canonical fee model).
- `core/pnl_tracking.py` — **unchanged** (canonical Sharpe formula, default 0.0).

Verified via `git status --porcelain` (empty for both paths) and `git diff` (no hunks).

---

## Stale / Non-Authoritative Formulas (left as-is, per task)

The audit identified these divergent, non-authoritative formulas. They were **not
modified** — left as stale and non-authoritative, and should NOT be used for the
`Sharpe ≥ 1.0` production gate:

| File | Formula | Why stale |
|---|---|---|
| `core/multi_model_ensemble.py` (~line 686) | `(mean_w − 0.5)/std_w · √252` | Annualized binary win/loss |
| `scripts/phase6_combinatorial_search.py` (~line 551) | `√n`-scaled win/loss | Search harness |
| `core/pnl_dashboard.py` (~line 34) | annualized `(mean − rf)/std` (doc/code mismatch) | Docstring says annualized, code omits √252 |
| `core/unified_backtest.py` (~line 106) | gross-minus-fee Sharpe on (confidence, correct) | Not P&L-based |

These remain non-authoritative and should not feed the production gate.

---

## Verification Summary

- ✅ Fee call sites now return **dollars** (0.18 / 0.36), matching `core/fees.py`.
- ✅ Sharpe cold-start default now **0.0**, matching `core/pnl_tracking.py`.
- ✅ 6/6 new regression tests pass.
- ✅ `core/fees.py` and `core/pnl_tracking.py` untouched.
- ✅ Stale annualized formulas untouched (documented as non-authoritative).
- ⏸️ Staged only — **not committed** (per task; commit withheld for Dan's merge approval).

**Receipt:** changes staged-for-review on `temp-sprint-branch`; owner = Gilfoyle;
exactly what is staged = 3 source files (2 fixes + 1 new test) as listed above;
stop condition / review trigger = Dan's merge approval.
