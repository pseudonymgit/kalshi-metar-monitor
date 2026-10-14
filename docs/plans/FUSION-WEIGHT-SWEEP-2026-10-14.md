# Fusion Weight Sweep — best weighting for the 5-member fusion (2026-10-14)

**Script:** `scripts/fusion_weight_sweep_2026-10-14.py`
**Artifacts:** `data/fusion_weight_sweep_2026-10-14.json` (task deliverable) · `artifacts/fusion-weight-sweep/fusion_weight_sweep_2026-10-14.json` (analysis copy)
**Axis:** strict strike-crossing, `T = floor(hsf@13:30)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN settlement. **Look-ahead-free.**
**Cohort:** the exact honest 2296-row fusion cohort (`2026-04-21 → 2026-10-09`), identical to `HONEST-SIGNAL-ACCURACY-2026-10-14.md` Part 2/3.

Dan (2026-10-14): *"If we're keeping fusion, what's the best weighting? Can we sweep to find the sweet spot?"*

---

## 0. Bottom line

The shipped equal-ish fusion (`DEFAULT_WEIGHTS` = 0.25/0.25/0.25/0.25 gefs/analog/traj/market + **om_gfs 0.15**) is **mis-weighted**: it dilutes the one strong member (om_gfs) with three weak ones. Sweeping the om_gfs vote weight fixes most of the gap without changing any member set:

| setup | p_core Brier | vs om_gfs alone | ECE | dir acc |
|---|---:|---:|---:|---:|
| **om_gfs(bc,ps) alone** | **0.1096** | — | 0.042 | 0.840 |
| shipped default (om_gfs 0.15) | **0.1458** | +0.0362 | 0.089 | 0.816 |
| **best sweep point — om_gfs 0.80** | **0.1063** | **−0.0033** | 0.024 | **0.843** |
| plateau (om_gfs 0.70) | 0.1066 | −0.0030 | 0.021 | 0.843 |

**Recommendation:** if fusion is kept, **weight om_gfs ≈ 0.80** (4-member split 0.05 each) — it recovers the shipped default's regression *and* edges out om_gfs-alone on the honest axis, while keeping the fusion's redundancy (weaker members shrink to 5% each, they no longer hurt). But see §4: the *gain over om_gfs alone is real but tiny (−0.003 Brier)*, and the **simpler and more honest call is to ship the single best member (om_gfs(bc,ps)) and drop the ensemble**.

---

## 1. Sweep — 5-member fusion, om_gfs weight 0.20 → 0.90

Remaining weight `(1 − w_om)` split equally among the WIRE'd 4-member (gefs, analog, traj, market). `p_core` is the shipped weighted core blend (before the trajectory modulator) — the exact quantity the honest baseline 0.1458 is measured on. `raw` = `0.5 + traj_mult·(p_core−0.5)`; `cal` = per-station ceiling clamp.

| w_om_gfs | 4-mem each | p_core Brier | raw Brier | cal Brier | ECE (core) | Δ vs om_gfs alone | dir acc |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.20 | 0.200 | 0.1365 | 0.1366 | 0.1492 | 0.050 | +0.0269 | 0.818 |
| 0.30 | 0.175 | 0.1258 | 0.1256 | 0.1445 | 0.045 | +0.0162 | 0.818 |
| 0.40 | 0.150 | 0.1178 | 0.1174 | 0.1401 | 0.049 | +0.0082 | 0.821 |
| 0.50 | 0.125 | 0.1121 | 0.1118 | 0.1370 | 0.038 | +0.0025 | 0.830 |
| 0.60 | 0.100 | 0.1085 | 0.1083 | 0.1352 | 0.032 | −0.0011 | 0.841 |
| 0.70 | 0.075 | 0.1066 | 0.1070 | 0.1345 | 0.021 | −0.0030 | 0.843 |
| **0.80** | **0.050** | **0.1063** | **0.1069** | **0.1345** | 0.024 | **−0.0033** | **0.843** |
| 0.90 | 0.025 | 0.1074 | 0.1081 | 0.1348 | 0.022 | −0.0022 | 0.845 |

The curve is **monotone decreasing up to ~0.70–0.80**, then flattens/slightly rises as the 4-member shrinks toward zero (at `w_om = 1.0` the fusion *is* om_gfs alone, Brier 0.1096). The minimum sits at **0.80 (0.1063)**, but **0.70–0.90 are statistically indistinguishable** (see §3) — the "sweet spot" is a **plateau, not a point**. Practical read: *give om_gfs most of the mass; the exact value in [0.70, 0.90] does not matter.*

**Calibration note.** Every weighted point improves ECE monotonically (0.050 → ~0.021–0.024) because om_gfs is the best-calibrated member (ECE 0.042 alone, mean_p 0.80 vs base 0.812). The shipped default's ECE is the *worst* (0.089) — another symptom of the mis-weighting.

## 2. Drop weak members — fuse only om_gfs + market

The two members with any genuine independence (om_gfs is an Open-Meteo downscale; market is the crowd price). ℹ️ **Om+market co-occurs on only n = 284 rows** (both votes present) — this is a thin subset, not the full 2296, so its Brier is not comparable to §1 in absolute terms; compare within the pair.

| w_om_gfs | 0.00 | 0.10 | 0.25 | 0.50 (=) | 0.75 | 0.85 | 0.90 | 0.95 | 1.00 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Brier | 0.6089 | 0.5107 | 0.3839 | **0.2270** | 0.1382 | 0.1218 | 0.1177 | **0.1163** | 0.1176 |
| ECE | 0.622 | 0.552 | 0.458 | 0.298 | 0.144 | 0.104 | 0.069 | **0.039** | 0.054 |

At **equal weight (0.50)** the pair is **0.2270** — *bad*, because market alone on this subset is ~0.61 and drags it hard. The pair only becomes respectable when market is down-weighted to ~5–10%: best at **w_om = 0.95 → 0.1163** (still *worse* than om_gfs alone on the same 284 rows). **Verdict: market is a drag, not an asset, on the honest axis.** Dropping the weak members and fusing om_gfs + market at equal weight is *worse* than om_gfs alone; the pair only "wins" by becoming ~om_gfs alone. **Do not wire an om_gfs+market pair.**

## 3. Is the best sweep point *significantly* better than om_gfs alone?

Paired bootstrap on per-row squared-error differences (fused − om_gfs alone; negative = fused better), 2000 resamples, seed 20261014:

| w_om_gfs | Δ Brier vs om_gfs alone | 95% CI | P(fused better) |
|---:|---:|---|---:|
| 0.50 | +0.0025 | [−0.0022, +0.0073] | 0.15 |
| 0.60 | −0.0011 | [−0.0048, +0.0026] | 0.72 |
| 0.70 | −0.0030 | [−0.0057, −0.0003] | 0.98 |
| **0.80** | **−0.0033** | **[−0.0051, −0.0015]** | **1.00** |
| 0.90 | −0.0022 | [−0.0031, −0.0014] | 1.00 |

So at **w_om ≥ 0.70 the fusion beats om_gfs alone at ~98–100% bootstrap agreement** — a real but **very small** edge (≈0.003 Brier). Below 0.60 it is *not* significant. This is the quantitative form of the "sweet spot": *the ensemble adds value only when om_gfs dominates.*

## 4. Recommendation

1. **If fusion is kept:** set the om_gfs vote weight to **0.80** (4-member 0.05 each). This beats both the shipped default (0.1458 → 0.1063, −0.0395) **and** om_gfs alone (0.1096 → 0.1063, −0.0033, P≈1.00). It is robust across 0.70–0.90.
2. **Simpler and equally defensible:** **ship om_gfs(bc,ps) alone** and drop the ensemble. The honest ranking has one clear winner (0.1067 full-cohort / 0.1096 on the fusion cohort) and the fusion's edge over it is ~0.3% Brier — within the range where "an ensemble of one strong + three weak members" is hard to justify operationally.
3. **Do not** fuse om_gfs + market (the pair is a drag; §2).
4. **Do not** keep the equal-ish shipped default — it is the worst option tested (dilutes om_gfs with three weak members; worst ECE).

**Does the best fusion beat om_gfs alone?** Marginally and significantly *yes* at w_om ≥ 0.70 (≈−0.003 Brier). But if the choice is "a tuned 5-member ensemble" vs "the single best member," the single member is the honest, maintenance-light call — the tuned ensemble only recovers the damage the weak members caused.

---

## Reproduce

```bash
python3 scripts/fusion_weight_sweep_2026-10-14.py run   # needs /tmp/we_grid, /tmp/we_asos_recs, /tmp/honest_omgfs_cache, /tmp/honest_analog_traj (from the honest benchmark)
```
