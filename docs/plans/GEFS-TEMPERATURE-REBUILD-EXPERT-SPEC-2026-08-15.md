# GEFS Temperature Provenance / Geometry Rebuild — Expert Specification

**Date:** 2026-08-15 UTC  
**Status:** design/specification only; no acquisition, database mutation, scoring, tournament, production, or trading wiring  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`  
**Spec ID:** `gefs-temperature-rebuild-expert-v1.0-2026-08-15`

## 1. Decision summary

The existing GEFS temperature archive is not a recoverable forecast lineage. The lineage audit found:

- **0** retained raw GRIB/NetCDF objects;
- **51,639** orphan `.idx` sidecars, which are evidence of prior discovery/download attempts but are not forecast values;
- **7** missing member sidecars in the retained sidecar inventory;
- **365,960** archive rows with an undated `12Z` cycle label but no proven dated initialization, availability, or member identity;
- station mapping without a pinned timezone/DST source.

Those rows remain forensic-only. They must not be relabeled with an inferred issue date, padded to a nominal ensemble size, or used in the temperature tournament. A rebuild creates a new immutable lineage rather than repairing the old table in place.

The rebuild has two temporal products:

1. **`AS_OF_CERTIFIED`** — eligible for a cutoff-constrained tournament only when initialization and availability are proven to precede the frozen cutoff.
2. **`ARCHIVE_REPLAY`** — useful for parser/geometry and descriptive research, but not eligible for a leakage-sensitive accuracy score when historical availability cannot be proven.

The default disposition of the current archive is `BLOCKED / FORENSIC_ONLY`.

## 2. Scope and non-goals

### In scope

- official GEFS source and request contract;
- immutable raw-object/request capture;
- dated initialization, availability, valid-time, lead, and member metadata;
- exact 20-station/grid geometry;
- IANA timezone and NWS local-standard-time climate-day conversion;
- complete valid-time trajectories for daily HIGH and LOW;
- member and timestep missingness;
- resumable, content-addressed ingestion;
- pilot and full historical rollout;
- storage, bandwidth, and wall-clock estimates;
- QC gates and deterministic replay.

### Explicitly out of scope

- acquiring data in this documentation task;
- changing `data/gefs_archive.db` or any existing GEFS table;
- HMM, ECMWF/TIGGE fusion, HRRR substitution, or production forecast wiring;
- calibration fitting, trading, alert delivery, or a performance claim;
- reconstructing missing historical availability from filesystem timestamps, database timestamps, `fetch_timestamp`, or an undated `12Z` label.

## 3. Frozen target contract

The forecast target key is:

```text
(station_id, target_date_local, event_type ∈ {HIGH, LOW})
```

The station universe is exactly:

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

`TEST`, unknown stations, city-only aliases, and grid cells not linked to one of these station IDs are excluded.

The outcome side is a separate label ledger. It must retain the exact NWS CLI/Kalshi event identity and must never be used while constructing the forecast-side rows. HIGH and LOW rows for the same station/date are distinct targets.

Canonical forecast unit is **degrees Fahrenheit**, with the source unit and conversion formula retained. Do not round before storing a trajectory or calculating continuous metrics. If the eventual settlement target is an integer °F CLI value, preserve both the raw forecast value and the explicitly documented evaluation rounding rule.

## 4. Official GEFS source contract

### 4.1 Primary endpoint

Use NOAA/NCEP's official GEFS product archive on NOMADS as the source of record:

```text
https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.YYYYMMDD/HH/atmos/
```

where `YYYYMMDD` and `HH` are the **dated GEFS initialization date and UTC cycle**. The acquisition implementation must resolve the exact published file path from the directory listing, not synthesize a filename and assume success.

Expected atmospheric GRIB2 members are the control and perturbed ensemble files, conventionally named along the lines of:

```text
gec00.tHHz.pgrb2ap5.fFFF
gep01.tHHz.pgrb2ap5.fFFF
...
 gep30.tHHz.pgrb2ap5.fFFF
```

The exact naming/layout, variable inventory, grid definition, and forecast-hour availability must be read from the dated source listing and GRIB metadata for each vintage. The implementation must not assume that a current layout applies to every historical date.

A NOAA-hosted mirror may be used only as a separately recorded source candidate if the source owner, object identity, byte content, and request semantics are proven equivalent. It is not silently interchangeable with NOMADS. If the primary endpoint is unavailable during a live capture, mark the request unresolved; do not substitute another model or cycle.

### 4.2 Required source records

For every requested object or byte-range response, capture an append-only request record containing:

- canonical URL and any query string;
- HTTP method, request headers relevant to identity, and byte `Range` header;
- response status, response headers (`ETag`, `Last-Modified`, `Content-Length`/`Content-Range`, `Content-Type`, `Accept-Ranges`);
- request start/end UTC timestamps and first successful observation timestamp;
- retry number, failure class, and `Retry-After` if supplied;
- raw response byte count and SHA-256;
- path in the content-addressed object store;
- parser, acquisition, and configuration git/version identifiers.

A `200` response to a range request, a truncated `206`, a content-type mismatch, an HTML error body, or a changed ETag for the same request fingerprint is a hard QC event. Preserve it and do not parse it as forecast data.

### 4.3 Availability rule

`available_time_utc` is not the local filesystem mtime and is not the time the old archive row was inserted. For a new `AS_OF_CERTIFIED` capture it must be derived from an auditable source-availability observation made by the acquisition runner, with a documented relation to the cutoff. At minimum retain:

- the first successful complete object/listing observation;
- source `Last-Modified`/ETag and listing evidence;
- acquisition clock and clock-sync status;
- the cutoff against which eligibility is evaluated.

A retrieval performed years after the target date does **not** prove that the object was available by the historical cutoff. If an official historical publication/availability record cannot establish that fact, set `availability_status=UNKNOWN` and classify the row as `ARCHIVE_REPLAY`, never `AS_OF_CERTIFIED`.

`fetch_timestamp` from `gefs_archive` is ingestion time only and must not be promoted to availability.

## 5. Initialization, cycle, lead, and valid metadata

### 5.1 Initialization identity

Every parsed GRIB message must agree with the filename and GRIB keys for:

- `model = GEFS` and product/vintage if exposed;
- `init_date_utc`, `init_time_utc`, and `init_cycle` (`00Z`, `06Z`, `12Z`, `18Z`);
- member class (`control` or `perturbed`), member number, and expected member universe;
- variable (`2 m temperature` / `TMP` at the exact level and discipline);
- grid/projection, latitude/longitude, resolution, and scan order;
- `forecast_reference_time`, `step`, `step_start`, `step_end`, and `valid_time_utc`;
- source file/object and byte offset/range.

The canonical issue/initialization timestamp is the actual dated GEFS reference time, not a target date, database partition date, or cycle label by itself.

### 5.2 Cutoff selection

The tournament must freeze one selection policy before reading outcomes. Recommended policy:

> For target local date `D`, choose the latest GEFS cycle whose required trajectory objects have a proven `available_time_utc <= 12:00:00 UTC on D-1`, with a fixed minimum operational safety margin. If no cycle meets the proof, exclude the target rather than selecting a later or earlier cycle opportunistically.

The safety margin and whether the eligible cycle is 00Z, 06Z, or 12Z must be configuration, not inferred per row. A cycle selected by a historical backfill without availability proof is `ARCHIVE_REPLAY` only.

Each forecast/event row must expose:

```text
init_time_utc
available_time_utc
availability_status
cutoff_time_utc
cycle
lead_to_window_start_hours
lead_to_window_end_hours
selection_policy_version
```

### 5.3 Valid-time trajectory

The derived trajectory is the ordered set of all source valid times in the selected forecast window. Store each point, rather than only a `step=24` scalar:

```text
(station_id, target_date_local, init_time_utc, member_id,
 valid_time_utc, valid_time_local, valid_time_is_dst,
 lead_hours, value_f, source_object_hash, source_message_offset)
```

Use every available GEFS valid time covering the target window. Do not interpolate, smooth, or fill missing points in the canonical trajectory. If GEFS changes temporal spacing at longer leads, retain the native spacing and report it. A trajectory with a gap remains gapped.

## 6. Station and grid geometry

### 6.1 Station identity source

`data/station_mapping.json` is an input mapping cache, not sufficient geometry authority by itself. Before acquisition, freeze a versioned station registry derived from the exact NWS/Kalshi settlement station identity and an authoritative station metadata source. Retain:

- station ID, NWS `issuedby`, NWS office, CLI URL, station name;
- latitude, longitude, elevation, and coordinate source;
- mapping version and verification timestamp;
- source-side station identifier and any alias mapping;
- distance and bearing from the selected GEFS grid cells.

The current mapping contains no timezone and its KNYC coordinates must be independently checked: the settlement name is Central Park, not a New York airport coordinate. No forecast may use the current coordinate blindly when the authoritative station record disagrees.

### 6.2 Extraction method

The baseline extraction method is deterministic **nearest GEFS grid cell** from the frozen station coordinate. For each station and source grid, store:

- grid identifier and resolution;
- selected `i,j` indices and cell-center latitude/longitude;
- great-circle distance in km;
- four neighboring cell coordinates and values if available;
- longitude convention, latitude ordering, and scan mode;
- land/sea or orography metadata if present;
- extraction method/version.

Bilinear interpolation may be computed as a separately named challenger, but must not replace the baseline or be mixed within a run. A station outside the source domain or beyond a predeclared maximum distance is ineligible. The grid mapping is frozen before outcomes are attached.

### 6.3 Twenty-station timezone registry

Pin the IANA tzdb release and map the 20 stations as follows, subject to the station-registry verification gate:

| Station IDs | IANA timezone | Standard offset used for NWS climate boundary |
|---|---|---|
| KATL, KBOS, KDCA, KMIA, KNYC, KPHL | `America/New_York` | UTC-05:00 |
| KAUS, KDFW, KHOU, KMDW, KMSP, KMSY, KOKC, KSAT | `America/Chicago` | UTC-06:00 |
| KDEN | `America/Denver` | UTC-07:00 |
| KLAS, KLAX, KSEA, KSFO | `America/Los_Angeles` | UTC-08:00 |
| KPHX | `America/Phoenix` | UTC-07:00, no DST |

The normalized list is:

```text
America/New_York:   KATL KBOS KDCA KMIA KNYC KPHL
America/Chicago:    KAUS KDFW KHOU KMDW KMSP KMSY KOKC KSAT
America/Denver:     KDEN
America/Los_Angeles: KLAS KLAX KSEA KSFO
America/Phoenix:    KPHX
```

Pin:

- IANA tzdb release and zoneinfo file hash;
- NWS climate-day/local-standard-time interpretation source;
- station-to-zone mapping hash;
- conversion library/version and test vectors around both DST transitions.

## 7. Daily HIGH/LOW geometry

### 7.1 NWS climate-day interval

The target date is the station's local climate date, not the UTC calendar date. NWS daily climate products use midnight-to-midnight **local standard time** year-round. Therefore define the target window using the station's fixed standard offset:

```text
window_start_utc = D 00:00 at station standard offset
window_end_utc   = (D + 1 day) 00:00 at station standard offset
include valid_time_utc iff start <= valid_time_utc < end
```

On a daylight-time date, this corresponds to 01:00-to-01:00 on the local daylight clock. The implementation must retain both UTC and IANA-local renderings and test normal days, spring-forward, fall-back, and year-boundary dates. Do not use a fixed UTC date or a naive local-midnight conversion.

### 7.2 Event derivation

For each member with a complete-enough trajectory in the interval:

```text
member_high_f = max(value_f for valid points in [start, end))
member_low_f  = min(value_f for valid points in [start, end))
```

Store the argmax/argmin valid timestamp, lead, source message, and grid mapping. The event-level ensemble is the ordered set of member extrema, retaining member identity:

```text
(station_id, target_date_local, event_type, init_time_utc,
 member_id, event_value_f, extremum_valid_time_utc,
 trajectory_point_count, expected_point_count, missing_point_count)
```

The daily HIGH and LOW are separate projections of the same full trajectory. A step-24 value cannot stand in for either extreme. If the target interval has no valid points, or a member lacks the required trajectory coverage, mark the event/member missing. Never substitute another cycle, member, variable, station, climatology, or observation.

### 7.3 Native sampling limitation

GEFS's native time steps do not prove the continuous-time maximum/minimum between valid points. The canonical rebuild reports extrema over native valid times and records temporal spacing. Any future sub-grid interpolation or hourly reconstruction must be a separately versioned method and evaluated without changing the canonical source trajectory.

## 8. Member identity and missingness

### 8.1 Expected ensemble

For each dated cycle, derive `n_expected` from the verified source member listing and GRIB ensemble metadata. For a standard full GEFS cycle this is normally control `c00` plus perturbed members `p01`–`p30` (`n_expected=31`), but the value must be proven for the exact vintage. Never infer it from the count in the old database.

Every value must carry a stable member ID. `gec00`/`gepNN` filename identity, GRIB `number`, and ensemble type must agree. Duplicate IDs, unknown IDs, or a member whose object/hash changes during a run are hard failures.

### 8.2 Missingness accounting

Track missingness at four levels:

1. object/request (`not_found`, partial, checksum mismatch, unavailable by cutoff);
2. member/valid-time (missing source message or non-finite value);
3. member/trajectory (number and fraction of missing expected points);
4. station/event panel (members present, expected, and effective sample size).

No missing value may be zero-filled, mean-filled, climatology-filled, persistence-filled, copied from another cycle, or reconstructed from a target-day observation.

Recommended eligibility gates, frozen before the pilot:

- at least `ceil(0.80 * n_expected)` members with explicit IDs;
- each retained member has at least 95% of the expected native valid points in the event window;
- both window boundaries and at least one interior valid point are represented by the source trajectory;
- no unresolved conflicting duplicate object/message;
- `effective_N = 1 / sum(w_i^2)` is reported, not silently equated to `n_expected` when weights differ.

Rows failing a gate remain in the audit ledger with reason codes; they are not scored.

## 9. Immutable storage model

Use a new versioned namespace, never an in-place repair of `gefs_archive.db`:

```text
data/external/gefs_temperature_rebuild/<run_id>/
  manifests/
  requests/
  raw/sha256/<prefix>/<object>
  parsed/grib_messages/
  trajectories/
  derived_events/
  qc/
  checkpoints/
```

Required logical tables/files:

- `source_objects`: URL, request fingerprint, response metadata, hashes, byte ranges, source status;
- `source_listings`: dated directory/listing response and hash;
- `messages`: GRIB keys, variable/level, init, member, step, valid time, offsets, object hash;
- `station_grid_map`: station and grid geometry/version;
- `trajectory_points`: one row per member/station/valid time;
- `event_member_extrema`: HIGH/LOW extrema and missingness;
- `eligibility`: `AS_OF_CERTIFIED`, `ARCHIVE_REPLAY`, or excluded with reason codes;
- `run_manifest`: code/config/source/tzdb/spec hashes and deterministic counts.

Raw bytes, sidecars, listings, and manifests are immutable after verification. Derived tables may be regenerated into a new run ID; they must not overwrite prior outputs.

## 10. Resumability and crash safety

The acquisition runner is a finite-state machine:

```text
PLANNED → REQUESTED → STORED → HASH_VERIFIED → PARSED → DERIVED → QC_PASSED
                                      ↘ QC_FAILED / RETRYABLE / PERMANENTLY_MISSING
```

The request key is a canonical fingerprint of URL, query, method, range, and relevant headers. Resume behavior:

- consult the manifest before network access;
- skip only a content-addressed object whose size and SHA-256 already verify;
- write downloads to a temporary file, flush/fsync, hash, then atomically rename;
- checkpoint after each object, member, and target date;
- make retries bounded and exponential with `Retry-After` support;
- preserve partial responses as evidence, never as parseable objects;
- permit range reassembly only when every segment has its own hash and the final object hash verifies;
- record parser failures and missing members without aborting unrelated targets;
- rerun the same manifest/config and require byte-identical derived output.

The 51,639 orphan sidecars are not eligible resume artifacts unless a new run can prove their URL, exact object identity, complete member/path context, and hash relationship. In practice they should be inventoried as forensic evidence and reacquired under this contract. The seven missing member sidecars are explicit missingness, not permission to infer or pad members.

## 11. QC and acceptance gates

### Hard gates

- exact GEFS source/model and dated object identity;
- raw response and request manifest present and hash-verified;
- dated init/cycle and member identity agree across filename, listing, and GRIB keys;
- availability is proven for `AS_OF_CERTIFIED`, or row is demoted to `ARCHIVE_REPLAY`;
- exact station identity and frozen coordinate source;
- timezone/tzdb hash and local-standard-time window pass DST tests;
- complete valid-time trajectory metadata;
- no target-day or post-cutoff data in forecast construction;
- missingness and duplicate/conflict checks pass;
- deterministic rerun is byte-identical.

### Pilot promotion gates

- 100% of pilot rows have a valid dated initialization and explicit member IDs;
- 100% of station/grid mappings pass distance and coordinate review;
- 100% of DST test vectors produce expected UTC intervals;
- zero use of `fetch_timestamp`, filesystem mtime, or `12Z` alone as availability/issue;
- every accepted event has HIGH and LOW extrema derived from the same full trajectory policy;
- missingness is reported by member and valid time;
- a clean crash/resume rerun matches a clean run;
- any failed row is excluded with a stable reason code, never silently dropped.

Passing the pilot authorizes full **rebuild acquisition/QC**, not model advancement or trading.

## 12. Pilot plan

Use the five-station geometry/provenance pilot already identified in adjacent target-source reviews:

```text
KAUS, KDEN, KMDW, KNYC, KDFW
```

Use dates that deliberately include:

- one ordinary winter and summer date;
- the spring-forward transition;
- the fall-back transition;
- at least one date near a source/member missingness incident;
- one duplicate/retry/resume simulation.

Recommended pilot matrix: 30 target dates per station, with the same selected cycle policy and both HIGH and LOW. The acquisition should request the shared GEFS objects once and derive all five stations from those immutable bytes. Retain a hand-auditable sample of every member's trajectory and extrema.

Pilot outputs:

- source listing/request/raw-object manifest;
- object and sidecar hashes;
- dated init/availability certificate or explicit `ARCHIVE_REPLAY` classification;
- station-grid map with distances and four-neighbor geometry;
- timezone/DST test report;
- member/valid-time missingness report;
- trajectory and HIGH/LOW event samples;
- clean-vs-resumed byte comparison;
- QC disposition and unresolved issue ledger.

No accuracy score is reported if the pilot is only `ARCHIVE_REPLAY` or if any hard gate fails.

## 13. Full rollout

After pilot acceptance:

1. Freeze the source, parser, grid, timezone, cutoff, and selection-policy versions.
2. Build an immutable target manifest from the exact settlement date range and 20-station universe.
3. Acquire all required dated GEFS objects for the frozen historical window. Preserve raw bytes and request evidence before parsing.
4. Parse all members and valid times, then derive station trajectories and HIGH/LOW extrema in a separate deterministic pass.
5. Produce an eligibility manifest separating `AS_OF_CERTIFIED`, `ARCHIVE_REPLAY`, and excluded rows.
6. Run independent QC/replay from raw objects into a new output directory.
7. Only the non-empty, provenance-complete `AS_OF_CERTIFIED` population may enter the pre-registered temperature tournament.
8. Keep the old 365,960-row archive read-only as a lineage comparison; do not merge it into the rebuilt population.

Historical rows without availability proof may still be retained for geometry/parser diagnostics, but must be excluded from any evaluation claiming the `D-1 12:00 UTC` information barrier.

## 14. Storage, network, and wall-clock estimate

These are planning estimates, not acquisition measurements. The pilot must measure actual object sizes and response rates before the full run.

### Assumptions

- approximately 2021-08-19 through 2026-08-13 target dates;
- one selected cycle per target date under the frozen policy;
- 31 GEFS members when a complete cycle exists;
- native 3-hourly/6-hourly temperature points covering the local 24-hour climate window;
- GRIB index-assisted byte-range extraction of the required temperature messages and station-neighbor cells, not unconditional download of every variable;
- immutable raw responses plus parsed/derived metadata.

### Planning envelope

| Component | Pilot (5 stations × 30 dates) | Full rebuild (20 stations, historical window) |
|---|---:|---:|
| Requests/manifest records | 2,000–10,000 | 0.5–3 million |
| Selected raw ranges (compact mode) | 2–20 GB | 0.5–2 TB |
| Full source-file retention (optional audit tier) | 20–150 GB | 5–20 TB |
| Parsed trajectories/events/manifests | <1 GB | 20–100 GB |
| Wall clock at 100 Mbps, compact mode | 30–120 min | 12–72 h |
| Parse/QC/replay on 8–16 cores | 15–60 min | 4–16 h |

The compact estimate assumes byte-range retrieval of the exact temperature messages. If the endpoint disallows reliable range extraction or the implementation elects to retain complete GRIB2 files, use the full-file tier and plan additional disk, bandwidth, and retry time. The first pilot must record actual bytes per member, HTTP success rate, range count, parse throughput, and duplicate/retry rate; revise the full-run budget only from those measurements.

## 15. Suggested schema-level invariants

The following must be machine-checked:

```text
UNIQUE(source_url, byte_range, response_sha256)
UNIQUE(init_time_utc, member_id, variable, grid_id, valid_time_utc, source_message)
UNIQUE(station_id, target_date_local, event_type, init_time_utc, member_id)
```

And:

- `valid_time_utc = init_time_utc + lead` from parsed GRIB metadata;
- all event valid points lie in the declared NWS climate window;
- `event_value_f` equals the deterministic max/min of the retained member trajectory;
- `n_present <= n_expected` and member IDs are unique;
- `HIGH` and `LOW` for a target share the same init/cycle/geometry policy;
- forecast-side hashes do not depend on outcome-side values;
- rerunning from identical raw objects produces identical manifests and sorted rows.

## 16. Accuracy-focused suggestion

**After provenance acceptance, pre-register a station × event-type rolling bias correction with spread-preserving empirical calibration, fitted only on dates strictly before each target.** Apply it first to the locked shadow population and compare against the uncorrected ensemble using the same full-trajectory HIGH/LOW geometry. This is the highest-value accuracy test because airport/gridpoint mismatch and local-extreme bias can persist even when the GEFS lineage is correct, while spread preservation avoids falsely improving MAE by destroying uncertainty information. It must remain a challenger experiment, not a reason to relax provenance or missingness gates.

## 17. Final disposition

This specification authorizes implementation planning for a new provenance-complete GEFS rebuild. It does **not** authorize acquisition in this task and does not rehabilitate the existing archive. The required next artifact is an implementation/QC contract that realizes this schema and emits captured stdout/stderr evidence under the repository's B-MODE rules.
