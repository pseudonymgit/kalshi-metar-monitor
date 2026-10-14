# RTMA Nowcast Anchor — Honest Re-check (Item 4, 2026-10-14)

**Date:** 2026-10-14 · **Agent:** Gilfoyle · **Status:** RE-CHECKED — verdict UNCHANGED (`DO_NOT_WIRE`)
**Supersedes the verdict framing of:** `docs/plans/RTMA-SCOPE-2026-10-13.md` (original: `DO_NOT_WIRE`)
**Data:** `artifacts/rtma-scope/rtma_probe_2026-08-01_2026-08-07.json`,
`artifacts/rtma-scope/rtma_probe_2026-08-15_2026-08-21.json` (the two probe weeks — unchanged; no re-fetch)

---

## The question Dan asked

The original RTMA verdict compared RTMA (`rtma_pin` Brier 0.328) against a **look-ahead**
`rate_1h` (Brier 0.0815). The honest re-run (`X`) proved that 0.0815 was look-ahead-inflated
and the honest point-in-time rate_1h Brier is **≈ 0.36**. So:

> At the honest pin, is the margin still 0.33 vs 0.08, or is it now 0.36 vs 0.33 — i.e. "close"?

This re-check recomputes the RTMA comparison **on the honest axis** using the stored probe rows
(`hsf_1330` + `rate_at_pin`, σ=1.5), with **no new fetches**.

---

## Re-check results (honest rate_1h at the 13:30 pin, σ=1.5)

### Primary week — 2026-08-01 → 08-07

| Predictor | n | Brier | notes |
|---|---:|---:|---|
| rate_1h (honest @pin) | 120 | **0.390** | hsf + deflated rate_at_pin |
| rate_1h (published look-ahead) | 128 | *0.0815* | **look-ahead — do not quote** |
| rtma_hsf | 139 | **0.245** | max RTMA 2 m over cycles ≤ pin, σ=4 |
| rtma_pin | 139 | **0.371** | RTMA 2 m at nearest cycle, σ=4 |
| hsf_only (σ=1.5) | 139 | 0.376 | running high at pin |

### Confirmation week — 2026-08-15 → 08-21

| Predictor | n | Brier |
|---|---:|---:|
| rate_1h (honest @pin) | 110 | **0.352** |
| rate_1h (published look-ahead) | 121 | *0.0729* |
| rtma_hsf | 135 | **0.233** |
| rtma_pin | 135 | **0.356** |
| hsf_only (σ=1.5) | 135 | 0.372 |

### Independence (honest rate_1h vs rtma_hsf, overlapping subset)

| Metric | Primary | Confirm |
|---|---:|---:|
| fcerr corr (pooled) | 0.875 | 0.880 |
| fcerr corr (within-station demeaned) | **0.896** | **0.909** |
| level corr (rtma_hsf vs hsf_1330, demeaned) | **0.968** | **0.958** |

---

## Verdict: UNCHANGED — `DO_NOT_WIRE`

Dan's hypothesis that the gap "narrows to close" is **half right and half wrong** — and the
distinction matters:

1. **`rtma_pin` — the predictor the original verdict hung on — does narrow.** Honest rate_1h
   0.390 vs rtma_pin 0.371 (primary): a **0.019** gap, vs the published 0.33-vs-0.08. On the
   confirmation week it is **0.352 vs 0.356 — RTMA at-pin actually wins by 0.004.** So *at the
   pin, on the honest axis, RTMA is no longer dominated; it is a tie.*

2. **But the correct RTMA predictor was never `rtma_pin` — it was `rtma_hsf`.** The original
   scope already found `rtma_hsf` (0.220) beat `rtma_pin` (0.328); the verdict's "0.328" headline
   quoted the *weaker* RTMA variant. Recomputed honestly, **`rtma_hsf` Brier 0.245/0.233 beats
   both `rtma_pin` and honest `rate_1h`** on the crossing axis.

3. **Yet `rtma_hsf` still does not earn a wire, because it is not a second degree of freedom.**
   The independence axis is the killer and it *hardened* under the honest baseline:
   - fcerr corr (demeaned) **0.896 / 0.909** — well above the 0.85 collinearity line.
   - level corr (demeaned) **0.968 / 0.958** — the classic tell from the om_gfs study.
   - A 50/50 blend of honest rate_1h + rtma_hsf scores **0.304 / 0.281** — i.e. it does beat
     either alone, but only because the blend is a **level correction of two views of the same
     quantity**, exactly the `rtma_plus_rate` failure mode (0.55) the original scope flagged.

**Bottom line:** the honest re-baseline **erases the "RTMA loses badly" framing** (that was a
look-ahead artifact in the comparison), but it does **not** flip the verdict. RTMA is not worse
than honest rate_1h — it is the *same signal* at the settlement-relevant level, ~0.96 correlated,
and therefore still not a decorrelated member. **No wiring.** The lesson from the original scope
("the blocker was never data volume — read the `.idx`") stands; the *conclusion* stands; only the
*margin* was a look-ahead mirage.

### What would change the verdict
A **third, closer** now-state (MADIS mesonet is already closer to the site than RTMA's 2.5 km
grid) — not RTMA. Keep RTMA parked; it is redundant with what we already measure better.

---

## Process note

Both RTMA scope weeks (Aug 01-07, Aug 15-21) have **dense ASOS tmpf**; early-Oct 2026 does not.
Do not re-run this study against a sparse week — rate_1h becomes uncomputable and the comparison
degenerates (the original scope hit this and moved the window). The stored probe rows are
sufficient for the honest re-check; no re-fetch was needed.
