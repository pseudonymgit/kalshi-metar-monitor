# RTMA + om_gfs — Independence, Tested RIGHT (2026-10-14)

**Date:** 2026-10-14 · **Agent:** Gilfoyle · **Status:** COMPLETE — verdict `DO_NOT_WIRE` (RTMA) / `WIRE_FUSION` (om_gfs) — **confirmed, mechanism named**
**Script:** `scripts/rtma_independence_honest_2026-10-14.py` (read-only)
**Probe:** `artifacts/rtma-scope/rtma_probe_2026-07-28_2026-08-26.json` (NEW, 30 days) + the two 7-day probes
**Builds on:** `docs/plans/RTMA-HONEST-RECHECK-2026-10-14.md`, `docs/plans/RTMA-SCOPE-2026-10-13.md`
**Question answered:** Dan (2026-10-14) — *"We thought multiple signals were collinear. How do we
make sure this is truly collinear, or if it actually is independent?"*

---

## TL;DR

1. **The "0.96/0.99 collinear" headline was LEVEL correlation, not error correlation.** For RTMA the
   number quoted is explicitly labelled *level corr* (0.968/0.958 in the honest re-check). For om_gfs
   the published 0.99 is likewise `level_pooled`; the honest re-run's **error**-domain fcerr is
   **0.21**. These are different quantities, and only the error one bears on independence.
2. **On the right metric, RTMA's info is NOT collinear — and the correlation is REGIME-DEPENDENT.**
   Temperature-domain error correlation: **0.47 (primary week) → 0.72 (confirm week) → 0.57 (30-day)**.
   The 0.72 breaches Dan's 0.70 residual bar in the confirm week. So "collinear" is *sometimes* true,
   which is exactly why a thin window gave a false absolute.
3. **Extending the window to 30 days did NOT rescue RTMA.** Honest rate_1h stays the best predictor
   (Brier 0.083 vs rtma_hsf 0.227 vs rtma_pin 0.336); RTMA rescues only ~36% of rate_1h's misses.
   The DO_NOT_WIRE verdict **stands** — but the honest reason is **strict dominance**, not collinearity.
4. **om_gfs is the opposite case and the honest re-verification CONFIRMS the WIRE.** Its error fcerr
   is 0.21 (< 0.85) and its Brier (0.123) beats honest rate_1h (0.362). It was never collinear; the
   0.99 was a level. The `om_gfs_nwp.py` docstring still carries the stale *"COLLINEAR (fcerr 0.99)"*
   claim and should be corrected.

---

## The methodological trap (why the prior conclusion was fragile)

| Metric | What it measures | RTMA (confirm wk) | om_gfs | Meaning |
|---|---|---:|---:|---|
| **Level corr** | do the two *values* track together | 0.958–0.973 | 0.992 | **Meaningless** — all temperatures correlate. This is what "0.96/0.99 collinear" quoted. |
| **fcerr via `pearson(P(x)−y, Q(x)−y)`** | do the two bounded *probability* residuals move together | 0.66–0.73 | 0.69 | **Compressed & regime-sensitive**: for a base rate of 0.85, both models assign ~0.9 to the common positive cases, so the correlation is dominated by the ~15% hard cases. |
| **Temperature-domain error corr** | do the two *Fahrenheit* errors move together | **0.47–0.72** | ~0.21 | **The right axis.** Unbounded, not floor/ceiling-compressed. |

**Two degenerate tells to avoid:**
- On the P(cross) surface, RTMA's residual `P−y` has a **single sign** on every window (RTMA is
  under-confident on essentially every positive case). A residual correlation is then undefined/
  degenerate as an independence measure — it has no contrast. (Confirmed: `sign-disagree` 0.03–0.11,
  i.e. the two errors almost never flip sign, but that is an artefact of the shared *level*, not of
  shared *information*.)
- The within-station **demeaned** fcerr (0.83–0.85) sits right on the 0.85 flag and flips sign across
  windows — a metric sitting on its own threshold across regimes is a red flag, not a verdict.

---

## The honest test (Dan's decision rule)

> RTMA is a useful second member iff **(a) fcerr < 0.85 AND (b) residual (temperature) corr < 0.70.**

Ran on three windows. "fcerr" = within-station demeaned level corr (the study's own standard);
"resid" = within-station demeaned **temperature-domain** error corr; "sign-dis" = fraction of days
where the two predictors err in *opposite* directions (0.5 = fully independent).

| Window | n | fcerr(dem) | resid(dem) | sign-dis | (a) | (b) | verdict |
|---|---:|---:|---:|---:|:-:|:-:|:--|
| **30-day 07-28→08-26** (NEW) | 478 | 0.949 | **0.566** | 0.094 | ✗ | ✓ | DO_NOT_WIRE |
| 7-day primary 08-01→08-07 | 120 | 0.934 | 0.473 | 0.075 | ✗ | ✓ | DO_NOT_WIRE |
| 7-day confirm 08-15→08-21 | 110 | 0.957 | **0.715** | 0.109 | ✗ | ✗ | DO_NOT_WIRE |

**Reading:** criterion (a) fails on every window (fcerr is fundamentally a level in disguise).
Criterion (b) **passes on the primary + 30-day windows but FAILS on the confirm week** — the sign
that the residual correlation is *regime-dependent* (0.47 ↔ 0.72). Neither criterion, on any window,
gives a "wire."

### Raw honest scores (30-day, n=562 station-days)

| Predictor | n | Brier | dir acc |
|---|---:|---:|---:|
| **rate_1h** | 511 | **0.0834** | 0.902 |
| rtma_hsf | 562 | 0.2272 | 0.626 |
| rtma_pin | 562 | 0.3355 | 0.329 |
| hsf_only (σ=4) | 562 | 0.2907 | 0.144 |
| rtma_plus_rate | 478 | 0.5382 | 0.257 |

- **Murphy decomposition (30-day):** rate_1h RES=0.0735 vs rtma_hsf RES=0.0208 — rate_1h has
  **3.5×** the resolution. RTMA's low Brier comes from reliability + the high base rate, not skill.
- **Complementarity:** on the 47 days rate_1h misclassifies, rtma_hsf rescues only **36%**. On the
  431 days rate_1h is right, rtma_hsf agrees ~66%. It adds little on the cases that matter.

---

## om_gfs re-verification — the claim flips the *other* way

Dan asked to re-verify the om_gfs "collinear with rate_1h (0.99)" the same way. It **fails the
collinearity claim**:

| Metric | Value | Verdict |
|---|---:|---|
| Published `fcerr.level_pooled` (strike-crossing study) | **0.9924** | level — meaningless |
| Honest **error**-domain fcerr (=`honest_arsenal_omgfs_2026-10-13.py`) | **0.21** | **< 0.85 → NOT collinear** |
| om_gfs(bc) Brier vs honest rate_1h | 0.123 vs 0.362 | om_gfs **wins** |

The honest re-run (`artifacts/honest-arsenal/omgfs_stage2_2026-10-13.json`) computes fcerr on the
**error** domain (`rate_1h_pit_mu − settled_max` vs `om_gfs − settled_max`) and gets 0.212 pooled /
0.177 demeaned. So when the study fixed the rate_1h baseline (look-ahead → point-in-time), the
collinearity **disappeared** and the verdict flip-flopped to WIRE — which is exactly the instability
Dan is pointing at. The **current** state (`OM_GFS_FUSION_FLAG`, default OFF) is correct; the stale
docstring is not.

**Action item (non-blocking):** correct `core/intraday/om_gfs_nwp.py` lines 15–17 —
*"COLLINEAR with the rate_1h nowcast (fcerr 0.99 ≥ 0.85)"* → *"level-correlated 0.99 (meaningless
for independence); error-domain fcerr 0.21 → independent; wired as a fusion member."*

---

## Why the verdict is still DO_NOT_WIRE (the honest reason)

Not collinearity — **strict domination on every axis**:
- rate_1h beats rtma_hsf by **0.14 Brier** (0.083 vs 0.227) and rtma_pin by **0.25**.
- RTMA is an **off-site, hourly, smoothed** analysis with a **−3.9 °F cold bias**; rate_1h is
  at the settlement site and carries the rate of change.
- The 50/50 blend (0.138) sits between the members → it is averaging a worse view with a better one,
  not adding orthogonal information.

RTMA's information is *partly* complementary (resid 0.57 < 0.70 on 2 of 3 windows) but the amount is
too small and too unreliable to earn a wire when the incumbent is already 0.14 Brier better.

---

## What would change the verdict

1. **A third now-state closer than RTMA's 2.5 km grid** (MADIS mesonet is already closer) — not RTMA.
2. **A regime-conditioned member.** The resid correlation flipping 0.47↔0.72 means RTMA might be a
   *conditional* member (e.g. only in weak-synoptic / ridge regimes). Worth one bounded test **only**
   if a downstream study specifically needs it — not a standalone wire.

---

## Process note (the honest-axis lesson, restated)

- **Always name the axis.** "Correlated 0.96" without "level / error / probability-domain" is not a
  finding. Level correlation between two temperature views is ~guaranteed.
- **Longer windows exposed instability, not a new answer.** The 30-day run was worth doing precisely
  because the confirm week's resid (0.715) broke the primary week's (0.473): a single-window
  "independent" claim would have been over-fit to a regime.
- **A metric that sits on its own threshold across windows is not a verdict.** Demeaned fcerr
  (0.83–0.96) hovered at the 0.85 line; the temperature-domain residual (0.47–0.72) is the metric
  with actual discriminating power.

## Reproduction

```bash
# 1. fetch the 30-day probe (AWS byte-range; ~7 min; cache gitignored in /tmp)
.venv-gefs/bin/python scripts/rtma_nowcast_anchor_test_2026-10-13.py all \
    --start 2026-07-28 --end 2026-08-26
# 2. the honest independence test over ALL probe windows
python3 scripts/rtma_independence_honest_2026-10-14.py
# -> artifacts/rtma-scope/rtma_independence_honest_2026-10-14.json
```
Window chosen deliberately: 2026-07-28→08-26 sits inside a contiguous **63-day dense-ASOS run**
(2026-06-25→08-26) so rate_1h is computable every day; it contains both prior 7-day probe weeks.
