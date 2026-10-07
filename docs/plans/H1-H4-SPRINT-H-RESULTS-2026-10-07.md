# Sprint H — Make the Number Real (H1–H4)

**Date:** 2026-10-07 · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Harness:** `scripts/b2_real_price_sweep.py` · **Canonical artifact:** `data/b2_real_price_results.json`
**Gate:** Gilfoyle reviews H4 before any number is quoted internally.

---

## Summary — does the number move?

| | Trades | Wins | Acc | P&L/contract | Sharpe | Brier (cal) |
|---|---:|---:|---:|---:|---:|---:|
| **Pre-Sprint-H** (14:00 comment; the quoted 09:00 canonical was 0.01751/1.17) | 95 | 55 | 0.5789 | +0.01751 | 1.17 | — |
| **H4 final** (13:30Z, fee+calib+intraday) | **81** | **51** | **0.6296** | **+0.01753** | **1.28** | **0.126** |

The task predicted the canonical-fee change alone would drop P&L to ~+0.01116 / Sharpe ~0.74. That
recompute reproduces **exactly** — but only under one specific construct (fee changed in the P&L
line, selection held). Applying the canonical fee *everywhere* (so the EV picker re-selects) gives
a different, higher number. Both are reported below; neither is tuned to flatter the result.

---

## H1 — One canonical fee

**Canonical:** `fee = ceil(0.07 · P · (1−P) · 100) / 100` per contract (dollars).

### What was wrong

| Site | Before | Defect |
|---|---|---|
| `scripts/b2_real_price_sweep.py` | `0.07·P·(1−P)` | **no ceiling** (task's flagged crack) |
| `core/strike_selection_ev.kalshi_fee` | `ceil(0.07·price)` | **dropped `p·(1−p)`** → P=0.90 over-charged 7× ($0.07 vs $0.01) |
| `core/fees.kalshi_fee` | `ceil(0.07·P·(1−P)·C)/100` | **per-order** ceil + wrong units → $0.01 at C=1 where canonical is $0.02 |
| `core/continuous_kelly.kalshi_fee` | `ceil(0.07·C·P·(1−P))` | returned **cents**, per-order ceil |
| `core/paper_trading_engine.py:1932` | `ceil(0.07·q·P·(1−P))` | **cents**, per-order ceil, added to a dollar `position_size` |
| `scripts/sweep/*.py`, 12 validation/test harnesses, `sweep_metrics.py` | local copies | divergent (per-order ceil / bare) |

### What was done

- `core/fees.py` now exposes the **single primitive** `fee_per_contract(price)` =
  `ceil(0.07·P·(1−P)·100)/100`; `kalshi_fee(contracts, price)` = `fee_per_contract(price)·contracts`
  (per-contract ceil). `fees.py:59`'s `/100`-after-ceil bug and the `dex`/`market_cost_model`
  divergence are gone.
- Every other site **imports and delegates** to it: `strike_selection_ev` (EV picker),
  `market_cost_model` (live paper path), `continuous_kelly` (Kelly sizer), `paper_trading_engine`
  (live bridge), the sweep family, and 13 local harness copies.
- `scripts/verify_fee_formula.py` updated to the canonical expectation; `kalshi_fee` self-test
  updated; the one buggy test assertion (`test_kalshi_fee_price_only`, `0.90→0.07`) corrected to
  the true `0.90→0.01`.
- Audit: `grep` finds **no residual divergent formula**; every `def kalshi_fee` delegates.

**Before → after (fee-only, selection held, 09:00Z):** mean P&L **+0.01751 → +0.01116**,
Sharpe **1.17 → 0.74** — matches the task's prediction to 5 decimals.

---

## H2 — Calibration layer

- Realized P&L used the RAW model probability. `calibration_curves.json` (M8, populated) is now
  applied: per-station `HIGH/up` bins → pooled `_global` `HIGH/up` → raw fallback.
- The sweep reports **calibrated p**, plus **Brier (calibrated)** and **Brier (raw)**, plus a
  **10-bin reliability table** (outcome axis = `P(high≥strike)` = the YES event, matching the
  curve's axis).

**Effect (13:30Z, fee+intraday on):** mean model−market edge **0.205 → 0.179**; Brier(cal)
**0.154 → 0.126** (better). Calibration **shrinks the edge** — the expected/honest outcome.

### ⚠️ Axis caveat (must be surfaced, not hidden)

`calibration_curves.json` is built **per `scripts/build_calibration_curves.py`** on the
**day-over-day warming direction**: `compute_ensemble_fraction` returns
`predicted_warming = ensemble_mean > prev_temp`; the stored bins are `(confidence, predicted_warming
== actual_warming)`. Its `up`/`down` split is the *warming direction*, and confidence is the
ensemble **fraction**, not a strike probability. The B2 sweep's `P(high≥T)` is the
**strike-crossing axis** (per Dan's 2026-09-19 ruling). Feeding the Gaussian strike probability
into the day-over-day bins is an axis mismatch — visible in the reliability table, where the
lowest-p bin still holds 0.998 "locked" values (e.g. KATL T89 at 13:30 with the observed high at
78.8°F).

A **strike-axis** calibration artifact exists (`data/ensemble_fraction_calibration.json`: "model_prob
= count(members > threshold)/31 … calibrated_prob = P(actual_exceeds_threshold | model_prob in
bin)") and is the correct curve for this layer. **Recommendation:** swap H2's source to it in a
follow-up and re-measure; the current H2 is a faithful wiring of the artifact the brief named, with
the mismatch flagged.

---

## H3 — Entry time pinned + intraday constraint

1. `--entry-utc` default **13:00:00 → 13:30:00** (pinned). A loud warning fires from both the
   `--entry-utc` help path and at runtime when the default is overridden. Bare runs no longer
   silently emit the 87-trade/Sharpe-2.39 artifact; the canonical bare run and the explicit
   `--entry-utc 13:30:00` run are **byte-identical** (verified).
2. `high_so_far` is folded into `P(high≥X)`: `P=1` for `X ≤ high_so_far`, else Gaussian with the
   remaining-day sigma — mirroring `core/intraday/distribution_adapter.update_from_intraday`. Per-day
   station-local windows use `core.station_time`; the tz registry gaps (KLAS/KMSY/KOKC/KSAT fell
   back to New York) were fixed.

**Note:** the intraday constraint was initially inert (computed but not fed to selection) — a
composition bug caught by A/B. It is now applied **before** calibration and drives selection
(`intraday_applied_n = 81` for the canonical run).

---

## H4 — Canonical run @13:30Z (fee + calibration + intraday)

**Artifact:** `data/b2_real_price_results.json` (bare run; `entry_utc_used = 13:30:00`).

| Metric | Value |
|---|---:|
| Trades | **81** |
| Wins | **51** |
| Directional accuracy | **0.6296** |
| Wilson 95% CI | **[0.5208, 0.7267]** |
| Near-money (0.15–0.85) accuracy | **0.3846** (n=13) |
| Mean model−market edge | 0.18389 |
| Mean P&L/contract (canonical fee) | **+0.01753** |
| Sharpe (annualized, daily units) | **1.28** |
| Brier (calibrated / raw) | 0.126 / 0.162 |
| Sides | yes 35 / no 46 |
| Stations | 10 |

### Reliability table (calibrated p vs empirical P(high≥strike))

| bin | n | mean p | empirical |
|---|---:|---:|---:|
| 0.0–0.1 | 56 | 0.009 | 0.054 |
| 0.1–0.2 | 6 | 0.163 | 0.167 |
| 0.2–0.3 | 2 | 0.242 | 0.000 |
| 0.3–0.4 | 1 | 0.346 | 0.000 |
| 0.4–0.5 | 3 | 0.477 | 0.000 |
| 0.5–0.6 | 9 | 0.550 | 0.222 |
| 0.6–0.7 | 2 | 0.653 | 0.000 |
| 0.9–1.0 | 2 | 1.000 | 0.000 |

### Honest verdict — does it clear 0.58?

- **Point accuracy 0.6296 clears 0.58.** But the **Wilson 95% CI is [0.521, 0.727]** — the lower
  bound is **below 0.58**, so the result is **NOT statistically significant** at the Gray-Room gate
  on n=81.
- **Near-money subset is 0.385 (n=13)** — the accuracy lives in far-from-money "no" trades
  (bin 0.0–0.1, n=56). That is the same structural caveat Gilfoyle flagged on the pre-H number, and
  it did not improve.
- P&L (+0.0175) and Sharpe (1.28) are positive but rest on a small, thin sample; the near-money
  cell is negative.
- **Bottom line: the corrected number is still not quotable as an edge.** Accuracy is nominally above
  the gate, but the CI straddles it and the near-money slice is weak. Do not treat as durable.

| Variant (13:30Z) | n | acc | P&L | Sharpe | edge | Brier(cal) |
|---|---:|---:|---:|---:|---:|---:|
| legacy fee + no calib + no intraday | 87 | 0.598 | 0.0321 | 2.39 | 0.203 | 0.152 |
| + canonical fee (P&L only) | 86 | 0.593 | 0.0256 | 1.89 | 0.205 | 0.154 |
| + intraday (no calib) | 82 | 0.646 | 0.0318 | 2.30 | 0.212 | 0.164 |
| + calibration (no intraday) | 86 | 0.581 | 0.0172 | 1.26 | 0.179 | 0.121 |
| **H4 final (all three)** | **81** | **0.630** | **0.0175** | **1.28** | **0.184** | **0.126** |

---

## Files

| path | change |
|---|---|
| `core/fees.py` | canonical `fee_per_contract`; `kalshi_fee` per-contract ceil; self-test fixed |
| `core/strike_selection_ev.py` | `kalshi_fee` delegates to canonical (was `ceil(0.07·P)`) |
| `core/continuous_kelly.py` | fee delegates to canonical (was cents/per-order) |
| `core/market_cost_model.py` | fee delegates to canonical (unchanged formula) |
| `core/paper_trading_engine.py` | live-path fee → canonical dollars |
| `core/station_time.py` | tz registry gaps (KLAS/KMSY/KOKC/KSAT) |
| `scripts/b2_real_price_sweep.py` | H1 fee + H2 calibration + H3 entry/intraday + reliability/Brier/Wilson + ablation flags |
| `scripts/verify_fee_formula.py` | canonical expectation |
| `scripts/sweep/{kalshi_sweep_eval,gefs_grid_sweep,old_signal_validation}.py`, `scripts/sweep_metrics.py`, 12 harness files | delegate to canonical |
| `tests/test_sprint_modules.py` | corrected the buggy `0.90→0.07` assertion |
