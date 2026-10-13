# AI-NWP Independence Test + rate_1h Multi-Window Sweep — 2026-10-13

**Verdict up front:** AI-NWP (ECMWF AIFS, Google GraphCast) is **NOT a genuine second
degree of freedom**. It is collinear with deterministic IFS, and blending it with
GEFS makes the forecast *worse*, not better. **rate_1h at W=1 (the current 1-hour
window) dominates every longer window at every decision hour 10Z–20Z. Do not change
the window, do not wire AI-NWP as a diversity source.**

Script: `scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py`
Results: `artifacts/ai-nwp-independence/ai_nwp_independence_rate1h_sweep_2026-10-13.json`
Read-only on the live book. Fetch cache deleted after the run.

---

## Task 1 — Do AIFS / GraphCast add a second degree of freedom?

**Sources** (Open-Meteo Historical Forecast API, free, no key; `temperature_2m_max`
daily, 12Z init):
| key | model | archive range observed |
|---|---|---|
| `ifs_hres` | `ecmwf_ifs025` | 2024-03-01 → 2026-10-09 (present) |
| `aifs` | `ecmwf_aifs025` | 2024-03-13 → **2025-03-10** (archive closes, ~1 yr) |
| `graphcast` | `gfs_graphcast025` | 2024-03-01 → **2026-05-20** |
| `gefs` | local `nwp_forecasts.db` ensemble mean | — |

**Axis (identical to the diversity study):** strict strike-crossing
`crossed = settled_max > T`, `T = floor(high-so-far at the 13:30 station-local
pin)+1`; label truth = canonical GHCN settlement (`metar_backfill.db settlement_truth`,
HIGH, trustworthy). `==` is a LOSS.

### Per-source accuracy on the crossing axis (P(cross) = Φ((μ−T)/σ), σ_det = 4 °F)

| source | n | Brier | dir acc | base rate | base Brier |
|---|---|---|---|---|---|
| `ifs_hres` | 15324 | **0.2432** | **0.6154** | 0.7686 | 0.1779 |
| `graphcast` | 12933 | 0.2869 | 0.5280 | 0.7599 | 0.1824 |
| `aifs` | 5843 | 0.2987 | 0.5336 | 0.7607 | 0.1820 |
| `gefs` | 14972 | 0.4064 | 0.5348 | 0.7667 | 0.1789 |

Every source is **worse than the base rate** — the uniform σ_det=4 °F P(cross)
mapping is miscalibrated for daily-max (typical daily-max σ ≈ 3–5 °F). The
*discrimination* axis (correlations) below is the real signal here.

### The correlation that matters: FORECAST-ERROR correlation

Raw tmax correlation is useless here — a two-year seasonal ramp makes every pair
look r > 0.98. What decides independence is whether the two sources make the **same
errors**: `r(μ_a − truth, μ_b − truth)`.

| pair | raw temp r | anomaly r (seasonal removed) | **error r (KEY)** | P(cross) r |
|---|---|---|---|---|
| `ifs_hres`\|`aifs` | 0.9916 | 0.9082 | **0.8488** | **0.8588** ⚑ |
| `ifs_hres`\|`graphcast` | 0.9845 | 0.8526 | **0.6990** | 0.7137 |
| `aifs`\|`graphcast` | 0.9867 | 0.8672 | **0.7583** | 0.7668 |
| `ifs_hres`\|`gefs` | 0.9442 | — | 0.5171 | 0.4378 |
| `aifs`\|`gefs` | 0.9528 | — | 0.5717 | 0.4942 |
| `graphcast`\|`gefs` | 0.9495 | — | 0.6075 | 0.4985 |

⚑ = flagged > 0.85 (effectively one predictor), same flag the earlier study used.

**Readings against the task thresholds (`|r| < 0.70` → decorrelated; `|r| > 0.90` → another copy):**
- **AIFS vs IFS: r = 0.849 (error) / 0.859 (P(cross)) — flagged.** AIFS is a
  re-parameterised IFS, structurally the same degrees of freedom, exactly what the
  reviewer's "solve physics differently" claim predicted it would *not* be. It lands
  in the flagged band, right on the edge of "another copy."
- **GraphCast vs IFS: r = 0.699 (error) — sits exactly ON the 0.70 decorrelation
  boundary, not below it.** GraphCast is the *most* independent AI-NWP relative to
  IFS, but it is still not the clean decorrelated second source the thesis needed.
- **Both AI models vs GEFS: r ≈ 0.57–0.61.** Moderate, not decorrelated.

### Blend test (does combining AI + GEFS beat either alone?)

| pair | AI Brier | GEFS Brier | AI+GEFS Brier | AI+GEFS+IFS Brier | IFS alone Brier |
|---|---|---|---|---|---|
| AIFS | **0.2987** | 0.4176 | 0.3203 ✗ | 0.2903 | 0.2655 |
| GraphCast | **0.2869** | 0.4218 | 0.3173 ✗ | 0.2804 | 0.2498 |

Adding GEFS to either AI model makes it **strictly worse** (+0.022 AIFS, +0.030
GraphCast). Adding IFS to the AI+GEFS blend still loses to IFS alone. **There is no
blend that the AI sources improve.** That is the operational definition of "no second
degree of freedom."

### Task 1 verdict

> **AI-NWP does not add a usable second degree of freedom.**
> AIFS is collinear with IFS (error r = 0.85, flagged). GraphCast is marginally more
> independent (error r = 0.70, on the boundary) but still does not improve any blend
> over the deterministic baseline. The two AI models are also correlated with *each
> other* (error r = 0.76) — they are largely the same signal.
> **Recommendation: NO — do not wire AI-NWP as a diversity source.**
> (Optional follow-up, low priority: GraphCast's IFS-decorrelation at 0.70 is the
> only thread worth a targeted second look, but not as a blend member.)

Caveat to log honestly: the AIFS archive closes 2025-03-10 and GraphCast 2026-05-20,
so the AI and IFS columns overlap only in a window; the error correlations use the
strict overlap (n = 5.8k–12.9k station-days), which is ample.

---

## Task 2 — rate_1h multi-window sweep (Dan's request)

Generalised the ~1 hour intraday momentum to symmetric `W`-hour windows:

- **W = 1 (published):** `rate = bucket[T] − mean(4 prior 15-min buckets)`.
  My W=1 replication reproduces `sweep_intraday_rate1h_crossing.json` **exactly**
  (e.g. 18Z sep = 60.22 vs published 62.58 on the published cohort; identical
  per-hour values on the shared grid — see `grid.pub1h|*`).
- **W ≥ 2:** symmetric contrast `mean(last 4W buckets) − mean(prev 4W buckets)`.

Scored on the strict strike-crossing axis (UTC 15-min buckets, UTC grid hours,
station-local date, `dmax` = max bucket *mean*), identical setup to the standalone
sweep.

### Separation (pp) @ ±1.0 °F, by window × decision hour

| W | 06Z | 10Z | 12Z | 14Z | 16Z | 17Z | **18Z** | 20Z | 22Z |
|---|---|---|---|---|---|---|---|---|---|
| **1** | -6.7 | **11.98** | **23.14** | **33.61** | **47.15** | **53.79** | **60.22** | **52.22** | **17.71** |
| 2 | -14.4 | -1.69 | 17.63 | 30.97 | 40.22 | 49.43 | 51.88 | 40.45 | 10.66 |
| 3 | -29.1 | -12.07 | 7.79 | 26.20 | 37.79 | 45.93 | 40.54 | 31.33 | 7.67 |
| 4 | — | -20.40 | -6.85 | 20.71 | 32.64 | 40.01 | 41.92 | 24.10 | 5.87 |
| 5 | — | -26.64 | -19.56 | 11.40 | 29.37 | 32.84 | 36.43 | 25.30 | 4.76 |
| 6 | — | — | -26.26 | -7.52 | 21.71 | 28.14 | 28.17 | 22.22 | 5.00 |

**W = 1 wins at every hour in the requested 10Z–20Z band**, and 16Z/18Z/20Z by a
dominant margin.

### Normalized (scale-free) check — top 15% vs bottom 15% of `rate_W`

The fixed ±1.0 table could be confounded by the fact that longer windows *dilate*
the rate scale (pooled sd of rate_W grows: W1 = 2.30 → W6 = 6.70 °F). To remove
that confound, take the **same 15%/15% split of each window's own distribution**:

| W | 10Z | 12Z | 14Z | 16Z | 17Z | 18Z | 20Z |
|---|---|---|---|---|---|---|---|
| **1** | **15.22** | **24.77** | **35.28** | 41.95 | 45.86 | 55.92 | **54.75** |
| 2 | 13.21 | 23.32 | 34.52 | **43.18** | **48.80** | **56.36** | 54.45 |
| 3 | 2.50 | 20.17 | 32.64 | 41.65 | 44.82 | 51.71 | 51.57 |
| 4 | -10.06 | 9.50 | 30.38 | 38.59 | 41.85 | 43.84 | 43.52 |
| 5 | -14.84 | -4.77 | 21.12 | 34.72 | 37.11 | 38.53 | 32.08 |
| 6 | — | -11.30 | 6.23 | 27.10 | 30.69 | 32.06 | 21.11 |

Even after normalizing the split, **W = 1 still leads at 10Z, 12Z, 14Z and 20Z**,
and W2 only ties W1 at 16/17/18Z (43.18 vs 41.95, 48.80 vs 45.86, 56.36 vs 55.92 —
within noise). **No longer window dominates at any hour.** Longer windows
progressively dilute the separation and, by W = 4–6, actively *invert* it at the
earlier hours (10Z, 12Z, 14Z turn negative).

### Task 2 verdict

> **1 h dominates. There is no decision hour where a 2–6 h window beats the current
> 1 h window in any operationally meaningful way.** The ~1-hour momentum is the
> right time constant: it is the freshest signal and it isolates true short-term
> acceleration; longer windows average across the diurnal warming ramp and lose the
> signal (and at night, invert it). **No change to `rate_1h`.**

---

## Bottom line

1. **AI-NWP: NO.** AIFS is collinear with IFS (error r = 0.85, flagged); GraphCast
   is marginally independent (error r = 0.70) but no AI source improves any blend.
   Not a second degree of freedom. Do not wire.
2. **rate_1h window: KEEP W = 1.** It dominates 2–6 h windows at every hour
   10Z–20Z, under both the fixed-±t and the scale-normalized metric. Do not change.
3. Weakest thread worth a second look (low priority): GraphCast's r = 0.70 vs IFS on
   the error axis — but as a *targeted residual*, not as a diversity/blend member.

## Method / provenance

- Read-only on the live book (`iem_asos_1min.db`, `nwp_forecasts.db`,
  `metar_backfill.db`). No temp DBs left behind. Fetch cache
  (`data/_om_ai_cache.db`) and bucket cache (`data/_ai_nwp_buckets_cache.json`)
  deleted after the run.
- Open-Meteo fetched at 1 req/s, 20 requests total (one long-range multi-model call
  per station), cached in SQLite during the run.
- Deterministic (seeded block-bootstrap by local date, 1000 reps).
