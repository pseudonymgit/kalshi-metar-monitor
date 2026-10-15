# GEFSv12 per-member skill on the honest axis

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`  
**Script:** `scripts/gefsv12_member_skill_2026-10-15.py` · **Artifact:** `data/gefsv12_member_skill_2026-10-15.json`

**Read-only. No DB writes.**

## Source correction (the task's premise was wrong)

The task said the backfill reached 5,517 rows with members `c00, p01..p04` (5 members) in `data/gefsv12_reforecast.db`. **That is not what the DB holds.**

- `data/gefsv12_reforecast.db`: members present are `c00, p01, p02`; **c00 = 550 station-days**, and **p01/p02 = 10/7 rows** (a single probe day, 2010-06-01, KNYC). Window **2000-01-01..2010-06-30**.
- The 5,517-row count is almost entirely c00 (5,500 rows); there is **no p03/p04**, and no member backfill.
- More fundamentally, that DB's window (2000-2010) **does not overlap the honest cohort (2026)**, whose axis needs intraday running-high buckets that only exist from ~2021. **`gefsv12_reforecast` cannot support this analysis at all.**

**The honest-era member data lives elsewhere:** `data/nwp_forecasts.db::nwp_forecasts` (model `gefs_ens`), variables `temperature_2m_max_member<M>` — **31 members** (`gec00`, `gep01`..`gep30`), window **2021-01-02..2026-10-21**, which covers the honest cohort. This script scores those members.

## Method

- dedup to the latest `fetch_date` per (station, target_date, variable);
- °F = °C·9/5 + 32;
- member P(cross) = `p_ge(member_F − bias_st, sigma_st, T)` — the same per-station bias/sigma treatment the shipped om_gfs uses;
- truth = corrected Kalshi (GHCN fallback); T = `floor(running_high@13:30)+1`, y = `1{settled > T}` (strict).
- cohort scored: **790** station-days; members with < 500 station-days are flagged, not scored.

## Per-member skill

Members come in two coverage tiers: `gec00`+`gep01..gep05` cover the full honest window (~3020 station-days); `gep06..gep30` cover a shorter ~800-day sub-window. Rankings below are computed on the **common cohort** (rows where every member AND om_gfs are present) so they are apples-to-apples.

| member | n (common) | Brier | dir acc | ECE | resolution |
|---|---:|---:|---:|---:|---:|
| gec00 | 790 | **0.207** | 0.7 | 0.151 | 0.0068 |
| gep01 | 790 | **0.2086** | 0.6962 | 0.1405 | 0.0074 |
| gep02 | 790 | **0.2145** | 0.6759 | 0.1597 | 0.0057 |
| gep03 | 790 | **0.2141** | 0.6886 | 0.165 | 0.0063 |
| gep04 | 790 | **0.2144** | 0.6823 | 0.152 | 0.0059 |
| gep05 | 790 | **0.2104** | 0.6848 | 0.1406 | 0.0074 |
| gep06 | 790 | **0.2189** | 0.6734 | 0.163 | 0.0097 |
| gep07 | 790 | **0.2159** | 0.6772 | 0.1649 | 0.0097 |
| gep08 | 790 | **0.2172** | 0.6899 | 0.1628 | 0.0106 |
| gep09 | 790 | **0.2181** | 0.6772 | 0.1591 | 0.0072 |
| gep10 | 790 | **0.2244** | 0.6734 | 0.1726 | 0.0069 |
| gep11 | 790 | **0.2165** | 0.6861 | 0.1571 | 0.0097 |
| gep12 | 790 | **0.2153** | 0.6848 | 0.1562 | 0.0097 |
| gep13 | 790 | **0.2223** | 0.6772 | 0.1755 | 0.0074 |
| gep14 | 790 | **0.2263** | 0.6646 | 0.1712 | 0.0078 |
| gep15 | 790 | **0.2162** | 0.6911 | 0.1596 | 0.0107 |
| gep16 | 790 | **0.2151** | 0.6759 | 0.1586 | 0.0093 |
| gep17 | 790 | **0.2096** | 0.6949 | 0.1581 | 0.0123 |
| gep18 | 790 | **0.2183** | 0.6722 | 0.1689 | 0.0099 |
| gep19 | 790 | **0.2108** | 0.6962 | 0.1529 | 0.0128 |
| gep20 | 790 | **0.2159** | 0.6911 | 0.1622 | 0.0095 |
| gep21 | 790 | **0.2224** | 0.6722 | 0.159 | 0.008 |
| gep22 | 790 | **0.2207** | 0.6646 | 0.167 | 0.0079 |
| gep23 | 790 | **0.2203** | 0.6772 | 0.17 | 0.0093 |
| gep24 | 790 | **0.2118** | 0.6886 | 0.1506 | 0.0107 |
| gep25 | 790 | **0.2189** | 0.6747 | 0.1709 | 0.0088 |
| gep26 | 790 | **0.2179** | 0.6696 | 0.1661 | 0.0088 |
| gep27 | 790 | **0.2149** | 0.6797 | 0.1601 | 0.0121 |
| gep28 | 790 | **0.2144** | 0.6759 | 0.1629 | 0.0095 |
| gep29 | 790 | **0.2166** | 0.6823 | 0.1543 | 0.0095 |
| gep30 | 790 | **0.2172** | 0.6709 | 0.1649 | 0.0088 |

## Ensemble vs single member vs om_gfs

### Tier-6 cohort (gec00+gep01..05, the task's requested shape)

| predictor | n | Brier | dir acc | ECE |
|---|---:|---:|---:|---:|
| best single member (gec00) | 2853 | **0.2502** | 0.6358 | 0.2183 |
| ensemble mean (prob avg, 6 members) | 2853 | **0.2454** | 0.6376 | 0.2173 |
| ensemble mean (level avg) | 2853 | 0.2481 | 0.6362 | 0.2221 |
| **deterministic om_gfs (shipped)** | 2853 | **0.107** | 0.8472 | 0.0384 |

### All-31 cohort (common sub-window)

| predictor | n | Brier | dir acc | ECE |
|---|---:|---:|---:|---:|
| best single member (gec00) | 790 | **0.207** | 0.7 | 0.151 |
| ensemble mean (prob avg, 31 members) | 790 | **0.2035** | 0.6937 | 0.1465 |
| ensemble mean (level avg) | 790 | 0.2093 | 0.6886 | 0.1612 |
| **deterministic om_gfs (shipped)** | 790 | **0.1009** | 0.8608 | 0.0399 |

The task's exact 5-member subset (gec00+gep01..04) mean: n=2853, Brier 0.2455, dir acc 0.6365.

## Findings

- **No single GEFS member beats deterministic om_gfs.** Best member gec00 at Brier **0.207**; om_gfs at **0.1009**. The deterministic bias-corrected om_gfs still beats all 31 GEFS ensemble members on the honest axis.
- **The ensemble mean beats the best single member** (0.2035 vs 0.207) — the ensemble advantage holds *within* the GEFS members.
- **Even the ensemble mean does not reach om_gfs** (0.2035 vs 0.1009) — the per-member/ensemble approach is a dead end for beating the current sole signal.

## Recommendation

- **Do not pursue GEFS per-member selection** as a path to beating om_gfs. No member, and not the ensemble mean, closes the gap.
- **The task's `gefsv12_reforecast` member plan is moot as specified.** If a reforecast-based member study is wanted, it must either (a) align to a historical settlement era, or (b) use the honest-era `gefs_ens` members in `nwp_forecasts.db` (done here).
- Correct the register: `gefsv12_reforecast.db` holds c00 + a single probe day for p01/p02 — **not** `c00,p01..p04`.

## Reproduce

```bash
python3 scripts/gefsv12_member_skill_2026-10-15.py
```

