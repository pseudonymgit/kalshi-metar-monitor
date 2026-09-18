# SRRS Full Backfill — Independent QC Specification

**Date:** 2026-08-15 UTC  
**Lane:** independent, read-only quality control  
**Scope:** approved NCEI SRRS CLI full backfill for the frozen 20-station contract  
**Target dates:** 2025-01-15 through 2026-02-28 inclusive  
**Expected keys:** 410 dates × 20 stations = **8,200 station/date keys**  
**Status:** QC design only; do not run, resume, or modify the backfill

## 1. Purpose and stop boundary

This document defines checks that an independent reviewer performs **after** the approved full backfill has produced its immutable artifacts. The reviewer must not run the acquisition, resume a checkpoint, make HTTP requests, rewrite raw data, reclassify producer artifacts in place, or change source/evaluator/calibration/trading behavior.

The QC result is an evidence disposition, not source approval. In particular:

- SRRS/CLI evidence remains descriptive and audit-only.
- No evaluator, calibration, production, signal, sweep, capital, deployment, or trading change is authorized.
- A missing or blocked station/date is accounted for; it is never filled from LCD, GHCN, METAR/ASOS, a neighboring station, a city proxy, or a different NWS product.
- The independent checker must recompute counts, hashes, identities, and canonical outputs from frozen inputs rather than accepting `run_summary.json`, a checkpoint, or a producer-selected row as proof.

## 2. Governing inputs

The reviewer records path, byte count, SHA-256, and retrieval/creation metadata for every input before inspection. A changed, absent, unhashable, or mutable input is a **BLOCK**, not a reason to refresh it.

### 2.1 Acquisition and lineage inputs

1. Frozen acquisition specification: `docs/plans/SRRS-CLI-FULL-BACKFILL-SPEC-2026-08-15.md`.
2. Frozen v2 lineage policy: `docs/plans/SRRS-CLI-REPORT-LINEAGE-QC-2026-08-15.md`.
3. Approved backfill root: `data/raw/srrs_cli_backfill/2026-08-15/`.
4. Expected v1 artifacts, where retained: `manifest.jsonl`, `attempts.jsonl`, `checkpoint.json`, `raw/`, `scans/`, `reports/`, `failures/`, and `run_summary.json`.
5. Any separately approved full-backfill receipt and its referenced artifact hashes.
6. Bounded v2 lineage receipt: `.meta/continuity/weather-engine/2026-08-15-srrs-lineage-qc.md` and, if supplied as an input, `data/raw/srrs_cli_backfill/2026-08-15-lineage-qc-v2/`. The v2 result is a policy fixture and sanity reference for the `2025-01-15` date; it is **not** evidence that the 8,200-key backfill is complete.

The v2 receipt's known policy behavior must be preserved in the full-run review: an intermediate plus a coherent full-day report is accepted lineage; byte-identical occurrences are duplicate content, not conflicting revisions; and a non-identical unresolved full-day conflict blocks selection.

### 2.2 Frozen identity inputs

1. `data/station_mapping.json` — exact 20-station mapping, including NWS office/site, `nws_issuedby`, Kalshi series, and station identity.
2. `data/kalshi_official_mapping.json` — series/source reference used only as an audit input.
3. Settlement identity policy: `docs/plans/KALSHI-NWS-TARGET-CONTRACT-EXPERT-REVIEW-2026-08-15.md` and the duplicate/target rules in `docs/plans/GHCN-KALSHI-TARGET-EQUIVALENCE-AUDIT-SPEC-2026-08-15.md`.

The minimum settlement identity is not `(station, date)` alone. Preserve and audit at least:

```text
(event_ticker or market family, station, target_date, direction/series)
```

A HIGH and LOW event for the same station/date is distinct. A repeated occurrence of the same event ticker may be duplicate ingestion or revision and must be classified, never collapsed silently.

### 2.3 Three-way audit inputs

The three-way audit is descriptive only and runs only after the raw/report ledger is frozen:

- selected, identity-validated SRRS CLI full-day value and raw body/hash;
- frozen GHCN-Daily row for the exact mapped station/date, retaining raw TMAX, unit conversion, measurement/quality/source/time flags, and source-file hash;
- frozen Kalshi settlement row/event payload, retaining event ticker/series, market direction, target date, settlement value, source type, rules/settlement-source metadata, and database/extract hash.

GHCN and Kalshi are comparison inputs, not substitutes for CLI and not automatic relabeling authorities. A missing or non-unique third-party row is an explicit audit gap.

## 3. Independent evidence package

The reviewer produces a new, separate QC receipt/output directory. It must contain:

- input inventory and independently recomputed hashes;
- expected 8,200-key station/date matrix;
- raw occurrence ledger, including every scan row, attempt, body, rejection, and failure;
- per-key terminal status and reason;
- station/PIL/WMO/internal-date identity results;
- report class, issuance/revision lineage, selection decision, and selected raw hash;
- duplicate-content groups and occurrence counts;
- missing/blocked ledger with searched directories, scan counts, candidate counts, and request outcomes;
- three-way audit extract and identity/exclusion ledger;
- two deterministic canonical rerun outputs and hashes;
- PASS/REJECT/BLOCK result for each check and an overall disposition.

The output must not overwrite any backfill artifact, receipt, raw body, manifest, checkpoint, audit database, evaluator file, calibration file, or trading file.

## 4. Independent checks

### QC-01 — Raw and scan completeness

**Question:** Does every acquired artifact have complete, immutable provenance from archive locator to exact raw bytes?

For every `.scan` file and every report body, verify:

- official SRRS HTTPS host/path and immutable archive URL;
- scan filename, archive date/hour, row number, WMO header, start index, byte range, and candidate key;
- HTTP status and exact `Content-Range` for range fetches;
- body byte length equals the declared range length;
- SHA-256 recomputed from the retained bytes;
- raw filename/path and manifest entry agree with URL/range/hash;
- no whole-ANX bundle was fetched or used as a substitute;
- repeated archive occurrences remain separately represented.

**Pass threshold:** 100% of successful scan/body artifacts have matching locator, range, size, and hash; 100% of attempts have a terminal outcome.  
**Reject threshold:** any body is proven not to be from the official range, has a wrong length/hash, or a proxy/non-SRRS product was used.  
**Block threshold:** missing scan/attempt/manifest evidence, unhashable bytes, HTTP/range metadata not retained, or an artifact changed after the producer receipt. No tolerance applies to critical provenance failures.

### QC-02 — Raw/hash completeness and no-silent-loss accounting

Recompute the following conservation equations from the independent ledger:

```text
all candidate scan rows
  = accepted raw occurrences + rejected/mismatched rows + failed/unavailable attempts

accepted raw occurrences
  = unique retained raw bodies + duplicate-content occurrences

all 8,200 station/date keys
  = accepted_full_day + explicit missing_no_cli + explicit blocked_* terminal keys
```

A producer count is not accepted without row-level reconciliation. Every rejected row has a reason; every failure has request status/error/retry metadata; every raw occurrence points to an immutable body or a recorded failed fetch.

**Pass threshold:** 100% conservation at row level; zero unexplained candidate, body, attempt, or key loss.  
**Reject threshold:** evidence proves a row/body was deliberately discarded or replaced by a non-SRRS source.  
**Block threshold:** any unexplained difference, missing ledger segment, duplicate key overwrite, or status count that cannot be reproduced. One unexplained row is sufficient to block.

### QC-03 — Full 20-station/date coverage

Construct the Cartesian expected matrix from the frozen mapping and date range. Verify exactly 20 station keys and exactly 410 inclusive target dates. For each of 8,200 keys, verify one and only one terminal disposition:

- `accepted_full_day`;
- `missing_no_cli`;
- `blocked_archive_access`;
- `blocked_station_or_date_mismatch`;
- `blocked_unresolved_lineage`;
- `blocked_conflicting_full_day_revisions`;
- another explicitly documented `blocked_*` status.

`found` without a v2-quality selection decision is not sufficient for acceptance.

**Pass threshold:** 8,200/8,200 keys (100%) present exactly once in the independent status matrix; 0 duplicate or absent station/date keys.  
**Reject threshold:** station universe/date range is proven to differ from the frozen contract, or a key was omitted/substituted.  
**Block threshold:** any key lacks a terminal status, has multiple terminal statuses, or coverage cannot be independently reconstructed. Explicit missing/blocked keys may be counted as accounted-for, but they prevent a clean source-readiness pass.

### QC-04 — Exact station, PIL, and WMO identity

For each candidate and selected body, independently verify all of:

1. station key equals the frozen `data/station_mapping.json` key;
2. WMO header contains the expected NWS office token;
3. bulletin contains the exact expected PIL `CLI{nws_issuedby}`;
4. parsed report station/office agrees with the mapping;
5. no city, airport, WMO, NWS office, GHCN, or ticker alias was substituted;
6. the selected body is for the intended station, not merely a same-city report.

Critical examples include `KMDW` = Chicago Midway (not O’Hare), `KNYC` = the mapped NYC climate site (not a generic airport), and `KDFW` whose ticker token may differ from the NWS `issuedby` token.

**Pass threshold:** 100% of selected rows and 100% of rows counted as accepted candidates pass exact station/PIL/WMO identity.  
**Reject threshold:** any selected or settlement-facing row has a wrong station, wrong PIL, or wrong WMO office token.  
**Block threshold:** identity cannot be established from raw text and frozen mapping, or a candidate has conflicting identity fields. A non-selected wrong-identity candidate remains retained as rejected evidence.

### QC-05 — Internal summary date and local climate-day identity

The internal summary date in the bulletin controls target identity. Independently parse and compare:

```text
requested target date == bulletin internal summary date
```

Do not use archive directory date, UTC request date, filename date, issuance date, or retrieval date to relabel a report. Inspect the `D-1`, `D`, and `D+1` search window behavior, including late next-day issuance. Record out-of-window reports separately.

**Pass threshold:** 100% of selected rows have an exact internal summary-date match; 100% of mismatches are rejected and retained; 0 UTC-date substitutions.  
**Reject threshold:** any report is selected after relabeling a different internal summary date, or the product/date is proven to be the wrong target.  
**Block threshold:** internal date is absent/unparseable, date semantics cannot be established, or a key's candidate set includes unresolved date identity.

### QC-06 — Full-day selection and intermediate handling

Classify from raw report semantics, never from file order, fetch time, byte size, or non-null values:

- `full_day`: completed local climate day, including the appropriate completed-day observation table/record section;
- `intermediate`: open/current-day report, including `TODAY` and/or explicit `VALID AS OF` language where the observation period is identifiable;
- `revision`: explicit correction/amendment/update or later same-lineage supersession;
- `duplicate_content`: orthogonal provenance label for byte-identical occurrences;
- `unresolved`: class/date/station semantics cannot be established.

For each key, select a full-day body only after exact station/PIL/date identity passes. An intermediate plus a full-day body is coherent accepted lineage: retain the intermediate and select the full-day. Never select an intermediate as a settlement value merely because no full-day is currently visible.

**Pass threshold:** 100% of `accepted_full_day` keys have exactly one coherent full-day semantic value after duplicate/revision handling; 100% of intermediates remain in the ledger and link to the key/lineage.  
**Reject threshold:** an intermediate is used as the selected settlement value, or a full-day is selected using arbitrary file/retrieval order.  
**Block threshold:** no full-day exists, class is unresolved, or multiple non-identical full-day values remain without an evidence-backed finality/revision rule.

### QC-07 — Revision and lineage integrity

For every same-key report occurrence, preserve issuance timestamp/locator where available, raw hash, class, lineage key, supersession relation, and selection flag. Verify:

- byte-identical bodies are counted as duplicate occurrences, not separate conflicting values;
- non-identical revisions remain separate bodies;
- later issuance is not automatically treated as final without explicit evidence;
- a revision may be selected only when it is itself an unambiguous full-day report and the revision order is documented;
- conflicting full-day max/min values produce `blocked_conflicting_full_day_revisions`.

**Pass threshold:** 100% of accepted/rejected/revision occurrences retain raw hash and lineage metadata; 0 unexplained overwrites.  
**Reject threshold:** a later body is proven to have replaced evidence by destructive overwrite or a revision value is selected contrary to the policy.  
**Block threshold:** unresolved revision order, non-identical full-day disagreement, missing issuance/lineage evidence, or a body that cannot be classified.

### QC-08 — Duplicate-content accounting

Group all accepted raw occurrences by SHA-256 and separately by semantic key. For every group, report:

- number of occurrences;
- unique body count;
- archive locator/range for each occurrence;
- underlying class (`full_day`, `intermediate`, `revision`, or unresolved);
- whether any occurrence was selected.

A duplicate-content occurrence is not a missing row and not a conflict. It must remain separately auditable. Identical bodies from different scan rows are counted once as unique content and once per provenance occurrence.

**Pass threshold:** 100% of repeated hashes are explicitly classified and included in the conservation equations; selected value has a stable unique body hash.  
**Reject threshold:** duplicates are silently dropped, counted as independent conflicting values, or used to mask a wrong station/date.  
**Block threshold:** hash groups cannot be reconstructed, body identity differs despite a claimed duplicate, or occurrence accounting does not balance.

### QC-09 — Missing and blocked rows

For each `missing_no_cli`, record every searched directory/hour, scan count, candidate count, and request outcome. For each `blocked_*` row, record the precise evidence and whether the block is archive access, identity/date mismatch, unresolved class, or conflicting full-day lineage.

Rules:

- `missing_no_cli` means the complete configured search produced no matching CLI; it is not archive failure.
- `blocked_archive_access` means the search could not be completed; it is not missing.
- wrong identity/date candidates are rejected with raw evidence, not relabeled.
- no value is carried forward, imputed, or borrowed from a neighboring station/date.

**Pass threshold:** 100% of non-accepted keys have an explicit status and evidence record; 0 silent missingness. For a clean full-backfill QC disposition, `blocked_*` must be 0 and `missing_no_cli` must be separately disclosed.  
**Reject threshold:** a missing/blocked row is filled or represented as a valid settlement label.  
**Block threshold:** any missing/blocked row lacks the required search/failure evidence, or any blocked key is presented as accepted.

### QC-10 — Deterministic rerun and immutability

Without network access and without changing frozen inputs, perform two independent read-only QC passes in clean output locations. Canonicalize ordering by station, target date, archive locator, range, and hash. Exclude wall-clock fields from the canonical payload; retain them only in the receipt.

Compare both runs for byte identity and SHA-256 equality of:

- terminal status matrix;
- raw occurrence and duplicate ledgers;
- selected report hashes and parsed values;
- rejection/missing/blocked ledger;
- three-way audit extract and exclusions;
- canonical summary and disposition.

Recompute before/after hashes for every frozen input. A deterministic rerun must not fetch the network or mutate any source.

**Pass threshold:** 2/2 canonical outputs byte-identical and hash-identical; 100% frozen input hashes unchanged.  
**Reject threshold:** not ordinarily applicable; if nondeterminism proves that the artifact cannot represent a stable target, reject the affected derived result.  
**Block threshold:** any byte/hash difference, clock/network dependence, unordered selection, input mutation, or missing before/after hash.

### QC-11 — Three-way audit identity and completeness

Build the audit only from `accepted_full_day` selected CLI rows. For each attempted comparison, verify:

```text
CLI station/PIL + internal summary date + direction
↔ exact GHCN station ID + local date + element/flags
↔ exact Kalshi event/series + station + target date + direction
```

Retain raw and converted values separately. For GHCN, preserve raw tenths °C and flags; convert only for descriptive comparison. For Kalshi, include event ticker/series and source/rules metadata. HIGH and LOW rows remain distinct. Ambiguous or absent GHCN/Kalshi rows are excluded with reasons, not imputed.

The audit reports at minimum: selected CLI count, GHCN overlap, Kalshi overlap, three-way overlap, station/date/product identity failures, missing third-party rows, duplicate/revision exclusions, CLI-minus-GHCN absolute/directional differences, CLI-minus-Kalshi differences, and unresolved rows.

**Pass threshold:** 100% of included three-way rows pass exact identity; 100% of excluded rows have an explicit reason; zero duplicate event/ticker groups are silently selected. A numerical agreement percentage is not a pass substitute.  
**Reject threshold:** evidence proves GHCN or Kalshi is a different station/product/date target for the included comparison.  
**Block threshold:** any included row lacks exact identity, source/rules payload, frozen hash, or unique settlement row; any unresolved identity/revision row is treated as a source-acceptance block.

## 5. Acceptance, rejection, and block thresholds

### 5.1 Accounted-for backfill artifact

The full backfill is **accounted-for** only if all of the following hold:

- 8,200/8,200 expected station/date keys have exactly one terminal status;
- raw/scan/attempt conservation equations balance exactly;
- every successful raw body has exact range length and independent SHA-256 verification;
- every accepted selected row passes exact station/PIL/WMO and internal-date checks;
- every intermediate, duplicate, revision, rejection, missing, and blocked row remains represented;
- no source substitution or destructive overwrite is found;
- two offline QC reruns are canonical-byte/hash identical.

This status does **not** mean every key has a usable final CLI value.

### 5.2 Clean QC acceptance

A clean independent QC disposition (`ACCEPTED_FOR_DESCRIPTIVE_AUDIT_ONLY`) requires all account-for conditions plus:

- 8,200/8,200 keys are terminal;
- `blocked_* = 0` for the reviewed full-backfill scope;
- every key intended for a selected value is `accepted_full_day` with one coherent full-day value;
- exact station/PIL/WMO and internal-date checks are 100% pass;
- no unresolved full-day revision conflicts;
- all duplicate-content occurrences are accounted for;
- three-way audit rows are exact-identity only, with missing/ambiguous third-party rows disclosed;
- no evaluator, calibration, production, trading, or source-acceptance change is made.

`missing_no_cli` may be zero for a clean source-readiness result only if the approved contract requires a value for every key. If explicit missingness is allowed by the acquisition contract, it remains accepted as **accounted missing**, not as a usable label; the receipt must report the count and rate.

### 5.3 Rejection

Use `REJECTED` when evidence proves a substantive contract violation, including:

- non-SRRS/proxy data used as an accepted body;
- wrong station, PIL, WMO office, market direction, product, or target date selected;
- UTC/archive/filename date substituted for the internal summary date;
- intermediate selected as the full-day settlement value;
- duplicate HIGH/LOW or distinct event rows collapsed into one target;
- raw bytes are corrupted or do not match the declared range/hash;
- destructive overwrite or fabricated label is proven.

A rejection is not repaired by rerunning the same backfill; the evidence and affected keys must be preserved for a separately approved correction.

### 5.4 Block

Use `BLOCKED/NEEDS_REVIEW` when correctness cannot be established, including:

- any missing manifest/scan/body/attempt evidence;
- any unexplained row or byte accounting difference;
- any unhashable, mutable, or changed input;
- any absent or multiply assigned terminal key;
- unresolved station/PIL/internal-date identity;
- unresolved class, revision order, or conflicting non-identical full-day reports;
- missing/blocked rows represented as valid values;
- incomplete missing/failure search evidence;
- nondeterministic rerun or input mutation;
- any included three-way row without exact event/station/date/product identity.

Critical checks have **zero-error tolerance**. Percentages may describe coverage, but they do not waive a critical failure. For non-critical descriptive metrics, report the numerator, denominator, and rate; never hide a small denominator behind a rounded percentage.

## 6. Required receipt summary

The independent receipt must state:

1. exact scope: 20 stations, 410 dates, 8,200 keys;
2. governing spec/policy paths and hashes;
3. frozen input inventory and hashes;
4. raw scan/body/attempt counts and conservation equations;
5. per-station and per-date coverage matrix;
6. counts for accepted full-day, intermediate, revision, duplicate-content, unique bodies, rejected/mismatched, missing, and blocked rows;
7. exact station/PIL/WMO/date pass/fail counts;
8. full-day selection and revision conflict counts;
9. missing/blocked ledger summary;
10. three-way input and identity counts, overlaps, exclusions, and descriptive metrics;
11. two deterministic rerun hashes and before/after immutability hashes;
12. check-by-check PASS/REJECT/BLOCK decisions;
13. overall disposition and smallest evidence needed to unblock any block;
14. explicit statement that no evaluator, calibration, production, trading, sweep, or source-acceptance change occurred.

## 7. Accuracy-focused suggestion

After a separately approved source gate and after the applicable NWS/NCEI quality-control lag, compare every selected full-day CLI maximum/minimum against a frozen finalized independent official record (preferably NCEI LCDv2 or another explicitly documented final record for the exact mapped station). Keep the original CLI body/hash and the later record side by side; classify any difference as a revision/provenance discrepancy rather than overwriting the selected value. Stratify discrepancies by station, local-standard-time/DST boundary, report issuance timing, and intermediate-to-full-day lineage. This improves accuracy by detecting wrong-day, station, rounding, and late-revision errors without contaminating the settlement label or enabling calibration.

## 8. Explicit non-actions

This specification does not authorize:

- running or resuming the SRRS full backfill;
- new archive requests or retries;
- editing raw scans, raw bodies, manifests, checkpoints, or reports;
- changing station mappings, settlement identity, evaluator labels, calibration inputs, signal logic, sweeps, production, or trading;
- treating GHCN, LCD, METAR/ASOS, Kalshi, or any proxy as a replacement for the exact SRRS CLI without a separate approval.
