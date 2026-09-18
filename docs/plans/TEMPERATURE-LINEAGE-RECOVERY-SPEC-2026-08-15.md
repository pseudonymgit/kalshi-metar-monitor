# Temperature Forecast Lineage Recovery Specification

**Date:** 2026-08-15 UTC  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`  
**Scope:** research/QC/data lineage only. No production, trading, sweep, HMM, or ECMWF-combined changes.  
**Disposition target:** recover GEFS provenance and local-day HIGH/LOW geometry if, and only if, retained evidence proves them; otherwise define a minimal bounded rebuild.

## 1. Purpose and fail-closed rule

The existing temperature evaluator is blocked because an archived scalar is not automatically an auditable forecast vintage and `step=24` is not automatically a station-local daily HIGH or LOW. This specification separates three independent gates:

- **Gate A — forecast provenance:** exact issue/init timestamp, availability time, source object/hash, and member IDs.
- **Gate B — target geometry:** valid timestamps covering the station-local calendar day for HIGH/LOW, including timezone and DST rules and the required trajectory/window steps.
- **Gate C — outcome labels:** historical Kalshi/NWS settlement HIGH/LOW labels are valid outcome truth under temporal separation. SRRS is not required for core scoring; it is optional corroboration only.

No forecast is scored unless every applicable gate passes for the exact `(station, target_date_local, event_type)` key. Missing evidence is a rejection, not an invitation to infer or impute.

## 2. Canonical key, scope, and temporal barrier

The canonical forecast/outcome key is:

```text
(station, target_date_local, event_type ∈ {HIGH, LOW})
```

A lineage row is keyed more finely by:

```text
(source_object_hash, station, target_date_local, event_type,
 issue_time_utc, valid_time_utc, member_id)
```

The fixed station universe is the 20-station registry in `data/station_mapping.json`. HIGH and LOW are separate events. The target date is the station's local climate calendar date, not a UTC date copied from a filename or database row.

Use a common information cutoff of **12:00:00 UTC on calendar day D-1** unless a later frozen evaluation contract explicitly replaces it. Both `issue_time_utc` and `availability_time_utc` must be at or before the cutoff. Target-day observations, target-day aggregates, settlement values, and later source revisions are label-side only.

## 3. Gate A — forecast provenance

### 3.1 Required evidence

Every selected source object/member must have all of:

1. **Exact issue/init timestamp:** UTC date and cycle from native GRIB/NetCDF metadata or an immutable provider object/request record. A bare `12Z`/`00Z` label, target date, directory name, or expected publication schedule is insufficient to date a run.
2. **Availability time:** auditable time at which this exact object/revision was obtainable by the cutoff. Retrieval time may document local acquisition, but cannot be relabeled as provider availability; if provider availability is absent, the row remains unavailable for cutoff scoring unless a frozen object publication record proves it.
3. **Source object identity:** canonical request URL/object key, provider, dataset, member, grid/variable selection, and response/revision identifier where available.
4. **Immutable content identity:** SHA-256 of the raw object (or a cryptographically linked immutable archive object). A hash of a derived SQLite row, compressed member blob, or cfgrib index alone is not a raw-source hash.
5. **Member identity:** native member number/ID mapped to a stable identifier (`gec00`, `gep01`... or provider-equivalent), with expected panel size and an explicit missing-member ledger.

The evidence record must retain the raw object, request metadata, retrieval timestamp, and hash together. A sidecar is acceptable only if it is complete, independently hashed, and one-to-one with the source object.

### 3.2 Recoverable evidence and no-inference rules

**Recoverable:** native GRIB keys (`dataDate`, `dataTime`, `number`, `step`, valid-time keys), a retained raw object, a request/response manifest with URL and response hash, provider inventory/object metadata with an auditable timestamp, or a complete ingestion sidecar.

**Not sufficient and never promoted by inference:**

- `target_date`, SQL row order, database mtime, filesystem mtime, checkpoint completion time, or an ingestion `fetch_timestamp` as issue/availability;
- a cycle-only value such as `init_cycle='12Z'` or a filename date without native run metadata;
- a cfgrib `.idx` hash as a substitute for the deleted GRIB object's hash;
- member count, blob byte length, array position, or expected ordering as member identity;
- an HTTP URL or script request template without the exact response/object hash;
- provider publication schedules used to backdate availability.

If multiple issue/revision candidates remain possible, reject as `ambiguous_issue_origin`. If availability cannot be proven, reject as `missing_availability_provenance`. Do not substitute another cycle/source or impute missing members.

### 3.3 Gate A acceptance

Gate A passes for a selected row only when issue, availability, object/hash, and member identity are all present, unique, and cutoff-eligible. The manifest must report `n_expected`, `n_present`, `n_missing`, member IDs, source hash, and any competing/rejected revisions.

## 4. Gate B — target geometry

### 4.1 Local calendar-day window

For station timezone `tz` and local date `D`, construct the timezone-aware interval:

```text
local_start = D 00:00:00 in tz
local_end_exclusive = (D + 1 day) 00:00:00 in tz
```

Convert both endpoints to UTC using the versioned IANA timezone database and record the UTC offset at each endpoint. The window is half-open `[local_start, local_end_exclusive)`, so DST transition days naturally have 23 or 25 elapsed hours while retaining the same local calendar boundaries. Do not assume a fixed 24-hour UTC interval.

A valid trajectory/window must include valid timestamps inside the complete local-day interval according to the declared source accumulation/instant semantics. It must report first/last valid timestamps, count, spacing, local offsets, and whether the source field is an instantaneous temperature or an interval maximum/minimum. The source's native valid-time/step semantics must be preserved.

### 4.2 HIGH/LOW geometry requirement

Daily HIGH and LOW require a trajectory/window over valid timestamps, not one arbitrary `step=24` scalar. A source may pass only if its native `tmax`/`tmin` accumulation window is proven to correspond to the station-local climate day, or if a sufficiently dense instantaneous trajectory covers that local-day window and the deterministic extrema reduction is predeclared. A single scalar cannot be treated as both HIGH and LOW.

Required manifest fields include:

```text
station, target_date_local, timezone, dst_rule,
local_start, local_end_exclusive, window_start_utc, window_end_utc,
valid_time_utc[], step_hours[], field/accumulation semantics,
first_valid_time_utc, last_valid_time_utc, window_complete
```

No target-date label may be used to manufacture `valid_time_utc`. A native `step` becomes a valid timestamp only after exact issue/init time is known; `valid_time = issue + step` must agree with native valid-time metadata when both exist.

### 4.3 Gate B acceptance

Gate B passes only when all selected members share the same proven geometry, every event's local-day window is complete, timezone/DST conversion is deterministic, and no target-day observation or post-cutoff field is used. Missing steps remain missing; no interpolation, persistence, climatology, or member/step imputation is permitted.

## 5. Gate C — outcome labels

Historical Kalshi settlement rows are valid outcome truth for core scoring when the exact station/date/event key is identifiable and the settlement value is retained. The series identity (`KXHIGH...` versus `KXLOW...`) defines event type; preserve the historical Kalshi/NWS settlement value and its source fields. A core score does **not** require SRRS.

Gate C still requires:

- exact event key identity with no duplicate conflicting values;
- explicit handling of duplicate identical rows and rejection of conflicting revisions;
- label acquisition/settlement after the forecast cutoff (temporal separation);
- no outcome, target-day observations, or corrected labels entering prediction-side inputs.

An official NWS/CLI raw payload, report ID, or SRRS record may improve label provenance and resolve revisions, but its absence alone does not block core scoring if the historical Kalshi settlement record is complete, exact, and temporally separated. Do not silently use a non-identical station, series, or date.

## 6. Conditional build if Gates A+B are recoverable

Build only a read-only deterministic lineage manifest; do not mutate `gefs_archive.db` or production code. One row per source object/member/valid time and one deterministic event-level reduction record are required. Sort by a stable canonical key and serialize with sorted keys and fixed separators. Record:

- source object/request/hash and all input hashes;
- station, event, target-local date, issue, availability, cutoff;
- member ID, expected/present/missing IDs, value and units;
- complete local-day window and native field semantics;
- outcome key identity only in the label-side attachment;
- code/config/schema/query hashes and exclusion reasons.

The evaluator may consume this manifest only from an isolated research script. It must fail closed if any row lacks a required field. It must never read HMM, trading, production, sweep, or combined ECMWF inputs.

## 7. Minimal rebuild if any required lineage/geometry evidence is absent

Do not start bulk acquisition in this recovery task. The smallest safe rerun is a bounded, representative acquisition first, then expansion only after QC passes:

1. Freeze the exact station/date/event population and cutoff contract.
2. For each selected GEFS issue/cycle and member, fetch and retain the **raw GRIB/NetCDF/object**, exact request URL/object key, provider response/revision metadata, retrieval timestamp UTC, issue/init timestamp, valid times/steps, member ID, variable/level/grid, and SHA-256.
3. Retain the raw response before parsing; never delete it after writing a summary DB row. Store a sidecar manifest atomically with the object.
4. Request a trajectory/window sufficient for the full local-day HIGH/LOW geometry. Do not request or score only one arbitrary `f024`/`step=24` scalar.
5. Parse read-only into a new isolated lineage store. Preserve native metadata and map station timezone/DST with the pinned IANA ruleset.
6. Run all QC gates below on a small sample spanning standard time, DST spring-forward, DST fall-back, missing-member, and missing-step cases. Expand only if the sample is deterministic and passes.

The rebuild must not repair old rows by guessing their vintage. Old rows can remain as blocked historical artifacts.

## 8. Phase 3 focused QC gates

The isolated QC command must test and fail closed on:

1. **No target-day leakage:** prediction-side manifest contains no outcome/target observation fields; mutating labels cannot change source rows, eligibility, or forecast reductions.
2. **Issue/availability cutoff:** exact UTC timestamps exist and both satisfy the frozen D-1 cutoff; post-cutoff or unknown rows are rejected.
3. **Local timezone/DST window:** zoneinfo conversion produces correct 23/24/25-hour UTC spans and all valid timestamps map to the requested local date.
4. **Complete member identity:** IDs are unique, expected panel is explicit, missing IDs are listed, and no value is padded/imputed.
5. **Missingness:** NaN/non-finite/missing steps/members reject the affected event; no silent drop that changes the declared panel.
6. **Deterministic manifest:** same immutable inputs/code/config produce byte-identical manifest and event reductions; stable sorting and hashes are checked.
7. **Historical Kalshi outcome key identity:** exact station/date/event mapping, duplicate/conflict policy, and temporal separation are checked; SRRS is not a prerequisite.
8. **Geometry:** HIGH/LOW reduction uses the complete trajectory/window and native field semantics, never an arbitrary scalar.

No score is emitted when any hard gate fails; report `BLOCKED` or `REBUILD_REQUIRED` with machine-readable reasons.

## 9. Rebuild acceptance criteria

A rebuild is accepted only when:

- every scored row passes Gates A, B, and C;
- raw source objects and sidecars are retained and hash-verifiable;
- issue/availability, member IDs, valid timestamps, timezone/DST, and event geometry are complete;
- no target-day leakage or imputation is detected;
- exact historical Kalshi outcome keys are temporally separated and conflict-free;
- two independent reruns are byte-identical;
- all input, code, config, schema/query, manifest, and output hashes are recorded;
- the first scoring run is isolated and read-only.

Until then, the proper disposition is `REBUILD_REQUIRED` (missing recoverable artifacts) or `BLOCKED` (evidence cannot be recovered without inference). No accuracy result is valid.

## 10. Accuracy-focused suggestion

After lineage repair, first test a station × event-type rolling bias correction with spread-preserving empirical calibration, fit only on strictly prior settled outcomes and evaluated on a locked future shadow. Keep the raw ensemble spread and report MAE, CRPS, interval coverage, and effective member count together; this targets airport/grid local-extreme bias without hiding geometry or provenance errors.
