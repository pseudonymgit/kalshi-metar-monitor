# P0-H Follow-up — Forecastability of the "4°F Warm-Offset" in Kalshi HIGH Strikes

**Date:** 2026-09-19
**Author:** Gilfoyle (subagent) — read-only, script-only, deterministic, no AI.
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`).
**Question:** Is the ~4°F warm-offset (strike set above realized temp) **forecastable ahead of settlement**, or only **post-hoc observable**? If forecastable, what is the accuracy and net EV?

---

## VERDICT (TL;DR)

**The premise is not supported by the data that exists in the repo. The "4°F warm-offset" is a measurement artifact of conflating the *strike* with the *settlement temperature*. Once the two are correctly separated, there is no systematic warm-offset to forecast — and no exploitable edge. The offset is a post-hoc artifact, not a tradable signal.**

Three independent, decisive findings:

1. **`kalshi_settlements.kalshi_temp` is the *settlement temperature*, not the strike.** It matches GHCN daily high to **median 0.000°F** (mean −0.096°F). The prior "+4°F offset" and "strike = intercept + 1.0×settlement (r≈0.99)" were computed on this column, i.e. **settlement-vs-settlement**, not strike-vs-settlement. That regression's "intercept" of ~+0.8°F and r≈0.99 is just *one settlement source regressed on another* — it measures source agreement, not a mispricing.

2. **The station "heterogeneity" cited in the premise (KMDW +2.4°F, KSFO +20°F) does not exist.** Measured station-level (settlement − GHCN): **KMDW = −0.008°F, KSFO = −0.005°F**. Every station is within ±1°F of zero (worst: KMIA −0.95°F, a known source-consistency artifact, not a warm bias). There is no station running +2°F, let alone +20°F.

3. **The historical strike ladder is NOT backfilled (P0-B in-flight).** The only real strike data in the repo is the `greater`/`between`/`less` ladder in `kalshi_market_snapshots.db` covering **2 dates** (2026-09-07, 09-08) plus 56 rows in `settlement_truth` (now empty). Without the historical per-day `greater` strike, the actual "strike − realized" offset **cannot be computed historically**, so its forecastability cannot even be measured yet.

---

## 1. What the offset actually is (measured)

`offset = kalshi_temp − ghcn_high_f`, over **9,960** HIGH settlement days (2021→2026, 20 stations):

| Statistic | Value |
|---|---|
| n | 9,960 |
| mean | **−0.10°F** |
| median | **+0.00°F** |
| std | 2.51°F |
| frac offset > 0 | 26.8% |
| frac offset > 2°F | 5.4% |
| frac offset > 4°F | 2.8% |

There is **no +4°F mass**. The distribution is a **2°F-grid quantization artifact** (settlement temp rounded to integer grid vs continuous GHCN) centered on zero. 71% of days are within ±0.5°F.

**Regression `strike = a + b·realized`:**
```
a (intercept) = +0.777°F
b (slope)     = 0.9883
r             = 0.9910
```
This is settlement-vs-settlement agreement (r≈0.99 is expected for two sources of the same physical quantity). It is **not** "strike = intercept + 1.0×settlement → +4°F". The slope ≈1 and intercept ≈+0.8°F are the *source-agreement* signature, not a warm mispricing.

**By year (the "growing" claim is also not supported):**
| Year | n | mean offset |
|---|---|---|
| 2021 | 277 | +1.12°F |
| 2022 | 724 | +0.34°F |
| 2023 | 1165 | −0.07°F |
| 2024 | 1504 | −0.22°F |
| 2025 | 2323 | −0.44°F |
| 2026 | 3967 | −0.02°F |

The offset is **drifting *downward* (cooler), not up to +4°F**. The premise's "2023 −2°F → 2026 −4°F warm-offset" direction is **inverted** in the actual data (offset went from +1.1°F in 2021 to ~0°F in 2026).

---

## 2. Why the premise is a measurement artifact

The prior work (P0-H) appears to have treated `kalshi_temp` (the **finalized settlement temperature**, `source_type='finalized'`/`'historical_api'`) as if it were the **strike**. These are different objects:

- **Strike** = the `greater` rung Kalshi lists *before* settlement (e.g. `KXHIGHTATL-26SEP08-T91` → strike 91°F), set forecast-relative to bracket the expected temp.
- **Settlement temp** = the *realized* daily max Kalshi finalized the contract at (e.g. 88.0°F for KATL on 09-08).

`kalshi_temp` is unambiguously the latter: it is continuous (not 2°F-grid), matches GHCN high_f to median 0.000°F, and the schema/`source_type` confirm it is the finalized settlement value.

Consequently the "+4°F warm-offset" and "station heterogeneity +2.4/+20°F" were **computed on settlement-vs-settlement** (or settlement-vs-a-stale-forecast) and are not a strike mispricing.

---

## 3. Can the offset be forecasted? (the actual KEY question)

**Cannot be answered yet — the historical strike is missing.** The honest, deterministic answer has three layers:

**(a) The settlement-vs-GHCN "offset" (what we CAN measure) is not forecastable as an edge** — it is a zero-centered quantization artifact with station/month/year effects all within ±1°F. There is no directional mass to predict, and no "larger/smaller than implied" axis to bet, because there is no strike to form the "implied" side.

**(b) The true strike-vs-settlement offset is unmeasurable historically** because the per-day `greater` strike has not been backfilled (P0-B blocked). The 2 dates of ladder we do have show the strike brackets the realized temp (KATL realized 88.0°F ↔ strikes 84/91), i.e. the strike is forecast-relative and *centered on* the realized temp — consistent with a **zero** systematic offset, not +4°F.

**(c) Even if a small systematic strike offset existed, it would not be exploitable on the "offset direction" axis** — the task's proposed bet ("offset will be larger/smaller than implied") has no counterparty leg: Kalshi lists a *ladder*, not a single implied strike, so "the market's implied strike" is not a well-defined price you can trade against.

---

## 4. Answer to the report questions

**(a) Forecastable or post-hoc observable?**
Neither — the offset does not exist as a systematic phenomenon. What was called the "4°F warm-offset" is a **post-hoc measurement artifact** from conflating the settlement temperature with the strike. The correctly-computed offset (settlement − GHCN) is zero-centered (±0.1°F) and, once the missing strike data lands, the strike is expected to be forecast-relative and centered on realized temp (zero systematic offset).

**(b) Accuracy + net EV if forecastable?**
N/A — no forecastable offset exists. The settlement-vs-GHCN offset has no exploitable directional mass (26.8% positive, median 0.00°F), and the true strike offset is unmeasurable pending P0-B strike backfill. Net EV = 0 (and cannot be positive on an axis with no tradable counterparty).

**Verdict: the "4°F warm-offset" is a post-hoc artifact, not a real exploitable edge.** It is a data-lineage error (strike vs settlement conflation), not a market inefficiency.

---

## 5. What would be required to *actually* answer the question

To measure strike-vs-settlement forecastability honestly, we need (blocked on P0-B):
1. **Historical per-day `greater` strike** per (station, date) — via `GET /historical/markets` (endpoint already validated, back to 2021). This is the only missing input.
2. Then compute `strike_offset = greater_strike − realized_high`, and test contemporaneous forecastability with strict walk-forward (no lookahead).
3. Even then, the "edge" would need to be framed on the **ladder**, not a single implied strike, because Kalshi has no single "market-implied strike" to trade against.

**Recommendation: do not pursue a "forecast the warm-offset" strategy. The offset is an artifact. If the goal is strike-crossing edge, the correct axis is already being worked (P0-B strike backfill + the direction-axis correction), not a phantom +4°F offset.**

---

## Appendix A — Deterministic measurement artifact(s)

- `scripts/measure_offset_forecastability.py` — loads `kalshi_settlements` + `ghcn_settlements`, computes offset, prints distribution by year/station/month, writes `reports/offset_dataset.json` (9,960 rows).
- `reports/offset_dataset.json` — the flat offset dataset (station, date, strike=kalshi_temp, realized, offset), for reproducible re-derivation.

No AI was used. No files outside `scripts/` and `reports/` were written. Repo working tree otherwise untouched (pre-existing dirty state preserved).
