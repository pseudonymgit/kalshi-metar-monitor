# GEFS Temperature `f057` Boundary Recovery Specification

**Date:** 2026-08-15 UTC  
**Status:** Frozen for one fixed-provenance pilot only  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`  
**Prior root (immutable):** `data/raw/gefs_temperature_pilot/2026-08-15/`  
**New root:** `data/raw/gefs_temperature_pilot/2026-08-15-f057-recovery-v1/`

## 1. Objective and hard boundaries

Recover the exact missing official GEFS boundary lead `f057` needed to close native TMAX/TMIN interval coverage for the already frozen four included fixtures. This is a bounded provenance/geometry pilot, not a rebuild. No scoring, accuracy evaluation, production/trading/evaluator integration, bulk acquisition, calibration, or target-day observation access is permitted.

The prior 2026-08-15 pilot root is never overwritten. The expected-missing fixture and its two exact 404 records remain unchanged. No other lead, date, member, station, source, or route may be added.

## 2. Frozen source route and request contract

Official NOAA GEFS AWS PDS only:

```text
https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/00/atmos/pgrb2ap5/{member}.t00z.pgrb2a.0p50.f057
https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/00/atmos/pgrb2ap5/{member}.t00z.pgrb2a.0p50.f057.idx
```

The source vintage remains the frozen 00Z cycle on D-1 and the availability cutoff remains D-1 12:00:00Z. Each request is serialized with a minimum 10-second gap measured from the prior network response boundary to the next request boundary. Retrieval time is not treated as availability. Preserve exact URL/key, request/retrieval timestamps, status, all response headers (including Content-Length, Last-Modified, ETag, Content-Type, Accept-Ranges), error evidence, raw byte count/SHA-256, native decoded header metadata, source member identity, and matching `.idx` TMP/TMAX/TMIN index lines.

No inferred sizes, hashes, headers, timestamps, member IDs, interval endpoints, or values are written. Expected object sizes and hashes are populated only from the retrieved bytes and recorded HTTP/native evidence.

## 3. Exact recovery inventory

The object is shared by stations; station rows below identify why each exact object is required. Required object set is **16 objects total: 8 GRIB2 plus 8 matching `.idx` sidecars**, covering 4 dates × 2 members × 2 object kinds. No KATL/KNYC-only object is requested because their current native interval geometry already passes; no station-specific duplicate is created.

| Fixture / target local date | Member | Official object URL (GRIB2) | Official object URL (`.idx`) | Stations whose blocked boundary it closes |
|---|---|---|---|---|
| `ordinary_2024_01_15` / 2024-01-15 | `gec00` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20240114/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |
| `ordinary_2024_01_15` / 2024-01-15 | `gep01` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20240114/00/atmos/pgrb2ap5/gep01.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |
| `dst_spring_2024_03_10` / 2024-03-10 | `gec00` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20240309/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KPHX, KSEA |
| `dst_spring_2024_03_10` / 2024-03-10 | `gep01` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20240309/00/atmos/pgrb2ap5/gep01.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KPHX, KSEA |
| `dst_fall_2024_11_03` / 2024-11-03 | `gec00` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20241102/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |
| `dst_fall_2024_11_03` / 2024-11-03 | `gep01` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20241102/00/atmos/pgrb2ap5/gep01.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |
| `historical_2025_11_15` / 2025-11-15 | `gec00` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20251114/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |
| `historical_2025_11_15` / 2025-11-15 | `gep01` | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.20251114/00/atmos/pgrb2ap5/gep01.t00z.pgrb2a.0p50.f057` | same URL + `.idx` | KDEN, KPHX, KSEA |

For every GRIB2 row above, retrieve the exact URL shown. For its sidecar, append `.idx` to that URL. These are the only permitted network URLs. The expected-missing probe remains exactly `missing_expected_2020_01_15/gec00/f024` GRIB2 and `.idx` in the prior manifest and is not retried or changed.

## 4. Provenance and native-header requirements

For each successful object, retain verbatim bytes and record:

- HTTP request start/end/retrieval UTC timestamps and monotonic serialized-gap evidence;
- status and complete response metadata;
- URL, canonical S3 key, path in the new root, content length, raw SHA-256;
- native `dataDate/dataTime`, `validityDate/validityTime`, `step`, `startStep/endStep`, `stepType`, `shortName`, units, level/type-of-level, grid metadata, `number`/`perturbationNumber`, and decoded native member ID;
- matching `.idx` SHA-256, size, and exact selected TMP/TMAX/TMIN index lines and byte ranges where decoded;
- source-manifest linkage to the prior manifest hashes and the recovery spec hash.

For `f057`, native valid time must equal issue + 57 hours. `f057` is an interval-boundary object, not an inferred station value and not a replacement for any prior lead. If either official object or sidecar is unavailable, preserve the exact non-200 response and stop the affected recovery; do not substitute `f054`, `f060`, another date, another member, another route, or any local archive.

## 5. Geometry and replay gates

Using the new immutable root, rerun the focused geometry validator twice. The recovery validator must prove:

1. all prior 144 successful official GRIB2/`.idx` objects remain byte-identical;
2. exactly the 16 listed `f057` requests are added, with no other request-set mutation;
3. complete local-day half-open `[local_start, local_end)` coverage, with 24/23/25-hour ordinary/spring/fall handling and Phoenix 24-hour control;
4. complete 3-hour TMP trajectory cadence, with no target-day leakage or synthesized points;
5. independent native TMAX and TMIN interval coverage, including the `f057` closure of exact prior gaps;
6. native TMAX/TMIN semantics and native cross-checks pass, including TMAX ≥ TMIN pair ordering;
7. native issue/member IDs match the frozen source contract and all availability times meet D-1 12Z;
8. expected-missing fixture remains the exact two 404s, fail-closed;
9. validator output manifests/rows are byte-identical between run 1 and run 2.

`PILOT_PASS` is allowed only if every included geometry row passes all gates. If any official `f057` request is unavailable or any gate fails, receipt status is `PILOT_BLOCKED`; exact 404/non-200 evidence and hashes of all successful objects are retained.

## 6. No-inference policy and later suggestion

No interpolation, boundary padding, scalar substitution, target-day observation, fallback source, archive sidecar, member relabeling, or inferred interval is allowed. A missing `f057` remains missing.

Accuracy-focused suggestion for a separately approved later phase: on a locked, full-member holdout, compare trajectory-derived HIGH/LOW against native interval-derived TMAX/TMIN by station, event side, DST class, and boundary-coverage class; use the result only to choose a documented extrema representation. This pilot does not authorize that comparison, scoring, or rebuild.
