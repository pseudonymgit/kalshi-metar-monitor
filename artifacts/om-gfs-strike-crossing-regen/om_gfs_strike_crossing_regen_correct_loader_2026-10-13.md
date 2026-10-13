# om_gfs strike-crossing validation — 2026-10-13

**Question:** does Open-Meteo `om_gfs` (bias-corrected GFS pipeline) carry an edge
on the STRIKE-CROSSING axis, or is it another copy of rate_1h? The aggregation
study's 1.240 F MAE was a LEVEL-axis result — it does not establish a crossing edge.

**Window:** 2026-04-21 → 2026-10-09 · 3440 grid station-days · read-only on the live book.
**Axis:** T = floor(high-so-far at 13:30 station-local pin) + 1 (climatology fallback if ASOS absent); label `crossed = settled_max > T (strict; == is a LOSS)`.

## Head-to-head (same common rows)

| source | n | **Brier** | dir_acc | ECE | resolution | mean_p |
|---|---:|---:|---:|---:|---:|---:|
| rate_1h | 2464 | **0.0921** | 0.8916 | 0.1141 | 0.0871 | 0.7202 |
| om_gfs_raw | 2464 | **0.1392** | 0.8019 | 0.1101 | 0.0428 | 0.6991 |
| om_gfs_bc | 2464 | **0.1182** | 0.8336 | 0.0462 | 0.0443 | 0.7825 |
| om_gfs_bc_per_station_bias | 2464 | **0.1128** | 0.8401 | 0.0394 | 0.049 | 0.7862 |
| gfs_raw_sigma4 (ref) | 2960 | **0.1597** | 0.7882 | 0.1446 | 0.0277 | 0.6775 |

Baselines: rate_1h study n=2464 → **0.0921**; same-window overlap → **0.1241**; recomputed common rows → **0.0921**.

## Independence (fcerr)

- level (pooled): **0.9924**
- level (within-station demeaned): **0.9881**
- P(cross) pooled: **0.6874**
- max |r| = **0.9924** (flag at 0.85) → collinear = **True**

## Decision

- criterion (a) Brier(om_gfs bc) ≤ rate_1h: **False** (0.1182 vs 0.0921)
- criterion (b) fcerr < 0.85: **False**
- **VERDICT: `DO_NOT_WIRE`**

## Bias / sigma

- global bias = **-0.6506 F**; global sigma = **1.5964 F**

## Level MAE (reproduction + swap win)

- om_gfs raw = 1.3304 F, bias-corrected = 0.6798 F
- raw local gfs = 2.0946 F  →  om_gfs(bc) − local gfs = **-1.4148 F**
- rate_1h mu level MAE = 0.7646 F (already tighter than any NWP)

## Blend test — does om_gfs add on top of rate_1h?

- common rows: 2464
- rate_1h alone = 0.0921; om_gfs(bc) alone = 0.1182
- best blend (w_om=0.25) = **0.0894** (-0.0027 vs rate_1h alone)
- paired Δ = -0.00269 ± 0.00098 (t=-2.761) — significant at 2s.e.: **True**, but w is in-sample selected.

## Verdict interpretation

- om_gfs(bc) is a better NWP than raw local GFS on BOTH axes: level MAE 1.4148 F better; strike-crossing Brier 0.1079 vs 0.1597.
- But it does NOT beat rate_1h's Brier (0.0921) and it is collinear with rate_1h (fcerr max |r| = 0.9924 ≥ 0.85), so it is **not an independent second member**.
- Verdict: **DO_NOT_WIRE**. The free win is on the LEVER/level axis (substitute om_gfs for raw local GFS); do not claim a diversity gain or wire it as a fusion member ahead of rate_1h.

