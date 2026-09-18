# GEFS Temperature Provenance Pilot — Acquisition Specification

**Frozen:** 2026-08-15 UTC  
**Version:** `gefs-temperature-pilot-acquisition-v1.0-2026-08-15`  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`  
**Scope:** bounded, read-only official NOAA GEFS provenance pilot. No bulk acquisition, scoring, production, trading, sweep, or evaluator integration.

## 1. Frozen disposition and source route

The pilot uses the official NOAA Open Data GEFS AWS PDS route because it retains exact historical raw GRIB2 and `.idx` objects and supplies S3 `Last-Modified`, `ETag`, and `Content-Length` metadata. NOMADS HTTPS/GRIB Filter is retained as documented official fallback/cross-check but is not used for this pilot's historical retrieval after the live route returned an official `Request for Old Data`/expired response for the selected historical dates. No Open-Meteo, SQLite archive, GHCN, METAR, proxy, or orphaned local `.idx` is used as a forecast-value source.

Canonical endpoint pattern:

```text
https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/00/atmos/pgrb2ap5/{member}.t00z.pgrb2a.0p50.f{HHH}
https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/00/atmos/pgrb2ap5/{member}.t00z.pgrb2a.0p50.f{HHH}.idx
```

Product is GEFS atmospheric primary 0.50° grid (`pgrb2ap5`), GRIB2, 2 m above-ground temperature (`TMP`, native `shortName=2t`, units K). Each selected full GRIB2 object is retained verbatim together with its matching official `.idx`; a deterministic byte-range extraction of the 2 m TMP message is also retained. AWS object metadata is retained in a request manifest. The selected cycle is **00Z on D-1**, not 12Z, because the frozen cutoff is 12:00:00Z on D-1 and the AWS publication evidence for 00Z files is before that cutoff. The 00Z run is therefore the pre-cutoff vintage for target date D.

All requests are serialized. A minimum 10-second inter-request delay is used, including HEAD/list/GET requests; no parallel fetches. On any 4xx/5xx, hash mismatch, truncation, or ambiguous route, record the failed request and stop the affected fixture; never substitute a source.

## 2. Frozen stations and mapping

Exact stations: `KNYC`, `KDEN`, `KPHX`, `KSEA`, `KATL`. Coordinates and IANA zones are frozen in the pilot fixture manifest and are not read from a mutable production database. Mapping is deterministic nearest grid point on the native regular 0.50° grid; no target-day values are used. The parser independently records station coordinates, grid latitude/longitude/index, distance, longitude convention, mapping method/version, and mapping hash. The station fixture is:

| Station | Latitude | Longitude | IANA zone | Geometry purpose |
|---|---:|---:|---|---|
| KNYC | 40.7772 | -73.8726 | America/New_York | urban / Eastern DST |
| KDEN | 39.8617 | -104.6731 | America/Denver | Mountain / DST |
| KPHX | 33.4342 | -112.0116 | America/Phoenix | no-DST control |
| KSEA | 47.4499 | -122.3118 | America/Los_Angeles | Pacific DST |
| KATL | 33.6407 | -84.4277 | America/New_York | Eastern / humid |

Pinned mapping algorithm: nearest native grid cell by haversine distance; ties are resolved by lowest `(grid_i, grid_j)`; longitudes normalized to `[0,360)`. Mapping code/config and tzdata versions are hashed in the final manifest.

## 3. Predeclared date/cycle fixtures

`target_date_local=D`; frozen cutoff is `D-1T12:00:00Z`, inclusive. The forecast source vintage is `D-1T00:00:00Z` (`00Z` cycle) for every date where the object is retained. Required forecast valid hours are the full 3-hour trajectory `f006..f054` (17 points) from the 00Z D-1 run, which covers each station-local D window in UTC for all five zones. The `f000` object is retained as an initialization/reference object; it is never used as a target-day value.

| Fixture | Target D | Source run | Case | Expected local-day geometry | Expected status |
|---|---|---|---|---|---|
| `ordinary_2024_01_15` | 2024-01-15 | 2024-01-14 00Z | ordinary standard time | 24 elapsed hours in all zones | included; source availability to verify |
| `dst_spring_2024_03_10` | 2024-03-10 | 2024-03-09 00Z | US DST spring-forward | 23 elapsed hours in DST zones; 24 in Phoenix | included; 23/24-hour assertion |
| `dst_fall_2024_11_03` | 2024-11-03 | 2024-11-02 00Z | US DST fall-back | 25 elapsed hours in DST zones; 24 in Phoenix | included; 25/24-hour assertion |
| `historical_2025_11_15` | 2025-11-15 | 2025-11-14 00Z | historical retention, ordinary standard time | 24 elapsed hours | included; AWS retention check |
| `missing_expected_2020_01_15` | 2020-01-15 | 2020-01-14 00Z | expected missing/legacy route fail-closed | not evaluated | expected failed fixture; no substitute |

The date set, stations, source route, product/grid, cycle, lead range, member subset, missing fixture, cutoff, raw-retention rules, and stop conditions are frozen before acquisition. No dates may be added after observing availability.

## 4. Member policy and bounded volume

For each date, retrieve the control `gec00` and one declared perturbation `gep01` as a native identity/format pilot. This is intentionally a small provenance pilot, not a forecast panel or scoring sample. Expected member IDs are exactly `{gec00, gep01}`; no array-position inference, renaming, padding, or imputation. The expected full GEFS panel is documented as control plus numbered perturbations, but this pilot's two-member fixture is not eligible for scoring and is only used to prove native identity/missingness. Any absent selected member is recorded in the missingness ledger and fails that fixture closed.

Objects per included date/member: 17 forecast GRIB2 objects (`f006` through `f054` at 3-hour spacing), 17 matching `.idx` objects, plus one `f000` GRIB2 and `.idx` reference pair. A successful included fixture therefore has 36 raw objects (18 GRIB2 + 18 `.idx`) and 34 extracted TMP message objects (17 × 2 members). The raw full files and sidecars remain the authoritative retained inputs; extracted messages are derived, hash-linked artifacts.

## 5. Timing and availability contract

For each request record, retain:

- exact URL and canonical S3 key;
- request start/end UTC and retrieval timestamp UTC;
- HTTP status, content type, content length, `Last-Modified`, `ETag`, `Accept-Ranges`, and all relevant response headers;
- object key, expected and received bytes, raw SHA-256, and `.idx` SHA-256;
- byte-range request URL/headers/response metadata and extracted-message SHA-256;
- source issue/init time from native GRIB (`dataDate`/`dataTime`) and valid time (`validityDate`/`validityTime`);
- `available_time_utc` set only from S3 `Last-Modified` metadata for this exact immutable object; retrieval time is separate and never relabeled availability;
- cutoff `D-1T12:00:00Z` and an explicit `issue_before_cutoff` / `available_before_cutoff` result.

The parser must confirm native `dataDate/dataTime` equals the frozen 00Z run and that `valid_time = issue_time + step`. Any disagreement, post-cutoff availability, or unverifiable timing is `INELIGIBLE_MISSING_TIMING`/`INELIGIBLE_AFTER_CUTOFF`, not guessed.

## 6. Local-day HIGH/LOW geometry

For each station/date/member, construct timezone-aware local endpoints `[D 00:00:00, D+1 00:00:00)` using pinned IANA rules. Convert each endpoint and each native valid timestamp to UTC and local time, retaining UTC offset and fold where applicable. The required trajectory is the complete 3-hour points whose local timestamps lie in the half-open target-day window. HIGH and LOW are separate reductions over the same proven in-window trajectory:

```text
HIGH_member = max(TMP_2m_K values in window)
LOW_member  = min(TMP_2m_K values in window)
```

Each reduction stores contributing valid time, local time, UTC offset, source raw hash, extracted message hash, member ID, grid mapping, and unit conversion (K to °F only in the derived research row; no scoring). No `f024` scalar is treated as a daily extreme. Native `TMAX`/`TMIN` records are not used as substitutes for the trajectory in this pilot.

Expected window elapsed durations are 24 hours for ordinary dates, 23 hours on 2024-03-10 in DST zones, 25 hours on 2024-11-03 in DST zones, and 24 hours in America/Phoenix for all fixtures. Incomplete/missing valid points remain missing and cause a fixture/event to fail closed; no imputation.

## 7. Output layout and read-only boundary

All pilot artifacts go below:

```text
data/raw/gefs_temperature_pilot/2026-08-15/
  fixtures.json
  requests/
  objects/{fixture}/{member}/
  extracted/{fixture}/{member}/
  manifests/
  derived/
  qc/
```

`data/gefs_archive.db`, `data/gefs_work`, production scripts, trading artifacts, and evaluator inputs are not written, opened for forecast values, or modified. The lineage manifest and candidate forecast rows are isolated JSONL/JSON artifacts under the pilot directory and link back to raw/extracted hashes. Labels and market scoring are out of scope.

## 8. Stop conditions

Stop the pilot and write `PILOT_BLOCKED` or `REBUILD_BLOCKED` if any of the following occurs:

1. official route does not expose exact raw GRIB2 plus matching `.idx` for a predeclared included fixture;
2. historical retention cannot be proven for the historical fixture;
3. exact issue/init or availability timing cannot be proven against D-1 12Z;
4. a raw response is truncated, hash-mismatched, unreadable, or not GRIB2;
5. `.idx` is missing or cannot identify the requested native 2 m TMP message;
6. native member identity, valid timestamps, units, or grid metadata are absent/conflicting;
7. local-day windows are incomplete or DST duration/offset mapping fails;
8. any missing member/step would require imputation or substitution;
9. any old archive, orphaned sidecar, proxy source, target-day label, production/trading/evaluator integration is observed;
10. focused QC reruns are not byte-identical;
11. any request would exceed the bounded fixture/object budget or violate the serialized >=10s spacing.

The expected missing fixture is a deliberate fail-closed test, not a reason to substitute a neighboring date or source. A missing/failed fixture is reported explicitly in the final receipt.

## 9. Acceptance and disposition

`PILOT_PASS` requires all included fixtures to satisfy raw/hash/timing/member/grid/valid-time/geometry/deterministic replay gates, the intentional missing fixture to fail closed, and two focused QC runs to produce byte-identical manifest and candidate rows. Any hard gate failure yields `PILOT_BLOCKED`; inability to establish exact official raw provenance yields `REBUILD_BLOCKED`. No status authorizes bulk acquisition or scoring.

Accuracy-focused suggestion for a later separately approved phase: once a full-member, lineage-complete panel exists, compare trajectory-derived station-local extrema against native 2 m `TMAX`/`TMIN` message semantics in a predeclared validation set; use that comparison to choose the least biased extrema geometry before any calibration or scoring.
