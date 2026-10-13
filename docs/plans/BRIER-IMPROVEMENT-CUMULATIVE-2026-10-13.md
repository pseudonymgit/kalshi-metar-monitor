# Brier Improvement — Cumulative Framing (Item 5)

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Scope:** Brier improvement sprint
**Axis:** strike-crossing P(cross) at the 13:30 station-local pin, Brier vs canonical settled truth

---

## The question

Dan's directive: *"Squeeze the juice from the fruit. Run all the small tweaks that add
up to 1–2 pp Brier improvement — RTMA, timing, bias-corrected consensus, cumulative
framing."* This table is the honest accounting: what each standing tweak is actually
worth on the crossing axis, and whether they add to 1–2 pp.

---

## The table

| Source | Brier (current) | Brier (projected) | Gain (pp) | Confidence | Status |
|---|---:|---:|---:|---|---|
| **rate_1h (baseline / best single)** | 0.092 | 0.092 | — | high | live (unchanged) |
| raw local GFS (NWP path member) | 0.1597 | — | — | high | live |
| **↳ om_gfs substitution** (replaces raw GFS in NWP path) | 0.1597 | **0.1079** | **−5.2 pp** | med-high | ✅ built, **flag OFF** (`USE_OM_GFS_NWP`) |
| **↳ bias-corrected consensus** (mean/median/trim/inv-MAE on P(cross)) | 0.1346¹ | 0.1346–0.1588 | **+0.0 to +2.4 pp (WORSE)** | high | ❌ **TESTED — LOSES** (Item 3) |
| ↳ bias-corrected consensus (level MAE axis) | 1.116°F | 1.184°F | +0.07°F MAE (worse) | high | ❌ TESTED — loses (Workstream Q) |
| **RTMA / URMA nowcast anchor** | — | — | **0 (DO_NOT_WIRE)** | high | ❌ **TESTED** (Item 1) |
| **Market microstructure entry timing** | — | — | **0 (not actionable)** | high | ❌ **TESTED** (Item 2) |
| Calibration fix (p_fused saturation 49.7%→0%) | — | — | **no direct Brier test run** | med | ⚠️ built, unmeasured |
| Grade gates + exits (F7/F3) | — | — | **0 (selection, not forecast)** | high | 🟡 wiring pending |
| GEFSv12 reforecast | — | — | **unknown (needs collection)** | low | 🟡 collector prepared, not run (Item 4) |
| Slippage / cost term (F1) | — | — | **0 Brier** (P&L only) | high | 🟡 wiring pending |

¹ best single source in Item 3 (om_gfs, bias-corrected per-station), test set.

**Cumulative forecast-path Brier change from this sprint: `0.0 pp` on the crossing
axis** (net of the four tests: RTMA, timing, consensus, GEFSv12).

---

## What the sprint actually found

**This sprint produced zero verified crossing-axis Brier improvement.** Four
candidate levers were tested; none cleared the bar. That is the result — stated
plainly, per the register's own methodology rule ("numbers I quoted without verifying
were wrong three times in one week").

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | RTMA/URMA nowcast anchor | **DO_NOT_WIRE** | Brier 0.328 (pin) / 0.220 (hsf) vs rate_1h 0.082; within-station demeaned fcerr 0.77; level corr 0.85–0.96 → collinear, dominated |
| 2 | Market microstructure timing | **not actionable** | global median spread 0.01 (00–13Z) vs 0.02–0.03 (14–20Z); the 13:30 pin lands in the *widest* window; moving the pin there destroys the rate_1h signal |
| 3 | Bias-corrected consensus (crossing) | **LOSES** | best consensus 0.1588 vs best single 0.1346 = +0.0242; confirms Workstream Q on the money axis |
| 4 | GEFSv12 reforecast | **unmeasured** | collector prepared (5 members, not 31); needs a backfill before any Brier can be quoted |

---

## Where the 1–2 pp is (and isn't)

### It is NOT in aggregation or new now-state sources
Three independent tests (Workstream Q level, Item 3 crossing, Workstream I diversity)
now agree: **our sources share one forecast** (level corr 0.84–0.96). Averaging
correlated draws re-centres but does not reduce variance — it pulls the sharp source
toward the mis-calibrated members. Adding RTMA (another collinear now-state) or a
multi-source consensus both **increase** Brier. This is a settled result, stated three
different ways.

### It IS (partly) in the NWP substitution — already built
The **only verified crossing-axis Brier improvement in the register** is the om_gfs
NWP substitution: raw local GFS 0.1597 → om_gfs bias-corrected **0.1079 (−5.2 pp)**.
It is free, wired behind `USE_OM_GFS_NWP`, and **currently OFF**. This is a *member
swap* on the NWP path, not a diversity gain (om_gfs is collinear with rate_1h). It
moves the NWP member's quality; it does **not** move the decision-driving rate_1h.

### The largest unmeasured lever is calibration, not prediction
`p_fused` saturation (47.6% of trades at the 0.99 clip; realized win rate 67.6% →
~31 pt overstatement) is a **P&L** problem, not a Brier problem on the crossing axis.
No direct Brier test has been run for the calibration fix. If graded on `calibrated_p
vs market vs settled`, the fix is worth far more than 1–2 pp of *selection* — but that
is the EV axis, not the forecast-Brier axis, and it is unmeasured here.

---

## Bottom line

- **Cumulative crossing-axis Brier gain from Sprint items 1–4: 0.0 pp.** The "1–2 pp
  from small tweaks" thesis is **not supported** by these four tests. The tweaks that
  were supposed to add up do not add up, because the error is shared, not averaged away.
- **Single verified lever:** om_gfs substitution, **−5.2 pp on the NWP member**, built
  and flag-gated. Whether it moves P&L depends on how much of the decision rides on the
  NWP path vs rate_1h — and rate_1h dominates.
- **Two unknowns remain:** (a) GEFSv12 reforecast backfill → possible debias/analog
  gain (collector ready); (b) `p_fused` calibration → a P&L lever with no Brier test.
- **Recommendation:** stop hunting the forecast axis for pp that aren't there. Spend
  the next cycle on the **EV axis** (F1 slippage, F3 exits, F7 grades, calibration),
  where the register already shows the money is, and where the sprint gate
  (bullshit-crawl flagged <50%) can actually be moved.

> The fruit was squeezed. Four of the five pieces had no juice. The one that did —
> om_gfs — was already in the fridge, waiting on a flag flip (Dan's call).
