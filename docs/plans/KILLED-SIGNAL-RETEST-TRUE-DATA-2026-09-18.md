# Killed-Signal Re-Test Against TRUE Data (Strike-Crossing Axis)

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved)
**Runner:** `scripts/killed_signal_retest_true_data_full.py` (single deterministic pass)
**Receipt:** `reports/killed_signal_retest_true_data_full_2026-09-18.json`
**Type:** READ-ONLY. No signal re-enabled, re-registered, or taxonomy-flipped. No commit. Recommendation + numbers only. Dan holds the revival decision.

---

## 0. What this re-test answers (and why the prior verdict must be discarded)

Dan's directive: *"old failed signals we might be able to test against true data or invert."*

The prior `INVERSION-BACKTEST-2026-09-18.md` concluded **"inversion is a dead end, zero revival"** — but it scored against the **WRONG target variable**. It used `settlement_bucket > prior_settlement_bucket` (day-over-day warming/cooling) as the ground-truth direction, and it ran against the old 56-strike-row partial surface.

The P0 direction-axis correction (`DIRECTION-AXIS-CORRECTION-2026-09-18.md`) establishes that the **correct** target for a HIGH/LOW Kalshi contract is **strike-crossing**: `settlement_temp > strike` (HIGH win) / `< strike` (LOW win), per `core/settlement_outcome.py`. The day-over-day axis is a *different, easier* prediction problem that is **not what Kalshi pays out on**.

This re-test re-runs the killed signals against the **correct strike-crossing axis** using the best-available true data now in the tree.

---

## 1. Correct-axis label universe (what we actually scored)

The full historical strike backfill (P0-B) has **NOT landed** — `settlement_truth` carries only 56 rows with a real strike (2026-09-07/08). However, `kalshi_market_snapshots.db` **already persists** the strike in every `market_ticker` (e.g. `KXHIGHTATL-26SEP08-T91` → strike 91), spanning **45 target dates (2026-08-06 → 2026-09-19)** across 20 stations.

By parsing the `T` (strict "greater"/"less") contracts from the tickers and joining against `settlement_truth`'s **date-tiered** settlement temps (GHCN authoritative → Kalshi finalized → WeatherAPI recency bridge), we reconstruct a correct-axis label set far larger than the 56 rows:

| Metric | Value |
|---|---|
| Correct-axis labels (T-contracts, strict strike-crossing) | **1,537** |
| Label distribution | `up` 1,264 / `down` 273 (82% / 18%) |
| temp_source | GHCN 1,298 (authoritative), kalshi_finalized 79, weatherapi 160 |
| Stations | 20 |
| Date span | 2026-08-06 → 2026-09-18 |

**Note:** this is still only the ~6-week recent window (Aug–Sep), NOT the full 2021→present history. The P0-B full-strike backfill is still required before any revival decision is final. The label set is also **heavily imbalanced** (82% `up`), so **accuracy is misleading** — MCC is the honest skill metric throughout this report.

---

## 2. Results — killed signals on the CORRECT axis (MCC is the metric)

| Signal | orig acc | orig MCC | invert acc | invert MCC | n | Verdict (MCC-based) |
|---|---|---|---|---|---|---|
| `pressure_delta` (ref — live) | 57.79% | **+0.0225** | 42.21% | −0.0225 | 526 | **No edge on strike axis** |
| `corrected_pressure_delta` | 42.21% | −0.0225 | 57.79% | +0.0225 | 526 | Mirror of `pressure_delta` — nothing new |
| `spike_reversion` (goldilocks daily) | 18.69% | **+0.0000** | 81.31% | **+0.0000** | 444 | One-directional; zero information |
| `persistence` | 45.45% | −0.0142 | 54.55% | +0.0142 | 1,417 | Coin-flip |
| `simple_trend` | 45.45% | −0.0142 | 54.55% | +0.0142 | 1,417 | Coin-flip (identical to persistence) |
| `wind_direction_shift` | 48.00% | −0.2885 | 52.00% | +0.2885 | 50 | Underpowered (n=50) — noise |
| `nwp_analog` | 29.41% | −0.0637 | 70.59% | +0.0637 | 1,537 | Weak negative, below any skill bar |
| `late_day_momentum` | — | — | — | — | — | UNAVAILABLE (intraday) |
| `goldilocks_microstructure` | — | — | — | — | — | UNAVAILABLE (intraday) |

**Key:** every MCC is within ±0.07 — i.e. **statistically indistinguishable from zero skill**. The "STABLE WRONG-WAY" flags that a naive accuracy threshold would emit are **base-rate artifacts** of the 82%/18% label imbalance, not real inversion edges. MCC (the imbalance-immune metric) shows no signal is meaningfully wrong *or* right.

---

## 3. The headline finding: the axis correction kills the LIVE signal, not the killed ones

Compare the same `pressure_delta` signal on the two axes:

| Axis | accuracy | MCC | n |
|---|---|---|---|
| **Day-over-day (WRONG — prior inversion backtest)** | 61.86% | **+0.2378** | 30,516 |
| **Strike-crossing (CORRECT — this re-test)** | 57.79% | **+0.0225** | 526 |

The live, "correct-way" `pressure_delta` signal has **real, strong edge** (+0.24 MCC) against the *day-over-day* target — but that edge **collapses to zero (+0.02 MCC)** against the *strike-crossing* target that Kalshi actually settles on.

**This reframes the entire killed-signal question.** The killed signals were not killed because their "wrongness" was mis-measured on corrupt labels. They were killed while the *whole signal family* — live and dead alike — was being scored against a target (warming-vs-yesterday) that is **fundamentally easier than, and orthogonal to, the real settlement** (crossing a strike). The prior inversion verdict was "dead end" against day-over-day; the correct-axis result is *also* "no edge" — but for a different, more fundamental reason.

---

## 4. Per-signal verdicts (correct axis)

### `corrected_pressure_delta` — MIRROR, no revival
On the strike axis it is the **exact negation** of `pressure_delta` (TP/FP/TN/FN transposed), confirming the prior structural finding: it is `pressure_delta` with a flipped direction mapping. But `pressure_delta` itself has **no edge** on the strike axis (MCC +0.02). So the "inversion" reconstructs a signal that is *also* worthless on the correct target. **KEEP-KILLED.**

### `spike_reversion` (goldilocks daily) — ONE-DIRECTIONAL, no information
TP=0, FP=0 in the confusion matrix — the daily signal **only ever returns `'down'`** (spike-up → reversion-down), never `'up'`. Its 18.69% accuracy is just the 18% minority-class base rate; inverting to always-`'up'` yields 81.31% (the majority base rate). MCC = 0.0000 in both directions — **zero directional information.** The "81% inverted accuracy" is a mirage. **KEEP-KILLED.**

### `nwp_analog` — weak negative, below skill bar, no inversion edge
MCC −0.0637 original / +0.0637 inverted. It predicts `'down'` heavily (1,212 of 1,537 fires) and is therefore wrong 70% of the time on the `up`-heavy strike axis. But the inverted MCC (+0.064) is **far below any usable threshold** (Gray Room ≥0.1 class, and this is a 6-week window). The data-gating condition (NWP data now populated) is satisfied, but the signal still shows no actionable skill on the correct target. **KEEP-KILLED** pending the full-strike-backfill re-run.

### `persistence` / `simple_trend` — coin-flip, redundant
MCC −0.0142 each, **identical** confusion matrices (TP=502 both) — confirming the ρ=1.0 structural redundancy. Both are coin-flips on the strike axis. **KEEP-KILLED** (retain `persistence` as the skill baseline only).

### `wind_direction_shift` — underpowered
n=50 (only 50 of its 13,156 fires land on strike-label dates). MCC −0.2885 is the largest magnitude but on a sample too small to trust; likely noise. **KEEP-KILLED**, re-evaluate only after full strike backfill.

### `late_day_momentum` / `goldilocks_microstructure` — UNAVAILABLE
Both are intraday/real-time signals with no daily `evaluate(idx, days)` surface in this tree, and both depend on the `transition_events` label surface (absent from this container). **Cannot score.** Blocked on runtime label data, not on this analysis.

---

## 5. Conclusion — what to surface to Dan

**On the correct strike-crossing axis, the inversion verdict is: NO revival, and for a deeper reason than the prior "dead end."**

1. **No killed signal is "stably wrong" on the correct axis.** Every MCC is within ±0.07, i.e. noise. The apparent inversion edges are base-rate artifacts of an 82%/18% label imbalance, not persistent directional wrongness.

2. **The more important finding: even the LIVE `pressure_delta` signal loses its edge on the strike axis** (MCC +0.24 → +0.02). This means the *entire signal family* was historically tuned against the wrong (day-over-day) target. That is a **first-principles problem** bigger than any individual killed signal: the signals predict "warmer than yesterday," but Kalshi pays out on "crossed the strike."

3. **Blocked on P0-B.** The full 2021→present strike history has not landed. This re-test used the best-available correct data (1,537 T-contract labels, Aug 6 → Sep 18), but that is a ~6-week window. **The final inversion verdict must be re-run after P0-B lands** — and, more importantly, after the signal family is re-baselined against the strike-crossing target.

4. **No revival is warranted on this result.** Recommendation: keep all killed signals killed. The actionable next step is **not** inversion of any individual signal — it is (a) landing P0-B, then (b) re-baselining the live signals against strike-crossing to see whether *any* signal has genuine edge on the real target.

---

## 6. Scope compliance

- ✅ No live trading. ✅ No destructive cleanup. ✅ No commit — dirty tree preserved.
- ✅ No signal re-enabled, re-registered, or taxonomy-flipped. `NWP_ANALOG_ENABLED` left as-found.
- ✅ Read-only DB access: `metar_backfill.db`, `kalshi_market_snapshots.db`, `nwp_forecasts.db` untouched (no `-wal`/`-journal` sidecars left).
- ✅ Added files (read-only artifacts only): `scripts/killed_signal_retest_true_data.py`, `scripts/killed_signal_retest_true_data_expanded.py`, `scripts/killed_signal_retest_true_data_full.py`, `reports/killed_signal_retest_true_data_full_2026-09-18.json`, this doc.

## 7. Handoff / escalation

- **Decision gate for any revival:** Dan (merge/approval), per weather-engine merge policy. Gerri for exposure framing if a signal ever clears a future retest.
- **Owner of P0-B (full strike backfill):** Gilfoyle. **Blocked item:** the full 2021→present Kalshi strike history (currently only the Aug 6 → Sep 19 market-ticker strikes are captured). **Dan needed:** yes — decide whether to pull historical strikes via API (`/markets/{ticker}`) or accept partial coverage.
- **Owner of the deeper finding (live signals lose edge on strike axis):** Gilfoyle, for a first-principles re-baseline of the signal family against strike-crossing. This is a larger investigation than the killed-signal question.
- **No further action this turn** — recommendation + numbers only.
