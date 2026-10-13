# RTMA/URMA Nowcast-Anchor Test — Item 1 (Brier improvement sprint)

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Status:** COMPLETE — verdict `DO_NOT_WIRE`
**Artifacts:** `artifacts/rtma-scope/rtma_probe_2026-08-01_2026-08-07.json`,
`artifacts/rtma-scope/rtma_probe_2026-08-15_2026-08-21.json`
**Script:** `scripts/rtma_nowcast_anchor_test_2026-10-13.py` (read-only on the live book)

---

## TL;DR

RTMA does **not** beat `rate_1h` as the "now state" — on either axis. It is worse by ~0.15 Brier
on the strike-crossing axis, worse on level, and its forecast errors are **not independent**
(level corr 0.84–0.96; demeaned fcerr 0.77–0.78). The direct access path is cheap (~20 min for a
full study-window pull), but the source does not merit a wiring. **Verdict: DO_NOT_WIRE.**

The one genuinely useful output is a **collection spec + working script** for the full
2026-04-21→2026-10-09 window, should a future question need an obs-model now-state.

---

## The endpoint (this is the whole point — RTMA is fetchable)

Historically RTMA was thought "too heavy" (grib2, ~700 MB/cycle). **Not true.** AWS Open Data
publishes RTMA 2.5 km as pre-indexed GRIB2 with a `.idx` sidecar; the 2-m temperature record is a
**fixed byte range**, identical across cycles (verified 03Z/13Z/17Z/23Z on 2026-10-09):

```
3:14980236:d=2026100917:TMP:2 m above ground:anl:
```

- **URL:** `https://noaa-rtma-pds.s3.amazonaws.com/rtma2p5.<YYYYMMDD>/rtma2p5.t<HH>z.2dvaranl_ndfd.grb2_wexp`
- **Fetch:** `Range: bytes=14980236-21065992` → **~6.1 MB / cycle** (not 85 MB file, not 700 MB)
- **Content:** the full CONUS 2.5 km grid (1597 × 2345) → **all 20 stations from one request**
- **Units:** Kelvin → `°F = K·9/5 − 459.67`
- **Cadence:** hourly (`t00z`…`t23z`), available back through 2013; 2026 dates present and current.
- **Decode:** `.venv-gefs/bin/python` + `cfgrib`/`eccodes` (already installed) → `xarray` dataset.
- **No auth, no token, no rate limit.** We spaced at ≥1.2 s/request anyway.

An even lighter plain-text product exists — `airport_temps/rtma2p5.FAA_T_stn_analysis_values.txt`
(21 KB, 501 stations incl. 19/20 of ours, 2-m temp in °C, most-recent cycle only). Useful for a
live sanity check, **not** for history (it is overwritten each cycle).

### Collection effort estimate (full study window)
2026-04-21 → 2026-10-09 = **172 days × 24 cycles = 4,128 cycles × 6.1 MB ≈ 25 GB** raw.
At the observed ~0.7 s/fetch + 1.2 s spacing ≈ **2.2 h wall**. Decode is the real cost
(1597×2345 float32 per cycle ≈ 75 MB in memory); sample-and-discard per cycle keeps RSS low.
A single daily cron backfilling 24 cycles/day would complete the window in ~7 months; a bulk
catch-up run is ~2–3 h. Both are practical. **Not started (per instruction).**

---

## Test design

- **Axis:** identical to the diversity study / rate_1h sweep — strict strike-crossing,
  `T = floor(ASOS high-so-far at the 13:30 station-local pin) + 1`, `y = 1 if settled > T`
  (`==` is a LOSS). Truth = canonical `settlement_truth` (HIGH, trustworthy=1).
- **Predictors** (P(cross) = Φ((μ−T)/σ)):
  - `rate_1h` — current best: `μ = hsf(13:30) + deflated rate_1h`, σ=1.5
  - `rtma_pin` — RTMA 2-m temp at the cycle nearest the pin, σ=4.0
  - `rtma_hsf` — max RTMA 2-m temp over cycles ≤ pin, σ=4.0
  - baselines: `hsf_only_s4`, `hsf_only_s1.5`, `rtma_plus_rate`
- **Windows:** two dense-ASOS weeks (the 1-min archive's `tmpf` is sparse in early Oct 2026;
  both probe weeks are ≥131/140 dense station-days).
  - Primary: **2026-08-01 → 08-07** (all 14 cycles 10Z–23Z, 139 station-days)
  - Confirm: **2026-08-15 → 08-21** (cycles 12–16Z, 135 station-days)

---

## Results

### Primary week — 2026-08-01 → 08-07 (139 station-days, base rate 0.87)

| Predictor | n | Brier | dir acc |
|---|---:|---:|---:|
| **rate_1h (current best)** | 128 | **0.0815** | 0.891 |
| rtma_hsf | 139 | 0.2197 | 0.648 |
| hsf_only (σ=4) | 139 | 0.2932 | 0.130 |
| rtma_pin | 139 | 0.3282 | 0.331 |
| hsf_only (σ=1.5) | 139 | 0.3762 | 0.130 |
| rtma_plus_rate (σ=1.5) | 120 | 0.5533 | 0.225 |

### Confirmation week — 2026-08-15 → 08-21 (135 station-days, base rate 0.87)

| Predictor | n | Brier | dir acc |
|---|---:|---:|---:|
| **rate_1h (current best)** | 121 | **0.0729** | 0.909 |
| rtma_hsf | 135 | 0.2153 | 0.667 |
| hsf_only (σ=4) | 135 | 0.2915 | 0.133 |
| rtma_pin | 135 | 0.3289 | 0.333 |

### Independence

| Metric | Primary | Confirm |
|---|---:|---:|
| fcerr rate_1h vs rtma_pin (pooled) | 0.702 | 0.663 |
| fcerr rate_1h vs rtma_pin (**within-station demeaned**) | 0.777 | 0.773 |
| level corr (rate μ vs RTMA, pooled) | 0.962 | 0.973 |
| level corr (within-station demeaned) | 0.849 | 0.839 |
| RTMA at-pin bias vs settled max | −3.97 °F | −3.99 °F |

---

## Interpretation

1. **RTMA loses on the crossing axis.** Brier 0.328 (at-pin) / 0.220 (hsf) vs rate_1h 0.073–0.082.
   Both probe weeks agree. The margin is not close.
2. **It is not the fix for hsf's conservatism.** `hsf` alone (the running high at the pin, σ=4) is
   Brier 0.29 — better than RTMA at-pin (0.33). RTMA's hourly analysis has a **~4 °F cold bias**
   against the settled max, and it lags the peak more than the raw ASOS running high does.
3. **It is structurally a lagging now-state, not a leading signal.** That is exactly the role
   `rate_1h` already fills, and rate_1h dominates because it is (a) at the settlement site and
   (b) carries the *rate of change*. RTMA adds an off-site, hourly, smoothed view of a quantity we
   already measure better with the 1-min ASOS.
4. **Independence is insufficient.** Pooled fcerr 0.66–0.70 clears the 0.85 line *superficially* —
   but only because RTMA's ~4 °F cold bias adds a constant offset. Within-station demeaned
   fcerr is **0.77–0.78**, and the level correlation is **0.84–0.96**. This is the same pattern the
   om_gfs study found: pooled fcerr understates collinearity; the level correlation is the tell.
5. **Combining does not help** (`rtma_plus_rate` 0.51–0.55 — worse than either alone), because
   naively summing a cold-biased analysis with the rate double-counts the level.

**Verdict: DO_NOT_WIRE.** RTMA is a now-state that is strictly dominated by the observation we
already hold. It is not a second degree of freedom.

---

## Lesson / process note

The "RTMA is too heavy to test" prior was **wrong** — the AWS pre-indexed byte-range path makes the
2-m temperature record a 6 MB fetch covering all 20 stations. **The blocker was never the data
volume; it was that nobody had read the `.idx` sidecar.** Future "this feed is too heavy" claims
should be falsified against the `.idx` before being used to defer a test.

---

## Collection plan (documented, NOT run)

If a future question needs an obs-model now-state anchor:

1. `scripts/rtma_nowcast_anchor_test_2026-10-13.py fetch --start <A> --end <B>` — pulls the
   TMP:2m byte-range for cycles 10Z–23Z into `/tmp/rtma_probe_cache` (re-fetchable).
2. `… score --start <A> --end <B>` — decodes, samples the 20 stations, scores vs settled truth.
3. For a **full history** pull: extend `CYCLE_HOURS` to `range(0,24)` and the window to
   `2026-04-21 → 2026-10-09`; ~4,128 cycles, ~25 GB temp, ~2–3 h. Cache is gitignored (`/tmp`).
4. Persist to a `data/rtma_nowcast.db` table `rtma_2m(station, cycle_utc, t2m_f)` only if a
   downstream study needs random access; the probe keeps everything in artifacts.

**Recommendation:** park. Revisit only if a future study needs a *third* independent now-state
(we currently have ASOS 1-min + MADIS mesonet, both closer to the site).
