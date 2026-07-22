# AI/ML Weather Model Research

**Date:** 2026-07-22
**Status:** Research only — AI/ML gate is CLOSED per Gray Room decision (Round 9)
**Purpose:** Document available AI weather models, Open-Meteo access patterns, and integration path for when the gate opens.

---

## 1. Overview

Five major AI weather models are available as of mid-2026. Three are accessible via Open-Meteo's free API. All are open-source or available as open data.

| Model | Developer | Resolution | Horizon | Steps | Via Open-Meteo | Open Source |
|---|---|---|---|---|---|---|
| **AIGFS** | NOAA | 0.25° (~25km) | 16 days | 6-hourly | ✅ /v1/gfs (models=gfs_global) | ✅ Open data (AWS) |
| **GraphCast** | Google DeepMind | 0.25° (~25km) | 10 days | 6-hourly | ✅ /v1/gfs (models=gfs_graphcast025) | ✅ Weights + code released |
| **AIFS** | ECMWF | 0.25° (~25km) | 15 days | 6-hourly | ✅ /v1/ecmwf (models=ecmwf_aifs025) | ✅ Open license 2024 |
| **GenCast** | Google DeepMind | 0.25° (~25km) | 15 days | 6-hourly | ❌ Not yet | ✅ Code released |
| **Pangu-Weather** | Huawei | 0.25° (~25km) | 7 days | 1-hourly | ❌ Not yet | ❌ Proprietary weights |

---

## 2. Model Details

### 2.1 AIGFS (NOAA)
- **Launched:** December 2025
- **Description:** NOAA's AI-enhanced Global Forecast System. Built on techniques from Google DeepMind's GraphCast, trained on NOAA's GFS archive.
- **Strengths:** Better accuracy than standard GFS, especially for temperature and pressure at mid-latitudes. Runs 4x daily (00Z, 06Z, 12Z, 18Z).
- **Limitations:** 6-hourly time steps (not hourly). Limited to variables available in GFS. Still experimental — NOAA warns of potential outages.
- **Open-Meteo access:** Via `/v1/gfs` with `models=gfs_global` parameter. The "seamless" model combines AIGFS, GraphCast, and classical GFS.

### 2.2 GraphCast (Google DeepMind)
- **Released:** November 2023 (research), operational 2024
- **Description:** Graph Neural Network trained on ERA5 reanalysis data. Google DeepMind's flagship AI weather model.
- **Strengths:** Outperforms ECMWF HRES on 90%+ of verification targets at 10-day lead time. Requires minimal compute for inference (single GPU).
- **Limitations:** Trained on ECMWF ERA5 but initialized with GFS data — slight inconsistency. 6-hourly time steps only. No solar radiation or soil moisture variables.
- **Open-Meteo access:** Via `/v1/gfs` with `models=gfs_graphcast025` parameter. NOAA runs operational GFS-GraphCast as open data on AWS.
- **Historical data:** Available from Feb 5, 2024 onwards.

### 2.3 AIFS (ECMWF)
- **Released:** 2024 (open weights)
- **Description:** ECMWF's Artificial Intelligence Forecasting System. Builds on GraphCast methods but trained on ECMWF's own IFS data.
- **Strengths:** Native alignment with ECMWF IFS data. 0.25° resolution. 15-day forecasts. 4x daily runs.
- **Limitations:** 6-hourly time steps. Fewer variables than classical IFS.
- **Open-Meteo access:** Via `/v1/ecmwf` with `models=ecmwf_aifs025` parameter.
- **Historical data:** Available from Feb 20, 2025 onwards.

### 2.4 GenCast (Google DeepMind)
- **Released:** 2025 (research)
- **Description:** Probabilistic ensemble version of GraphCast. Samples from a learned distribution to generate multiple forecast members — similar to traditional ensemble but at much lower cost.
- **Strengths:** Outperformed ECMWF's 51-member ENS on 97.2% of verification targets. Provides probabilistic forecasts natively.
- **Limitations:** Not yet available via Open-Meteo or as operational open data. Requires running inference on local GPU.
- **Verdict:** Most promising for probabilistic weather trading, but not yet operationally accessible.

### 2.5 Pangu-Weather (Huawei)
- **Released:** 2023 (research)
- **Description:** 3D Earth-Specific Transformer (3DEST). Multi-timescale model combination strategy.
- **Strengths:** 1-hourly time steps (unique among AI models). Strong medium-range performance.
- **Limitations:** Proprietary weights — not directly available. 7-day horizon only. AWS Open Data reforecasts available but not real-time.
- **Verdict:** Not viable for operational integration without proprietary access.

---

## 3. Open-Meteo Access Patterns

### 3.1 Current NWP Collection (classical models)
The existing `nwp_collect.py` uses model-specific endpoints:
```
/v1/gfs          → GFS (classical, 25km, 16-day)
/v1/ecmwf        → ECMWF (classical, 9km, 15-day)
/v1/dwd-icon     → ICON (classical, 13km, 7-day)
/v1/gem          → GEM (classical, 15km, 10-day)
```

### 3.2 AI Model Access (when gate opens)
AI models are accessed through the **same endpoints** with a `models` parameter:
```
/v1/gfs?models=gfs_global          → AIGFS (combined with classical GFS)
/v1/gfs?models=gfs_graphcast025    → GFS GraphCast (pure GraphCast)
/v1/ecmwf?models=ecmwf_aifs025    → ECMWF AIFS (pure AI model)
```

The Open-Meteo "seamless" model automatically blends the best available model:
- US locations: HRRR (0-48h) → AIGFS/GraphCast (2-16d)
- Global: GFS (0-16d) with AI enhancements

### 3.3 Key Differences from Classical Models
| Aspect | Classical (GFS/ECMWF) | AI (AIGFS/GraphCast/AIFS) |
|---|---|---|
| Resolution | 9-25km | 25km (all AI models) |
| Time steps | 1-hourly (GFS), 3-hourly (ECMWF) | 6-hourly (all AI models) |
| Variables | Full (including upper air, soil, radiation) | Limited (surface + standard pressure levels) |
| Update frequency | 4x daily (GFS), 2x daily (ECMWF) | 4x daily (all AI models) |
| Forecast horizon | 7-16 days | 10-16 days |
| Historical data | Long (years) | Short (2024 onwards) |

---

## 4. Integration Path (When Gate Opens)

### Step 1: Add AI models to nwp_collect.py
Add to MODELS list in `nwp_collect.py`:
```python
("aigfs",     "https://api.open-meteo.com/v1/gfs?models=gfs_global"),
("graphcast", "https://api.open-meteo.com/v1/gfs?models=gfs_graphcast025"),
("aifs",      "https://api.open-meteo.com/v1/ecmwf?models=ecmwf_aifs025"),
```

### Step 2: Create AI model storage table
New table `ai_forecasts` with 6-hourly timestamps (or extend `nwp_forecasts` with a `model_type` column).

### Step 3: Build AI composite signal
Create `core/signals/ai_composite_signal.py` that:
- Fetches from all 3 available AI models
- Blends with classical NWP predictions
- Reports AI-vs-classical divergence as a confidence modulator

### Step 4: Training (requires AI/ML gate lift)
- Compare AI model accuracy vs classical models for Kalshi temperature markets
- Train lightweight calibrator on AI model output
- Potential edge: AI models may outperform classical for 3-10 day range

---

## 5. Assessment

### Current State
- **AI/ML gate: CLOSED** (per Gray Room Round 9, 2026-07-21)
- No AI model data is being collected
- All signals use classical NWP (GFS, ECMWF, ICON, GEM) + METAR + ERA5

### Recommendation (when gate opens)
1. **Start with AIGFS** — easiest integration (same endpoint as GFS, same variables)
2. **Add AIFS** — ECMWF-native AI, best alignment with existing ECMWF pipeline
3. **Add GraphCast** — third AI model for ensemble diversity
4. **Skip GenCast and Pangu-Weather** — not operationally accessible yet

### Key Risks
- **6-hourly time steps** — AI models can't match hourly resolution of classical GFS/HRRR
- **Short history** — AI models only have data from 2024-2025; insufficient for multi-year backtesting
- **Variable gaps** — AI models may not provide all variables needed by existing signals (e.g., 850hPa temperature, wind shear)
- **Experimental status** — NOAA warns of potential outages for AIGFS/GraphCast
- **Overfitting risk** — AI models trained on ERA5 (reanalysis) may perform differently on real forecasts

---

## 6. References

- Open-Meteo GFS & HRRR API: https://open-meteo.com/en/docs/gfs-api
- Open-Meteo ECMWF API: https://open-meteo.com/en/docs/ecmwf-api
- Open-Meteo GraphCast announcement: https://openmeteo.substack.com/p/exploring-graphcast
- NOAA AIGFS announcement: https://snowbrains.com/noaa-launches-a-i-enhanced-versions-of-gfs-and-gefs-weather-models/
- ECMWF AIFS open data: https://www.ecmwf.int/en/forecasts/datasets/open-data
- AWS Open Data AIWP: https://registry.opendata.aws/aiwp/
- Open-Meteo open data models: https://github.com/open-meteo/open-data