# SRRS KHOU 2025-01-26 conflict review specification

**Date:** 2026-08-15 UTC  
**Scope:** focused, read-only review of `KHOU|2025-01-26` in SRRS backfill v2.  
**Out of scope:** full acquisition, calibration, evaluator, trading, sweep, proxy substitution, and modification of v2 evidence.

## Objective and invariants

The v2 record has four identity-matching retained reports (two intermediate and two full-day) and was correctly stopped because the two full-day values differ. This review may classify the conflict only from official report semantics and explicit official revision/lineage evidence. It must not select the first, latest by timestamp alone, largest/smallest value, longest body, file order, retrieval order, or an arbitrary SQL row. Every candidate, raw occurrence, rejection, and hash remains retained.

The frozen identity is `KHOU` → NWS `issuedby=HOU`, WMO office `KHGX`, PIL `CLIHOU`, Kalshi series identity `KXHIGHTHOU`. A target key is keyed by station plus the **internal report summary date**, not archive directory date or publication date.

## Conflict classes

### 1. Conflicting full-day reports

Two or more identity-matching reports are `full_day` when their primary temperature table is for `YESTERDAY` (the completed local day) and their internal summary date is the target date. They are a conflict when their settlement fields (here maximum and/or minimum) differ and no explicit official correction/supersession relation resolves the difference. A conflict is not resolved by chronology alone. The terminal disposition is `blocked_conflicting_full_day_revisions`; selected value is null.

Byte-identical occurrences are not value conflicts: retain each occurrence, label duplicate content, and use one semantic value. Reports with different narrative bytes but identical settlement fields require an explicit deterministic same-value policy and remain separate provenance occurrences.

### 2. Revision versus duplicate

A **duplicate** is a repeated occurrence of identical raw bytes (same SHA-256), regardless of archive location. It is retained and labeled `duplicate_content`; it cannot be used to manufacture a second value.

A **revision** is a distinct body whose official product identity explicitly marks correction/amendment/revision (for this review, the WMO correction marker `CCA` is accepted) or provides an official same-lineage supersession relation. The revision label is orthogonal to `full_day`/`intermediate`. A corrected report may be selected only when it is itself an identity-matching, unambiguous `full_day`, and the official marker plus issuance/lineage evidence establishes that it supersedes the earlier conflicting full-day report. Retain the superseded body and link both hashes.

A later body without an explicit official correction/supersession marker is **not** a revision merely because its issue time is later. It remains a conflicting full-day report.

### 3. Internal summary date versus issue/valid date

- **Internal summary date:** date printed in `SUMMARY FOR ...`; this is the target climate/report date and must equal `2025-01-26`.
- **Issue date/time:** WMO header timestamp (UTC), e.g. `CDUS44 KHGX 270730`; it identifies publication chronology and is not the target date.
- **Body local timestamp:** report prose timestamp, retained as text; it does not replace the internal summary date.
- **Valid date/time:** `VALID TODAY AS OF ...` identifies an open/intermediate report. It is not a full-day settlement value. A same-date `TODAY` report plus a completed `YESTERDAY` report is accepted lineage, not a conflict.

Archive date/hour and retrieval time are locator/provenance only. They never determine the target date or final value.

### 4. PIL/station mapping

The exact station gate is all of:

| field | required KHOU value |
|---|---|
| station contract | `KHOU` |
| NWS issuedby | `HOU` |
| WMO office token | `KHGX` |
| PIL | `CLIHOU` |
| internal summary date | `2025-01-26` |
| Kalshi identity mapping | series `KXHIGHTHOU`, issuedby `HOU` |

A WMO bundle may contain other climate stations. A body with the wrong PIL or a different internal date is a retained rejection (`mapping_mismatch` or `internal_date_mismatch`), even if its WMO header and city are nearby. No neighboring station, LCD, GHCN, METAR, or other proxy can resolve a KHOU identity failure.

## Allowed resolution policy

1. Preserve and hash every v2 candidate/raw occurrence.
2. Partition by exact PIL/station and internal summary date.
3. Classify the primary observation period from the body (`TODAY` = intermediate; `YESTERDAY` = full-day; otherwise unresolved).
4. For multiple full-day values, accept a selected revision only if an explicit official correction/supersession marker and coherent same-lineage identity establish order. For this case, `CCA` in the later WMO header is the required official marker; the corrected report must itself be full-day and match station/date/PIL.
5. If that evidence is absent, or if two corrected full-day reports still disagree without an order, remain blocked. Never use an arbitrary latest/first rule.
6. Write a new v3 artifact; never mutate v2 manifest/raw/checkpoint/summary.

## KHOU-specific review expectation

The retained bodies are:

- intermediate `CDUS44 KHGX 262220`, max/min `77/55`;
- intermediate `CDUS44 KHGX 262257`, max/min `70/55`;
- full-day `CDUS44 KHGX 270730`, max/min `77/55`;
- full-day `CDUS44 KHGX 271523 CCA`, max/min `70/55`.

The last body carries an explicit official correction marker, has the same `CLIHOU` and internal date, and is later than the uncorrected full-day body. The focused checker may therefore resolve the conflict as an official correction and select `70/55` in v3. It must record the original `77/55` body as superseded provenance. This is not a general “latest wins” policy.

The related Kalshi mapping is identity evidence only. The frozen settlement DB has no exact `KHOU|2025-01-26` row; absence must be reported, not imputed.

## QC and resume gate

The focused checker must verify candidate-row accounting, raw-file existence and SHA-256, exact accepted report classes/values, CCA marker/order, v2 immutability, v3 deterministic output, and the absence of an exact Kalshi settlement row. A deterministic second run must produce identical v3 manifest, summary, review, and checker-output hashes.

Resolution of this one key permits a **conditional GO** to resume backfill only with v3 as the explicit source state; it does not authorize calibration/evaluator/trading/sweep use. If the checker cannot prove the CCA lineage, disposition is **NO-GO** and escalation must name both full-day hashes, values, issue headers, and request an authoritative NWS/NCEI correction/supersession record. Accuracy suggestion: after source acceptance is separately approved, compare the selected corrected CLI value against the delayed finalized official record while retaining both pre-correction and correction hashes as revision labels.
