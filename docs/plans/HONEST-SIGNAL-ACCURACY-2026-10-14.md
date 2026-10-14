# Honest Signal Accuracy — look-ahead-free re-benchmark (2026-10-14)

**Script:** `scripts/honest_signal_accuracy_2026-10-14.py`  
**Artifacts:** `data/honest_signal_accuracy_2026-10-14.json` (task deliverable) · `artifacts/honest-signal-accuracy/` (analysis copies + part slices)  
**Axis:** strict strike-crossing, `T = floor(high-so-far at pin)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN settlement.  
**Look-ahead-free:** every signal is point-in-time at its pin; `rate_1h` reads only buckets `mod <= pin` (no 13:00–16:45 window-max).

**Cohort:** 2026-04-21 → 2026-10-09 (3440 station-days).

## Part 1 — Honest pin sweep (12:00–15:00, 30-min steps)

Brier of each signal at each station-local pin, re-anchored strike (`T = floor(hsf@pin)+1`), no look-ahead. Lower is better.

| source | 12:00 | 12:30 | 13:00 | 13:30 | 14:00 | 14:30 | 15:00 |
|---|---:|---:|---:|---:|---:|---:|---:|
| rate_1h_pit | 0.3631 | 0.3652 | 0.3648 | 0.3617 | 0.3667 | 0.3729 | 0.3856 |
| gfs | 0.1305 | 0.1371 | 0.1495 | 0.1630 | 0.1891 | 0.2099 | 0.2338 |
| om_gfs_bc | 0.1181 | 0.1122 | 0.1087 | 0.1113 | 0.1370 | 0.1644 | 0.2003 |
| ecmwf | 0.1535 | 0.1683 | 0.1910 | 0.2117 | 0.2451 | 0.2698 | 0.2981 |
| icon | 0.1402 | 0.1513 | 0.1701 | 0.1878 | 0.2200 | 0.2451 | 0.2731 |
| gem | 0.1391 | 0.1484 | 0.1652 | 0.1813 | 0.2118 | 0.2345 | 0.2592 |
| gefs | 0.2688 | 0.2940 | 0.3155 | 0.3342 | 0.3677 | 0.3888 | 0.4140 |
| market | 0.6287 | 0.6230 | 0.6231 | 0.6194 | 0.6191 | 0.6241 | 0.6250 |
| rate_1h (fixedT) | 0.7027 | 0.6293 | 0.5107 | 0.3617 | 0.2413 | 0.1803 | 0.1541 |

`rate_1h (fixedT)` holds the target at the canonical 13:30 strike and scores the point-in-time rate bounded to `13:00..pin` — this is the Workstream-W construction, re-derived without look-ahead, and it is the direct test of the monotonic claim.

### 15:00-pin headline table

| source | Brier@13:30 | Brier@15:00 | Δ (15:00 − 13:30) |
|---|---:|---:|---:|
| rate_1h_pit | 0.3617 | 0.3856 | +0.0239 |
| gfs | 0.1630 | 0.2338 | +0.0708 |
| om_gfs_bc | 0.1113 | 0.2003 | +0.0890 |
| ecmwf | 0.2117 | 0.2981 | +0.0864 |
| icon | 0.1878 | 0.2731 | +0.0853 |
| gem | 0.1813 | 0.2592 | +0.0779 |
| gefs | 0.3342 | 0.4140 | +0.0798 |
| market | 0.6194 | 0.6250 | +0.0056 |

## Part 2 — Full honest signal accuracy table @13:30 pin

| source | n | Brier | resolution | reliability | ECE | dir acc | base |
|---|---:|---:|---:|---:|---:|---:|---:|
| rate_1h_pit | 2311 | **0.3617** | 0.0189 | 0.2223 | 0.4689 | 0.1990 | 0.801 |
| gfs | 2762 | **0.1630** | 0.0174 | 0.0304 | 0.1559 | 0.7835 | 0.816 |
| om_gfs_raw | 2927 | **0.1266** | 0.0447 | 0.0189 | 0.0938 | 0.8230 | 0.812 |
| om_gfs_bc | 2927 | **0.1113** | 0.0452 | 0.0049 | 0.0397 | 0.8435 | 0.812 |
| om_gfs_bc_ps | 2927 | **0.1067** | 0.0494 | 0.0047 | 0.0376 | 0.8439 | 0.812 |
| ecmwf | 2762 | **0.2117** | 0.0132 | 0.0741 | 0.2382 | 0.6868 | 0.816 |
| icon | 2762 | **0.1878** | 0.0108 | 0.0488 | 0.1904 | 0.7263 | 0.816 |
| gem | 2356 | **0.1813** | 0.0104 | 0.0425 | 0.1667 | 0.7343 | 0.816 |
| gefs | 2575 | **0.3342** | 0.0016 | 0.1795 | 0.3320 | 0.6078 | 0.807 |
| market | 376 | **0.6194** | 0.0016 | 0.4459 | 0.6310 | 0.2872 | 0.774 |
| fused (5-mem, raw pre-ceiling) | 2296 | **0.1459** | 0.0172 | 0.0101 | 0.0868 | 0.8158 | 0.808 |
| fused (5-mem, calibrated/shipped) | 2296 | **0.1523** | 0.0133 | 0.0106 | 0.0942 | 0.8158 | 0.808 |
| fused (5-mem, p_core) | 2296 | **0.1458** | 0.0196 | 0.0114 | 0.0893 | 0.8158 | 0.808 |

**Level MAE (°F) for NWP members** (vs canonical settled high):

| member | level MAE |
|---|---:|
| gfs | 2.0946 |
| ecmwf | 2.9598 |
| icon | 2.2929 |
| gem | 2.5249 |
| gefs | 4.6971 |
| om_gfs_raw | 1.3031 |
| om_gfs_bc_global | 1.1477 |
| om_gfs_bc_ps | 1.0138 |
| rate_1h_pit_mu | 2.8145 |

## Part 3 — Agreement boost (n-of-k) exploration

- cohort (rows with gefs+analog+traj): **n = 2296**
- ≥3 members concur on the same side of 0.5: **92.47%** of rows
- ≤1 member concur (i.e. fused disagrees with the members): **0.04%**
- concurrence histogram: `{"1": 1, "2": 172, "3": 909, "4": 1192, "5": 22}`

| arm | Brier | Δ vs no-boost | ECE | reliability |
|---|---:|---:|---:|---:|
| core, no boost | 0.1458 | — | 0.0893 | 0.0114 |
| core, boost | 0.1463 | +0.0005 | 0.0917 | 0.0111 |
| calibrated, no boost | 0.1523 | — | — | — |
| calibrated, boost | 0.1517 | -0.0006 | — | — |

_boost re-uses the shipped n-of-k rule: >=3 concur -> +0.03*min(count-2,3)/3 ; <=1 concur -> -0.03*min(2-count,3)/3_

## Verdicts

### Q1 — Is a 15:00 (or 14:00/14:30) pin a real gain on the honest axis?

**No — it is a mirage from the look-ahead window-max.** On the honest axis every forecast-partition signal gets **worse** as the pin moves later, because the re-anchored strike rises with more of the day observed while the NWP forecast is fixed:

- om_gfs(bc): 0.1087 (13:00) → **0.1113** (13:30) → **0.2003** (15:00)
- gfs: 0.1630 (13:30) → 0.2338 (15:00); every NWP member +≈0.08–0.09 Brier by 15:00
- rate_1h_pit: **0.3617** (13:30) → **0.3856** (15:00) — also degrades.

The only curve that *falls* to 15:00 is `rate_1h (fixedT)` (0.3617 → 0.1541) — and that is a fixed-strike curve: it holds the target at the 13:30 strike, so it cannot see the anchor move. It is **not** comparable to the re-anchored columns and does not imply a tradeable gain. The Workstream-W 'signal wants the pin late' result was the fixedT curve read as if it were a re-anchored Brier.

### Q2 — Where does everything land on the honest 13:30 axis?

Ranking by Brier (best single first): **om_gfs(bc, per-station) 0.1067 < om_gfs(bc) 0.1113 < om_gfs(raw) 0.1266 < fused(5) 0.1458 < gfs 0.1630 < gem 0.1813 < icon 0.1878 < ecmwf 0.2117 < gefs 0.3342 < rate_1h_pit 0.3617 < market 0.6194**. The shipped 5-member fusion (0.1458) does **not** beat the best single (om_gfs(bc) 0.1067) and sits *between* plain gfs and gem — averaging in the weak members (rate_1h, gefs, market) dilutes the strong one. The calibration clamp costs ≈0.006 Brier vs raw (0.1458 → 0.1523).

### Q3 — Is the 1–3% agreement bump right?

**No — it is inert.** On the honest cohort the members are ~collinear: **92.47%** of rows have ≥3 of 5 members concurring, so the n-of-k bump fires on almost every row in the same direction and adds no discriminating power. ΔBrier (boost − no boost) is **+0.0005** on p_core and **-0.0006** on the shipped calibrated_p — i.e. within noise, and slightly *worse* on calibration (ECE 0.0893 → 0.0917). The shipped default (OFF) is correct; do not wire it.

## Reproduce

```bash
python3 scripts/honest_signal_accuracy_2026-10-14.py fetch   # om_gfs pull (20 reqs)
python3 scripts/honest_signal_accuracy_2026-10-14.py all     # full benchmark (~170s cold)
```

