# Trajectory / Magnitude Prediction — Kalman Nowcast Measurement

**Date:** 2026-09-19
**Owner:** Gilfoyle (subagent, deterministic measurement)
**Repo:** `prototypes/weather-engine-source` (`temp-sprint-branch`)
**Status:** Read-only, script-only, no AI, no signal modification, no DROP/backfill.

---

## 0. Question under test (Dan, 2026-09-19)

The timing MEASUREMENT already refuted the "accuracy rises approaching high-of-day"
hypothesis on the **direction** axis (flat ~61.7%, no timing edge). This task tests
the **magnitude** axis: can a Kalman/trajectory nowcast predict the *final settlement
temperature value* (the daily high for HIGH markets, daily low for LOW markets) as a
function of hour H, and does it beat the static h-24 NWP forecast?

Two sub-questions:
1. Does the nowcast reduce **MAE** vs static NWP, and does the error **shrink as H advances**?
2. Does the improved magnitude prediction yield a **fee-adjusted +EV** trade?

---

## 1. Method (deterministic, no lookahead, correct units)

- **Target:** `settlement_temp_f` magnitude (the realized daily high/low), the actual °F
  value, not a direction label. Ground truth `settlement_truth`, `trustworthy=1`,
  `strike_type IN ('greater','less')`.
- **Universe:** 20 stations × strike-crossing days **with an NWP h-24 forecast**
  (2025-01 → 2026-09-18; NWP coverage starts 2025-01-01). ~11,047 labelable days.
- **h-24 NWP prior:** `temperature_2m_max` (HIGH) / `temperature_2m_min` (LOW), ensemble
  mean across gfs/ecmwf/gefs_ens/gem/icon/era5, `fetch_date <= target_date` (no lookahead),
  lead-1d preferred. **Values converted °C → °F** (see §5 — a prior measurement skipped this).
- **Nowcast:** truncated-normal conditional mean. The settlement temp M ~ N(μ_nwp, σ²)
  (σ = 3.45°F, NWP error std, globally calibrated). Observing METAR temps up to hour H
  reveals the running extremum, which is a **one-sided bound** on M:
  - HIGH: M ≥ running_max  →  nowcast = E[M | M ≥ running_max]
  - LOW : M ≤ running_min  →  nowcast = E[M | M ≤ running_min]
  This is the mathematically-correct fusion of a forecast with a realized bound. (A
  symmetric constant-state Kalman was also tried and was strictly worse — it treats the
  monotone bound as an unbiased sample and biases the estimate.)
- **Fees:** round-trip 2.05¢ (spread 3.1¢/2 + slippage 0.5¢) + Kalshi taker `0.07·P·(1-P)`
  (same cost model as the direction measurement).
- **EV:** realized net EV = precision(cross) − base(cross) − round_trip − taker, matching
  the direction measurement's definition exactly.

---

## 2. Results

### 2a. Magnitude MAE — nowcast is WORSE than NWP-only, and does NOT improve

| Hour (local) | Nowcast MAE | NWP-only MAE | Δ (nowcast−NWP) |
|---|---|---|---|
| 0 (h-24) | 2.659 | 2.464 | **+0.196** |
| 4 | 2.724 | 2.464 | +0.260 |
| 8 | 2.775 | 2.464 | +0.311 |
| 12 | 2.811 | 2.464 | +0.347 |
| 16 | 2.961 | 2.464 | +0.497 |
| 20 | 3.022 | 2.464 | +0.558 |
| 23 (close) | 3.114 | 2.464 | **+0.650** |

The nowcast is **worse than NWP-only at every hour**, and the gap *widens* as the day
progresses (+0.20°F → +0.65°F). There is **no shrinking-error curve** — the opposite of
the hypothesis. The one-sided running-extremum bound introduces a systematic bias (upward
for HIGH, downward for LOW) that the conditional mean over-corrects, and the running
extremum carries no *predictive* information about the final magnitude beyond what NWP
already encodes.

### 2b. Fee-adjusted net EV — nowcast adds nothing over NWP-only

| Hour | NWP-only EV | Nowcast EV | Undecided-only EV |
|---|---|---|---|
| 0 (h-24) | +0.190 | +0.176 | +0.172 |
| 8 | +0.190 | +0.151 | +0.148 |
| 16 | +0.190 | +0.163 | +0.103 |
| 23 (close) | +0.190 | +0.161 | +0.095 |

The nowcast **never beats** NWP-only on EV, and on the still-undecided subset (the honest
predictive test) its EV *declines* with hour (+0.17 → +0.10), confirming the intraday
trajectory adds nothing.

### 2c. The real (surprising) finding — NWP h-24 has edge, once units are correct

The NWP-only directional precision (correct °F units) is **0.19–0.215** against a base
cross rate of **0.04–0.067**, for a net EV of **≈ +0.15 to +0.19** per $1. That is a
genuine, persistent edge in the h-24 forecast itself — *not* an intraday timing/magnitude
effect. (See §5: the prior timing measurement reported NWP precision ~0.05 / net EV ~−0.04
because it compared °C NWP values against °F strikes.)

---

## 3. Verdict

**The trajectory/magnitude branch is DEAD — no edge, same as the direction branch.**

1. **No shrinking MAE.** The nowcast is worse than static NWP at every hour (+0.20 → +0.65°F),
   and the error *grows* with H. Observing intraday temps does **not** let us predict the
   final settlement magnitude any better than the h-24 forecast already does.
2. **No tradeable magnitude edge.** Fee-adjusted EV is never improved by the nowcast; on the
   undecided subset it degrades (+0.17 → +0.10).
3. **The hypothesis is refuted on the magnitude axis too.** By the time observed temps would
   meaningfully constrain the final magnitude (mechanical realization near close), the outcome
   has already resolved; before that, the running extremum carries no predictive signal beyond
   NWP.

**What the data DOES support** (actionable, not an intraday layer): the **h-24 NWP forecast
itself has a real, positive directional edge** (~+0.15 to +0.19 net EV) once the °C→°F unit
error is corrected. That is a *stationary* h-24 forecasting edge, not a trajectory/timing edge.

---

## 4. Artifacts

- Script: `scripts/measure_trajectory_magnitude.py` (deterministic, re-runnable)
- Report data: `data/measurement_trajectory_magnitude.json`

**Receipt:** measurement is deterministic and re-runnable; no source data was written.

---

## 5. Data-integrity note (surfaced during this build)

Two latent data-quality issues were discovered and handled:

1. **NWP units:** `nwp_forecasts.temperature_2m_{max,min}` are stored in **Celsius**, while
   `settlement_truth.settlement_temp_f` and `metar_observations.temp_f` are **Fahrenheit**.
   The prior direction measurement (`measure_intraday_timing_edge_v2.py`) compared °C against
   °F directly, which scrambled its NWP-only precision (reported ~0.05; correct value ~0.19).
   This measurement converts °C → °F before any comparison.

2. **METAR outliers:** ~0.05% of `metar_observations.temp_f` rows are corrupt (a °C value
   double-stored as °F, e.g. 64,893°F = 36,034°C). These poison the running extremum and are
   filtered by a physical-plausibility bound (−60..140°F).
