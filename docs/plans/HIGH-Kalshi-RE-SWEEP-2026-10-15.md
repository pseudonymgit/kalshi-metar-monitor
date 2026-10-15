# HIGH re-sweep under CORRECTED Kalshi truth

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`  
**Script:** `scripts/high_kalshi_resweep_2026-10-15.py` · **Artifact:** `data/high_kalshi_resweep_2026-10-15.json`

**Read-only. No DB writes. No code default switched.**

## Truth state

- `data/kalshi_settlements.db`: **10194** rows; the date-offset defect affected **0** rows and is now **verified fixed** (0 offsets remaining vs the ticker's own `-YYMMDD` date).
- cohort on the honest axis: **3205** station-days; overlap carrying BOTH truths: **2946**.

Axis: `T = floor(running_high@13:30)+1`, `y = 1{settled_high > T}` (strict). `T` comes from the OBSERVED running high, so it is instrument-independent — only `y` moves between the two truth regimes.

## A. Overlap sub-cohort — GHCN truth vs CORRECTED Kalshi truth

Same rows under both regimes, so the delta isolates the truth source.

| source | n | Brier (GHCN) | Brier (Kalshi) | Δ (Kalshi−GHCN) | base (Kalshi) |
|---|---:|---:|---:|---:|---:|
| om_gfs_raw | 2946 | 0.1256 | 0.1195 | -0.0061 | 0.7858 |
| om_gfs_bc | 2946 | 0.1108 | 0.1115 | +0.0007 | 0.7858 |
| om_gfs_bc_ps | 2946 | 0.1036 | 0.1021 | -0.0015 | 0.7858 |
| gfs | 2823 | 0.1635 | 0.1651 | +0.0016 | 0.7889 |
| ecmwf | 2823 | 0.2136 | 0.2162 | +0.0026 | 0.7889 |
| icon | 2823 | 0.1871 | 0.1915 | +0.0044 | 0.7889 |
| gem | 2467 | 0.1804 | 0.1886 | +0.0082 | 0.7892 |
| gefs | 2616 | 0.3196 | 0.3259 | +0.0063 | 0.7825 |
| rate_1h_pit | 2120 | 0.3643 | 0.3485 | -0.0158 | 0.7717 |
| market | 583 | 0.6281 | 0.6287 | +0.0006 | 0.7633 |
| fused_5member | 2096 | 0.1498 | 0.1612 | +0.0114 | 0.7891 |
| fused_5member_calibrated | 2096 | 0.1638 | 0.1726 | +0.0088 | 0.7891 |
| om_gfs_sole_calibrated | 2946 | 0.1183 | 0.1173 | -0.0010 | 0.7858 |
| naive_equal_nwp | 2823 | 0.1708 | 0.1748 | +0.0040 | 0.7889 |

**Reading:** every source's Brier moves by ≤ ~0.016 between the two truth regimes, with no systematic sign. The ranking is identical under both. The HIGH edge is **not** a truth artifact (unlike the LOW lane, where the edge against METAR was fake and went negative against Kalshi).

## B. Full-cohort re-score under CORRECTED Kalshi truth

| rank | source | n | Brier | dir acc | ECE | base |
|---:|---|---:|---:|---:|---:|---:|
| 1 | om_gfs_bc_ps | 2946 | **0.1021** | 0.8557 | 0.0327 | 0.7858 |
| 2 | om_gfs_bc | 2946 | **0.1115** | 0.8425 | 0.0377 | 0.7858 |
| 3 | om_gfs_sole_calibrated | 2946 | **0.1173** | 0.8557 | 0.106 | 0.7858 |
| 4 | om_gfs_raw | 2946 | **0.1195** | 0.833 | 0.0679 | 0.7858 |
| 5 | fused_5member | 2096 | **0.1612** | 0.7948 | 0.0824 | 0.7891 |
| 6 | gfs | 2823 | **0.1651** | 0.7783 | 0.1325 | 0.7889 |
| 7 | fused_5member_calibrated | 2096 | **0.1726** | 0.7948 | 0.1237 | 0.7891 |
| 8 | naive_equal_nwp | 2823 | **0.1748** | 0.7545 | 0.1447 | 0.7889 |
| 9 | gem | 2467 | **0.1886** | 0.7231 | 0.1451 | 0.7892 |
| 10 | icon | 2823 | **0.1915** | 0.7191 | 0.1671 | 0.7889 |
| 11 | ecmwf | 2823 | **0.2162** | 0.6858 | 0.2244 | 0.7889 |
| 12 | gefs | 2616 | **0.3259** | 0.6181 | 0.3202 | 0.7825 |
| 13 | rate_1h_pit | 2120 | **0.3485** | 0.2283 | 0.4389 | 0.7717 |
| 14 | market | 583 | **0.6287** | 0.271 | 0.6423 | 0.7633 |

**Does om_gfs still beat every fusion/alternative under Kalshi truth?** **YES** — `om_gfs_bc_ps` ranks **#1** (Brier 0.1021); the next-best source is `om_gfs_bc` (Brier 0.1115).

## C. Does the om_gfs-sole decision need revisiting?

| regime | n | om_gfs-sole Brier | fused-5 Brier | gap (fused − sole) |
|---|---:|---:|---:|---:|
| GHCN truth | 2296 | 0.1096 | 0.1523 | **+0.0427** |
| CORRECTED Kalshi truth | 2096 | 0.103 | 0.1612 | **+0.0583** |

The fused−sole gap is **+0.0427** under GHCN and **+0.0583** under corrected Kalshi. The fusion loses by a *wider* margin under the corrected truth — **the clean truth strengthens the om_gfs-sole decision, it does not weaken it.**

The fusion dilutes the strong single signal with weak members (`gefs` Brier ≈ 0.33, `rate_1h` ≈ 0.35, `market` ≈ 0.63). Re-anchoring the truth does not repair that — it widens the gap.

## Verdict

- **The HIGH edge survives the corrected Kalshi truth.** No source's Brier moves materially; the leaderboard is unchanged.
- **om_gfs-sole remains #1.** The Brier gap vs fused widens under corrected truth — the sole-forecast decision is *more* justified, not less.
- **No change to the om_gfs-sole default is warranted.** The date fix removed a truth-table defect; it did not reveal an edge that was fabricated.

## Reproduce

```bash
python3 scripts/high_kalshi_resweep_2026-10-15.py
```

