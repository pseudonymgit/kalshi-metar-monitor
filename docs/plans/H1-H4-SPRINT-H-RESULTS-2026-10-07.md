# Sprint H — Make the Number Real (H1–H4)

**Date:** 2026-10-07 · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Harness:** `scripts/b2_real_price_sweep.py` · **Canonical artifact:** `data/b2_real_price_results.json`
**Gate:** Gilfoyle reviews H4 before any number is quoted internally.

---

## Summary — does the number move?

| | Trades | Wins | Acc | P&L/contract | Sharpe | Brier (cal) |
|---|---:|---:|---:|---:|---:|---:|
| **Pre-Sprint-H** (14:00 comment; the quoted 09:00 canonical was 0.01751/1.17) | 95 | 55 | 0.5789 | +0.01751 | 1.17 | — |
| **H4 v1** (13:30Z, fee + *day-over-day* calib + intraday) — **BLOCKED** | 81 | 51 | 0.6296 | +0.01753 | 1.28 | 0.126 |
| **H4 v2** (13:30Z, fee + *strike-axis* calib + intraday) — **canonical** | **83** | **25** | **0.3012** | **+0.01295** | **0.93** | **0.100** |

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

- Realized P&L used the RAW model probability. The sweep now reports **calibrated p**, **Brier
  (calibrated)** and **Brier (raw)**, plus a **10-bin reliability table** (outcome axis =
  `P(high≥strike)` = the YES event).

**First wiring (BLOCKED, superseded):** the initial H2 wired `calibration_curves.json` (the artifact
name in the brief). Gilfoyle's gate review found that artifact is built on the **day-over-day
warming axis** (`build_calibration_curves.py::compute_ensemble_fraction` → `predicted_warming =
ensemble_mean > prev_temp`) — a different event than the sweep's strike-crossing `P(high≥T)`. The
mapping silently fabricated the trade set (56/81 trades forced into the 0.0–0.1 bin with p≈0.009).
**Verdict: NOT QUOTABLE.** (Review: `docs/plans/H4-REVIEW-2026-10-07.md`.)

**Fix (implemented, required change #1):** H2 now consumes `data/strike_axis_calibration.json`,
built by `scripts/build_strike_axis_calibration.py` on the **correct axis**, from the sweep's own
(strict strike-crossing) ladder outcomes, and **out-of-sample** — trained only on station-days
**before 2026-08-01**, so the Aug–Oct eval window is scored against prior history. The predictor is
the same intraday-constrained `P(high≥T)` the sweep selects on (domain match); locked strikes
(`T ≤ high_so_far`, a mathematical certainty) are excluded from the fit and passed through
uncalibrated in the sweep.

**Why calibration *hurts* — honest finding:** the model's shadow Gaussian uses σ = ensemble
spread/4 (≈1.6°F), far too narrow. The reliability table shows it is **inverted** at high p:
model p≈0.9–1.0 actually settles yes ~10–15% of the time. The strike-axis curve captures this
(calibrated ceiling ≈ 0.11) — which is why calibration moves the number a lot (it rejects the
model's high-p "confident YES" picks). OOS Brier confirms the curve generalizes: **raw 0.159 →
calibrated 0.100**, including on the held-out Aug–Oct window the curve never saw.

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

## H4 — Canonical run @13:30Z (fee + strike-axis calibration + intraday)

**Artifact:** `data/b2_real_price_results.json` (bare run; `entry_utc_used = 13:30:00`).
**This is the H4 v2 number (post H2 axis fix).** H4 v1 (day-over-day calibration) is superseded.

| Metric | Value |
|---|---:|
| Trades | **83** |
| Wins | **25** |
| Directional accuracy | **0.3012** |
| Wilson 95% CI | **[0.2131, 0.4069]** |
| Near-money (0.15–0.85) accuracy | **0.6429** (n=14) |
| Mean model−market edge | 0.1092 |
| Mean P&L/contract (canonical fee) | **+0.01295** |
| Sharpe (annualized, daily units) | **0.93** |
| Brier (calibrated / raw) | 0.100 / 0.159 |
| Sides | yes 55 / no 28 |
| Stations | 11 |

### Reliability table — raw model p (the finding)

In-window raw-p vs actual `P(high≥strike)` (outcome = result=='yes') shows the model is
**miscalibrated, not merely overconfident**: high p does not mean likely yes.

| raw p bin | n | yes rate |
|---|---:|---:|
| 0.0–0.1 | 53 | 0.057 |
| 0.1–0.2 | 6 | 0.167 |
| 0.2–0.3 | 5 | 0.000 |
| 0.3–0.4 | 2 | 0.000 |
| 0.5–0.6 | 6 | 0.167 |
| 0.7–0.8 | 4 | 0.500 |
| 0.8–0.9 | 2 | 0.000 |
| 0.9–1.0 | 4 | 0.000 |

### Honest verdict — does it clear 0.58?

- **NO.** Corrected accuracy is **0.3012** — the strike-axis calibration shows the model's confident
  directional picks are largely wrong. **It does not clear 0.58**, and it is significant the wrong
  way (Wilson CI [0.213, 0.407], entirely below the gate).
- **The near-money subset is 0.643 (n=14)** — the *opposite* pattern from H4 v1: the model is better
  near the money than far from it. But n=14 is far too small to claim anything.
- **+0.01295/contract, Sharpe 0.93** — positive but far below the gate, and below the pre-H figure.
- **Bottom line: the corrected number is NOT quotable as an edge. It is not an edge.** The headline
  direction of the whole B2 effort — "B2 is a strong signal" — was a wrong-axis artifact; the
  honest, axed result is below coin-flip on the model's own picks.

| Variant (13:30Z) | n | acc | P&L | Sharpe | edge | Brier(cal) |
|---|---:|---:|---:|---:|---:|---:|
| legacy fee + no calib + no intraday | 87 | 0.598 | 0.0321 | 2.39 | 0.203 | — |
| no-calibration (canon fee + intraday) | 82 | 0.646 | 0.0318 | 2.30 | 0.212 | — |
| **H4 v1** (day-over-day calib) — BLOCKED | 81 | 0.630 | 0.0175 | 1.28 | 0.184 | 0.126 |
| **H4 v2** (strike-axis calib) — canonical | **83** | **0.301** | **0.0130** | **0.93** | **0.109** | **0.100** |

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
| `scripts/build_strike_axis_calibration.py` | new — strike-crossing-axis OOS reliability curve (H2 fix) |
| `data/strike_axis_calibration.json` | new — the H2 curve (trained pre-2026-08-01) |
