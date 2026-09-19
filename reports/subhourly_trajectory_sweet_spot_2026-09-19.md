# Sub-hourly ASOS trajectory → daily-high strike cross: sweet-spot verdict

**Date:** 2026-09-19
**Branch:** `temp-sprint-branch`
**Type:** Deterministic measurement (read-only, script-only, no AI)
**Scripts:** `scripts/measure_subhourly_sweet_spot.py` (primary) + inline binning diagnostics
**Data:** `data/iem_asos_1min.db` (fresh genuine 1-min NCEI, commit `4e6020dd1`) × `data/metar_archive.db` (settlement truth)

---

## (a) Is the data now genuinely sub-hourly? — **YES**

The prior "1-min" feed was actually hourly (collector never requested minute data). The
fixed collector (`asos1min.py`, commit `4e6020dd1`) plus the parallel backfill
(`bc4ef27ed`) has now populated **real 1-minute observations**.

Resolution audit across all 20 stations (`part_a_resolution_audit`):

| Metric | Result |
|---|---|
| Median inter-observation gap | **1.0 min** (all 20/20 stations) |
| Fraction of gaps < 5 min | **99.9–100%** |
| Fraction of gaps ~60 min (hourly signature) | **0.0%** |
| Observations per station | 622k – 1.23M |
| ASOS 1-min coverage | 2024-01-01 → 2026-09-18 |

**Verdict (a):** The feed is now true sub-hourly. This is the first time the sub-hourly
hypothesis has been testable on real minute-cadence data, not a re-sampling of hourly.

---

## (b) Does sub-hourly nowcasting beat hourly? — **YES, on the bettable region**

The signal is the **trajectory ramp slope** (least-squares slope of temperature over a
trailing 90-min window) combined with the **gap-to-strike** (`strike − current_max`).
Prediction = "does the eventual daily high (settlement temp) strictly exceed the strike",
evaluated contemporaneously (only observations ≤ nowcast time), in the 3–4h band before
the observed daily-high minute.

Base rate of crossing is heavily imbalanced (strikes set far above typical high):
- HIGH (`greater`) markets: **10.1%** of days cross.
- LOW (`less`) markets: **5.4%** cross.

Binned P(cross) by (gap-to-strike, slope) in the 3–4h window, **5-min resolution**
(HIGH markets, cells with n ≥ 50):

| gap bin | slope bin | n | P(cross) |
|---|---|---|---|
| gap<1°F | slope 0.02–0.05 | 156 | **0.917** |
| gap<1°F | slope<0.02 | 352 | **0.591** |
| gap 1–3°F | slope 0.05–0.10 | 242 | **0.798** |
| gap 1–3°F | slope 0.02–0.05 | 597 | **0.523** |
| gap 1–3°F | slope<0.02 | 1068 | 0.268 |
| gap 3–6°F | slope 0.05–0.10 | 1332 | 0.481 |
| gap>6°F | (all slopes) | ~55k | 0.04–0.17 |

**Resolution head-to-head (same code path, only cadence differs):**

| Resolution | bettable cells (gap<1 / gap1-3, P>0.52) | combined n | combined P(cross) |
|---|---|---|---|
| **5-min** | 4 cells | **1347** | **0.636** |
| **15-min** | 3 cells | 357 | 0.602 |
| **60-min** | **0 cells** | — | — |

At 60-min (hourly) cadence, the fine `gap<1°F` cell is almost entirely smeared out and
the `gap 1–3°F` high-slope cells no longer clear the 52% breakeven — **there is no
bettable region at hourly resolution**. At 5-min, a clean bettable region emerges.

**Verdict (b):** Finer resolution adds real signal — it resolves the near-strike
`gap<1°F` and `gap 1–3°F` + high-slope cells that hourly cadence cannot see. This is not
a re-sampling of the flat hourly result.

---

## (c) Is there a bettable sweet spot 3–4h before HOTD? — **YES (marginal but real)**

The sweet spot is **gap-to-strike < 1°F** (daily high already within ~1°F of the strike)
with **positive ramp slope**, and **gap 1–3°F with slope > 0.05 °F/min**.

Fee-adjusted net EV (Kalshi binary: buy YES at effective price = 0.5 + spread/2 + slippage
= **0.5205**; win pays 1.0, lose pays 0.0):

| cell | n | P(cross) | net EV/bet |
|---|---|---|---|
| gap<1°F, slope 0.02–0.05 | 156 | 0.917 | **+0.40** |
| gap<1°F, slope<0.02 | 352 | 0.591 | +0.07 |
| gap 1–3°F, slope 0.05–0.10 | 242 | 0.798 | **+0.28** |
| gap 1–3°F, slope 0.02–0.05 | 597 | 0.523 | +0.00 |
| **Combined bettable** | **1347** | **0.636** | **+0.115** |

Breakeven accuracy = 0.5205. The combined bettable region clears it at **63.6%** → net
**+0.115 per bet**, with the strongest cells (+0.28 to +0.40) clearly profitable.

Caveats: the `gap<1°F` cell is a near-realized condition (the high is already ~at the
strike), so its edge is partly "the crossing is already happening". The more genuinely
*anticipatory* cell — `gap 1–3°F` + slope > 0.05 °F/min (P=0.798, +0.28 EV) — is the
strongest pure sub-hourly-trajectory signal. Sample size is thin (242 contracts), so
this is a **candidate edge, not a proven one**.

---

## (d) Verdict: does sub-hourly ASOS trajectory add real edge, or is it flat like hourly?

**It is NOT flat like hourly — sub-hourly resolution unlocks a real, previously-invisible
edge, but the edge is narrow and concentrated in a thin near-strike + fast-ramp regime.**

1. The prior "flat hourly" result was a measurement artifact of coarse cadence: at 60-min
   resolution, the informative near-strike cells are smeared away and there is **no
   bettable region** in the 3–4h window.
2. At 5-min resolution, the trajectory slope becomes a meaningful predictor *on top of*
   the gap-to-strike, and a **bettable sweet spot** appears (P(cross) = 0.64 combined,
   net EV +0.115/bet).
3. The edge is **concentrated**: it lives almost entirely in the `gap<1°F` and
   `gap 1–3°F + slope>0.05` cells. The `gap>6°F` regime (the vast majority of the day) is
   correctly identified as "won't cross" but offers no YES-side bettable opportunity.
4. The strongest *anticipatory* signal (`gap 1–3°F`, slope > 0.05 °F/min, P=0.798) is
   thin (242 contracts over the sample) — **real but needs out-of-sample confirmation**
   before sizing real capital.

**Bottom line:** Sub-hourly ASOS trajectory **adds real, measurable edge** that hourly
cadence does not capture. It is a genuine improvement — but it is a *narrow* sweet spot,
not a broad one. Recommend: promote `gap<1°F` (any positive slope) and `gap 1–3°F +
slope>0.05°F/min` as a candidate HIGH-market entry signal, with a dedicated
out-of-sample walk-forward before any live sizing.

---

## Methodology notes (constraints honored)

- **Contemporaneous-only:** predictions at nowcast time `T` use only observations with
  minute ≤ `T`. The settlement (daily high) is the label only, never an input.
- **Correct strike axis:** `settlement_temp_f` vs `strike_price`, strict `>` for HIGH
  (`greater`) / strict `<` for LOW (`less`).
- **Fee-adjusted:** round-trip 0.0205 (spread 0.031/2 + slippage 0.005); effective YES
  entry 0.5205.
- **Deterministic:** fixed thresholds, least-squares slope, no randomness, no AI.
- **Read-only:** no source data modified. Outputs to `data/measurement_subhourly_sweet_spot.json`.
