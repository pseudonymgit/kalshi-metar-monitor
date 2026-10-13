# Predictor Diversity Study — 2026-10-13

**Question:** Are our "independent" forecast sources actually diverse, and does
combining them beat any single one? (Pre-requisite for any fusion rewrite.)

**Primary metric (Dan 2026-10-13):** Brier score + **calibration of P(cross)** vs
settled truth. Directional accuracy is secondary context only — it is reported but
is not a decision criterion.

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

## Task 1 (PRIMARY) — P(cross) Brier + calibration

`Brier = reliability − resolution + uncertainty`. Lower Brier / reliability / ECE /
MCE is better; higher resolution is better. Bins are fixed probability deciles.

| source | n | **Brier** | ECE | MCE | reliability | resolution | base rate | mean p |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **rate_1h** | 2464 | **0.0921** | **0.1141** | 0.3904 | **0.0231** | **0.0871** | 0.805 | 0.720 |
| gfs | 2960 | 0.1597 | 0.1446 | 0.2542 | 0.0256 | 0.0277 | 0.796 | 0.677 |
| gem | 2520 | 0.1793 | 0.1524 | 0.3387 | 0.0355 | 0.0161 | 0.799 | 0.674 |
| icon | 2960 | 0.1840 | 0.1778 | 0.3212 | 0.0426 | 0.0202 | 0.796 | 0.649 |
| ecmwf | 2960 | 0.2080 | 0.2264 | 0.4044 | 0.0663 | 0.0205 | 0.796 | 0.618 |
| gefs | 2740 | 0.3290 | 0.3243 | 0.6817 | 0.1631 | 0.0038 | 0.784 | 0.613 |
| market | 391 | 0.6247 | 0.6331 | 0.7559 | 0.4522 | 0.0020 | 0.775 | 0.164 |

**Read (metrics, not direction):**
- **rate_1h is the only source that clears the null in any meaningful way** —
  Brier 0.092, and it is the **only source with useful resolution (0.087)**. It is
  calibrated on the upside (p in [0.2,0.5] it under-predicts; p>0.5 it over-predicts
  slightly) but its resolution carries it.
- **gfs is the best-calibrated NWP** (ECE 0.145, reliability 0.026) but has almost
  no resolution (0.028) — it is close to the base rate, i.e. a well-calibrated but
  uninformative predictor on this axis.
- The other NWP models are **systematically under-confident**: their reliability gap
  is negative almost everywhere (e.g. icon bin 0.4–0.5 predicts 0.455 but observes
  0.776; ecmwf bin 0.3–0.4 predicts 0.355 but observes 0.759). They sit well below
  the diagonal across the whole mid-range.
- **gefs is the worst NWP**: ECE 0.324, MCE 0.682, resolution **0.004** (it separates
  nothing). Its `[0.0,0.1]` bin predicts 0.016 but the observed cross rate is 0.698
  — the underdispersion (σ=1.27 °F, see below) makes it a near-step function.
- **market is severely miscalibrated here** (ECE 0.633, all bins off by 0.4–0.76),
  reflecting the sparse-snapshot coverage, not the market's true skill. Treat as
  indicative only; the Task 4 path (n=143) is the reliable market read.

### Reliability tables (P(cross) decile → observed cross rate)

**rate_1h** (ECE 0.114) — over-confident above ~0.5, under-confident 0.2–0.4:

| bin | n | mean p | obs | gap |
|---|---:|---:|---:|---:|
| 0.2–0.3 | 168 | 0.272 | 0.137 | +0.135 |
| 0.4–0.5 | 244 | 0.451 | 0.512 | −0.062 |
| 0.5–0.6 | 194 | 0.543 | 0.830 | −0.287 |
| 0.6–0.7 | 164 | 0.654 | 0.963 | −0.310 |
| 0.7–0.8 | 210 | 0.749 | 0.991 | −0.242 |
| 0.9–1.0 | 921 | 0.970 | 0.997 | −0.027 |

**gfs** (best-calibrated NWP, ECE 0.145) — uniformly slightly under-confident:

| bin | n | mean p | obs | gap |
|---|---:|---:|---:|---:|
| 0.2–0.3 | 139 | 0.249 | 0.504 | −0.254 |
| 0.5–0.6 | 318 | 0.552 | 0.789 | −0.237 |
| 0.7–0.8 | 487 | 0.749 | 0.899 | −0.150 |
| 0.9–1.0 | 616 | 0.968 | 0.906 | +0.062 |

**ecmwf** (ECE 0.226) — large negative gaps through the whole mid-range
(0.2–0.5 predicts 0.25–0.45 but observes 0.58–0.84):

| bin | n | mean p | obs | gap |
|---|---:|---:|---:|---:|
| 0.1–0.2 | 154 | 0.150 | 0.500 | −0.350 |
| 0.3–0.4 | 245 | 0.355 | 0.759 | −0.404 |
| 0.5–0.6 | 303 | 0.550 | 0.858 | −0.308 |
| 0.9–1.0 | 608 | 0.971 | 0.854 | +0.117 |

**gefs** (ECE 0.324) — the `[0.0,0.1]` bin is the killer:

| bin | n | mean p | obs | gap |
|---|---:|---:|---:|---:|
| 0.0–0.1 | 689 | 0.016 | 0.698 | −0.682 |
| 0.2–0.3 | 83 | 0.251 | 0.771 | −0.520 |
| 0.9–1.0 | 1265 | 0.987 | 0.829 | +0.159 |

(Full reliability tables for gem, icon, market are in the results JSON under
`task1_per_source.<source>.calibration_table`.)

### Task 1b (SECONDARY) — directional accuracy

Reported for context only; **not** a decision criterion.

| source | n | dir. acc | base rate |
|---|---:|---:|---:|
| rate_1h | 2464 | 0.8916 | 0.805 |
| gfs | 2960 | 0.7882 | 0.796 |
| gem | 2520 | 0.7369 | 0.799 |
| icon | 2960 | 0.7331 | 0.796 |
| ecmwf | 2960 | 0.6916 | 0.796 |
| gefs | 2740 | 0.6131 | 0.784 |
| market | 391 | 0.2788 | 0.775 |

Note the base rate is 0.78–0.81, so a "predict everything crosses" rule already
scores ~0.80 directional accuracy — directional accuracy flatters these sources and
is why it is demoted. Brier/calibration is the honest read.

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
| gefs–market | 528 | 0.119 | 0.174 | |
| gfs–market | 599 | 0.108 | 0.114 | |
| **gefs–rate_1h** | 2197 | **0.097** | 0.115 | |
| market–rate_1h | 184 | −0.282 | −0.208 | |

**No pair exceeds 0.85 on the traded P(cross) axis.** The strongest coupling is
among the deterministic global models (ecmwf/icon/gfs/gem, 0.60–0.72). GEFS is more
decorrelated *because* of its broken spread. rate_1h and market sit apart from the
NWP pack.

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

**Every NWP–NWP and NWP–rate_1h temperature correlation is 0.87–0.98.** On the raw
temperature axis these are the *same predictor in slightly different clothes*. The
P(cross) decorrelation seen in Task 2 comes from the nonlinear
temperature→probability mapping and the differing spreads, **not** from independent
information. Only **market** carries a genuinely orthogonal signal (|r| < 0.13).

---

## Task 3 — Does the combination beat the best single?

| comparison | n | blend Brier | best single | best-single Brier | Δ (blend − best) |
|---|---:|---:|---|---:|---:|
| all sources (greedy indep. set) | 123 | 0.2068 | rate_1h | 0.1241 | **+0.083 (worse)** |
| gfs + rate_1h | 2311 | 0.1168 | rate_1h | 0.0919 | **+0.025 (worse)** |
| gfs + rate_1h + market | 177 | 0.2296 | rate_1h | 0.1093 | +0.120 (worse) |
| gefs + rate_1h + market | 123 | 0.2463 | rate_1h | 0.1241 | +0.122 (worse) |
| rate_1h + market | 177 | 0.2793 | rate_1h | 0.1093 | +0.170 (worse) |

**No blend beats the best single source on Brier.** Every equal-weight combination is
*worse* than rate_1h alone. This holds at n=2311 (a robust sample, not a small-n
artifact): the gfs+rate_1h blend costs +0.025 Brier versus rate_1h by itself.

Why: equal weighting treats GEFS and the sparse market mids as peers of rate_1h.
When one member is the best predictor and the others are weak *and* correlated with
it (but noisier), averaging drags the good member toward the bad ones. With no
member decorrelated *and* comparably accurate, equal-weight fusion cannot help.

---

## Task 4 — `trade_calibration.p_market` populated

- Resolution replicated read-only from the SBOX bridge
  (`scripts.sbox_intraday_ev_bridge.market_mid_prob`): YES mid at the snapshot
  nearest the 13:30 station-local pin, from `kalshi_market_snapshots`.
- Result: **143 / 143 rows filled** (was 0/143). Range 0.005–0.995, mean 0.096.
- **Integrity:** `trades` untouched (82 rows before and after); every
  `trade_calibration` column except `p_market` is byte-identical to the pre-write
  backup (`data/_backup_pre_pmarket_20261013T064037Z.db`).
- Note: 83/143 trades carry p_market = 0.005 (deep-OTM at the pin) — a real property
  of the booked strikes (all BUY side, brace above the market's implied median). The
  mean p 0.096 vs realized booking is consistent with the calibration story: the
  book buys high strikes the market prices cheap.

---

## Verdict

**We do not have real predictor diversity — the NWP family is collinear.**
(Unchanged by the metric refocus; if anything the Brier/calibration view makes the
case sharper, because the NWP models show almost no resolution.)

1. **On the temperature axis, ecmwf/gfs/icon/gem/gefs are one predictor**
   (r = 0.87–0.98). Any fusion that treats them as independent votes is double-counting.
2. **On the traded P(cross) axis the coupling drops below the 0.85 flag** (0.60–0.72
   among NWP), but the reliability tables show these models are largely
   **under-confident and low-resolution** — the decorrelation is a
   nonlinearity/units effect, not new information.
3. **Only two sources are genuinely distinct from the NWP pack:**
   - **rate_1h** — the only source that observes the high-so-far at the pin. It has
     the **best Brier (0.092)** and the **only meaningful resolution (0.087)**; every
     other source is ≤ 0.028.
   - **market** — near-zero correlation with everything (|r| < 0.13 in temperature,
     |r| < 0.21 in P(cross)) — truly orthogonal, but its snapshot coverage here is
     too thin to measure its standalone skill fairly.
4. **Diversity does not add measurable skill.** No equal-weight blend beats rate_1h
   on Brier; at n=2311 the blend is +0.025 worse. Fusion cannot help while one member
   dominates and the rest are both weaker and correlated with it.

**Recommendation for any fusion rewrite:** do not blend the NWP family as
independent predictors. If fusion is pursued, it must be (a) a *weighted* combination
dominated by rate_1h, with the NWP family collapsed to a single effective predictor,
and (b) evaluated against the rate_1h-only **Brier/calibration** baseline — which the
current data says is the bar to beat. The genuinely independent pair to test is
**rate_1h × market**; everything else is redundancy. Secondary (directional accuracy)
should not gate the decision.

### Caveats
- **Market coverage** (n=391 here vs 143 in the book) limits the market column; the
  Task 4 path is the reliable market read-out.
- Strike axis is the next-integer-above-pin convention (matches the rate_1h sweep);
  results are conditioned on that, not on a per-day market ladder.
- `station_climatology.mean_high` has 151 corrupt rows (up to ~10,900 °F); this study
  builds climatology from canonical GHCN truth instead and does not use that column.
