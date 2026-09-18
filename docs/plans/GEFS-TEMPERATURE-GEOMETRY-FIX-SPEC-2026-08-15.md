# GEFS Temperature Geometry Fix Specification

**Date:** 2026-08-15 UTC  
**Status:** Frozen for the bounded provenance pilot  
**Scope:** read-only GEFS raw-object geometry validation only

## 1. Scope and non-goals

This fix validates whether retained official GEFS GRIB2/`.idx` objects can represent a complete station-local calendar-day HIGH/LOW forecast geometry. It does not score forecasts, read outcomes/labels, tune calibration, build a common key set, touch production/trading/evaluator code, or perform a bulk rebuild. A two-member pilot remains a provenance/geometry diagnostic and is not an accuracy panel.

Every missingness, provenance, cadence, native-field, or replay failure is fail-closed. No interpolation, target-day observation, source fallback, member padding, or gap filling is allowed. No scoring is permitted until every intended fixture/member/station passes the complete geometry contract.

## 2. Frozen source and vintage contract

- Source: official NOAA GEFS PDS `pgrb2ap5` GRIB2 objects with their official `.idx` sidecars.
- Pilot members: `gec00`, `gep01`; source-issued member identity must agree with the decoded GRIB metadata.
- Fixture issue: the frozen 00Z run on D-1 (`issue = target_date_local - 1 day, 00:00:00Z`).
- Availability cutoff: `12:00:00Z` on D-1. Both native issue time and provider `Last-Modified` must be no later than this cutoff. Retrieval time is not an availability substitute.
- Raw object and sidecar bytes are immutable evidence. Every object is identified by URL/path, byte count, SHA-256, and extracted message SHA-256/byte range.
- Required lead range for this pilot: `f000` plus forecast leads `f006, f009, ..., f054`. A future boundary recovery may add only explicitly listed official leads (for example `f057`); it may not rewrite or fill an existing object.

## 3. Station-local calendar-day window

For each station and target date `D`, use the station's frozen IANA timezone and the installed tzdb through Python `zoneinfo`:

```text
local_start = D 00:00:00 in station IANA timezone
local_end   = (D + 1 calendar day) 00:00:00 in the same timezone
window      = [local_start, local_end)  # start inclusive, end exclusive
```

Valid timestamps are native GRIB `validityDate`/`validityTime` converted to UTC, then mapped to the station timezone. The endpoint is never included. Local timestamps retain their UTC offset and `fold` so fall-back's repeated hour is not collapsed.

Elapsed UTC window duration must be exactly:

- 24 hours for an ordinary local day;
- 23 hours for a spring-forward local day;
- 25 hours for a fall-back local day.

A fixed 24-hour window is invalid. `America/Phoenix` has no DST in these fixtures and therefore remains 24 hours.

## 4. Valid timestamps and cadence

The trajectory field is native 2 m temperature (`TMP`, GRIB short name `2t`, units `K`, `heightAboveGround`, level 2). Forecast trajectory points must satisfy `valid = issue + lead`, be unique, lie in the half-open local-day window, and follow the frozen 3-hour cadence. The expected in-window point leads are the multiples of three in the declared lead range whose native valid timestamp is in the window.

A point at an exact local-day boundary is not manufactured when the boundary is not a 3-hour lead. Instead, boundary coverage is proven by native interval products: `TMAX` (`stepType=max`) and `TMIN` (`stepType=min`) interval messages must form a gap-free union covering the entire `[local_start, local_end)` window in UTC. The interval check is performed independently for TMAX and TMIN. An interval ending exactly at `local_end` covers the endpoint but does not include it in the event reduction.

The validator reports exact unrepresented boundary leads (for example a local start at lead 5 or an end at lead 29) separately from exact missing source leads needed to close an interval gap (for example `f057` when retained intervals end at lead 54 and the window ends at lead 55). It never infers a value for either category.

## 5. HIGH/LOW reduction and native cross-check

For a geometry-complete member/station/fixture row:

- `HIGH = max(TMP trajectory values)` over all expected in-window points;
- `LOW  = min(TMP trajectory values)` over all expected in-window points.

Both events use the same complete, half-open station-local window; they are emitted separately. Empty, duplicate, out-of-window, or incomplete point sets fail closed.

Where native interval fields are present, the validator requires the `.idx` semantics and decoded metadata to agree (`TMAX`/`TMIN`, units `K`, 2 m height, `max`/`min`, and declared start/end steps). It cross-checks native TMAX against sampled TMP values in its interval (`TMAX >= sampled TMP max`) and native TMIN (`TMIN <= sampled TMP min`), plus `TMAX >= TMIN`. It reports full-interval native extrema as a semantic cross-check only; it does not substitute native interval values for missing trajectory points.

## 6. Missingness and disposition

Missing GRIB2, missing `.idx`, hash mismatch, ambiguous field index, parse failure, issue/availability cutoff failure, member mismatch, invalid valid-time relationship, duplicate lead, cadence failure, native interval gap, or absent expected fixture is a hard failure. The predeclared expected-missing fixture must retain its exact failed requests and fail closed; it is not an eligible pass.

The receipt is `PILOT_PASS` only if every included fixture/member/station has complete point cadence plus independent TMAX and TMIN interval coverage, native cross-checks pass, the expected-missing fixture matches its declared 404s, and two runs produce byte-identical artifacts. Otherwise the receipt is `PILOT_BLOCKED` and lists exact missing leads and hashes.

## 7. Deterministic artifacts and replay

The read-only validator emits canonical UTF-8 JSON/JSONL with sorted keys, stable row ordering, no runtime timestamps, and explicit validator/spec/source hashes:

- raw object/sidecar/message hash lineage;
- station-local geometry rows and HIGH/LOW extrema;
- native TMAX/TMIN coverage and semantic cross-check rows;
- missingness ledger with exact expected/present/missing leads and interval gaps;
- deterministic QC report.

Run the validator twice against the same immutable raw root and compare every artifact byte-for-byte. Any drift blocks the pilot.

## 8. Later accuracy work (not authorized here)

Once a full-member, geometry-complete panel exists, an accuracy-only experiment should compare trajectory-derived HIGH/LOW against native interval-derived extrema on a predeclared holdout, stratified by station, event type, DST class, and boundary coverage. That suggestion is intentionally deferred: no score, calibration, evaluator integration, trading, production wiring, or bulk rebuild may begin from this pilot.
