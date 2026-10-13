# Rate Window Sweep 1h–4h — HONEST (point-in-time) — 2026-10-13

**Script:** `scripts/rate_window_sweep_pointintime_2026-10-13.py`  
**Machine artifact:** `artifacts/rate-window-sweep/rate_window_pointintime_2026-10-13.json`  
**Axis:** strict strike-crossing, `T = floor(high-so-far at pin)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN settlement.

## Why this supersedes the prior sweep

The prior multi-window sweep scored `rate_1h` with a **window-max scan over 13:00–16:45** — it looked *ahead* of the decision instant and inflated the W=1 separation to 60.2 pp. This sweep is **past-only**: for window W,

```
rate_W = mean(tmpf of the W hours ending at the pin)
       - mean(tmpf of the W hours immediately before that)
```

Every bucket used is `mod <= pin`; one prediction per station-day per window per pin. No window-max, no future buckets.

**Cohort:** 2026-04-21 → 2026-10-09 (the diversity-study window), canonical truth required.
**Bucket source:** `cache:/tmp/we_asos_recs_2026-10-13.pkl.gz`.

## Pin 13:30 station-local

| W | n | base P(cross) | Brier(base) | sep ±0.5 (pp) | 95% CI | sep ±1.0 (pp) | 95% CI | norm sep ±1.0 (pp) | LOO Brier(±1.0) | dir acc |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|
| 1 | 2248 | 0.7998 | 0.1601 | 48.72 | [40.17, 57.02] | 55.81 | [46.14, 65.07] | 45.05 | 0.1362 | 0.8149 |
| 2 | 2132 | 0.7983 | 0.161 | 51.33 | [42.07, 60.0] | 55.63 | [44.87, 65.68] | 45.94 | 0.1438 | 0.8129 |
| 3 | 2054 | 0.7984 | 0.1609 | 46.3 | [32.27, 60.06] | 54.49 | [38.74, 68.83] | 41.23 | 0.1529 | 0.7931 |
| 4 | 1931 | 0.8001 | 0.1599 | 58.64 | [41.24, 74.04] | 56.54 | [38.01, 73.83] | 35.29 | 0.1533 | 0.8105 |

## Pin 14:30 station-local

| W | n | base P(cross) | Brier(base) | sep ±0.5 (pp) | 95% CI | sep ±1.0 (pp) | 95% CI | norm sep ±1.0 (pp) | LOO Brier(±1.0) | dir acc |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|
| 1 | 2276 | 0.6845 | 0.2159 | 41.49 | [35.16, 47.31] | 48.97 | [41.27, 56.43] | 50.81 | 0.1912 | 0.7087 |
| 2 | 2138 | 0.6819 | 0.2169 | 40.5 | [32.52, 48.15] | 45.12 | [36.45, 53.11] | 53.12 | 0.1941 | 0.7329 |
| 3 | 2039 | 0.6788 | 0.218 | 43.91 | [33.92, 53.56] | 48.12 | [36.74, 58.52] | 48.52 | 0.2082 | 0.7023 |
| 4 | 1961 | 0.6828 | 0.2166 | 45.9 | [31.62, 59.0] | 49.5 | [35.02, 62.48] | 39.8 | 0.2096 | 0.7022 |

## Pin 15:00 station-local

| W | n | base P(cross) | Brier(base) | sep ±0.5 (pp) | 95% CI | sep ±1.0 (pp) | 95% CI | norm sep ±1.0 (pp) | LOO Brier(±1.0) | dir acc |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|
| 1 | 2258 | 0.6001 | 0.24 | 33.69 | [28.12, 39.19] | 38.45 | [31.61, 45.15] | 40.41 | 0.2199 | 0.6236 |
| 2 | 2141 | 0.5983 | 0.2403 | 33.81 | [26.61, 40.78] | 36.2 | [28.02, 44.14] | 50.88 | 0.2238 | 0.6618 |
| 3 | 2027 | 0.592 | 0.2415 | 32.84 | [23.63, 41.48] | 34.17 | [23.3, 44.44] | 44.74 | 0.2343 | 0.6265 |
| 4 | 1943 | 0.5944 | 0.2411 | 38.39 | [26.66, 49.16] | 37.78 | [24.39, 50.67] | 39.52 | 0.2362 | 0.6176 |

### Reference: published-style W=1 point rate at the 13:30 pin

`rate = bucket[13:30] − mean(4 prior buckets)` — the published W=1 definition (point vs prior hour), for comparison with the symmetric W=1 row.

- n = 2311; ±0.5 sep = 39.49 pp; ±1.0 sep = 48.95 pp

## Verdict

At the 13:30 pin the honest ±1.0 separations are W=1 +55.81 pp, W=2 +55.63 pp, W=3 +54.49 pp, W=4 +56.54 pp. They are **statistically indistinguishable** — every CI overlaps (union of CIs spans [38.0, 73.8] pp; the widest single CI is W=4). The point-estimate 'winner' (W=4) is noise, not a real ordering at this n.

On the **calibration** axis (leave-one-date-out Brier / dir acc) the windows slightly favour the *shorter* windows: W=1 Brier 0.1362/dir 0.815, W=2 Brier 0.1438/dir 0.813, W=3 Brier 0.1529/dir 0.793, W=4 Brier 0.1533/dir 0.810. W=1 is lowest-Brier.

The scale-normalized separation (top-15% vs bottom-15% of each window's own distribution) shows the dilation dilution: W=1 +45.05 pp, W=2 +45.94 pp, W=3 +41.23 pp, W=4 +35.29 pp. W=2 is highest, W=4 lowest.

**Bottom line for Dan:** at the honest (past-only, point-in-time) axis **no longer window becomes newly viable** — W=1..4 are a near-tie at the 13:30 pin, with W=1 still preferred on calibration (lowest LOO Brier, highest dir acc). The prior 60.2 pp W=1 *dominance* was a look-ahead artifact; stripping it neither makes a longer window win nor invalidates W=1. **Keep W=1; nothing new to wire.** At later pins (14:30/15:00) the base rate and separation both fall (more of the day is already observed), so a later pin is not a rescue.


## Reproduce

```bash
.venv-gefs/bin/python scripts/rate_window_sweep_pointintime_2026-10-13.py
```

