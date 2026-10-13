# Honest Arsenal Re-evaluation — point-in-time re-score of everything

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Repo:** `prototypes/weather-engine-source`
**Trigger:** the ceiling refit (`9d0903f90`) proved the diversity study's rate_1h
scoring is **look-ahead** — it takes the window-MAX P(cross) over 13:00→16:45
(`RATE_MAX_MOD = 16:45`) while the decision pin is 13:30. Every downstream
conclusion that leaned on the 0.092 Brier is suspect. This re-scores the whole
arsenal on the honest axis.

**Method:** re-derive rate_1h as a **single point at the 13:30 station-local pin**
(local `mod == 810`; no bucket with `mod > 810` is ever read). Re-score all
sources on the identical strike-crossing axis: `T = floor(hsf@13:30)+1`, strict
label `crossed = settled_max > T`, canonical settlement truth, cohort
2026-04-21 → 2026-10-09. READ-ONLY on every book; no `trades` writes.

**Artifacts:** `artifacts/honest-arsenal/{rescore_stage1,omgfs_stage2,sweep_stage3}_2026-10-13.json`
**Scripts:** `scripts/honest_arsenal_{rescore,omgfs,sweep}_2026-10-13.py`

---

## Stage 1 — New honest ranking

Per-source, full coverage (not a common-row subset), on the honest axis:

| rank | source | n | **Brier** | ECE | reliability | resolution | mean p | range |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | gfs | 2960 | **0.1597** | 0.1446 | 0.0256 | 0.0277 | 0.677 | 05-01..10-09 |
| 2 | gem | 2520 | 0.1793 | 0.1524 | 0.0355 | 0.0161 | 0.674 | 05-23..10-09 |
| 3 | icon | 2960 | 0.1840 | 0.1778 | 0.0426 | 0.0202 | 0.649 | 05-01..10-09 |
| 4 | ecmwf | 2960 | 0.2080 | 0.2264 | 0.0663 | 0.0205 | 0.618 | 05-01..10-09 |
| 5 | gefs | 2740 | 0.3290 | 0.3243 | 0.1631 | 0.0038 | 0.613 | 04-21..10-09 |
| 6 | **rate_1h_pit (honest)** | 2311 | **0.3617** | 0.4689 | 0.2223 | 0.0189 | 0.332 | 04-21..09-17 |
| — | rate_1h (published window-max) | 2464 | *0.0921* | *0.1141* | *0.0231* | *0.0871* | *0.720* | *04-21..10-04* |
| 7 | market | 391 | 0.6247 | 0.6331 | 0.4522 | 0.0020 | 0.164 | 08-01..10-09 |

**Answer to the explicit question (Stage 1.4):** rate_1h is **not** the best single
at the decision point. Honest point-in-time rate_1h Brier ≈ **0.36**, which is
**worse than every NWP field** (gfs 0.16 … gefs 0.33). The 0.092 was entirely the
forward window (0.092 → 0.36 when the scan is bounded to the pin). The character of
the σ-swept estimate is unchanged from the refit: bounded rate_1h ≈ 0.33 at
σ=1.5 (that run's cohort guard) and ≈ 0.36 at the pin here — both far from 0.092.

- **Stage 1.5 — pairwise / "one forecast sampled five ways":** the NWP field
  stays highly collinear (as published), but the honest rate_1h is now **near-orthogonal
  to NWP on the level axis**: fcerr vs NWP = **0.16–0.30** pooled (0.22–0.29 within-station
  demeaned), versus the published **0.99**. The "one forecast sampled five ways"
  conclusion *survives for the NWP block*; it **no longer extends to rate_1h**, which
  was only collinear with NWP through the shared look-ahead.
- **Stage 1.6 — market orthogonality:** the market mid remains weakly correlated with
  the NWP field (|r| = 0.13–0.20; r = 0.199 vs the honest NWP-field mean) and is
  **weakly negative vs rate_1h** (r = −0.10 to −0.22). The market was never bad on
  *orthogonality*; it is bad on Brier because it is thin (n=391) and near-degenerate.
  Relative to the **collapsed** honest NWP/rate field it is now the most independent
  thing we hold — a real (if small-n) diversifier, not a better forecaster.

---

## Stage 2 — om_gfs re-validation under the honest baseline

Re-ran the om_gfs strike-crossing test with the honest point-in-time rate_1h
baseline (n=2311 common rows):

| source | n | Brier | dir_acc | ECE |
|---|---:|---:|---:|---:|
| rate_1h_pit (honest) | 2311 | **0.3617** | 0.1990 | 0.4689 |
| rate_1h_windowmax (reference) | 2311 | 0.0940 | 0.8910 | 0.1185 |
| om_gfs_raw | 2311 | 0.1418 | 0.7992 | 0.1066 |
| **om_gfs_bc** | 2311 | **0.1231** | 0.8256 | 0.0488 |
| om_gfs_bc (per-station bias) | 2311 | 0.1176 | 0.8256 | 0.0447 |

- criterion (a) `Brier(om_gfs bc) ≤ honest rate_1h`: **True** (0.1231 vs 0.3617)
- criterion (b) `fcerr < 0.85`: **True** (max |r| = **0.329**; was 0.992 vs window-max)
- **Doctrine flips: published `DO_NOT_WIRE` → honest `WIRE_AS_SECOND_MEMBER`.**
- The level-substitution claim (om_gfs preferred over raw local GFS) **still holds**
  (level MAE 1.148 F bc vs 2.095 F raw GFS).
- Any ranking flips? Yes — om_gfs now **beats the baseline** on the honest axis, so
  the "it doesn't beat rate_1h" premise is void. The honest blend test wants **w_om = 1.0**
  (om_gfs alone beats any rate_1h-inclusive blend).

**Caveat (do not over-claim):** the honest rate_1h is a *provably weak* member
(Brier 0.36, dir 0.20). "om_gfs is now the best single" is true on this axis, but the
om_gfs sigma/correction are fit on the same window (mild optimistic knob, disclosed in
the original test) and the honest rate_1h cohort shrinks to 04-21..09-17. The robust
claim is: **the om_gfs-vs-rate_1h verdict was look-ahead-driven and must not be quoted
as DO_NOT_WIRE.**

---

## Stage 3 — Full suite + honest sweep + calibration

**1. Test suite:** `1673 passed, 10 failed` (1683 collected; pytest's summary line is
suppressed by repo `addopts`, counts verified by terminal reporter). The 10 failures
are **all pre-existing SBOX schema drift**, unrelated to this read-only work and not
fixed:
- `tests/test_booking_wiring.py` (2), `tests/test_f2_side_inversion.py` (5),
  `tests/test_trades_uniqueness.py`/`test_trade_uniqueness.py` (3)
- root cause: `scripts/sbox_intraday_ev_bridge.py:413` →
  `sqlite3.OperationalError: table trade_forecaster_probs has no column named calibrated_p`
  (a pending `p_market`/`calibrated_p` migration, not applied to the test fixture).

**2. Honest pin-anchored rate-window sweep** (every window ends at `mod == 810`,
past-window only). Published sweep scored W=1..6 at UTC grid hours on a full-day label,
so its 60.2pp @18Z was a decision-time artifact:

| W | n | sd(rate) | sep @ ±1.0 °F | 95% CI | normalized 15/15 sep |
|---:|---:|---:|---:|---|---:|
| 1 | 13742 | 1.51 | 38.19 | [33.98, 42.18] | 36.72 |
| 2 | 13190 | 2.45 | 41.77 | [37.19, 46.07] | 41.22 |
| 3 | 12618 | 3.64 | 55.40 | [49.22, 60.87] | 41.29 |
| 4 | 11919 | 4.76 | 61.32 | [55.95, 66.62] | 40.08 |
| 5 | 11363 | 5.11 | 62.33 | [57.44, 67.48] | 39.18 |
| 6 | 10837 | 4.91 | 60.69 | [55.52, 64.91] | 37.70 |

- **Fixed ±1.0 °F:** W=5 looks best (62.3) but this is the **scale confound**
  (sd grows 1.5 → 5.1 °F, so longer windows push more samples past a fixed threshold).
- **Normalized (scale-free) 15/15 split:** **W=1 does NOT dominate** — W1=36.7,
  W2–W3 peak at ~41.3, then decay to 37.7 at W6. The spread is within boot CI.
- **Verdict:** the published "1 h dominates, no longer window wins" conclusion is
  **anchoring/scale-driven**. At the pin, all windows are ~37–41pp with a mild
  W2–W3 edge — a weak, not overwhelming, case for 1h. The 60.2pp separation was look-ahead.

**3. Honest calibration / ceiling of the point-time signal:** n=13986,
Brier **0.3758**, ECE **0.5004**, reliability 0.2892, resolution **0.0156**,
uncertainty 0.19 → ceiling(formula) = **0.825**, consistent with the refit's bounded
0.827 / single-point 0.857. **The honest decision-time ceiling is ~0.82–0.86, not 0.99.**

---

## What HOLDS

1. **The NWP block is ~one forecast sampled several ways** (gfs/gem/icon/ecmwf/gefs
   level + P(cross) collinearity) — survives; look-ahead did not create it.
2. **om_gfs is a better NWP than raw local GFS on the level axis** (MAE 1.15 vs 2.09 F)
   — survives; unaffected by the rate_1h look-ahead.
3. **The honest decision-time ceiling is ~0.82–0.86, not 0.99** — survives and is now
   independently reproduced three ways (refit bounded, refit single-point, Stage-3).
4. **The market mid is weakly/negatively correlated with the forecast field**
   (orthogonal, |r| ≤ 0.22) — survives; if anything it is now the most independent
   member since the NWP/rate field collapsed.
5. **The suite's 10 failures are pre-existing SBOX schema drift**, not caused by this work.

*Checked and excluded:* `docs/weather-engine/accuracy-numbers-cycles4-6.md`'s 0.092 is a
B-mode ensemble Brier on a different (unified-backtest) axis, not the diversity-study
rate_1h — **not** affected by the look-ahead.

## What BREAKS (register numbers that must be corrected)

Every headline that used the 0.092 window-max Brier or the 60.2pp separation:

| register headline | published | **honest** | where |
|---|---:|---:|---|
| rate_1h "best single" Brier | 0.092 | **0.36** (single-point) / ~0.33 (bounded σ=1.5) | PREDICTOR-DIVERSITY, BRIER-IMPROVEMENT, EXPERT-* |
| rate_1h resolution / ceiling | 0.087 / **0.99** | **0.016** / **~0.83** | CEILING-REFIT, PREDICTOR-DIVERSITY |
| "rate_1h best single survives" (ASOS-local-date doc) | YES | **NO** — rate_1h ≈ worst of the field | ASOS-LOCAL-DATE-VERIFICATION:106 |
| rate_1h-vs-NWP fcerr | 0.99 | **0.16–0.30** | om_gfs test, diversity study |
| om_gfs verdict | **DO_NOT_WIRE** | **WIRE_AS_SECOND_MEMBER** | BRIER-IMPROVEMENT row, om_gfs md |
| rate window: "1 h dominates, 60.2pp" | 60.22pp @18Z | **~37–41pp at pin; W1 does not dominate** | AI-NWP-INDEPENDENCE-WINDOW-SWEEP |
| "rate_1h = best single at decision time" (pin sweep note) | implied | **false** | PER-STATION-PIN-SCHEDULE |

**Corrected headline values for the register (use these):**
- rate_1h (honest, at the 13:30 pin): **Brier 0.36, resolution 0.019, ECE 0.47**.
- Best single source on the honest axis: **gfs 0.16**, then gem 0.18, icon 0.18, ecmwf 0.21.
- Honest decision-time ceiling: **0.82–0.86** (not 0.99).
- om_gfs(bc): **Brier 0.123 vs honest rate 0.362 → WIRE as second member**; level MAE 1.15 F.
- rate_1h vs NWP forecast-error correlation: **0.16–0.30** (not 0.99).
- Honest window sweep: **~37–41pp across W=1..6, W2–W3 mild peak** (not 60pp at W1).

## Look-ahead audit — other scripts reading future-window buckets

Found and **listed (not fixed)**:

1. `scripts/predictor_diversity_study_2026-10-13.py:74,387` — **source of the bug**:
   `RATE_MAX_MOD = 16*60+45` and `rate_nowcast()` scans `range(RATE_MIN_MOD, RATE_MAX_MOD+1)`,
   reading buckets up to 16:45 (and `_rate_and_roll` reads `mod+15` = 825 at the pin).
   Only affects the `rate_1h_p` column; all NWP/market columns are unaffected.
2. `scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py` — the W=1..6 sweep scored at
   UTC grid hours on a **full-day** label; the late-hour separation is inflated because
   the feature (high-so-far) and the label share the running max. Not a `RATE_MAX_MOD`
   copy, but the 60.2pp headline is anchoring-driven (corrected in Stage 3).
3. `scripts/rtma_nowcast_anchor_test_2026-10-13.py:307` — **re-implements** a local
   `rate_nowcast()` that also scans `13:00 .. 16:45`. Same look-ahead pattern; re-verify
   its baseline before quoting.
4. `scripts/pin_sweep_2026-10-13.py:61` — `SCAN_LO,SCAN_HI = 780,1005`; its `brier_reanchor`
   column is the literal look-ahead method (the doc already flags it anchor-dominated;
   confirm its `brier_fixedT` (bounded) column is the one quoted).
5. `scripts/ceiling_refit_2026-10-13.py:126` — reads 780..1005 **deliberately** as the
   look-ahead comparison; correct usage (it is the detector, not a victim).

No other scripts read buckets beyond their stated pin. `python-debugpy`/exec helpers
unaffected.

---

## Recommended next moves

1. **Freeze the register correction** — replace 0.092 / 0.99-ceiling / 60.2pp /
   DO_NOT_WIRE-om_gfs headlines with the honest values above across the 6 docs listed.
2. **Re-open the om_gfs wiring decision** (`USE_OM_GFS_NWP`, currently flag OFF) — under
   the honest baseline it is the *best* NWP member, not a rejected one. Re-run the
   bridge's own fused-Brier test, out-of-sample, before flipping the flag.
3. **Re-derive the ceiling** from the honest 0.82–0.86 bound — the ~0.75 floor is
   closer to correct than 0.99; do not raise it on the look-ahead resolution.
4. **Fix the source of the bug in `predictor_diversity_study`** (bound its scan to the
   pin, or add an explicit `--honest` mode) so no future study re-inherits it.
5. **Fix `rtma_nowcast_anchor_test` and the sweep's label anchoring** before those
   numbers are quoted again.
6. **Do not re-wire rate_1h into the bridge** on the strength of the 0.092 — that number
   was the look-ahead; the honest pin nowcast is a weak member.
7. **The market mid is the only genuinely independent member we hold** (|r| ≤ 0.22); if
   anything deserves a fresh look as a fusion input it is that — small-n caveat noted.

---

## Bottom line

The single number that carried the whole diversity/ceiling/window-sweep narrative —
rate_1h Brier **0.092** — was **look-ahead**. On the honest axis rate_1h is **0.36**,
*worse* than every NWP field, and the decision-time ceiling is **~0.82**, not 0.99.
The om_gfs `DO_NOT_WIRE` verdict and the "1 h dominates at 60pp" window verdict were
both artifacts of the same look-ahead and **flip** under honest scoring.
