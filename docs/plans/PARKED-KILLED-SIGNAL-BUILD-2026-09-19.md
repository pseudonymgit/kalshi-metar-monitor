# Parked/Killed Signal Build + Test — Receipt (2026-09-19)

**Branch:** `temp-sprint-branch`
**Directive:** Dan 2026-09-19 — stop parking signals, build and test them standalone.
**Axis:** strike-crossing (`settlement_temp_f` vs `strike_price`, T-contract strict
greater/less via `core/settlement_outcome.contract_is_itm`), NOT day-over-day.
**Commit:** `a84c9e320` (build receipt — signals + standalone harness + report).
**Scope:** READ-ONLY on data DBs. No live-path wiring (R/T-5 is Dan's gate). No DROP/backfill re-run.

---

## Label universe (correct axis)

Built from `kalshi_market_snapshots.db` T-contract tickers × `settlement_truth`
date-tiered temps (GHCN authoritative → Kalshi finalized → WeatherAPI bridge).

| Metric | Value |
|---|---|
| Strike-crossing labels | 1,537 |
| Distribution | up 1,264 / down 273 (82% / 18% — heavily imbalanced) |
| temp_source | ghcn 1,298 / kalshi_finalized 79 / weatherapi 160 |
| Stations | 20 |
| Date span | 2026-08-06 → 2026-09-19 |

---

## Per-signal build + standalone test result

| Signal | Built? | Fires | acc | MCC | minority (prec/share) | Note |
|---|---|---|---|---|---|---|
| `trajectory_state_vector` | ✅ REBUILT | 1,390 | 32.73% | −0.0085 | down (0.18/0.18) | was NO_FIRES stub → Kalman state-vector + magnitude |
| `nwp_analog` | ✅ REBUILT | 1,499 | 32.29% | −0.1057 | down (0.15/0.18) | fixed gate placement + upper-air-only field set |
| `rdae_mos` | ✅ (already built) | 1,307 | 58.84% | −0.0830 | down (0.13/0.18) | METAR-only analog MOS (inherently contemporaneous) |
| `ghcn_calibration` | ✅ (already built) | 1,402 | 18.62% | +0.0000 | down (0.19/0.19) | one-directional; fires via DB path, not in-memory |
| `sensor_bias_correction` | ✅ (already built) | 1,341 | 51.83% | +0.0366 | down (0.20/0.19) | fires |
| `goldilocks` | ✅ (already built) | 908 | 51.21% | +0.0358 | down (0.19/0.17) | fires |
| `persistence` | ✅ (already built) | 1,417 | 45.45% | −0.0142 | down (0.18/0.18) | coin-flip baseline |
| `late_day_momentum` | ⚠️ intraday-only | — | — | — | — | logic reconstructable but intraday (17–22 UTC), no daily surface |

---

## What was actually changed (commit `a84c9e320`)

1. **`core/signals/trajectory_state_vector_signal.py`** — replaced the hollow
   `return (None, 0.5)` stub with a deterministic constant-velocity Kalman filter
   over the daily temperature series. State vector `[temp, dT/dt]`, projected one
   day forward, compared against day-of-year climatology (`station_climatology`)
   or rolling baseline. Emits **directional** output (`up`/`down`) **plus** a
   `magnitude()` accessor returning the projected °F anomaly — Dan's
   "magnitude-play" angle (downstream can gate on `|ΔT| > 3°F`). Contemporaneous
   by construction (days strictly before target index only).

2. **`core/signals/nwp_analog_signal.py`** — two fixes:
   - **Gate placement bug (P0-F):** the `fetch_date <= target_date` predicate was
     appended *after* `GROUP BY station, target_date`, which SQLite parses as part
     of the GROUP BY expression — silently collapsing the feature pool to ~40 keys.
     Moved the predicate into the `WHERE` clause. Now 2,820 contemporaneous feature keys.
   - **Field set:** restricted `FIELD_WEIGHTS` to upper-air-only
     (`temperature_850hPa_daily_mean` 0.60 + `geopotential_height_500hPa_daily_mean`
     0.40). The prior 5-field set required surface fields (`cloud_cover`,
     `dew_point_2m`, `temperature_2m_max`) whose contemporaneous coverage ends at
     ~626 dates and does NOT reach the Aug–Sep 2026 label window — so the signal
     could never fire. Upper-air fields are the ones that were most
     lookahead-contaminated (92.5% per P0-F) and now carry the mandatory gate.

3. **`scripts/parked_killed_signal_build.py`** — standalone correct-axis eval
   harness (reuses the ticker-parsing label builder from
   `killed_signal_retest_true_data_full.py`). READ-ONLY.

4. **`reports/parked_killed_signal_build_2026-09-19.json`** — machine receipt.

---

## Honest read on skill

Every signal is at MCC ≈ ±0.11 — i.e. **statistically indistinguishable from
zero** on the strike-crossing axis. This is *expected* and matches the P0
finding (`KILLED-SIGNAL-RETEST-TRUE-DATA-2026-09-18.md`): the entire signal
family was historically tuned against day-over-day (warming-vs-yesterday),
which is orthogonal to strike-crossing (what Kalshi actually pays). The
build-and-fire is the deliverable here; genuine edge re-baselining is blocked
on P0-B (full 2021→present strike backfill) and is Dan's gate, not this task.

`ghcn_calibration` and `goldilocks` are **one-directional** (always `down` =
the 18% minority base rate), so their MCC=0 by construction — they carry no
directional information on this axis, same as the prior verdict.

---

## Which signals now fire vs still can't be built

**Now fire (7/8):** `trajectory_state_vector`, `nwp_analog`, `rdae_mos`,
`ghcn_calibration`, `sensor_bias_correction`, `goldilocks`, `persistence`.

**Still not a daily signal (1/8):** `late_day_momentum` — logic is reconstructable
and loads/runs correctly, but it is an **intraday** signal (17–22 UTC hourly
observations, rate/plateau thresholds). It has no daily `evaluate(idx, days)`
surface and cannot be scored on the daily strike-crossing axis. It is not a
hollow stub and needs no rebuild — it needs an intraday label surface
(`transition_events`, absent in this container) to be testable.

**Beyond-repair stubs:** not touched (per task — a separate worker reverse-engineers those).
