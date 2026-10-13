# PWS-Augmented Trajectory Test — 2026-10-13

**Task 1 of the "build toward 6h" workstream.** `rate_1h` stays at a 1h window
(confirmed best in the ai-nwp window sweep, commit `e22e9d61e`). This test asks
whether dense personal-weather-station (PWS) observations can improve the
trajectory *signal* — lower variance, earlier divergence — so the entry-price
gradient can later be pushed further out.

## TL;DR

- **Wiring the PWS blend standalone is NOT justified — but the reason matters.**
  PWS adds *no new information* at the 1h-rate axis (correlation with ASOS
  0.64–0.85 across stations; the blend merely smooths), yet it is **not
  redundant at the level axis**: nearby PWS are often several °F off ASOS, and
  the feed is **free, keyless, and already exists** in NOAA MADIS.
- **The Wunderground PWS API is a dead end for this:** it returns `401` without
  a key, and even with a free key it is *current-observation only* — no 7-day
  history. Do not build on it.
- **MADIS LDAD mesonet is the real winner:** free, no key, NetCDF, hourly,
  ~6-day rolling history, and it *already aggregates* the same citizen networks
  (APRSWXNET/CWOP) plus university/DOT mesonets. This is the PWS network, sans
  the marketing.
- **Recommendation: PARK for the rate axis, but the feed is worth a follow-up
  test on the *level/nowcast* axis** (a microclimate-divergence signal), not the
  rate axis.

## What was tested

- **Stations (5 of 20):** KLAX, KATL, KDEN, KSEA, KMIA.
- **Window:** every hourly MADIS mesonet file currently published
  (`2026-10-08 05Z` → `2026-10-13 07Z`, **122 hours**).
- **Nearby PWS:** temperature-reporting stations within **5 mi** (widened to
  12 mi only if <2 pass a 50% coverage test).
- **Series:** hourly mean °F — ASOS from `data/iem_asos_1min.db`; PWS mean from
  MADIS.
- **rate_1h:** `T(h) − T(h−1)` (the PWS feed is hourly, so this is the 1-h rate
  at hourly resolution).
- **Variants:** ASOS alone (baseline) · PWS mean · PWS de-biased · 50/50 blend.

## Why not the Wunderground API

| Path | Result |
|---|---|
| `api.weather.com/v2/pws/observations/current` | **401** without a key; `"apikey is not authorized for this product"` |
| WU free tier | current-observation only; **no historical series** — cannot build a 7-day rate |
| MADIS LDAD mesonet | **200, no key, hourly NetCDF, ~6-day rolling** |

The task brief asked: *if you can't get real PWS data without a key, document
what the free tier offers and whether a free account would unlock it.* Answer:
a free WU key unlocks **current** PWS observations only. It does **not** unlock
the history needed for a rate_1h test. MADIS already gives the same station
population (CWOP feeds MADIS) with history, free.

## Results

Station counts are PWS passing the 5-mi / ≥50%-coverage filter.

| Station | PWS | rate sd ASOS | rate sd PWS | rate sd blend | corr(ASOS,PWS) | mean abs div |
|---|---|---|---|---|---|---|
| KLAX | 11 | 1.247 | 1.234 | **1.134** | 0.642 | 0.809 |
| KATL |  2 | 1.885 | 1.799 | **1.760** | 0.845 | 0.777 |
| KDEN | 18 | 3.228 | 2.671 | **2.770** | 0.779 | 1.564 |
| KSEA |  7 | 1.619 | 1.665 | **1.539** | 0.744 | 0.810 |
| KMIA |  6 | 1.311 | **0.756** | 0.957 | 0.665 | 0.764 |

### Interpretation

1. **Variance:** the PWS feed *is* smoother than ASOS in 4/5 (KDEN −17%,
   KMIA −42%, KATL −5%, KLAX −1%); KSEA slightly *worse* (+3%). But smoothing
   comes from spatial averaging over several sites — it is not new signal.
2. **Correlation 0.64–0.85** vs ASOS. Not 0.95+ (so not a pure copy), but far
   from independent. At the **rate** axis, PWS mostly re-expresses ASOS momentum
   with extra noise.
3. **Microclimate divergence is real:** mean |Δrate| 0.76–1.56 °F/h. KDEN is
   the standout (1.56 °F/h) — the airport sits on the plains while nearby
   stations are in the urban/elevated corridor. That divergence is a *level*
   phenomenon, not a rate phenomenon.
4. **De-biasing changed nothing** (corr identical) — the offsets are small
   relative to hourly rate movement, confirming the split is not a bias artifact.

## Verdict

**Does the PWS-augmented rate_1h improve the trajectory signal? No — not on the
rate axis.** The blend smooths, but the smoothed series is a function of ASOS
momentum (corr ≥0.64) plus averaging noise; there is no evidence of an
independent rate-of-change signal that would push the entry-price gradient out.

**Is it worth wiring?** Not for `rate_1h`. **Yes for a separate follow-up test**
on the *level/nowcast* axis: the MADIS mesonet feed is free, keyless, hourly,
and already the PWS network — the right use is (a) a **microclimate-divergence
signal** (does the spread between ASOS and its nearby PWS cluster lead the
airport temperature?), and (b) a **nowcast anchor** for the current-hour
trajectory. Both are distinct from the rate family and should be pre-registered
as their own signal, not folded into `rate_1h`.

### Concrete follow-up (not run here)

1. Pre-register a `pws_microclimate_divergence` signal: `ASOS − median(nearby
   PWS)` over 12 mi, with a lead/lag cross-correlation vs ASOS 1h-forward ΔT.
2. Data source: MADIS `LDAD/mesonet/netCDF` (free) — poll hourly, retain ~6 days
   rolling; the same LDAD directory also carries `rwis` (road/pavement temp,
   useful for surface-state) and `coop`.
3. Cost: $0. Cadence 1 h (not 15 min — MADIS mesonet is hourly; the 1-min ASOS
   archive remains the only sub-hourly source).

## Reproduction

```bash
.venv-gefs/bin/python scripts/pws_trajectory_test_2026-10-13.py fetch
.venv-gefs/bin/python scripts/pws_trajectory_test_2026-10-13.py analyze
# cache: data/_pws_madis_cache/pws_rows.json (gitignored, disposable)
# artifact: artifacts/pws-trajectory/pws_trajectory_test_2026-10-13.json
```

Resource discipline: each 33 MB MADIS file is parsed **in memory and
discarded**; only the ~13 MB per-station extraction is cached. No temp DBs.
Read-only on the live book. Nothing wired.

## Caveats

- **5-day window.** MADIS keeps ~6 days rolling; a longer test needs their
  archive (hourly, heavier) or a standing collector.
- **Hourly, not 15-min.** The MADIS mesonet feed is hourly, so this tests the
  rate at hourly resolution — the production `rate_1h` family is 15-min-grid.
  Conclusions about *variance* transfer; a 15-min PWS study is not possible from
  this feed.
- **Siting/radiation bias.** Citizen stations are unshielded in the worst cases;
  MADIS QC flags but does not filter. The de-biased variant bounds the effect.
- One file (`20261011_0900.gz`) failed with a transient "Network is unreachable"
  and was skipped; 122/123 hours remain.
