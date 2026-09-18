# Temperature-Shadow Forecast-Vintage Alignment Specification

**Date:** 2026-08-15 UTC  
**Status:** QC design and inventory disposition only; no forecast backtest, trading, production, signal, or paper-trading change  
**Scope:** GEFS and ECMWF IFS/TIGGE daily-temperature shadow inputs

## 1. Decision and disposition

The current SQLite/TIGGE archive cannot prove an exact ECMWF forecast-origin (issue/initialization) time or a historical availability time for any row. No issue time will be inferred from `target_date`, `step`, database row order, database mtime, or the time a QC process runs. No TIGGE alignment manifest is generated from guessed vintages.

**Disposition:**

- **GEFS-only:** **VALID SOURCE-ONLY ARM** for a separately labeled, read-only accuracy shadow, using the recorded 12Z cycle label and the member/missingness rules below. This does not establish that every current GEFS row has a fully auditable dated issue timestamp; that is a provenance limitation to report, not a reason to repair data.
- **ECMWF IFS Direct:** **BLOCKED** pending exact issue-origin and availability provenance.
- **GEFS+ECMWF combined:** **BLOCKED** because the ECMWF component is blocked; there is no GEFS-only fallback inside the combined lane.
- **TIGGE/ECMWF alignment manifest for the requested overlap population:** **NOT GENERATED**. A manifest is permitted only after exact reconstruction passes the gates in §4.

The orchestration receipt reports 28,980 overlap keys. A fresh read-only query using `(target_date, station, step=24, source='tigge_ecmwf')` found 36,260 station/date rows shared by the two databases; this discrepancy is preserved in the inventory record and is not resolved by assumption. It does not change the blocked disposition.

## 2. Canonical identity and timing contract

### 2.1 Canonical scored key

A scored forecast row is keyed by:

```text
(station, target_date_local, event_type ∈ {HIGH, LOW}, source,
 issue_time_utc, availability_time_utc, lead_hours, valid_window, member_id)
```

The common comparison key is:

```text
(station, target_date_local, event_type)
```

The station universe is the frozen 20-station registry:

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

The registry must contain the exact NWS/settlement station identity, coordinates, timezone/DST rules, and validity dates. `target_date` in either archive is not accepted as `target_date_local` until the source valid-time geometry is proven. HIGH and LOW require separate daily extrema over the station-local calendar day; the current archives contain 2-m temperature step samples, not an official daily-extreme field.

### 2.2 Common cutoff

For target local date `D`, freeze one common cutoff:

```text
cutoff_time_utc = 12:00:00 UTC on calendar day D-1
```

A source vintage is eligible only if both are auditable and satisfy:

```text
issue_time_utc       <= cutoff_time_utc
availability_time_utc <= cutoff_time_utc
```

Select the latest eligible issue time; use availability and immutable revision/content hash as deterministic tie-breakers. A missing latest source run is an unavailable run, not a reason to substitute a later run or silently alter the lead stratum.

The cutoff is a forecast-information boundary. Target-day METAR, daily statistics, settlement/outcome, and any target-day-derived feature are label-only and forbidden to prediction-side code.

### 2.3 Lead and step semantics

`step=24` means **a 24-hour forecast lead from the model initialization/issue time**, not “the forecast for target date D” and not “24 hours before local-day end.” It can be converted into a valid timestamp only when the issue time is known exactly:

```text
valid_time = issue_time_utc + 24 hours
```

For daily HIGH/LOW, the evaluator must prove that the valid-time samples cover the target station-local day and record first/last valid timestamps and both leads to local-day start/end. The current TIGGE rows have steps `{0,6,12,18,24}` and the current GEFS rows have `{0,3,6,9,12,15,18,21,24}`; their `step=24` labels alone do not establish common valid windows.

## 3. Source metadata and reconstruction rules

### 3.1 GEFS 12Z issue metadata

The accepted GEFS metadata contract is an actual UTC initialization timestamp plus an availability/retrieval timestamp and immutable source/revision provenance. A cycle label such as `init_cycle='12Z'` is a required descriptive field but is not, by itself, a dated issue timestamp. The current GEFS table has `init_cycle='12Z'` on all 365,960 rows and `fetch_timestamp`, but no run date/initialization date column. `fetch_timestamp` is ingestion/population time and must not be relabeled as issue time.

The GEFS-only arm may remain a valid source-only descriptive arm under the recorded 12Z label, with this limitation explicitly attached. Before a cross-source comparison, dated GEFS issue/availability provenance must also be upgraded or the GEFS arm must be reported as a cycle-labeled, not fully vintage-audited, result.

### 3.2 TIGGE issue-origin metadata

The current `tigge_archive` row has no issue timestamp, availability timestamp, revision ID, file/object URI, request ID, or content hash. Its fields are `target_date`, `station`, `step`, `source`, summary values, a member blob, and `n_members`. The stored table therefore cannot distinguish, for a target/station/step, which ECMWF initialization generated the values or when that revision became available.

The runnable request paths inspected request ECMWF TIGGE with `time='00:00:00'`, `origin='ecmf'`, `type='pf'`, `number='1/to/50'`, and a request `date`; those are code-level request intentions, not proof of the origin of every already-materialized database row. The archive database is the only retained TIGGE data object found in the expected `data/tigge_grib/` location (no cached TIGGE GRIB files were present); its filesystem mtime is database-write metadata, not forecast issue metadata. Logs/checkpoints prove processing progress only.

### 3.3 Valid reconstruction methods

A TIGGE vintage may be reconstructed exactly only by one of these evidence paths:

1. **Native forecast metadata:** a retained GRIB/NetCDF/object record containing an unambiguous model reference/initialization time, valid time/step, member identity, and source revision, paired with an auditable availability timestamp.
2. **Immutable request/file provenance:** a request manifest or object key with the exact request date/time and response/object hash, plus a deterministic mapping showing that the response was not revised and was available by the cutoff.
3. **Versioned ingestion lineage:** a sidecar keyed to each database source row containing exact issue, availability, revision/content hash, and source file/object hash; the sidecar must be complete and independently hashable.

A filename date, SQL `target_date`, fetch timestamp, DB mtime, script default, or the known publication schedule is not sufficient. If more than one candidate issue remains possible, the row is rejected as `ambiguous_issue_origin`; if availability cannot be proven, it is rejected as `missing_availability_provenance`. No imputation or invented issue time is allowed.

## 4. Alignment manifest gate

If exact TIGGE reconstruction becomes possible, build a read-only deterministic manifest with one row per candidate station/date/step/member and at least:

```text
station, target_date_local, event_type, source,
issue_time_utc, availability_time_utc, cutoff_time_utc,
step_hours, valid_time_utc, valid_window,
member_id, member_value, n_expected, n_present, n_missing,
revision_id, source_file_or_object, source_hash,
exclusion_reason, code_commit, manifest_version
```

Rows must be sorted by stable canonical key and serialized deterministically. The manifest must record candidate, accepted, and rejected counts, hashes of all input files, schema/query text, and a no-leakage result. The manifest is accepted only when every selected row has exact issue and availability provenance, valid-time geometry, unique member identity, and cutoff eligibility. Otherwise the affected source remains blocked. Under the 2026-08-15 inventory, this gate fails for all TIGGE rows and no manifest is created.

## 5. Member normalization and missingness

### 5.1 Panel classes

Do not coerce panels to a common count or invent member identities.

- **GEFS:** retain and label the observed panel classes `6`, `30`, and `31`. A 31-member panel is the normal control-plus-perturbation panel only when source metadata verifies those identities. A 30-member panel is not padded to 31; a 6-member panel is not expanded or silently treated as a 31-member panel.
- **ECMWF/TIGGE:** retain the observed/requested 50-member panel only when member IDs and source provenance are verified. `n_members=50` in the current table is a count, not proof of unique ECMWF member identities or exact message ordering.

The current compressed/raw blobs preserve values but not member IDs. Therefore a future manifest must reject duplicate/unknown member identity unless the original source message ordering is independently proven and recorded.

### 5.2 Eligibility and no imputation

For each source and issue cycle, `n_expected` comes from verified source/issue metadata. A member is present only when its value is finite, physically valid, uniquely identified, and attached to the selected vintage. The default primary gate is:

```text
n_present >= ceil(0.80 * n_expected)
```

Rows below the gate are excluded with a machine-readable reason. No missing member may be replaced by zero, mean, climatology, persistence, another source, or a target-day value. For a verified six-member GEFS panel, six is the declared expected count; for a verified 31-member GEFS panel, six or 30 present members are missingness, not a valid 31-member panel. The chosen panel class and all present/missing IDs must be reported.

### 5.3 Weights, effective N, and correlation

Within a valid source panel, members receive equal conditional weight `1/n_present`; for a two-source combined lane, each source receives total weight `0.5`, then equal conditional member weights within that source. The combined lane is eligible only when both source lanes independently pass provenance, timing, valid-time, and member gates.

Report:

```text
n_expected, n_present, n_missing,
member_ids_or_hash, source_weight,
effective_N = 1 / Σ(w_i²),
within_source member dependence/correlation,
paired GEFS–ECMWF forecast correlation on identical keys,
```

For equal weights, effective N equals `n_present`; it must still be reported because weighted/calibrated variants can reduce it. Correlation is diagnostic, not permission to duplicate or de-correlate members. It must be computed only on common, provenance-valid rows and with the exact sample denominator and stratum (station, HIGH/LOW, lead/cycle) shown.

## 6. Leakage barrier

Prediction generation must receive only source forecasts and information strictly available by the common cutoff. Target outcomes, target-day observations, corrected settlements, target-day daily-stat rows, and any post-cutoff revisions may be attached only after predictions are frozen. Mutation tests must show that changing target-day data changes neither predictions nor vintage/key eligibility. A target-date coincidence in the archive is not evidence that the row was available before the target day.

## 7. Acceptance and block criteria

**Alignment PASS** requires all of the following:

1. Exact issue and availability timestamps for every selected source row.
2. Common 12Z D-1 cutoff satisfied without post-cutoff revisions.
3. Exact valid-time/step geometry for the local station date.
4. Unique source/member identity and no duplicate conflicting revisions.
5. Declared panel class, missingness ledger, effective-N, and correlation denominators.
6. No target-day leakage; outcomes absent from prediction-side inputs.
7. Deterministic rerun: identical inputs/code/config produce identical row and manifest hashes.
8. Immutable input, source, query, code, and output hashes retained.

Mark a source **BLOCKED** if issue origin, availability, valid-time mapping, revision ordering, member identity, or leakage cannot be proven; if satisfying a gate needs guessed metadata or imputation; or if the common key set changes after looking at results. Block ECMWF Direct and the combined lane when TIGGE fails. Preserve GEFS-only separately rather than treating it as a combined fallback.

No forecast backtest may begin until the applicable alignment gate passes. This document authorizes no trading, production, signal, or HMM integration.

## 8. Accuracy-focused suggestion

Once provenance is repaired, prefer a paired, key-matched MAE evaluation with station/date block bootstrap as the primary accuracy statistic, and report interval coverage and effective-N beside it. Paired keys control for weather difficulty and prevent a source from appearing more accurate merely because it has a different vintage or missingness population; effective-N and cross-source correlation prevent false precision from being attributed to a nominal 31+50 member count.
