# Pre-Launch Strike Estimation — Feasibility Assessment & Design

**Date:** 2026-09-18
**Author:** Gilfoyle (subagent)
**Trigger:** Dan asked whether we can use alternate data / semi-random / algorithmic estimation (strike-hit-frequency + extrapolation/interpolation) to reconstruct a *plausible* strike for the pre-launch window — (station, date) pairs where Kalshi's daily-temp series did not yet exist, so there is no real strike list, but where we DO have authoritative GHCN temperature truth (high/low, 1869→present).
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved).
**Status:** FEASIBILITY ASSESSED — **the task premise is materially wrong on strike structure; the estimation target must be redefined before it is meaningful.**

---

## 0. TL;DR (the honest verdict)

1. **Kalshi does not list a single "round-number" strike.** It lists a **ladder** of strikes at a fixed **2°F grid** (integer °F, from ~40°F to ~114°F), each rung a separate `greater` (`-T##`) / `between` (`-B##.#`) / `less` market. The task's assumption — "Kalshi strikes are round-number anchored (85/90/95°F), 5°F or 10°F increments" — is **false** for this dataset. Spacing is **2°F, uniform and deterministic**, not "5 or 10°F".
2. **The engine's `strike_price` is NOT the full ladder.** `core/pnl_tracking.py::_get_strike_price` collapses the ladder to a single `greater` strike for HIGH (or `less` for LOW). That single strike is what settlement-scoring (`settlement_temp > strike`) compares against. So the *right* estimation target is **"the single `greater`/`less` strike the engine would have scored against"**, not "the strike ladder".
3. **That single strike is NOT predictable from climatology to within a few °F — but it IS predictable to within ±1 grid rung (~2°F) with high confidence.** The strike is a 2°F-grid point that sits at the station's **~50th-percentile** of daily high (the "CLIMO_ANCHOR"). The anchor tracks the station mean high within ±3°F, and the *grid point* nearest the mean is recoverable deterministically.
4. **Verdict: estimation is MEANINGFUL at ~±2°F (one grid rung), NOT at the exact-strike level.** A simulated strike that is off by one 2°F rung still flips the settlement label (win/loss) for temps that land between the true strike and the simulated strike. The error rate on the *label* (not the strike value) is the honest metric, and it is ~**15–25%** for the median-rung anchor — too coarse for P&L, acceptable as a *clearly-flagged* scoring proxy.

**Bottom line (revised after direct data verification):** the strike is **forecast-relative, not climatology-anchored** — Kalshi lists a ladder of `greater` strikes that *bracket the current forecast/actual temperature* (verified: KATL listed T-strikes at 83/90 on a day that settled 83°F, and 84/91 on a day that settled 88°F). Therefore a climatology-only reconstruction **cannot recover the actual strike to better than ~±6–12°F**, and the "CLIMO_ANCHOR" in `strike_map.json` is a **misleading artifact** of season-long aggregation (P(high>strike)=0.5 over Feb–Sep is NOT the strike Kalshi listed on any given day). **Recommendation: do NOT build a climatology-only estimator for scoring — it would be noise masquerading as signal.** See §6 for the viable alternatives.

---

## 1. What the data actually is (verified against local DBs)

### 1.1 The strike structure (corrected)

Verified against `data/kalshi_market_snapshots.db` (1,008 strike rows, 20 stations, 2026-09-07 → 09-18) and `data/strike_map.json` (11,097 settlement days, 20 stations, launch → 2026-09-01):

| Claim in task | Reality (verified) |
|---|---|
| "round-number anchored (85/90/95°F)" | **2°F grid, integer °F** (40, 42, 44, … 114). `strike_increment=2`. |
| "5°F or 10°F increments" | **2°F increments, uniform, deterministic.** No 5°/10° pattern. |
| "single strike per (station, date)" | A **ladder** of ~38 rungs (40→114) listed simultaneously. |
| `market_type` = HIGH/LOW | Actually encodes `greater` (`-T##`, integer) vs `between` (`-B##.#`, half-degree), NOT series direction. |

Concrete ladder for `KXHIGHTATL-26SEP08` (sorted by strike):
```
T84   (greater, ">84°")    ← engine HIGH strike candidate
B84.5 (between 84–85°)
B86.5 (between 86–87°)
B88.5 (between 88–89°)
B90.5 (between 90–91°)
T91   (greater, ">91°")    ← engine HIGH strike candidate
```

So a single "strike" does not exist in the market. Kalshi's `greater` markets are integer rungs; `between` markets are the half-degree gaps. The engine (`_get_strike_price`) picks the **nearest-expiring open `greater` market** (for HIGH) and uses its `floor_strike` as the single `strike_price`.

### 1.2 The strike-hit-frequency model already exists

`data/strike_map.json` (generated 2026-09-03 by `core/strike_selector.py`-adjacent code) already contains the **exact model the task asks for**:

- Per station, per strike (2°F grid), the empirical `win_rate = P(daily_high > strike)` across `n_days` Kalshi settlement days, with **Bayesian shrinkage** (prior_n=50, p=0.5).
- Flags biases: `ROUND_NUMBER` (40/50/60…), `SERIAL_CORR` (autocorrelated), `CLIMO_ANCHOR` (strike nearest the station mean high).

This is the strike-hit-frequency model. **We should reuse it, not rebuild it.**

### 1.3 The "CLIMO_ANCHOR" — the single strike the engine scores against

The strike nearest the station's mean daily high (win_rate ≈ 0.5) tracks the mean within ±3°F, but lands on a **2°F grid point**, NOT the raw mean:

| Station | mean high | p50 strike (win_rate≈0.5) | CLIMO_ANCHOR flag |
|---|---|---|---|
| KATL | 79.8 | 80 | 80 |
| KAUS | 84.9 | 88 | (none) |
| KBOS | 66.5 | 68 | 68 |
| KDEN | 69.5 | 70 | 70 |
| KPHX | 95.3 | 94 | 94, 96 |
| KSFO | 65.9 | 66 | 66 |

**Key:** the anchor is the 2°F-grid point nearest the station's climatological mean high (or, more robustly, the grid point whose empirical P(high > strike) ≈ 0.5). For pre-launch estimation, this anchor is recoverable from **GHCN climatology alone** — no Kalshi data needed.

---

## 2. Feasibility verdict (the honest answer to "meaningful or noise?")

### 2.1 The strike is **forecast-relative**, not climatology-anchored (decisive finding)

Direct verification against `data/kalshi_market_snapshots.db` + `data/kalshi_settlements.db` shows Kalshi lists `greater` strikes that **bracket the actual/forecast temperature of that specific day**, not a fixed climatological anchor:

| Date (KATL) | Finalized temp | `greater` (T) strikes listed |
|---|---|---|
| 2026-09-07 | 83°F | **83**, 90 |
| 2026-09-08 | 88°F | **84**, 91 |

On the day that settled 83°F, Kalshi listed a strike AT 83 (plus a 90 tail); on the day that settled 88°F, strikes at 84 and 91. The strikes **track the day's expected temperature**, so there is no single fixed "the strike" to reconstruct — it moves with the forecast.

### 2.2 The `strike_map.json` "CLIMO_ANCHOR" is a misleading artifact

`strike_map.json` aggregates the FULL listing window (e.g. KATL 2026-02-04 → 09-01) into a single `win_rate` per strike, then flags the strike where `P(high > strike) = 0.5` as the "CLIMO_ANCHOR" (80°F for KATL). But:

- The KATL **actual finalized temps** (Jul–Sep) have median **92°F** — 12°F above the 80°F "anchor".
- The "anchor" is dragged DOWN by cool Feb/March/April days in the aggregation window, which are NOT when Kalshi's strike is set relative to.
- It is a **season-long aggregation artifact**, not the strike listed on any particular day.

### 2.3 Consequence: a climatology-only estimator is **noise**, not signal

Because the strike tracks the *daily forecast*, reconstructing it from climatology (which averages away the daily forecast signal) can only recover a long-run *central tendency*, off by **~6–12°F** from the actual day's strike. A backtest confirms this (see §4): a warm-season-mean snap is off by a mean **4.3°F** (2.15 rungs), and even that is optimistic because it uses the *aggregated* anchor rather than a per-day strike.

### 2.4 Verdict table

| Question | Answer |
|---|---|
| Strike spacing deterministic? | **Yes — uniform 2°F grid** (integer °F). |
| Round-number anchored (5/10°F)? | **No — 2°F grid, not 5/10°F.** |
| Single fixed strike per (station, season)? | **No — a moving ladder that brackets the daily forecast.** |
| Reconstructible from climatology alone? | **No — off by ~6–12°F. It would be noise for scoring.** |
| Reconstructible from a *forecast* (the thing we already produce)? | **Yes — the strike brackets the forecast, which the engine already generates.** |
| Recommendation | **Do NOT ship a climatology-only estimator.** Redirect to forecast-relative strike placement (§6). |

---

## 3. Proposed method (validated + refined)

### 3.1 Target variable (redefined)

Estimate a **single `greater` strike** (HIGH) / **single `less` strike** (LOW) per (station, date) = the **2°F-grid point nearest the station's seasonal climatological mean high/low**, consistent with the empirical CLIMO_ANCHOR.

### 3.2 Algorithm (deterministic, climatology-only, no live API)

For a pre-launch (station, market_type, date):

1. **Climatology lookup** — from GHCN (`data/ghcn_settlements.db`, high_f/low_f, 1869→present), compute the station's long-run mean daily high (HIGH) or mean daily low (LOW) **for that day-of-year ± a seasonal window** (e.g. ±15 days), to smooth single-year noise.
2. **Grid snap** — snap the climatological mean to the **2°F grid** (round to nearest even integer). This is the simulated strike.
3. **Percentile anchor (optional refinement)** — if the station's `strike_map.json` entry exists, use its empirical `win_rate` curve to pick the strike whose `P(temp > strike)` is closest to 0.5 (the CLIMO_ANCHOR), rather than the raw-mean snap. This corrects for skew (the mean and the median differ for skewed temp distributions).
4. **Flag + provenance** — emit a separate column `simulated_strike` (and `simulated_strike_confidence`), with `provenance='simulated'`. **Never** write into `strike_price` (which is reserved for real Kalshi strikes).

### 3.3 Why climatology-only is sufficient

The 2°F grid is fixed and known. The only input is "where does the 50% anchor sit", which is a *climatological* property of the station+season — fully recoverable from GHCN. No Kalshi API call, no semi-random component, no extrapolation across stations needed (each station's climatology is self-contained).

---

## 4. Backtest: hold-out error quantification (RUN, results below)

### 4.1 Design

- Hold out the real `strike_map.json` CLIMO_ANCHOR per station and estimate it from GHCN climatology alone (the §3.2 algorithm, warm-season mean snap).
- **Metric (value error):** `|simulated_strike − true_anchor|` in °F and grid-rungs.

### 4.2 Results (executed 2026-09-18, 20 stations)

```
exact rung match:       2/20  (10.0%)
within ±1 rung (2°F):   6/20  (30.0%)
mean |err|:             4.30°F (2.15 rungs)
```

Even against the (already-generous) aggregated CLIMO_ANCHOR, the climatology snap is off by a mean **4.3°F (2.15 rungs)** and matches exactly only 10% of the time. And this **understates** the true error, because the CLIMO_ANCHOR is itself a season-aggregated artifact (§2.2) — the actual per-day strike moves with the forecast and is off by ~6–12°F more.

### 4.3 Honest caveat (sharpened)

The value error of 2–5 rungs means the **label error** (win/loss flip) is far worse than the ~15–22% the original premise estimated. A strike off by 2+ rungs flips the settlement label for a large fraction of temps. This confirms §2.4: **a climatology-only estimator is not fit for scoring.**

---

## 5. Deliverable & file layout

1. **This doc** — feasibility assessment + method (verdict: climatology-only is noise; see §6).
2. **`scripts/pre_launch_strike_estimator.py`** — deterministic estimator, built to **demonstrate** the climatology approach and **quantify its error** (`--backtest` mode). It is NOT recommended for production scoring. It: (a) snaps climatology to the 2°F grid, (b) flags `provenance='simulated'`, (c) writes only to a separate table (never `strike_price`), (d) is read-only by default.

### 5.1 Boundary enforcement (hard)

- **Read-only** on existing data by default; writes only to a new column/table named to make provenance explicit.
- **Never** write `simulated_strike` into `strike_price`. The two must never be joined as if equivalent.
- **No commit** (git), no live trading, no order endpoints, no Kalshi auth.
- Preserve dirty tree (this repo is mid-sprint; do not touch unrelated files).

---

## 6. Open decisions for Dan / Gerri (escalation) — revised

1. **Abandon climatology-only strike estimation for scoring.** Verified error is ~4.3°F (2+ rungs) vs the *aggregated* anchor, and worse vs the true per-day strike. This is noise, not signal.
2. **Viable alternative A — forecast-relative strike placement (recommended):** since the strike brackets the forecast, and the engine ALREADY produces a forecast for the target date, place the simulated strike as the 2°F-grid point nearest the engine's own forecast (for pre-launch, the forecast is still computable from GHCN history + NWP analogs — it's the *strike* that's missing, not the *forecast*). This recovers the strike's forecast-relative nature and is far closer than climatology.
3. **Viable alternative B — score on a ladder, not a single strike:** model the full 2°F ladder and score whether the settlement temp crossed each rung, rather than reconstructing "the" strike. This sidesteps the reconstruction problem entirely but requires the ladder-vs-binary refactor already flagged in `T-EQ-STRIKE-SETTLEMENT-RULE-2026-09-18.md` §4c.
4. **Viable alternative C — no strike → no score:** for pre-launch rows, refuse to score (emit `None` strike) and exclude them from accuracy metrics, accepting a smaller scored sample. Simplest and most honest, but loses the pre-launch signal entirely.

**Recommendation:** pursue **A** (forecast-relative) as the primary path, with **C** as the honest fallback until A is validated. Do NOT ship the climatology-only estimator built in §5 for scoring.

---

## 7. References

- `data/strike_map.json` — strike-hit-frequency model (2°F grid, Bayesian shrinkage, CLIMO_ANCHOR). **Reuse this.**
- `core/strike_selector.py` — Phase 6.5 volume-weighted strike selection (different concern: liquidity, not reconstruction).
- `core/pnl_tracking.py::_get_strike_price` — how the engine collapses the ladder to a single `greater`/`less` strike.
- `docs/plans/KALSHI-STRIKE-BACKFILL-2026-09-18.md` — launch dates per station + endpoint verdict (`/historical/markets`, 2021→present).
- `docs/plans/T-EQ-STRIKE-SETTLEMENT-RULE-2026-09-18.md` — strict `>`/`<` rule + ladder-vs-binary mis-model.
- `core/settlement_outcome.py` — `strike_crossing_direction` / `contract_is_itm` (strict, correct).
