# P1.4 — NWP-Analog Ensemble Engine — 2026-07-06

**Date:** 2026-07-06
**Method:** K=50 nearest-neighbor analog search in NWP feature space
**NWP Variables:** 9 (averaged across 4 models)
**Normalization:** Z-score per variable (library stats only, no look-ahead)
**Walk-forward:** Strict — only uses NWP data available before target date
**NWP Window:** 2026-05-09 to 2026-07-12 (65 days)
**Baseline:** 65.26% (7-signal ensemble, late-day momentum excluded)

## Per-Station Results

| Station | Trades | Correct | Accuracy | Avg Confidence |
|---------|--------|---------|----------|----------------|

## Overall Results

| Metric | Full Window | Last 60 Trades/Station |
|--------|-------------|----------------------|
| Trades | 0 | 0 |
| Correct | 0 | 0 |
| Accuracy | 0.00% | 0.00% |
| Avg Confidence | 0.000 | 0.000 |
| Brier Score | 0.0000 | — |
| Sharpe (5% fee) | 0.000 | — |

## Comparison to Ensemble Baseline

| Metric | NWP Analog | Ensemble Baseline | Delta |
|--------|------------|-------------------|-------|
| Accuracy | 0.00% | 65.26% | -65.26% |
| Brier | 0.0000 | ~0.28 | — |
| Sharpe | 0.000 | ~0.33 | — |

## Methodology

1. **Feature extraction:** For each target_date × station, extract 9 NWP variables averaged across 4 models (GFS, ECMWF, ICON, GEM).
2. **Normalization:** Z-score per variable using only library statistics (no look-ahead).
3. **Analog search:** K=50 nearest neighbors by Euclidean distance in normalized feature space.
4. **Conditional probability:** Fraction of analogs where temperature HIGH went up vs prior day.
5. **Direction:** 'up' if P(up) > 0.5, else 'down'. Confidence = |P(up) - 0.5| × 2.
6. **Walk-forward:** Library grows incrementally. First ~5 days per station have too few analogs and produce no prediction.

## Variables Used

- `temperature_2m_max`
- `temperature_2m_min`
- `precipitation_sum`
- `temperature_850hPa_daily_mean`
- `wind_speed_10m_daily_mean`
- `wind_direction_10m_daily_mean`
- `cloud_cover_daily_mean`
- `dew_point_2m_daily_mean`
- `geopotential_height_500hPa_daily_mean`

## Assessment

**Status: BELOW BASELINE** — NWP analog signal underperforms (-65.26%).
Likely causes: small analog library (65 days), feature normalization quality, or
insufficient discrimination in Euclidean distance metric.

Possible improvements:
- Weighted analogs (inverse distance weighting)
- Larger K or adaptive K based on library size
- Principal component analysis for dimensionality reduction
- Include more NWP history (backfill further back than May 2026)
