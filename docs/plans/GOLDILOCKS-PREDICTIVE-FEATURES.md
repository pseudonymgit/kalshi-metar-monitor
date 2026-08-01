# Goldilocks Predictive Model — Feature Definitions

## Overview

This document defines all 29+ features computed by `scripts/goldilocks_feature_engineering.py`
for the Goldilocks Predictive Model at KNYC (Central Park).

**Principle:** All features are deterministically computable from public METAR data and
existing local databases. Same inputs → same features every time.

---

## A. Wind Features (7 features)

These are the most important predictors. Low wind enables surface decoupling.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `wind_avg_kt` | continuous | METAR | Mean wind speed over all hourly obs for the local trading date |
| `wind_max_kt` | continuous | METAR | Max gust (or max wind if gust unavailable) for the day |
| `wind_3pm_kt` | continuous | METAR | Wind speed at 15:00 Eastern (predictor for afternoon high-spike window) |
| `wind_sunset_kt` | continuous | METAR | Wind speed at 20:00 Eastern (predictor for overnight low-spike) |
| `wind_6am_kt` | continuous | METAR | Wind speed at 06:00 Eastern (predictor for dawn low-spike survival) |
| `wind_direction_sector` | categorical | METAR | 16-point compass sector (N, NNE, NE, ... NNW) — directional sheltering |
| `wind_stddev_3hr` | continuous | METAR | Standard deviation of wind speed across all obs (mixing indicator) |

## B. Cloud Cover & Radiation Features (6 features)

Cloud cover modulates radiational forcing — clear skies enable both daytime heating
and nighttime cooling extremes.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `cloud_cover_frac` | continuous (0-1) | METAR ceiling + NWP | Fraction of hourly obs with ceiling < 20,000 ft. Falls back to NWP `cloud_cover_daily_mean` if METAR ceiling unavailable. |
| `cloud_ceiling_ft` | continuous | METAR | Lowest reported cloud ceiling (feet) |
| `solar_elevation_max` | continuous (deg) | Computed | Maximum solar elevation at local noon (astronomical, lat=40.78) |
| `solar_flux_est` | continuous (W/m²) | Computed | Estimated surface insolation from sun angle + cloud attenuation |
| `longwave_flux_est` | continuous (W/m²) | Computed | Estimated outgoing longwave radiation at night from dewpoint depression + cloud cover |
| `dp_depression_C` | continuous (C) | METAR | Temperature minus dewpoint (mean of day) — dryness indicator |

## C. Stability Features (5 features)

Boundary layer stability determines how easily the surface decouples from the
free atmosphere.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `daily_temp_range_C` | continuous (C) | ERA5 + METAR | Daily temperature range (max - min). Large range → clear/stable conditions. |
| `lapse_rate_850_925` | continuous (C) | NWP | 850hPa temperature (proxy for lower-tropospheric stability). Higher = more stable. |
| `bl_height_m` | continuous | N/A | **Not currently available** — no BL height in local data sources. All NaN. |
| `bulk_richardson` | continuous | Computed | Approximate bulk Richardson number from wind + temp range. >0.25 → stable. |
| `inversion_strength_proxy` | continuous (F) | METAR | Afternoon temp minus morning temp. Small or negative → possible inversion. |

## D. Temporal Features (7 features)

Diurnal and seasonal timing modulates Goldilocks base rates.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `day_of_year` | continuous (1-366) | Computed | Seasonal modulation |
| `month` | continuous (1-12) | Computed | Monthly modulation (for LightGBM to learn seasonal patterns) |
| `season` | categorical | Computed | winter/spring/summer/fall |
| `is_weekend` | binary (0/1) | Computed | Weekend vs weekday (urban heat island variation) |
| `sunrise_utc_hours` | continuous | Computed | Sunrise time in decimal hours UTC |
| `sunset_utc_hours` | continuous | Computed | Sunset time in decimal hours UTC |
| `daylight_hours` | continuous | Computed | Daylight duration in hours (sunset - sunrise) |

## E. Synoptic Regime Features (4 features)

Large-scale weather patterns that set the stage for Goldilocks conditions.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `mslp_hPa` | continuous | METAR | **Note:** Not currently populated from KNYC METAR (no sea-level pressure data). Will be populated from NWP or left as NaN. |
| `mslp_trend_3hr` | continuous | N/A | 3-hour pressure trend. **Not currently available.** |
| `temp_range_forecast` | continuous (C) | NWP | Forecast high - low from NWP `temperature_2m_max` / `temperature_2m_min` |
| `synoptic_class` | categorical | Rule-based | continental_high, weak_high, trough, frontal, neutral — classified from MSLP + wind |
| `nwp_cloud_cover` | continuous (%) | NWP | Daily mean cloud cover from ECMWF/GEFS forecasts |
| `nwp_wind_speed_kt` | continuous (kt) | NWP | Daily mean 10m wind speed from ECMWF/GEFS (converted from m/s) |

## F. Recent Goldilocks History (3 features)

Persistence features — Goldilocks events may cluster in certain regimes.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `goldilocks_prev_day` | binary (0/1) | Labels | Was there a Goldilocks event yesterday? |
| `goldilocks_prev_3days` | integer (0-3) | Labels | Count of Goldilocks events in last 3 days |
| `goldilocks_rate_30d` | continuous (0-1) | Labels | Rolling 30-day Goldilocks frequency |

**Note:** These features require labels to be computed (by the labeling script) first.
They are filled during the training phase.

## G. Data Quality Features (3 features)

For diagnostic use only — not used as model features.

| Feature | Type | Source | Definition |
|---------|------|--------|------------|
| `metar_obs_count` | integer | METAR | Number of METAR observations for the day |
| `metar_wind_obs` | integer | METAR | Number of obs with wind speed reported |
| `data_quality_flag` | categorical | Computed | 'good' (≥12 obs, ≥6 wind), 'partial' (≥3 obs), 'poor', 'none' |

---

## Feature Availability by Source

| Source | Features | Coverage |
|--------|----------|----------|
| METAR DB | wind_*, dp_depression*, cloud_ceiling* | 2021-01-01 to present, ~720 obs/month (hourly) |
| ERA5 DB | daily_temp_range | 2021-01-01 to 2026-07-31, daily |
| NWP DB | nwp_cloud_cover, nwp_wind_speed, temp_range_forecast, lapse_rate proxy | 2021-01-02 to 2026-08-07, daily |
| Computed | solar_*, longwave_flux, temporal features, bulk_richardson | Any date |
| IEM ASOS (labels) | goldilocks_prev_*, goldilocks_rate_30d | Any date where labeled |

---

## Missing Data Strategy

LightGBM handles NaN natively — missing features are simply not used in splits
where they're absent. Current known gaps:

- `bl_height_m` — always NaN (no BL height data)
- `mslp_hPa` — always NaN for KNYC (no sea-level pressure in METAR DB)
- `mslp_trend_3hr` — always NaN (not implemented)
- `wind_gust_kt` — only available from 2026-07 onward (METAR changes)
- `ceiling_ft` — only available from 2026-07 onward
- `cloud_cover_frac` — uses NWP fallback pre-2026-07, METAR ceiling post-2026-07