# Cutting-Edge Forecasting Research — 2026-10-13

**Task 2.** A research scan of what's available beyond what the weather engine
has already tested, from three parallel web-research passes (operational NWP
advances, intraday/nowcasting state of the art, unusual/experimental obs
sources). Each item lists source, cost, maturity, and a follow-up verdict.

Context: we already tested AI-NWP (AIFS/GraphCast — independence test) and the
`rate_1h` window sweep. `rate_1h` stays 1h. The goal is the entry-price
gradient: a further-out entry only helps if the trajectory signal is sharper.

---

## 1. Operational NWP advances (non-AI) 2024–2026

| Item | What it is | Cost | Maturity | Follow-up? |
|---|---|---|---|---|
| **DWD ICON-D2-RUC** | Rapid-update regional model, hourly cycle, ~2 km, out to ~14 h, with simulated radar reflectivity. Commissioned ~May 2025. | Free (opendata.dwd.de) | Operational | **Maybe** — cutting-edge, but domain is central Europe. No US coverage. |
| **NOAA RRFS v1 / REFS** | Next-gen convection-allowing, rapidly-updated regional ensemble; FV3 dycore now, MPAS planned later. Cleared for operational use after 2025 SFE; AWS lists 2026 implementation. Prototype data already public. | Free (AWS Open Data) | Imminent-2026 / prototype public | **Yes** — the real successor to RAP/HRRR/HREF; MPAS versions are the frontier. |
| **NOAA GFS v17 / GDAS v17** | Coupled UFS global upgrade (9 km), JEDI DA, aerosol DA. PNS 26-30 targets ~Oct 2026. | Free (NOMADS) | Proposed | **Yes** — watch the file/directory rename; downstream ingest risk. Schedules have slipped before. |
| **ECMWF IFS Cycle 50r1** | Coupled NEMO4-SI3 ocean/sea-ice, new ocean DA, convection→cloud-scheme fix (coastal precip), EDA re-centring, SPP fix. Early 2026. | Licensed (charts free) | Imminent-2026 | **Maybe** — medium-range skill story; not a short-range lever for us. |
| **HAFS v2** | Operational TC model, moving nest, 3D ocean coupling. Strong 2024 track performance. | Free (AWS) | Operational | No — TC-only. |
| **GEFSv12 reforecast (1989–2019, 31-member)** | Multi-decadal reforecast for bias-correction / analog post-processing. | Free (AWS S3) | Operational/archival | **Yes** — high value for statistical post-processing to complement AI-NWP. |
| **Met Office LFRic / GC4 / RAL3** | LFRic (new dycore) is a ~2027 roadmap item; operational UM is incremental config steps. | N/A / freemium DataHub | Roadmap / operational | No — not a usable 2024–26 product. Watch item only. |

**Free US station data from foreign institutes:** none. A deliberate survey of
DWD, Météo-France, AEMET, KNMI, Met Office, ECCC, JMA, and BOM found **no**
free, no-key **US station observations** — they publish their own national
observations (free or key-gated) and global gridded model fields. **For US
station data the free no-key sources remain US agencies (NOAA/NWS, NCEI) and
aggregators (MADIS, Synoptic/MesoWest).** Foreign open data is useful only for
gridded global fields (DWD ICON, ECCC GDPS/RDPS, BOM ACCESS-G).

---

## 2. Intraday / nowcasting state of the art (0–6 h)

### The honest headline on AI nowcasting

- **DGMR (DeepMind) and MetNet-1/2 are PRECIPITATION-ONLY.** DGMR is a 90-min
  radar rain model — the most over-hyped acronym in the space for our purpose.
- **MetNet-3 is the only AI nowcaster that genuinely does surface
  temperature** (plus wind/dew point, to 24 h, operational inside Google). It is
  **fully closed** — no public API, no weights. Any claim that "we can call
  MetNet-3" is false; we can only cite its benchmark.
- **Pangu/FuXi are medium-range AI NWP, not nowcasting.** Pangu has no
  precipitation field at all. Commonly mis-sold.

### Observation–model blending

- **NBM (National Blend of Models)** — v5.0 operational May 2026, CONUS 2.5 km,
  **hourly at 1-h resolution to 36 h**. It is a **model blend** whose short
  range is analysis-anchored; it is *not* a pure obs-driven nowcaster. Use its
  dashboard to settle empirically which obs feed a given temperature cycle.
- **RTMA/URMA — the key OPEN obs–model fusion product for US temperature
  "now state".** Hourly 2.5 km 2-m temperature **analysis**, assimilating
  conventional + satellite obs. Free on AWS/NODD. It is an *analysis* (now), not
  a 1–6 h forecast — pair it as the initial-condition anchor with extrapolation.
- **MADIS** is the free minute-scale obs backbone feeding RTMA. **MRMS** (AWS,
  ~2-min cycle) is the radar fusion product — precip/storm context only.

### Open-source frameworks we could deploy

- **pySTEPS** (BSD) — the reference OSS implementation of STEPS/LINDA/S-PROG/
  ANVIL/IDE. Precipitation-only, but a strong architecture donor if we ever add
  a precip/storm overlay. **Worth a look, not a temperature product.**
- **metnet3-pytorch** (lucidrains) — architecture reimplementation, **no trained
  weights**. Template only.
- **Open-Meteo API** — free no-key, aggregates 15+ national NWP models with a
  `minutely_15` endpoint. Fast baseline series; it is downscaled *model* output,
  not observation-driven, so it lags real surface obs.

---

## 3. Unusual / experimental observation sources

### Free aggregators worth knowing (temperature-relevant)

| Source | Notes | Cost | Verdict |
|---|---|---|---|
| **MADIS** | NOAA aggregator: METAR, mesonet, CWOP, RWIS/Clarus, CRN, radiometer. **We used it for Task 1.** Hourly NetCDF, ~6-day rolling. | Free (some provider subsets restricted) | **Yes — proven free dense-CONUS temp path.** |
| **CWOP** | Citizen network feeding MADIS, 5-min cadence, MADIS-QC'd. | Free | Yes (via MADIS). Siting bias needs QC. |
| **Synoptic / MesoWest** | Best single aggregator across mesonets + PWS + METAR; SynopticPy client. | Freemium (free academic/non-commercial; token required) | **Yes** if non-commercial; token needed. |
| **State mesonets** (OK/Iowa/NY/WA...) | Gold-standard dense 5-min air temp, folded into MADIS/Synoptic. | Free | Yes — validate fusion/QC on a subset. |
| **SCAN / NRCS AWDB** | ~200+ sites, air temp + **soil temp/moisture**, free REST. | Free | Maybe — sparse; soil signal is unique. |

### Keyed / heavier citizen networks

- **Netatmo** — huge density, but documented siting/solar bias; heavy QC
  required. US density weaker than Europe.
- **PurpleAir** — 35k+ sensors with a *secondary* temperature field; AQ
  enclosure heating makes temp a weak proxy. Key required.
- **Ambient Weather / Tempest / Davis WeatherLink / PWSWeather** — own-station
  and network access is key-gated or paid; largely redundant with MADIS/Synoptic.
- **CoCoRaHS** — precipitation-focused; **not** a temperature source.

### Satellite surface temperature — checked, does not beat METAR for point temp

- **GOES-R ABI L2 LST**: 2 km CONUS, **hourly only** (the 1-min mesoscale scans
  do NOT apply to the LST product), **skin** temperature, cloud-blocked. Free on
  NOAA AWS. *Does not resolve earlier than METAR.*
- **MODIS/VIIRS NRT LST**, **ECOSTRESS**: ~2×/day or irregular, ≥1 h latency.
  Worse than METAR for near-term point temperature.

### Research frontier (watch, not deploy)

- **Smartphone pressure (SPO)** — dense surface *pressure* assimilation; the
  leading opportunistic-sensing paradigm. Not temperature.
- **Connected vehicles** (wipers/ABS) — rainfall/road-state research; no public
  temperature feed.
- **Ground-based microwave radiometers** — boundary-layer profiles, very sparse
  fixed sites. Niche.

---

## Bottom line

1. **Most-promising free deployable feed: MADIS LDAD mesonet** (already proven
   free/keyless/hourly in Task 1). Pair with **RTMA/URMA** as the initial-
   condition anchor. Both free, both US, both now.
2. **Most-promising *new model*: NOAA RRFS/REFS** (and later MPAS) — the
   convection-allowing successor; and **GEFSv12 reforecast** for statistical
   post-processing. Watch for GFS v17 file-layout changes.
3. **Be skeptical of AI nowcasting for temperature:** only MetNet-3 does it and
   it is closed. DGMR/MetNet-1/2 are precipitation-only; Pangu/FuXi are
   medium-range AI NWP mislabeled as nowcasting.
4. **Satellite LST does not help** point-temperature nowcasting — cadence and
   skin-vs-air kill it.
5. **No foreign met institute** provides free no-key US station data.

### Two follow-up tests worth queueing

- **A. RTMA/URMA as a now-state anchor** (free, AWS): does opening the entry at
  the RTMA 2-m temperature instead of the last ASOS METAR improve the
  entry-price gradient on the same 20 stations?
- **B. MADIS microclimate-divergence signal** (free): pre-register
  `ASOS − median(nearby PWS ≤12 mi)` and test lead/lag vs ASOS 1 h-forward ΔT —
  the one PWS angle Task 1 suggests is *not* already in `rate_1h`.

---

## Sources

Operational NWP:
- ECMWF IFS 49r1 — https://confluence.ecmwf.int/spaces/FCST/pages/414584216/Implementation+of+IFS+Cycle+49r1
- ECMWF IFS 50r1 — https://www.ecmwf.int/en/newsletter/185/earth-system-science/upgrade-ifs-cycle-50r1
- DWD ICON-D2-RUC — https://pilot-hub.com/en/2-13/ ; DWD open data — https://opendata.dwd.de/
- NOAA RRFS — https://gsl.noaa.gov/rrfs/rrfsv1/ ; AWS — https://registry.opendata.aws/noaa-rrfs/
- GFS v17 PNS — https://www.weather.gov/media/notification/pdf_2026/pns26-30_CommentsGFSv17_ProductRemoval.pdf
- GEFSv12 reforecast — https://registry.opendata.aws/noaa-gefs-reforecast/
- Met Office next-gen modelling — https://www.metoffice.gov.uk/blog/2025/next-generation-modelling-systems-at-the-met-office
- KNMI — https://developer.dataplatform.knmi.nl/open-data-api ; AEMET — https://www.aemet.es/en/datos_abiertos/AEMET_OpenData ; ECCC — https://eccc-msc.github.io/open-data/msc-geomet/readme_en/

Nowcasting / blending:
- pySTEPS — https://pysteps.readthedocs.io/en/latest/pysteps_reference/nowcasts.html ; LINDA — https://pysteps.readthedocs.io/en/stable/auto_examples/linda_nowcasts.html
- MetNet — https://research.google/pubs/metnet-a-neural-weather-model-for-precipitation-forecasting/
- MetNet-3 — https://arxiv.org/html/2306.06079 ; metnet3-pytorch — https://github.com/lucidrains/metnet3-pytorch
- DGMR — https://www.nature.com/articles/s41586-021-03854-z
- NBM — https://nomads.ncep.noaa.gov/txt_descriptions/BLEND_txt.html ; dashboard — https://blend.mdl.nws.noaa.gov/nbm-dashboard
- RTMA/URMA — https://registry.opendata.aws/noaa-rtma/
- MADIS — https://madis.ncep.noaa.gov/madis_OMO.shtml ; CWOP — https://madis.ncep.noaa.gov/madis_cwop.shtml
- MRMS — https://registry.opendata.aws/noaa-mrms-pds/
- Open-Meteo — https://open-meteo.com/en/docs

Experimental obs:
- CoCoRaHS — https://www.cocorahs.org/ViewData/ ; Netatmo — https://developer.legrand.com/solutions/weather-with-netatmo/
- Ambient — https://ambientweather.docs.apiary.io/ ; Tempest — https://weatherflow.github.io/Tempest/api/ ; Davis — https://weatherlink.github.io/v2-api/
- PurpleAir — https://www.purpleair.com/api ; OWM — https://openweathermap.org/api ; PWSWeather — https://www.pwsweather.com/
- OK Mesonet — https://www.mesonet.org/ ; IEM — https://mesonet.agron.iastate.edu/api/ ; NY Mesonet — https://www.nysmesonet.org/
- SCAN — https://www.nrcs.usda.gov/resources/data-and-reports/soil-climate-analysis-network ; RWIS/Clarus — https://madis.ncep.noaa.gov/madis_rwis_clarus.shtml
- Synoptic — https://synopticdata.com/pricing/
- GOES ABI LST — https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01521 ; MODIS NRT — https://www.earthdata.nasa.gov/data/instruments/modis/near-real-time-data ; ECOSTRESS — https://www.earthdata.nasa.gov/data/catalog/lpcloud-eco2lste-001
- Smartphone pressure — https://link.springer.com/article/10.1007/s00376-026-5647-y ; connected vehicles — https://www.nature.com/articles/s41598-018-36282-7 ; radiometers — https://amt.copernicus.org/articles/17/219/2024/

*Research pass via web_search/web_fetch on 2026-10-13. Items not individually
re-verified against live endpoints except MADIS/IEM/Open-Meteo, which were
directly probed for Task 1.*
