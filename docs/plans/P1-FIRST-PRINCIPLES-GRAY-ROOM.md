# First-Principles — Gray Room Session Findings

**Date:** 2026-08-03
**Method:** Strip away the 66 findings, 7 experts, 5 disagreements. What actually matters?

---

## The One Question That Dominates Everything

**Is the 66.2% baseline real?**

The entire Gray Room session — the cascade, the 82-member fusion, the trajectory lane, the Goldilocks alert — all of it is built on top of a baseline that has never been verified against the correct settlement data source.

The 66.2% accuracy comes from comparing GEFS ensemble fraction predictions to Kalshi settlement data. But the Gray Room questioned whether the settlement data is accurate. The NWS CLI (Climate Local Data) is the authoritative source. If the Kalshi settlements don't match CLI, the baseline is wrong.

First-principles: Verify the ground truth before building anything on top of it.

**If the baseline is real (66.2% ± 2pp):**
- Gaussian fusion adds +2.15pp → 68.3% → wire it
- Cascade is a nice-to-have, not a must-have
- 82-member fusion is optimization, not foundation
- Trajectory lane is 1-3pp marginal, worth building
- Goldilocks is a separate product lane

**If the baseline is wrong (56% or 76%):**
- Everything changes. The cascade math, the fusion weights, the station sizing — all optimized against the wrong number.
- Stop everything. Fix the baseline first.

---

## What the Experts Actually Told Us (Stripped Down)

| Expert | One Sentence | Priority |
|--------|-------------|----------|
| E1 (Bayesian) | The cascade is frequentist, 82 members = 1-4 effective, and the math reduces P&L by design | **P0** — fix the math |
| E2 (Fusion) | The LLOP code doesn't match the cascade design, and deterministic models are wasted as agreement checks | **P2** — after baseline |
| E3 (Microstructure) | Goldilocks lane mostly exists, just extract it, but the detection is backwards | **P1** — separate lane |
| E4 (Pattern) | Trajectory matching is viable at 1-3pp, needs climate-zone pooling, ~17h | **P4** — research spike |
| E5 (Implementation) | Goldilocks 2-3d, cascade 2-3wk, trajectory 4-6wk, and someone's paying $54/mo for no reason | **P0** — fix the cron |
| E6 (Meteorology) | ASOS chain kills sub-minute ticks, Goldilocks = sensor self-heating, trajectory = air mass ID | **P0** — recalibrate Goldilocks concept |
| E7 (Adversarial) | CLI validation is blocking, Goldilocks is for a dead product, the ±10pp CI makes everything uncertain | **P0** — CLI validation |

---

## Revised Priority (First-Principles Tight)

| Order | Item | Effort | First-Principles Rationale |
|-------|------|--------|---------------------------|
| **P0** | **CLI settlement verification** | 1-2 days | Everything is built on this baseline. If it's wrong, stop. If it's right, proceed. |
| **P0.1** | **Fix $54/mo cron waste** | 5 min | Free money. Switch to qwen2.5-coder:7b. |
| **P1** | **Gaussian fusion** | 4h | Already tested, already works (+2.15pp), independent of cascade. Wire it now. |
| **P2** | **Goldilocks lane extraction** | 2-3 days | Lane exists, just needs extraction. Separate from pipeline. |
| **P3** | **Cascade v1** | 2-3 weeks | Highest impact architectural fix. But only after CLI validation confirms baseline. |
| **P4** | **Trajectory research spike** | 8h + 30d shadow | Lowest impact, highest effort. Only worth doing after baseline and cascade. |

---

## What We Kill (First-Principles)

| Item | Why Kill |
|------|----------|
| Goldilocks ML model | Already killed. Scope creep. |
| Trajectory confirmation gate | Already killed. Scope creep. |
| Agreement boost (+0.05) | Redundant with ensemble fraction. Killed by panel. |
| Full Bayesian trajectory | Overkill at this data scale. Killed by panel. |
| LLOP flat fusion | Wrong architecture. Killed by panel. |
| 8 dead signals from Phase 2 | Sweep confirmed none beat baseline. |
| 13 Phase B legacy items | GEFS pipeline replaces the old alert-path system. |