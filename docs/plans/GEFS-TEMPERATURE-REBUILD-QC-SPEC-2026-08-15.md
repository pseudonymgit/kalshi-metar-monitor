# Independent QC Specification — GEFS Temperature Provenance / Geometry Rebuild

**Date:** 2026-08-15 UTC  
**Version:** `gefs-temperature-rebuild-qc-v1.0`  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`  
**Status:** frozen documentation/QC contract; no production, trading, sweep, HMM, ECMWF/TIGGE, or scoring authorization

## 1. Purpose and disposition

This is an **independent quality-control contract** for the GEFS temperature provenance and local-day geometry rebuild. It is intentionally separate from the acquisition/parser implementation and from any accuracy evaluator. QC must be able to reject a plausible-looking result when its source lineage, time geometry, member identity, or replay evidence is incomplete.

The existing `gefs_archive` rows are forensic evidence only. They do not become eligible merely because they contain a temperature array, a `target_date`, `step=24`, or an `init_cycle` label. **No forecast score, baseline, calibration fit, ranking, or accuracy claim may read or derive forecast values from old archive rows.** Until this contract passes, the only valid output is a machine-readable `BLOCKED`/`REBUILD_REQUIRED` report.

The rebuild has two separate event paths:

- **HIGH path:** reduce a proven temperature trajectory to the station-local-date maximum.
- **LOW path:** reduce a proven temperature trajectory to the station-local-date minimum.

The paths must use separate reductions and separate QC assertions. One scalar, one common reduction, or a value selected because it resembles the observed outcome cannot satisfy both paths.

## 2. Scope, authority, and independent boundary

### 2.1 In scope

QC covers, at minimum:

1. raw-object retention, immutability, and cryptographic identity;
2. exact GEFS initialization/issue and availability timing;
3. the fixed information cutoff;
4. valid-time, lead-step, station-local-day, and DST geometry;
5. station-to-grid/source mapping and mapping-version evidence;
6. member IDs, expected panel, missingness, duplicates, and value validity;
7. prevention of target-day leakage and old-archive contamination;
8. duplicate/revision resolution;
9. deterministic replay and byte-identical evidence;
10. a five-station/date pilot and gates for expansion to the full rebuild.

### 2.2 Not in scope

QC does not modify `data/gefs_archive.db`, production code, scheduler behavior, settlement labels, old rows, or source objects. It does not acquire data itself, tune a model, choose a forecast, or certify trading use. Any repair that requires guessing a cycle, member, station point, or valid window is rejected rather than inferred.

### 2.3 QC independence requirement

The QC runner must be a read-only consumer of an immutable rebuild bundle. It must not import the forecast evaluator, trading path, legacy archive reader, or any module that can write to source data. The implementation must expose an input manifest and a QC report; QC must recompute hashes, key counts, geometry, and extrema from raw/parsed rebuild inputs rather than trusting producer summary fields.

At least one pilot review must be performed by a person or process other than the acquisition/parser author. The independent reviewer receives the raw object manifest, expected-result fixture, QC report, and replay receipts—not an unverified narrative.

## 3. Canonical contracts

### 3.1 Canonical forecast key

```text
(station_id, target_date_local, event_type ∈ {HIGH, LOW}, source_vintage_id)
```

A source lineage key is finer:

```text
(raw_sha256, station_id, grid_point_id, target_date_local,
 event_type, issue_time_utc, available_time_utc, member_id, valid_time_utc)
```

`HIGH` and `LOW` are distinct event types even for the same station/date/vintage. A QC artifact must retain the exact source vintage and may not collapse cycles, revisions, or event types into an anonymous daily row.

### 3.2 Frozen source and cutoff contract

The source is GEFS only, with the exact dataset/product, variable, level, grid, resolution, member panel, and provider endpoint captured in the rebuild manifest. A source row is cutoff-eligible only if **both** conditions hold:

```text
issue_time_utc      <= 12:00:00 UTC on calendar date D-1
available_time_utc  <= 12:00:00 UTC on calendar date D-1
```

where `D` is the station's target local calendar date. This is the frozen common information cutoff. The cutoff is evaluated in UTC; it is not converted to station local time and is not relaxed for a later publication schedule. The cutoff timestamp itself is inclusive and must be serialized as an explicit UTC instant (for example `2026-08-14T12:00:00Z`).

`issue_time_utc` means the actual GEFS initialization/run issue instant proven by native metadata or an immutable provider record. `available_time_utc` means the earliest auditable time that this exact object/revision was obtainable for use. Retrieval time may be recorded separately; it may not be relabeled as provider availability. A cycle-only string (`12Z`), target date, SQL row order, filesystem mtime, parser completion time, or guessed publication schedule is not a timestamp and cannot pass.

If the source cannot prove exact init/issue and availability times for the selected object, the vintage is `INELIGIBLE_MISSING_TIMING`, not a guessed vintage. If either time is after the cutoff, it is `INELIGIBLE_AFTER_CUTOFF`. If the object was revised after the cutoff, only the exact pre-cutoff revision may be used; otherwise the key is rejected as ambiguous.

### 3.3 Required input bundle

Each QC run consumes an immutable bundle containing:

- raw GEFS objects, with original bytes preserved before parsing;
- one complete sidecar/request record per raw object;
- parsed values and native metadata, generated into a new derived store;
- station/grid mapping manifest and pinned mapping version;
- timezone/IANA tzdata version and geometry configuration;
- expected member-panel manifest and missingness ledger;
- schema, parser, QC code, configuration, and fixture hashes;
- a manifest of all included and excluded objects, including reasons;
- labels only in a separate post-prediction attachment for identity checks.

A raw object may be compressed or stored in content-addressed storage, but its original content hash and byte-preserving representation must remain recoverable. A parsed row or SQLite BLOB hash alone is not a raw-source hash.

## 4. Raw immutability and hash QC

### 4.1 Raw-object record

For every object/revision, require and independently verify:

```text
raw_object_id
provider / dataset / product / endpoint or canonical object key
request URL and normalized request parameters
response status, content type, content length, ETag/version/revision if supplied
retrieved_at_utc
issue_time_utc
available_time_utc and availability evidence kind
native filename/object metadata
raw_sha256
raw_size_bytes
sidecar_sha256
parser_input_sha256 (must equal raw_sha256 or a explicitly hashed decompression transform)
```

The sidecar must be one-to-one with a raw object. Its canonical JSON serialization uses sorted keys, UTF-8, fixed separators, and no nondeterministic timestamps. The manifest records the hash of both raw bytes and sidecar bytes.

### 4.2 Immutability checks

QC must:

1. recompute SHA-256 from every retained raw object and compare with the manifest;
2. recompute sidecar hashes and verify the sidecar references exactly one raw hash;
3. verify the parsed input hash points to the retained raw bytes or to a declared deterministic decompression hash chain;
4. re-read a sample after parsing and prove that source bytes did not change;
5. reject any missing, unreadable, truncated, or hash-mismatched object;
6. write no bytes into the input bundle during QC.

No “latest file wins” behavior is allowed. A raw object with a matching filename but different bytes is a different revision.

### 4.3 Hash scope

Record SHA-256 for: raw objects, sidecars, station mapping, timezone ruleset, parser/QC source, lock/config, schemas and queries, input manifest, parsed store, event reduction manifest, and final QC report. Hashes must be computed over canonical bytes and include a manifest schema version. A report is not accepted if it contains untracked inputs or if a hash is computed after the QC process has modified the source bundle.

## 5. Init/availability and revision QC

### 5.1 Native evidence hierarchy

The following evidence is accepted, in descending preference:

1. native GRIB/NetCDF initialization metadata paired with an immutable provider publication/object record;
2. immutable provider object/version metadata with exact run and publication times;
3. a complete, hash-linked request/response receipt captured at acquisition, including an auditable availability timestamp.

A request template, expected 00/06/12/18Z schedule, directory name, fetch timestamp alone, or a derived `target_date` is insufficient. If multiple sources disagree, QC reports the disagreement and rejects the candidate until resolved.

### 5.2 Cutoff assertions

For every eligible event/member, independently recompute:

```text
cutoff_utc = 12:00:00Z on (target_date_local - 1 calendar day)
assert issue_time_utc <= cutoff_utc
assert available_time_utc <= cutoff_utc
assert source_vintage_id is fixed before prediction reduction
```

A source that is fetched before cutoff but first published after cutoff fails. A source that was published before cutoff but only has a post-cutoff revision fails unless the pre-cutoff revision is separately retained and selected. A target-date observation or outcome may be present only in the label-side attachment after prediction rows are cryptographically frozen.

### 5.3 Duplicate and revision rules

The canonical duplicate identity is:

```text
(provider, dataset, issue_time_utc, valid_time_utc, member_id,
 variable, level, grid_point_id, raw content identity)
```

- Exact byte-identical duplicate objects are retained once as a canonical object and all aliases are recorded; they must not increase member count.
- Same logical forecast with different bytes is a revision. Preserve every revision and its timing/hash.
- At most one revision can be selected for a forecast key: the latest revision whose **availability** is at or before the cutoff, subject to the frozen revision policy. If availability order cannot be proven, reject as `AMBIGUOUS_REVISION`.
- A revision first available after cutoff can never replace a selected pre-cutoff object.
- Conflicting values with no auditable revision ordering reject the affected event/vintage; do not average, prefer the database row, or use file order.
- Duplicate member IDs, duplicate valid times, and duplicate event reductions are hard failures unless they are byte-identical aliases explicitly collapsed by the canonicalization step.

## 6. Station/source mapping QC

### 6.1 Mapping record

Each station/event/member extraction must carry:

```text
station_id
station_latitude / station_longitude / elevation if used
station registry/version hash
station timezone and IANA zone
mapping_method (nearest_grid, bilinear, native station, etc.)
grid_id / grid latitude / grid longitude / grid index
mapping parameters and distance/weights
mapping code/config hash
```

The mapping must be deterministic and selected without using target-day temperatures, outcomes, or post-cutoff data. A station can map to one declared grid point or one declared interpolation stencil per frozen method; silent nearest-grid fallback is not allowed. Coordinates, grid convention, longitude wrap, land/sea mask, and units must be independently checked. Out-of-domain or ambiguous mappings are ineligible.

### 6.2 Mapping acceptance

The pilot must include at least one station near a grid-cell boundary and one urban/coastal station if represented in the required 20-station universe. Recompute mapping from the manifest and prove byte-identical station/grid IDs and weights. Mapping changes create a new mapping version and invalidate comparability with the previous rebuild; they do not overwrite old evidence.

## 7. Valid-time, local-day, and DST geometry

### 7.1 Station-local day

For station timezone `tz` and target local date `D`, construct timezone-aware endpoints using the pinned IANA tzdata version:

```text
local_start          = D 00:00:00 in tz
local_end_exclusive  = (D + 1 calendar day) 00:00:00 in tz
```

The valid window is half-open `[local_start, local_end_exclusive)`. Convert endpoints to UTC using timezone rules, record both UTC offsets, and do not assume a 24-hour elapsed interval. DST spring-forward has 23 elapsed hours; fall-back has 25. The local calendar date remains the settlement date.

A valid timestamp must be derived from proven native valid-time metadata. If only `issue_time_utc + step` is available, it may be used only after issue time is proven and must agree with native valid time where native metadata exists. `target_date` cannot manufacture a valid time.

### 7.2 Complete trajectory requirement

The selected source must provide a declared trajectory/window sufficient for the complete station-local day. For every member, QC reports:

```text
native valid_time_utc[]
lead step[] and units
first/last valid time
local timestamp and UTC offset for every point
spacing and expected spacing
window membership mask
coverage duration and missing points
field semantics (instantaneous temperature or native tmax/tmin accumulation)
```

No arbitrary `step=24` scalar is accepted as a daily extreme. Missing valid times, irregular spacing, or an incomplete window reject the affected event unless the source's native accumulation semantics explicitly prove a complete daily interval.

### 7.3 Separate HIGH and LOW reductions

For each `(station, target_date_local, source_vintage_id, member_id)`, independently compute:

```text
HIGH_member = max(temperature values whose valid times are in the HIGH local-day window)
LOW_member  = min(temperature values whose valid times are in the LOW local-day window)
```

The two path definitions, inclusion masks, extrema values, and contributing valid timestamps must be materialized separately. A native `tmax`/`tmin` field may replace trajectory reduction only when its accumulation interval and station-local-day alignment are proven; the HIGH and LOW fields remain separate. A single scalar cannot populate both paths. No path may select an extrema timestamp based on the observed outcome.

QC must include fixtures for ordinary time, spring-forward, and fall-back dates. Every included valid timestamp must map to `D`; every expected in-window source timestamp must be accounted for. A window with zero or incomplete members is not scored and is reported with a specific missingness reason.

## 8. Members, IDs, and missingness

### 8.1 Member identity

The expected panel is declared by the verified GEFS cycle/product manifest, not inferred from array length. Every value must have a stable native member ID (for example control and numbered perturbations), source vintage, valid time, and grid mapping. Array position, BLOB length, or a count of values is not a member identity.

QC checks:

- IDs are unique within a source vintage/valid time/grid;
- control and perturbation roles are retained where native metadata supplies them;
- expected panel size and expected IDs are explicit;
- present IDs exactly match a subset of the expected panel;
- no member is silently renamed, duplicated, padded, or substituted from another cycle/source.

### 8.2 Missingness ledger

The ledger enumerates every expected-but-absent member and every absent/invalid valid-time point, with reason (`not_delivered`, `parse_error`, `nonfinite`, `hash_failure`, `outside_window`, etc.). Non-finite values, impossible units, missing steps, and missing members remain missing. There is no zero, mean, climatology, persistence, neighboring-cycle, or target-day imputation.

The panel policy must be frozen before the pilot. Recommended minimum: all required members present for a scored event; if the approved product permits partial panels, declare `n_expected`, `n_present`, member IDs, and the minimum fraction before inspection, and report results by panel stratum. A partial panel is never silently treated as a full panel. An event failing its declared panel policy is `INELIGIBLE_MISSING_MEMBER` and produces no score.

## 9. Leakage and old-archive exclusion QC

### 9.1 Prediction-side input allowlist

Prediction/reduction code may read only the frozen rebuild bundle: raw objects, parsed source values, source metadata, station/grid mapping, timezone rules, and pre-cutoff configuration. It may not read:

- `data/gefs_archive.db` or any old archive-derived table/blob;
- target-day observations, daily aggregates, settlement values, or corrected labels;
- post-cutoff source revisions;
- HMM, ECMWF/TIGGE, trading, production, or calibration artifacts.

Labels attach only after prediction-side manifest/reductions are frozen and hashed. The QC runner must inspect file/database access or use an explicit input allowlist and fail if an old archive path appears.

### 9.2 Mutation and contamination tests

The pilot must run these negative controls:

1. Rename or replace the old archive and show rebuild predictions/reductions are unchanged.
2. Mutate target-day outcomes and observations in a label-only copy and show eligibility, source hashes, member values, geometry, and HIGH/LOW reductions are unchanged.
3. Add a post-cutoff revision and show it is rejected, not selected.
4. Remove one expected member and show the event becomes ineligible rather than being renormalized silently.
5. Swap station mappings or timezone versions and show the hash/key gate fails.

Any changed prediction-side artifact under the first two tests is a hard `LEAKAGE_OR_ARCHIVE_CONTAMINATION` failure.

## 10. Deterministic replay contract

A run is deterministic when identical immutable inputs, code/config/schema hashes, timezone ruleset, and canonical ordering produce byte-identical:

- input manifest and exclusion ledger;
- per-object/member/valid-time lineage rows;
- station mapping rows;
- separate HIGH and LOW extrema rows;
- QC report and summary counts.

Canonical serialization uses UTF-8, sorted keys, fixed separators, normalized UTC timestamps, explicit nulls, stable decimal/unit formatting, and sorted canonical keys. No wall-clock retrieval time, process ID, random seed, hash-map order, network response, or unpinned timezone database may enter a derived result. QC runs must be read-only and write outputs outside the frozen input bundle.

Replay acceptance requires two clean runs in isolated directories and byte-for-byte equality of all canonical outputs and SHA-256 manifests. A mismatch is `NONDETERMINISTIC_REPLAY` even when numeric values appear equal. Code/config/input drift invalidates the comparison and requires a new run ID.

## 11. Pilot design and acceptance gates

### 11.1 Pilot population

Before full acquisition, use a frozen five-station/date pilot with raw objects. The exact station/date keys and source vintages are recorded before inspection and must include:

- at least five stations spanning timezones/geography and at least one urban/coastal or edge-of-grid mapping;
- ordinary-time dates;
- one spring-forward and one fall-back local date across the pilot or an explicitly retained geometry fixture if operational dates cannot provide both;
- at least one expected missing-member or missing-step fixture, tested as an intentional rejection;
- both HIGH and LOW paths for every pilot key.

The pilot must use newly retained raw objects and must not use old archive rows for forecast values or scoring.

### 11.2 Pilot pass criteria

Pilot status is `PASS` only if all hard criteria hold:

1. 100% of included raw objects and sidecars hash-verify; no missing/truncated object.
2. 100% of scored pilot source rows have exact issue/init and availability timestamps and satisfy the inclusive D-1 12:00 UTC cutoff.
3. 100% have unique revision selection or an explicit, auditable rejection; no unresolved conflict.
4. 100% have recomputed, deterministic station/grid mappings with pinned version and no target-day inputs.
5. All pilot HIGH and LOW windows are complete under their declared source semantics, with contributing valid times recorded separately.
6. Member IDs are explicit and unique; all missing members/steps are in the ledger; no silent imputation or panel padding.
7. Leakage tests pass, including old-archive removal and target-label mutation tests.
8. Two independent replay runs are byte-identical.
9. Independent review signs the manifest, exclusion ledger, hashes, and QC report.

Any hard failure is `PILOT_BLOCKED`; no full rebuild or accuracy score may proceed. Non-fatal observations must be recorded, not silently waived.

## 12. Full-rebuild authorization gates

Full historical acquisition/rebuild may begin only after a signed pilot receipt records:

- frozen source/product, station/date population, cutoff, geometry, member, revision, and mapping contracts;
- raw/sidecar hash manifest and successful immutability checks;
- separate HIGH/LOW geometry evidence, including DST fixtures;
- missingness and duplicate/revision policy results;
- successful leakage and old-archive exclusion tests;
- byte-identical replay receipts;
- independent reviewer approval.

During the full rebuild:

1. raw objects are retained before parse and never deleted after derived storage;
2. source inputs are read-only and each object/revision is hash-pinned;
3. no old archive values are backfilled into the new store;
4. every excluded object/event has a machine-readable reason;
5. full-population QC is run before any labels are attached or scores are produced;
6. any hash drift, unexplained coverage change, duplicate conflict, timing ambiguity, geometry gap, member identity failure, leakage signal, or replay mismatch stops the rebuild and marks it `FULL_REBUILD_BLOCKED`.

A full rebuild may be promoted to an isolated evaluation dataset only when every intended scored key passes all provenance, timing, mapping, geometry, member, missingness, leakage, duplicate, and replay gates. A failed historical row is excluded with reason; it is not repaired by old archive data or imputation. The first evaluation remains read-only and separate from production.

## 13. Required QC evidence and report

The QC report must be machine-readable and human-reviewable, with:

```text
run_id, spec_version, UTC creation time (report metadata only)
source/product/config/code/schema/timezone/mapping hashes
input object count and raw/sidecar hash status
eligible/ineligible counts by reason
issue/availability/cutoff counts and revision decisions
station/grid mapping counts and mapping versions
member expected/present/missing counts and ledger hash
valid-time coverage, DST cases, and geometry failures
separate HIGH and LOW reduction counts, hashes, and extrema timestamps
old-archive access test and leakage mutation results
replay run IDs, output hashes, and byte-equality result
pilot/full disposition: PASS, BLOCKED, REBUILD_REQUIRED, or FULL_REBUILD_BLOCKED
independent reviewer and receipt reference
```

No score fields are required or permitted in this QC report. If any old archive row appears in a scoring input or source reduction, the report must fail closed even if all numerical comparisons look reasonable.

## 14. Accuracy-focused suggestion (after QC only)

Once the rebuilt lineage passes and a locked evaluation population exists, test a **station × event-type (HIGH versus LOW) rolling bias correction with spread-preserving empirical calibration**, fit only on strictly earlier settled dates and evaluated on a future shadow. Report MAE/CRPS and interval coverage by event type, station, lead, and member-count stratum. This is an accuracy experiment—not a reason to relax provenance, geometry, or no-leakage gates—and it targets systematic grid-to-station/local-extreme bias without collapsing the ensemble spread.

## 15. Final fail-closed rule

The rebuild is not complete because files parse, member counts look plausible, or a score can be calculated. It is complete only when raw lineage, exact cutoff timing, source/station mapping, member identity/missingness, valid local-day geometry, separate HIGH/LOW extrema, duplicate/revision handling, no-target-day leakage, and deterministic replay all pass. Otherwise the disposition is `BLOCKED` or `REBUILD_REQUIRED`, and **no score from old archive rows is valid**.
