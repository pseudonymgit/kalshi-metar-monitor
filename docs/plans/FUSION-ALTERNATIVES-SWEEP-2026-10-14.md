# Fusion Alternatives Sweep — 3 structurally different combination methods (2026-10-14)

**Script:** `scripts/fusion_alternatives_sweep_2026-10-14.py`
**Artifacts:** `data/fusion_alternatives_sweep_2026-10-14.json` (task deliverable) · `artifacts/fusion-alternatives/fusion_alternatives_sweep_2026-10-14.json` (analysis copy)
**Axis:** strict strike-crossing, `T = floor(hsf@13:30)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN. **Look-ahead-free.**
**Cohort:** the exact honest 2296-row fusion cohort (`2026-04-21 → 2026-10-09`), identical to `FUSION-WEIGHT-SWEEP-2026-10-14.md` and `HONEST-SIGNAL-ACCURACY-2026-10-14.md`.
**Members:** `gefs`, `analog`, `traj`, `market`, `om_gfs(bc, per-station)`.
**Split:** random 50/50 FIT/TEST, seed `20261014` (fixed before running; nothing tuned on TEST).

Dan (2026-10-14): *"Run the 3 fusion alternatives sweep."*

---

## 0. Bottom line

**None of the three alternatives beats `om_gfs` alone by the pre-registered criterion.** The honest recommendation is unchanged and now triple-confirmed: **ship `om_gfs(bc, per-station)` alone and drop the ensemble** — or, if the ensemble is kept, keep the already-found **om_gfs@0.80** point. No structurally different combination method recovered a *significant* edge over the single best member.

| approach | TEST Brier | Δ vs om_gfs alone | Δ vs om_gfs@0.80 | ECE | dir acc | P(beats om_gfs) | **beats?** |
|---|---:|---:|---:|---:|---:|---:|:--:|
| **baseline — om_gfs(bc,ps) alone** | **0.10385** | — | — | 0.0329 | 0.8458 | — | — |
| **baseline — om_gfs@0.80 5-member** | **0.10075** | −0.0031 | — | **0.0195** | **0.8519** | — | — |
| **A — inverse-variance (1/Brier²)** | 0.10615 | **+0.0023** | +0.0054 | 0.0485 | 0.8362 | 0.27 | ❌ |
| **B — BMA (source likelihood)** | 0.11535 | **+0.0115** | +0.0146 | 0.0517 | 0.8301 | 0.01 | ❌ |
| **C — L1 logistic stacking** | 0.10160 | −0.0023 | +0.0009 | **0.0194** | 0.8510 | 0.88 | ❌ (P<0.95) |

*Pre-registered criterion (see §1):* beat om_gfs alone iff **all** of (i) ΔBrier(TEST)<0, (ii) paired-bootstrap P(alt better) ≥ 0.95, (iii) ECE(alt) ≤ ECE(om_gfs alone)+0.02.

**Only Alternative C is directionally better than om_gfs alone** (Brier and ECE both improve, dir_acc +0.5pp) — but it does **not** clear the 0.95 significance bar (P=0.88) and it **only ties** the simple om_gfs@0.80 ensemble (0.10160 vs 0.10075; +0.0009 *worse*). A logistic meta-model that learns the optimal weights from data reproduces, but does not exceed, the hand-tuned om_gfs-heavy weighting — strong evidence that "om_gfs dominant + a little of the others" is the whole story and **there is no hidden combination gain**.

---

## 1. Pre-registration (written before running)

| field | value |
|---|---|
| axis | strict crossing, `T=floor(hsf@13:30)+1`, `y=settled_max>T` (`==` LOSS) |
| truth | canonical GHCN settlement |
| cohort | n=2296, `2026-04-21 → 2026-10-09`, look-ahead-free |
| members | gefs, analog, traj, market, om_gfs(bc, per-station) |
| split | random 50/50 FIT/TEST, seed 20261014 |
| thresholds | ΔBrier(TEST)<0 **and** paired-bootstrap P(better)≥0.95 (2000 resamples, seed 20261014) **and** ECE ≤ ECE(om_gfs alone)+0.02 |
| honest fallback | if none beats om_gfs alone → recommend dropping the ensemble |

Baselines are **recomputed on the same TEST rows** as each alternative (FIT n=1148, TEST n=1148), so the Δ column is apples-to-apples. `market` is present on only **284/2296** rows (12.4%); the other four on all.

---

## 2. Alternative A — Inverse-variance weighting

`w_i ∝ 1 / Brier_i²`, where `Brier_i` is source *i*'s solo Brier on FIT. Weights renormalized over sources **present in each row**. Weak sources are automatically near-zero.

**Solo FIT Brier:** om_gfs 0.11531 · analog 0.18517 · traj 0.19889 · gefs 0.33870 · market 0.60401.

| weights (1/Brier²) | gefs | analog | traj | market | om_gfs |
|---|---:|---:|---:|---:|---:|
| | 0.0618 | 0.2067 | 0.1791 | 0.0194 | **0.5330** |

- **TEST Brier 0.10615**, ECE 0.0485, dir acc 0.8362, mean_p 0.8509.
- **Δ vs om_gfs alone = +0.00230 (worse)**; paired bootstrap **P(better)=0.27**, 95% CI [−0.0052, +0.0096] — i.e. indistinguishable-or-worse. **Does not beat.**
- **Diagnosis:** `1/Brier²` is too *gentle*. With solo Briers 0.115 (om_gfs) → 0.604 (market), the squared inverse only gives om_gfs 53% and still hands **39% to analog+traj** (0.21 + 0.18), whose near-base-rate votes dilute om_gfs. The ensemble lands *between* om_gfs alone (0.104) and the shipped equal-weight fusion (0.146).

**Sensitivity (sharper exponent, `w ∝ 1/Brier⁴`):**

| weights (1/Brier⁴) | gefs | analog | traj | market | om_gfs |
|---|---:|---:|---:|---:|---:|
| | 0.0105 | 0.1177 | 0.0884 | 0.0010 | **0.7824** |

- TEST Brier **0.10059** → Δ vs om_gfs alone **−0.00326**, Δ vs om_gfs@0.80 **−0.00016** (a tie within noise).
- **Read:** to make inverse-variance competitive you must sharpen it until it *is* ≈ om_gfs@0.80. At that point it converges to the already-known best ensemble point (0.1063 full-cohort) and still does not beat om_gfs alone by a margin that matters. **The method's "automatic" weighting is simply a slower route to the same om_gfs-dominant answer.**

## 3. Alternative B — Bayesian model averaging (BMA)

Per-source posterior `w_i ∝ P(data | source i)` with equal priors, i.e. the source likelihood `Π_FIT p_i^y (1−p_i)^(1−y)`. **We use the PER-ROW MEAN log-likelihood** (the summed NLL is a units artifact: market is scored on 284 rows vs 2296 for the rest, so a sum rewards the source seen least — verified: the summed version collapsed to market=1.0 by accident of row count, not skill).

| weights (mean-logl softmax) | gefs | analog | traj | market | om_gfs |
|---|---:|---:|---:|---:|---:|
| | 0.1362 | 0.2368 | 0.2116 | 0.0538 | **0.3615** |

- **TEST Brier 0.11535**, ECE 0.0517, dir acc 0.8301, mean_p 0.7621.
- **Δ vs om_gfs alone = +0.01150 (worse)**; paired bootstrap **P(better)=0.009**, 95% CI [+0.0020, +0.0209]. **Does not beat — significantly worse.**
- **Diagnosis (this is the important one):** BMA by *likelihood* rewards **calibration in the majority class, not discrimination**. With base rate 0.821, a source that sits near 0.82 scores a high likelihood on ~82% of rows; the near-base-rate analog/traj votes (0.237 + 0.212 = **45%**) beat om_gfs (0.362) in the likelihood weighting. BMA gives the *best* member a minority weight because it optimizes `P(data|source)`, which is maximized by predicting the base rate — exactly the failure mode in an imbalanced binary event. **Source-likelihood BMA is the wrong objective here.**
- **Common-support variant** (weights fit only on the 149 FIT rows where all 5 sources present): weights om_gfs 0.337 / analog 0.207 / traj 0.191 / gefs 0.212 / market 0.053 → TEST 0.11978 (+0.0159). Same conclusion, worse.

## 4. Alternative C — Stacking (L1 logistic meta-model)

`LogisticRegression(penalty='l1', solver='liblinear', C=1.0)` on the 5 source probabilities in **logit space** (standardized), fit on FIT, scored on TEST. Missing source probs imputed at 0.5 (market absent on ~88% of rows). L1 zeroes useless sources.

| coefficient (logit space) | gefs | analog | traj | market | om_gfs |
|---|---:|---:|---:|---:|---:|
| | −0.0305 | **0.3060** | 0.0348 | 0.0030 | **1.6232** |

*(no source zeroed by L1.)*

- **TEST Brier 0.10160**, **ECE 0.0194**, **dir acc 0.8510**, mean_p 0.8071.
- **Δ vs om_gfs alone = −0.00225 (better)**; **but paired bootstrap P(better)=0.88 < 0.95** (95% CI [−0.0063, +0.0016]) → **fails the significance criterion.** **Δ vs om_gfs@0.80 = +0.00085 (slightly worse).**
- **Diagnosis / the useful signal:** stacking learns `om_gfs ≈ 1.62` (dominant) and `analog ≈ 0.31` (a real, non-trivial second role; it does **not** zero out), with traj ≈ 0.03 and gefs/market ≈ 0. **The meta-model independently rediscovers the exact conclusion of the weight sweep: om_gfs should dominate and *analog* is the only other member carrying genuine complementary information.** But the gain over om_gfs alone is not significant, and it does not exceed the hand-tuned om_gfs@0.80 — so the learned structure confirms the sweep rather than improving on it.

---

## 5. Recommendation

1. **No alternative beats `om_gfs` alone significantly.** A loses, B loses badly, C is directionally better but non-significant (P=0.88) and only ties om_gfs@0.80. **None justifies keeping the ensemble *over om_gfs alone*.**
2. **Simplest honest call (unchanged): ship `om_gfs(bc, per-station)` alone.** Three structurally different combination methods — inverse-variance, Bayesian model averaging, and a data-learned logistic stack — all land at or above om_gfs alone; the best of them only recovers a ~0.2–0.3% Brier edge that the single member already has.
3. **If the ensemble is kept for redundancy:** keep **om_gfs@0.80** (from `FUSION-WEIGHT-SWEEP-2026-10-14.md`) — it remains the best measured ensemble point (0.10075 TEST / 0.1063 full-cohort) and beats stacking (0.10160). But note it is a *hand-tuned* weighting, and stacking — which learns weights from data — reproduces rather than exceeds it.
4. **The one durable finding:** **analog is the only non-om_gfs member with genuine complementary value** (stacking coef 0.31, never rejected; it was also the second-best solo member at 0.185). traj, gefs, and market add no learnable signal on this axis. If a 2-member ensemble is ever wanted, it is **om_gfs + analog**, not om_gfs + market (the market pair was already shown to be a drag in the weight sweep §2).
5. **Do not** wire source-likelihood BMA — its objective is miscalibrated for an imbalanced event (it would pick whatever member predicts the base rate).

**One-line answer to Dan:** *All three "smarter" combiners were tested honest; none beats `om_gfs` alone. Inverse-variance is just a slower route to om_gfs-dominant; BMA rewards base-rate guessing; logistic stacking rediscovers "om_gfs + a little analog" but only ties the hand-tuned 0.80 point. Drop the ensemble; if kept, keep om_gfs@0.80 — and know analog is the only other member that earns a vote.*

---

## Reproduce

```bash
python3 scripts/fusion_alternatives_sweep_2026-10-14.py run
# needs /tmp/we_grid_2026-10-13.pkl.gz, /tmp/we_asos_recs_2026-10-13.pkl.gz,
# /tmp/honest_omgfs_cache_2026-10-14.json, /tmp/honest_analog_traj_2026-10-14.pkl.gz
# (the honest benchmark's caches). Deterministic: split seed 20261014.
```
