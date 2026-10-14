# Weak-Signal Salvage — can the old signals help the decision model? (2026-10-14)

**Script:** `scripts/disagreement_quality_flag_2026-10-14.py`  
**Pre-registration:** `docs/plans/WEAK-SIGNAL-SALVAGE-PREREG-2026-10-14.md` (written before any test was run)  
**Artifacts:** `data/weak_signal_salvage_2026-10-14.json` · `artifacts/weak-signal-salvage/weak_signal_salvage_2026-10-14.json`  
**Axis:** strict strike-crossing, `T = floor(hsf@13:30)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN. **Look-ahead-free.**  
**Cohort:** 2026-04-21 → 2026-10-09, n = 2927 station-days.

---

## 0. Bottom line

**Verdicts against the frozen gates:**

| signal | role tested | literal gate | decision verdict |
|---|---|---|---|
| NWP cross-source spread (H1) | quality gate | SALVAGE / SALVAGE | **NO USE** (base-rate artifact — see §H1) |
| rate_1h (0.362) (H2) | residual correction | NO USE | **NO USE** |
| market (0.619) (H3) | lag / residual | NO USE | **NO USE** |
| gefs spread (H4) | uncertainty/sizing | NO USE | **NO USE** |

> **Honest zero.** No weak signal earns a non-fusion role in the decision model. One test (H1, NWP cross-source spread) *passed its literal pre-registered gate* (ΔBrier(Q4−Q1) = +0.024 ≥ 0.02, Spearman p < 0.05) — but on interrogation the effect is a **base-rate/difficulty artifact**, not a usable quality flag: the base-rate-normalized skill (BSS) is *highest* in the high-disagreement bin, the implementable gate gain is **≈0.001 Brier** (noise), and it is not month-stable. Reported as NO USE. The other three are clean zeros.

**Nothing here is worth wiring.** The old directional-accuracy signals remain useful only as *context* — they confirm the market is priced off roughly the same information set (`market` ρ≈0, §H3). The actionable lever remains the incumbent: ship `om_gfs(bc,ps)` (Brier 0.1067) and drop the fusion.

Incumbent on this cohort: `om_gfs(bc,ps)` Brier = **0.1067** (n = 2927, ECE 0.0376).

## H1 — Cross-source DISAGREEMENT as a quality flag

Hypothesis: when the NWP members disagree, `om_gfs` is less trustworthy, so fused error should be worse. **Gate:** `Brier(Q4)−Brier(Q1) ≥ 0.02` AND Spearman(spread, sq-err) > 0 with p < 0.05.

### spread5 — verdict **SALVAGE** (n = 2762)

| bin | spread range | n | om_gfs Brier | mean\|err\| (°F) | mean y |
|---|---|---:|---:|---:|---:|
| Q1 | [—, 1.52) | 688 | 0.0855 | 1.1726 | 0.862 |
| Q2 | [1.52, 2.19) | 691 | 0.1098 | 1.3328 | 0.822 |
| Q3 | [2.19, 3.17) | 692 | 0.1194 | 1.3210 | 0.817 |
| Q4 | [3.17, —) | 691 | 0.1097 | 1.4652 | 0.763 |

ΔBrier(Q4−Q1) = **0.0242**; Spearman ρ = 0.0761 (p = 0.0001); monotone: none.

### spread6 — verdict **SALVAGE** (n = 2762)

| bin | spread range | n | om_gfs Brier | mean\|err\| (°F) | mean y |
|---|---|---:|---:|---:|---:|
| Q1 | [—, 1.52) | 693 | 0.0770 | 1.1560 | 0.864 |
| Q2 | [1.52, 2.17) | 684 | 0.1043 | 1.3132 | 0.848 |
| Q3 | [2.17, 3.12) | 692 | 0.1305 | 1.3789 | 0.811 |
| Q4 | [3.12, —) | 693 | 0.1127 | 1.4440 | 0.740 |

ΔBrier(Q4−Q1) = **0.0357**; Spearman ρ = 0.0833 (p = 0.0); monotone: none.

### range5 — verdict **SALVAGE** (n = 2762)

| bin | spread range | n | om_gfs Brier | mean\|err\| (°F) | mean y |
|---|---|---:|---:|---:|---:|
| Q1 | [—, 3.60) | 678 | 0.0828 | 1.1665 | 0.858 |
| Q2 | [3.60, 5.29) | 704 | 0.1125 | 1.3361 | 0.817 |
| Q3 | [5.29, 7.74) | 696 | 0.1211 | 1.2965 | 0.826 |
| Q4 | [7.74, —) | 684 | 0.1074 | 1.4918 | 0.762 |

ΔBrier(Q4−Q1) = **0.0246**; Spearman ρ = 0.0747 (p = 0.0001); monotone: none.

### H1 interrogation — the gate passes, but the mechanism is a base-rate artifact

The literal gate passed on **raw Brier**. But the quartile shape is *non-monotone* (Q1 0.0855 → Q2 0.1098 → Q3 0.1194 → Q4 0.1097) and the base rate falls across the bins — the signature of a **difficulty confound**, not a disagreement signal. Two checks settle it.

**(1) Base-rate-normalized skill (BSS).** If disagreement meant *less* skill, BSS should be lowest in Q4. It is **highest** (Q1 0.282 → Q4 **0.394**): on disagreement days `om_gfs` is *sharper*, not worse. The raw-Brier gap was the lower base rate, not degradation of the forecast.

| bin | n | raw Brier | BSS | base rate | mean err (signed) | mean\|err\| |
|---|---:|---:|---:|---:|---:|---:|
| Q1 | 688 | 0.0855 | 0.2816 | 0.862 | +0.555 | 1.173 |
| Q2 | 691 | 0.1098 | 0.2495 | 0.822 | +0.647 | 1.333 |
| Q3 | 692 | 0.1194 | 0.2033 | 0.817 | +0.629 | 1.321 |
| Q4 | 691 | 0.1097 | 0.394 | 0.763 | +0.474 | 1.465 |

**(2) Gate simulation.** What a "skip the high-disagreement bin" gate would actually buy: dropping Q4 moves the incumbent Brier from 0.1061 to 0.1049 — a gain of **-0.0012** (noise; the gate costs you 691 of 2762 rows and buys ~nothing). The mean level error only moves -0.0474 °F. This is the number that decides "usable" — and it does not.

**(3) Stability.** Q4−Q1 ΔBrier is positive in 5/6 months (5–7% in Jul–Sep), but the last partial month (2026-10) flips sign — and month-reranked quartiles are a different partition. Not the stable-if-monotone picture the H1 gate was written to require.

**H1 verdict: NO USE.** The pre-registered gate was met in letter (ΔBrier ≥ 0.02, Spearman p < 0.05) but the mechanism is a base-rate artifact and the implementable gain is ≈0.001 Brier. A quality gate that fires on ~25% of days to buy 0.1% Brier is not a tradeable edge. Honest zero.

## H2 — rate_1h as a residual correction

Hypothesis: rate_1h's sign/magnitude carries dynamics that add skill to the `om_gfs` level residual `e = settled − om_gfs_raw`. **Gate:** block-bootstrap 95% CI on the OLS slope excludes 0 AND the correction improves held-out Brier by ≥ 0.005.

- **rate** — verdict **NO USE** (n = 2311): slope -0.020977 (r² 0.00029); bootstrap CI [-0.081962, 0.049443]; held-out ΔBrier 0.0064.
- **rate_rise** — verdict **NO USE** (n = 2311): slope -0.136099 (r² 0.00507); bootstrap CI [-0.213429, -0.066707]; held-out ΔBrier 0.0084.

## H3 — market as a lag / residual indicator

Hypothesis: the market mid at the pin is slow to reprice and adds to the `om_gfs` residual. **Verdict: NO USE** (n = 376). Slope -0.236749 (r² 0.00088); bootstrap CI [-2.479483, 1.627927]; held-out ΔBrier 0.0095. Consistent with the register's `corr ≈ −0.002` null.

## H4 — gefs ensemble SPREAD as an uncertainty input

Hypothesis: gefs is a poor point predictor but its spread estimates `om_gfs`'s error magnitude (heteroscedasticity), making it a *sizing* input. **Verdict: NO USE** (n = 2575). Spearman ρ = 0.0353 (p = 0.0735); Q4/Q1 mean\|err\| ratio = 1.068 (gate ≥ 1.5); bootstrap CI [-0.032724, 0.090132].

| bin | gefs sd range | n | mean\|om_gfs err\| |
|---|---|---:|---:|
| Q1 | [—, 0.87) | 644 | 1.2807 |
| Q2 | [0.87, 1.33) | 644 | 1.3424 |
| Q3 | [1.33, 1.92) | 637 | 1.3034 |
| Q4 | [1.92, —) | 650 | 1.3683 |

## Reproduce

```bash
python3 scripts/disagreement_quality_flag_2026-10-14.py run
# needs the honest-benchmark /tmp caches (/tmp/we_grid_2026-10-13.pkl.gz, /tmp/we_asos_recs_2026-10-13.pkl.gz,
#  /tmp/honest_omgfs_cache_2026-10-14.json) from scripts/honest_signal_accuracy_2026-10-14.py
```

