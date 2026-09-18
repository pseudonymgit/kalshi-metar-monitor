# Frozen GEFS-only Direct Temperature Evaluation Specification

**Version:** `gefs-direct-temperature-v1.0-2026-08-15`
**Status:** frozen research/QC contract; no trading, production, SignalRegistry, HMM, ECMWF/TIGGE, or sweep wiring
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`

## 1. Scope and disposition

This document freezes a GEFS-only, direct ensemble temperature evaluator. It is an accuracy experiment against the settlement station's daily HIGH/LOW outcomes. It does not estimate P&L, emit trades, alter production behavior, or authorize a model for market use.

The evaluator must fail closed. In particular, a database `target_date`, `step`, `fetch_timestamp`, filesystem time, or `init_cycle='12Z'` label may not be promoted to a dated model issue time. If target-date/valid-time or issue/availability semantics cannot be proven, the run is **BLOCKED** and produces an eligibility manifest with exclusions, not an inferred score.

**Frozen disposition for the 2026-08-15 archive:** BLOCKED. The current GEFS archive has a 12Z cycle label but no dated initialization/run timestamp, no independently auditable availability timestamp, no member IDs, and no proven HIGH-vs-LOW valid-window semantics. The settlement table identifies HIGH/LOW series and values but does not retain the immutable NWS CLI report payload/provenance. No accuracy score is claimed.

## 2. Target, units, station universe, and outcome identity

### 2.1 Target

The target key is:

```text
(station, target_date_local, event_type ∈ {HIGH, LOW})
```

`HIGH` is the NWS settlement station daily maximum for local climate date `D`; `LOW` is the corresponding daily minimum. The outcome value is degrees Fahrenheit. Preserve the settlement value as stored; no Celsius conversion or numerical reinterpretation is permitted. HIGH and LOW are separate events even when station/date match.

The available settlement table is used only as an outcome snapshot. Its `series` prefix (`KXHIGH...` or `KXLOW...`) is the event-type identity. It is not silently relabeled as an immutable CLI report: raw CLI text, report issue time, revision ID, and settlement-source payload are absent in the current snapshot. This provenance failure blocks scoring.

Directional labels, if a future source passes all gates, are based on the same station/event type's strictly prior settled outcome:

```text
actual_delta = actual_f(D) - actual_f(previous_settled_date < D)
up   if actual_delta > +0.5°F
down if actual_delta < -0.5°F
flat otherwise; flat rows excluded from binary directional denominator
```

### 2.2 Units and rounding

- Forecast and outcome canonical unit: °F.
- No hidden rounding before continuous metrics.
- The settlement value is retained at source precision; if a future official CLI source provides integer °F, retain the raw integer and compare in °F.
- Interval quantiles use deterministic linear interpolation over sorted valid members only.
- The frozen directional epsilon is `0.5°F`.

### 2.3 Exact stations

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

Station identity comes from `data/station_mapping.json`; unknown stations and the development `TEST` row are excluded. The mapping's NWS `issuedby`/CLI URL is retained as expected provenance, not proof that the current settlement snapshot contains the report.

## 3. Frozen temporal geometry and information barrier

### 3.1 Partitions

Inclusive target-local-date windows are frozen before scoring:

| Partition | Dates | Use |
|---|---|---|
| `FIT_HISTORY` | `2021-08-19..2025-12-31` | Any future pre-registered bias/spread/calibration fit only |
| `CALIBRATION` | `2026-01-01..2026-02-28` | Optional calibration only; not used by this blocked run |
| `LOCKED_SHADOW` | `2026-03-01..2026-08-13` | Untouched final evaluation only |

The archive's 2026-08-14 forecast date is outside the locked outcome window and is not scored. No partition boundary may move after inspecting results.

### 3.2 Issue/cutoff/lead

The common cutoff is **12:00:00 UTC on calendar day D-1**. A selected forecast must prove:

```text
issue_time_utc       <= 12:00 UTC on D-1
available_time_utc   <= 12:00 UTC on D-1
valid window covers exactly the station's local climate date D
```

The intended lead is `step=24` hours from the actual GEFS initialization/issue timestamp. `step=24` is not accepted as a target-date label by itself. For each eligible row, report issue time, availability time, cutoff, valid start/end, lead to local-day start/end, and cycle. The station local-day conversion must honor the station timezone and DST/local-standard-time settlement semantics.

Current archive fields are `target_date`, `step`, `fetch_timestamp`, and `init_cycle`; there is no dated run/issue field. `fetch_timestamp` is ingestion time and cannot stand in for issue or availability. The evaluator therefore rejects current rows with `missing_issue_timestamp`, `missing_availability_provenance`, and `unverified_valid_time_semantics`.

### 3.3 Leakage barrier

Prediction generation may consume only GEFS source values and metadata proven available by the cutoff. Target-day observations, target-day daily aggregates, settlement outcomes, corrected labels, and post-cutoff revisions are label-only and may be attached only after predictions are frozen. This run generates no predictions because the source gate fails. A mutation test must show that outcome/target-day changes cannot alter prediction-side rows or eligibility.

## 4. GEFS member normalization and fail-closed eligibility

### 4.1 Observed panel classes

The current step-24 inventory has exactly these observed `n_members` strata:

- `6`: 200 rows;
- `30`: 149 rows;
- `31`: 40,651 rows.

These are reported as observed classes, never silently padded or imputed. A panel's `n_expected` must come from verified issue-cycle metadata. A count in the database is not proof of member identity.

### 4.2 Member gate

For a future provenance-valid row:

1. member IDs must be unique, known, and attached to the selected vintage;
2. values must be finite and physically valid;
3. `n_expected` must be explicit;
4. `n_present >= ceil(0.80 * n_expected)`;
5. no missing member may be replaced by zero, mean, climatology, persistence, another source/cycle, or target-day information.

Current blobs contain values but no member IDs. Therefore all current rows fail closed with `missing_member_identity`; no effective-N is promoted as independent information. If a future valid row has equal member weights, `effective_N = 1 / sum(w_i^2) = n_present`; report it beside nominal member count and by member-count stratum.

### 4.3 HIGH/LOW valid-window gate

The current `gefs_archive` row is one scalar at each step and has no variable/event field or proven daily-extreme valid window. It cannot be inferred to be both HIGH and LOW, and a single step-24 scalar cannot be treated as an official local-day maximum/minimum. Current rows therefore also fail with `unverified_target_kind` and `unverified_valid_time_semantics`.

## 5. Source/input hashes and read-only inventory

The evaluator records SHA-256 hashes and immutable schema/query evidence for:

- `data/gefs_archive.db` / `gefs_archive`;
- `data/kalshi_settlements.db` / `kalshi_settlements`;
- `data/station_mapping.json`;
- this frozen spec;
- evaluator source code.

No source database is modified. The read-only manifest reports candidate counts, accepted/rejected counts, exclusion reasons, station/date coverage, outcome HIGH/LOW counts, observed member strata, and temporal partition counts.

## 6. Metrics (only when all gates pass)

All metrics use one immutable, sorted key set and are reported pooled and by HIGH/LOW, station, DJF/MAM/JJA/SON, lead/issue-cycle, and member-count/effective-N strata:

- MAE (primary), RMSE, signed bias, median absolute error;
- directional accuracy with non-flat denominator and flat count;
- CRPS from the valid empirical ensemble CDF;
- Brier score and ECE only when a predeclared probability event and valid probability construction exist;
- 50/80/90% interval coverage, interval width, and interval score;
- paired station/date block-bootstrap uncertainty if a non-empty scored population exists.

For this run every metric is `BLOCKED` with `n=0`; no value is imputed or presented as zero skill. `PARK`/`KILL`/`ADVANCE` is not an accuracy verdict when provenance gates fail.

## 7. Gates and dispositions

- **ADVANCE:** only a future provenance-complete run may advance to separate accuracy research; never to trading automatically.
- **PARK:** source or target provenance is incomplete but recoverable without changing the frozen contract.
- **KILL:** a source contract is disproven or would require inference/imputation. For 2026-08-15, the direct lane is `PARK/BLOCKED` pending dated GEFS lineage and exact CLI snapshot evidence; it is not a negative forecast result.

Hard gates: source scope, exact 20 stations, no ECMWF/TIGGE/HMM/trading/sweep input, dated issue and availability, valid local-day geometry, explicit member identity/count, no target-day leakage, exact HIGH/LOW outcome identity, no duplicate conflicting revisions, deterministic output, and byte-identical reruns.

## 8. Accuracy-focused suggestion

The first accuracy improvement after provenance repair should be a **station × event-type × lead rolling bias correction with spread-preserving empirical calibration**, fit only on dates strictly before each scored target and evaluated first on the locked shadow. This attacks persistent airport/grid local-extreme bias without collapsing the ensemble or allowing target-day labels into the forecast.
