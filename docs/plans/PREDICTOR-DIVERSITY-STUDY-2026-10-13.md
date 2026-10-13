# Predictor Diversity Study — 2026-10-13

**Question:** Are our "independent" forecast sources actually diverse, and does
combining them beat any single one? (Pre-requisite for any fusion rewrite.)

**Script:** `scripts/predictor_diversity_study_2026-10-13.py`
**Results:** `artifacts/predictor-diversity/predictor_diversity_2026-10-13.json`
**Window:** 2026-04-21 → 2026-10-09 · 3,440 station-days · read-only on `trades`.

---

## Axis definition (uniform across all sources)

- **Decision instant:** 13:30 **station-local** (the H3/B2 canonical pin).
- **Strike:** `T = floor(high-so-far at the pin) + 1` — the next integer strictly
  above the running high. `high-so-far` is the max 15-min bucket **mean** with
  local minute-of-day ≤ 13:30 (bucket means, not the raw 1-min max, so single-minute
  ASOS spikes — e.g. KSEA +12 °F on 2026-08-01 — cannot inflate it).
- **Label:** `crossed = settled_high > T` — **strict; `==` is a LOSS**.
- **Truth:** canonical settlement (`metar_backfill.settlement_truth`, HIGH,
  trustworthy=1: Kalshi/GHCN). **Not** the raw ASOS daily max, which disagrees with
  canonical truth by up to +12 °F on ~8 % of station-days.
- **Units verified:** `nwp_forecasts.value` is **°C → °F** (`*9/5+32`);
  `iem_asos_1min.tmpf` is **already °F** (no C→F).

---

## Task 1 — Per-source accuracy

| source | n | Brier | dir. acc | base rate | mean p | date range |
|---|---:|---:|---:|---:|---:|---|
| **rate_1h** | 2464 | **0.0921** | **0.8916** | 0.805 | 0.720 | 2026-04-21..2026-10-04 |
| gfs | 2960 | 0.1597 | 0.7882 | 0.796 | 0.677 | 2026-05-01..2026-10-09 |
| gem | 2520 | 0.1793 | 0.7369 | 0.799 | 0.674 | 2026-05-23..2026-10-09 |
| icon | 2960 | 0.1840 | 0.7331 | 0.796 | 0.649 | 2026-05-01..2026-10-09 |
| ecmwf | 2960 | 0.2080 | 0.6916 | 0.796 | 0.618 | 2026-05-01..2026-10-09 |
| gefs | 2740 | 0.3290 | 0.6131 | 0.784 | 0.613 | 2026-04-21..2026-10-09 |
| market | 391 | 0.6247 | 0.2788 | 0.775 | 0.164 | 2026-08-01..2026-10-09 |

Notes:
- **rate_1h wins decisively** (Brier 0.092, the only source to beat the 0.805 null
  by a large margin). This is expected: it is the only source that *observes* the
  high-so-far at the pin and extrapolates the intraday rate — the NWP models predict
  the whole day from a 24 h lead and are blind to the observed high-so-far the strike
  is anchored to.
- **GEFS is the worst NWP** (0.329). Its ensemble spread is broken for this
  purpose: mean ensemble σ = **1.27 °F** while its RMSE vs truth is **7.37 °F** —
  severely underdispersed, so the Gaussian maps the tight ensemble to a near-0/1
  step and misses. The deterministic models (rmse 3.1–4.1 °F) do better.
- **Market is unreliable in this archive** (n=391). The `market_snapshots` table is
  sparse for Aug–Sep 2026 (often a single 04:00Z snapshot per ticker, no intraday
  ladder at the 13:30 pin), so most market values default to deep-OTM near-zero
  mids. Treat the market row as **indicative only, not a benchmark** — see Task 4
  where snapshot coverage is adequate.

---

## Task 2 — Pairwise correlation (THE key question)

Per-station-day **P(cross)**, Pearson / Spearman, union of overlapping days.
Flag threshold |r| > 0.85.

| pair | n | Pearson | Spearman | flag |
|---|---:|---:|---:|:--:|
| ecmwf–icon | 3240 | 0.717 | 0.725 | |
| gfs–icon | 3240 | 0.698 | 0.713 | |
| ecmwf–gem | 2800 | 0.691 | 0.696 | |
| gem–icon | 2800 | 0.643 | 0.662 | |
| ecmwf–gfs | 3240 | 0.622 | 0.630 | |
| gem–gfs | 2800 | 0.595 | 0.615 | |
| gfs–rate_1h | 2352 | 0.458 | 0.487 | |
| gefs–icon | 2820 | 0.453 | 0.536 | |
| gefs–gem | 2380 | 0.392 | 0.484 | |
| ecmwf–gefs | 2820 | 0.379 | 0.458 | |
| gefs–gfs | 2820 | 0.364 | 0.455 | |
| icon–rate_1h | 2352 | 0.345 | 0.382 | |
| ecmwf–rate_1h | 2352 | 0.340 | 0.371 | |
| gem–rate_1h | 1973 | 0.295 | 0.322 | |
| icon–market | 599 | 0.209 | 0.183 | |
| ecmwf–market | 599 | 0.205 | 0.200 | |
| gem–market | 599 | 0.162 | 0.151 | |
| **gefs–market** | 528 | 0.119 | 0.174 | |
| **gfs–market** | 599 | 0.108 | 0.114 | |
| **gefs–rate_1h** | 2197 | **0.097** | 0.115 | |
| market–rate_1h | 184 | −0.282 | −0.208 | |

**No pair exceeds 0.85 on the traded P(cross) axis.** The strongest coupling is
among the deterministic global models (ecmwf/icon/gfs/gem, 0.60–0.72) — as expected,
they are the same dynamical products and share the synoptic signal. GEFS is more
decorrelated (0.36–0.45 vs the deterministics) precisely *because* of its broken
spread. rate_1h and market sit apart from the NWP pack.

### Task 2b — Correlation of the underlying **temperature** (°F, raw μ)

This is where the collinearity is stark — and it matters:

| pair | Pearson | flag |
|---|---:|:--:|
| gfs–icon | 0.976 | **EFFECTIVELY ONE** |
| gem–icon | 0.967 | **EFFECTIVELY ONE** |
| ecmwf–icon | 0.966 | **EFFECTIVELY ONE** |
| gem–gfs | 0.965 | **EFFECTIVELY ONE** |
| gfs–rate_1h | 0.965 | **EFFECTIVELY ONE** |
| ecmwf–gem | 0.960 | **EFFECTIVELY ONE** |
| ecmwf–gfs | 0.953 | **EFFECTIVELY ONE** |
| gem–rate_1h | 0.951 | **EFFECTIVELY ONE** |
| icon–rate_1h | 0.945 | **EFFECTIVELY ONE** |
| ecmwf–rate_1h | 0.943 | **EFFECTIVELY ONE** |
| gefs–icon | 0.910 | **EFFECTIVELY ONE** |
| gefs–gem | 0.905 | **EFFECTIVELY ONE** |
| gefs–gfs | 0.898 | **EFFECTIVELY ONE** |
| ecmwf–gefs | 0.891 | **EFFECTIVELY ONE** |
| gefs–rate_1h | 0.872 | **EFFECTIVELY ONE** |
| *(all × market)* | 0.03–0.13 | — |

**Every NWP–NWP and NWP–rate_1h temperature correlation is 0.87–0.98.** On the
raw temperature axis these are the *same predictor in slightly different clothes*.
The P(cross) decorrelation seen in Task 2 comes from the nonlinear
temperature→probability mapping and the differing spreads, **not** from independent
information. Only **market** carries a genuinely orthogonal signal (|r| < 0.13 vs
everything).

---

## Task 3 — Does the combination beat the best single?

| comparison | n | blend Brier | best single | best-single Brier | Δ (blend − best) |
|---|---:|---:|---|---:|---:|
| all sources (greedy indep. set) | 123 | 0.2068 | rate_1h | 0.1241 | **+0.083 (worse)** |
| gfs + rate_1h | 2311 | 0.1168 | rate_1h | 0.0919 | **+0.025 (worse)** |
| gfs + rate_1h + market | 177 | 0.2296 | rate_1h | 0.1093 | +0.120 (worse) |
| gefs + rate_1h + market | 123 | 0.2463 | rate_1h | 0.1241 | +0.122 (worse) |
| rate_1h + market | 177 | 0.2793 | rate_1h | 0.1093 | +0.170 (worse) |

**No blend beats the best single source.** Every equal-weight combination is
*worse* than rate_1h alone. This holds at n=2311 (a robust sample, not a small-n
artifact): the gfs+rate_1h blend costs +0.025 Brier versus rate_1h by itself.

Why: equal weighting treats GEFS and the sparse market mids as peers of rate_1h.
When one member is the best predictor and the others are weak *and* correlated with
it (but noisier), averaging drags the good member toward the bad ones. With no
member decorrelated *and* comparably accurate, equal-weight fusion cannot help.
We did not test per-source weighting (out of scope for the equal-weight question),
but the correlation + accuracy structure says the only weighting that could help is
one that is essentially "rate_1h, plus a small market tilt."

---

## Task 4 — `trade_calibration.p_market` populated

- Resolution replicated read-only from the SBOX bridge
  (`scripts.sbox_intraday_ev_bridge.market_mid_prob`): YES mid at the snapshot
  nearest the 13:30 station-local pin, from `kalshi_market_snapshots`.
- Result: **143 / 143 rows filled** (was 0/143). Range 0.005–0.995, mean 0.096.
- **Integrity:** `trades` untouched (82 rows before and after); every
  `trade_calibration` column except `p_market` is byte-identical to the pre-write
  backup (`data/_backup_pre_pmarket_20261013T064037Z.db`).
- Note: 83/143 trades carry p_market = 0.005 (deep-OTM at the pin). This is a real
  property of the booked strikes — they are all BUY side on strikes well above the
  market's implied median — not a resolution defect.

---

## Verdict

**We do not have real predictor diversity — the NWP family is collinear.**

1. **On the temperature axis, ecmwf/gfs/icon/gem/gefs are one predictor**
   (r = 0.87–0.98). Any fusion that treats them as independent votes is double-counting.
2. **On the traded P(cross) axis the coupling drops below the 0.85 flag** (0.60–0.72
   among NWP), but that is a nonlinearity/units effect, not new information — the
   members remain strongly dependent.
3. **Only two sources are genuinely distinct from the NWP pack:**
   - **rate_1h** — the only source that observes the high-so-far at the pin. It is
     also the **best single predictor** (Brier 0.092) and the one that beats the null.
   - **market** — near-zero correlation with everything (|r| < 0.13 in temperature,
     |r| < 0.21 in P(cross)) — truly orthogonal, but its snapshot coverage here is
     too thin to measure its standalone skill fairly.
4. **Diversity does not add measurable skill.** No equal-weight blend beats rate_1h
   alone; at n=2311 the blend is +0.025 Brier worse. Fusion cannot help while one
   member dominates and the rest are both weaker and correlated with it.

**Recommendation for any fusion rewrite:** do not blend the NWP family as
independent predictors. If fusion is pursued, it must be (a) a *weighted* combination
dominated by rate_1h, with the NWP family collapsed to a single effective predictor
and (b) evaluated against the rate_1h-only baseline — which the current data says is
the bar to beat. The genuinely independent pair to test is **rate_1h × market**;
everything else is redundancy.

### Caveats
- **Market coverage** (n=391 here vs full book) limits the market column; the
  Task 4 path (n=143 trades) is the reliable market read-out.
- Strike axis is the next-integer-above-pin convention (matches the rate_1h sweep);
  results are conditioned on that, not on a per-day market ladder.
- `station_climatology.mean_high` has 151 corrupt rows (up to ~10,900 °F); this
  study builds climatology from canonical GHCN truth instead and does not use that
  column.
