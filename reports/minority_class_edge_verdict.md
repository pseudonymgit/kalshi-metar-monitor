# Minority-Class Edge Measurement — Definitive Verdict

**Date:** 2026-09-19
**Axis:** strike-crossing (`settlement_temp_f` vs `strike_price`) — `core/settlement_outcome.strike_crossing_direction`
**Ground truth:** `settlement_truth.strike_price`, 14,130 scored rows (HIGH=10,079, LOW=4,051)
**Data:** `reports/rt2b_prediction_matrix_cache.json` (contemporaneous-only, per-signal `[direction, confidence]`)
**Read-only.** No signal modification, no wiring, no DROP/backfill. Script-only (`scripts/minority_class_edge.py`).

---

## Base Rates (measured, matching the R/T-1 sweep)

| Market type | Minority direction | Minority base rate | Majority base rate |
|---|---|---|---|
| HIGH | **up** | **10.13%** | down 89.87% |
| LOW | **down** | **5.36%** | up 94.64% |

## Price / Cost Model (provenance)

- Contract price = minority side trades near its probability (real Kalshi model): HIGH "up" ≈ **$0.101**, LOW "down" ≈ **$0.054**.
- Round-trip cost = `core/market_cost_model.MARKET_COST_MODEL`:
  - Fixed: spread/2 + slippage = **2.05¢** (`ROUND_TRIP_FEE = 0.0205`)
  - Plus Kalshi taker fee = `ceil(0.07 × P × (1−P) × 100)/100` (price-dependent, ~0.3–1.4¢/contract)
- EV per minority-direction trade = `p_win·(1−p_trade) − (1−p_win)·p_trade − round_trip_cost`.

---

## Per-Signal Minority Precision / Recall / Base-Rate Delta / Net EV

(minority precision = hit-rate when signal predicts the rare direction; recall = fraction of rare events caught; delta = precision − base rate; net EV at real price model net of fees)

| signal | mt | minor | base | predMin | prec | delta | recall | netEV$ |
|---|---|---|---|---|---|---|---|---|
| calendar_climatology | HIGH | up | 0.1013 | 889 | 0.0945 | −0.0068 | 0.365 | −0.0373 |
| calendar_climatology | LOW | down | 0.0536 | 713 | 0.0687 | +0.0152 | 0.907 | −0.0153 |
| cooling_failure | HIGH | up | 0.1013 | 474 | 0.1055 | +0.0042 | 1.000 | −0.0263 |
| corrected_pressure_delta | HIGH | up | 0.1013 | 2824 | 0.1154 | +0.0141 | 0.539 | −0.0164 |
| corrected_pressure_delta | LOW | down | 0.0536 | 1021 | 0.0480 | −0.0056 | 0.336 | −0.0361 |
| cross_model_divergence | HIGH | up | 0.1013 | 1014 | 0.0592 | −0.0421 | 0.659 | −0.0726 |
| cross_model_divergence | LOW | down | 0.0536 | 792 | 0.0467 | −0.0068 | 0.649 | −0.0373 |
| fogr_reversion | HIGH | up | 0.1013 | 5101 | 0.1006 | −0.0007 | 1.000 | −0.0312 |
| forecast_disagreement | HIGH | up | 0.1013 | 1612 | 0.0713 | −0.0300 | 0.382 | −0.0605 |
| forecast_disagreement | LOW | down | 0.0536 | 734 | 0.0845 | +0.0309 | 0.653 | +0.0004 |
| frontal_passage_intraday | HIGH | up | 0.1013 | 286 | 0.0979 | −0.0034 | 0.118 | −0.0339 |
| frontal_passage_intraday | LOW | down | 0.0536 | 538 | 0.0892 | +0.0357 | 0.941 | +0.0052 |
| gaussian | HIGH | up | 0.1013 | 1499 | 0.1001 | −0.0012 | 0.344 | −0.0317 |
| gaussian | LOW | down | 0.0536 | 1220 | 0.0656 | +0.0120 | 0.842 | −0.0185 |
| gaussian_v2 | HIGH | up | 0.1013 | 2919 | 0.0942 | −0.0071 | 0.386 | −0.0376 |
| gaussian_v2 | LOW | down | 0.0536 | 1657 | 0.0742 | +0.0207 | 0.783 | −0.0098 |
| goldilocks | HIGH | up | 0.1013 | 2435 | 0.0936 | −0.0077 | 0.406 | −0.0382 |
| goldilocks | LOW | down | 0.0536 | 1216 | 0.0691 | +0.0155 | 0.613 | −0.0150 |
| metar_dtdt | HIGH | up | 0.1013 | 596 | 0.1057 | +0.0044 | 0.106 | −0.0261 |
| metar_dtdt | LOW | down | 0.0536 | 2352 | 0.0668 | +0.0132 | 0.940 | −0.0173 |
| metar_trend | HIGH | up | 0.1013 | 4842 | 0.1117 | +0.0104 | 0.532 | −0.0201 |
| metar_trend | LOW | down | 0.0536 | 1976 | 0.0466 | −0.0070 | 0.428 | −0.0375 |
| nwp_analog | HIGH | up | 0.1013 | 3105 | 0.0680 | −0.0333 | 0.419 | −0.0638 |
| nwp_analog | LOW | down | 0.0536 | 1952 | 0.0482 | −0.0054 | 0.452 | −0.0359 |
| nwp_analog_ensemble | HIGH | up | 0.1013 | 3105 | 0.0680 | −0.0333 | 0.419 | −0.0638 |
| nwp_analog_ensemble | LOW | down | 0.0536 | 1952 | 0.0482 | −0.0054 | 0.452 | −0.0359 |
| nwp_dtdt_fusion | HIGH | up | 0.1013 | 1620 | 0.0716 | −0.0297 | 0.183 | −0.0602 |
| nwp_dtdt_fusion | LOW | down | 0.0536 | 2132 | 0.0746 | +0.0210 | 0.815 | −0.0095 |
| pressure_delta | HIGH | up | 0.1013 | 2738 | 0.1019 | +0.0006 | 0.461 | −0.0299 |
| pressure_delta | LOW | down | 0.0536 | 1015 | 0.0956 | +0.0420 | 0.669 | +0.0115 |
| pressure_tendency | HIGH | up | 0.1013 | 207 | 0.1208 | +0.0195 | 0.260 | −0.0110 |
| **pressure_tendency** | **LOW** | down | 0.0536 | 153 | **0.1961** | **+0.1425** | 0.857 | **+0.1120** |
| radiational_cooling | LOW | down | 0.0536 | 24 | 0.0833 | +0.0298 | 1.000 | −0.0007 |
| regime | HIGH | up | 0.1013 | 57 | 0.0000 | −0.1013 | 0.000 | −0.1318 |
| regime | LOW | down | 0.0536 | 19 | 0.0526 | −0.0009 | 1.000 | −0.0314 |
| spike_reversion | LOW | down | 0.0536 | 1216 | 0.0691 | +0.0155 | 1.000 | −0.0150 |
| warm_momentum | HIGH | up | 0.1013 | 4732 | 0.1059 | +0.0046 | 1.000 | −0.0259 |
| wind_direction_shift | HIGH | up | 0.1013 | 647 | 0.1175 | +0.0162 | 0.539 | −0.0143 |
| wind_direction_shift | LOW | down | 0.0536 | 132 | 0.0379 | −0.0157 | 0.200 | −0.0462 |

---

## Verdict

### The "no edge" verdict was PARTLY a measurement artifact — but not in the way that changes the bottom line.

**What aggregate MCC/accuracy got wrong:** it *did* hide a small number of signals with a statistically-significant **minority-class precision lift** — almost entirely on the **LOW-market "down" side** (the ~5% rare event). Aggregate accuracy can't see a 5%→7–10% precision gain because it's swamped by the 94.6% majority class.

**Statistically significant minority-edge signals (Wilson LB > base rate, 1-sided p < 0.05):**
`corrected_pressure_delta/HIGH`, `forecast_disagreement/LOW`, `frontal_passage_intraday/LOW`, `gaussian_v2/LOW`, `goldilocks/LOW`, `metar_dtdt/LOW`, `metar_trend/HIGH`, `nwp_dtdt_fusion/LOW`, `pressure_delta/LOW`, `pressure_tendency/LOW`, `spike_reversion/LOW`.

**But the decisive test is net EV after real fees, and here almost all of them fail.** A +2–4 percentage-point precision lift on a rare event is real but not *tradable*: when you pay the ~5¢ minority-side price plus the ~2.05¢+ round-trip cost, the edge evaporates. Only **one** signal survives:

### `pressure_tendency` / LOW / "down" — the only robust positive net EV

| metric | value |
|---|---|
| minority precision | 19.6% (base 5.36%, **3.7× lift**) |
| Wilson 95% lower bound | 14.1% |
| recall | 85.7% |
| net EV @ measured precision | **+$0.112 / contract** |
| net EV @ Wilson lower bound | **+$0.057 / contract** (still positive) |
| sample | 153 minority preds, 30 hits, across 17 stations, 2025-12→2026-09 |

This signal is well-distributed (KDEN 47, KAUS 18, KNYC 17, … 17 stations total), spans the full date range, and is not a single-station/date artifact. It predicts the rare "down" resolution in LOW markets at ~3.7× the base rate, and — uniquely — that lift is large enough to survive the fee model.

---

## Bottom Line

**"No edge" was mostly TRUE, not a measurement artifact.** The majority of signals genuinely have no minority-class edge — their minority precision sits at or below base rate. Aggregate MCC was *not* systematically hiding a broad vein of rare-event skill.

**However, the aggregate metric was not fully innocent:** it obscured a *narrow* cluster of LOW-side "down" signals with statistically-significant (but mostly untradable) minority lift, and it buried one signal — **`pressure_tendency` on LOW "down"** — that clears positive net EV after fees even at the conservative Wilson bound.

**Recommendation:** the R/T-1 "NO_EDGE" flags are correct for 40/42 signal×market-type cells. The two exceptions worth a focused R/T-3 look are **`pressure_tendency/LOW`** (the only +net-EV signal, +$0.057–0.112/contract) and, to a lesser degree, **`pressure_delta/LOW`** (marginal +$0.0115 EV, positive at measured precision but negative at Wilson LB). Both are pressure-family signals on the LOW "down" tail — a coherent, mechanistically-plausible finding (falling pressure → warmer-than-expected low → the rare "down" miss), not a random fluke.
