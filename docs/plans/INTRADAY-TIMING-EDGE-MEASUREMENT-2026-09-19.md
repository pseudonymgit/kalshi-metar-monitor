# Intraday-Timing Edge Measurement — Trajectory / Magnitude Branch

**Date:** 2026-09-19
**Owner:** Gilfoyle (subagent, deterministic measurement)
**Repo:** `prototypes/weather-engine-source` (`temp-sprint-branch`)
**Status:** Read-only, script-only, no AI, no signal modification, no DROP/backfill.

---

## 0. Hypothesis under test (Dan, 2026-09-19)

> As the day progresses toward the daily-high time (early-to-mid afternoon), the daily
> high becomes more constrained by already-observed temps, so predicting whether the high
> crosses the strike should get MORE ACCURATE. The h-24 directional settlement scoring so
> far is blind to intraday timing.

**Question:** does intraday timing add measurable edge over the h-24 daily prediction?
Is there a tradeable window?

---

## 1. Method (deterministic, no lookahead)

- **Axis:** strike-crossing (correct axis per `core/settlement_outcome.py`). HIGH = `settlement_temp_f > strike_price`; LOW = `settlement_temp_f < strike_price`; strict `>`/`<`.
- **Ground truth:** `settlement_truth` (date-tiered: GHCN → Kalshi finalized → WeatherAPI), `trustworthy=1`, `strike_type IN ('greater','less')`.
- **Universe:** 20 canonical stations, 14,130 labelable strike-crossing days (2022-12-22 → 2026-09-18).
- **h-24 baseline:** NWP daily forecast `temperature_2m_max`/`temperature_2m_min` (ensemble mean across gfs/ecmwf/gefs_ens/gem/icon/era5), `fetch_date <= target` (contemporaneous).
- **Intraday signal:** running observed extremum from METAR `metar_observations` (UTC→local hour via `station_time` timezones).
- **At each local hour H:** predict "cross" using `max(forecast, running_observed_max)` (HIGH) / `min(forecast, running_observed_min)` (LOW).
- **Fees:** `market_cost_model` round-trip (spread 3.1¢/2 + slippage 0.5¢ = 2.05¢, commission 0) + Kalshi taker fee `0.07·P·(1-P)`.

**Critical refinement — "undecided-only" filter.** Predicting "cross" is *trivially correct*
once the observed temp has already crossed the strike (mechanical realization, no predictive
value). To isolate predictive skill, we score the **still-undecided subset**: days where the
cross has NOT yet been observed at hour H. This is the only subset that answers the timing
question honestly.

---

## 2. Results

### 2a. Naive trajectory (ALL days) — looks like a rising curve (misleading)

| Hour (local) | n | Accuracy | Cross-base | Precision(cross) | Recall | Net EV (cross) |
|---|---|---|---|---|---|---|
| 0 (h-24) | 14129 | 0.666 | 0.088 | 0.065 | 0.209 | **−0.049** |
| 6 | 14130 | 0.664 | 0.088 | 0.067 | 0.218 | −0.047 |
| 12 | 14130 | 0.662 | 0.088 | 0.090 | 0.312 | −0.024 |
| 14 | 14130 | 0.669 | 0.088 | 0.128 | 0.477 | **+0.014** |
| 15 | 14130 | 0.670 | 0.088 | 0.139 | 0.532 | +0.025 |
| 16 | 14130 | 0.669 | 0.088 | 0.147 | 0.578 | +0.033 |
| 23 (close) | 14130 | 0.666 | 0.088 | 0.153 | 0.620 | +0.040 |

This apparent "rising precision/EV toward 14–16h" is **entirely mechanical** — it counts days
where the cross has *already been observed* by that hour, which are trivially (not
predictively) correct.

### 2b. Predictive-only (UNDECIDED days) — the honest answer: flat / no edge

| Hour (local) | n (undecided) | Accuracy | Cross-base | Precision(cross) | Net EV (cross) |
|---|---|---|---|---|---|
| 0 (h-24) | 10988 | 0.617 | 0.066 | **0.052** | **−0.039** |
| 6 | 10919 | 0.618 | 0.065 | 0.051 | −0.039 |
| 12 | 10691 | 0.617 | 0.060 | 0.051 | −0.034 |
| 14 | 10533 | 0.618 | 0.055 | 0.050 | −0.030 |
| 15 | 10451 | 0.618 | 0.052 | 0.050 | −0.027 |
| 16 | 10380 | 0.618 | 0.051 | 0.049 | −0.025 |
| 18 | 10301 | 0.617 | 0.049 | 0.048 | −0.025 |
| 23 (close) | 10106 | 0.620 | 0.045 | 0.036 | −0.032 |

**Precision of predicting the cross is flat-to-slightly-declining across all hours**
(0.052 → 0.036), and **net EV is negative at every hour**, never turning positive. Accuracy
is flat (~61.7%). The NWP-only baseline is identical at every hour (0.617 / precision 0.054),
confirming the intraday trajectory adds *nothing* over the h-24 forecast on undecided days.

### 2c. Per-station "undecided" precision (cross) at key hours

| Station | p(h0) | p(h8) | p(h12) | p(h15) | p(h16) | EV(h15) |
|---|---|---|---|---|---|---|
| KDEN | 0.313 | 0.309 | 0.309 | 0.310 | 0.305 | **+0.120** |
| KAUS | 0.157 | 0.159 | 0.160 | 0.153 | 0.153 | **+0.061** |
| (all others) | 0.00–0.05 | ≈flat | ≈flat | ≈flat | ≈flat | negative |

Two stations (KDEN, KAUS) show positive net EV — but their precision is **flat across hours**
(KDEN 0.313→0.310→0.299; KAUS 0.157→0.153), i.e. it is a *persistent station-level NWP
forecast skill*, already present at h-24 and **not improving intraday**. It is not a timing edge.

---

## 3. Verdict

**NO TIMING EDGE — the trajectory/magnitude branch is NOT worth building.**

1. **No rising accuracy curve.** Predictive accuracy on still-undecided days is flat (~61.7%)
   across all hours; there is no accuracy peak at the climatological high-of-day (14–16h local).
2. **No tradeable window.** Net EV (after round-trip 2.05¢ + Kalshi taker fee) is **negative at
   every hour** on the cross side; the apparent positive-EV afternoon window is an artifact of
   already-realized crosses, not predictive skill.
3. **The hypothesis is refuted.** The daily high is *not* meaningfully "more predictable" as the
   day progresses — by the time observed temps constrain the outcome, the cross has usually
   already resolved (mechanical), and before that the NWP forecast carries all the information
   (flat precision).
4. **What the data does support:** a *station-level* persistent edge at KDEN (+0.12 EV) and
   KAUS (+0.06 EV) that is independent of time-of-day. If anything is worth a follow-up, it is
   station-selective h-24 trading on those two stations — not an intraday timing layer.

---

## 4. Artifacts

- Script: `scripts/measure_intraday_timing_edge.py` (naive + all-days)
- Script: `scripts/measure_intraday_timing_edge_v2.py` (undecided-only + per-station)
- Report data: `data/measurement_intraday_timing_edge.json`, `data/measurement_intraday_timing_edge_v2.json`

**Receipt:** measurement is deterministic and re-runnable; no source data was written.
