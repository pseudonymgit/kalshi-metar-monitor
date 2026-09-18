# SRRS CLI Report-Lineage QC — 2026-08-15

## Scope and stop boundary

This QC corrects selection/audit lineage for **only 2025-01-15** across the frozen 20-station SRRS contract. It may reuse the immutable cached `.scan` listings and raw HTTP range bodies already acquired under `data/raw/srrs_cli_backfill/2026-08-15/`; it must not broaden the date range or resume the full backfill. No calibration, evaluator, production, trading, or sweep artifacts are changed.

The historical official archive remains a provenance lane: a selected SRRS body is not silently treated as an approved settlement source. This QC determines whether report lineage is internally coherent and produces an audit-only comparison where existing settlement rows permit it.

## Required raw retention and accounting

1. Every raw bulletin body successfully fetched for a station/date is retained immutably, addressed by SHA-256, even when it is an intermediate, duplicate, revision, mismatch, or ultimately blocked.
2. Every candidate scan row and every fetched body has a manifest/provenance record. Repeated byte-identical archive copies are retained as separate provenance occurrences but classified `duplicate_content` in addition to their semantic class.
3. A body is eligible for target-date lineage only if its internal summary date, station/PIL, and report semantics match the requested station/date. Wrong internal dates remain accounted for as rejected candidates/failures and never become settlement values.
4. No-silent-loss reconciliation is mandatory: `candidate scan rows = accepted raw occurrences + rejected/mismatched rows`; `accepted raw occurrences = unique retained raw bodies + duplicate occurrences`; and each station/date has exactly one terminal status.
5. Reclassification does not delete or rewrite v1 artifacts. v2 manifest/audit files include source-v1 hashes and selected/provenance hashes.

## Report classes

Classification is derived from raw report text, not file size, fetch time, or guessed issuance time:

- `full_day`: report semantics show the completed prior local day, including a `YESTERDAY` observation table/record section. A same-target-date full-day report may be issued after midnight on the following archive date.
- `intermediate`: report semantics show the still-open current local day, including a `TODAY` observation table/record section and/or an explicit `VALID AS OF` time. `VALID AS OF` alone is not enough if the observation period cannot be identified; such a body is unresolved and blocks.
- `revision`: a report explicitly identifies correction/amendment/revision/updated content, or a later same-lineage report supersedes an earlier one. It remains retained. A revision is eligible for selection only when it is itself an unambiguous full-day report and does not conflict with another full-day revision.
- `duplicate_content`: a byte-identical body occurrence already retained under another scan row. This is an orthogonal provenance label; preserve the occurrence and classify its underlying semantic class too.

A body that cannot be classified from the raw text, or whose station/date/report semantics cannot be established, is unresolved and blocks the station/date. Stop rather than substitute LCD/GHCN/METAR or another proxy.

## Selection policy

For each `(station, internal_summary_date)` key:

1. Filter only bodies with exact target internal summary date and exact PIL/station identity. Keep all raw occurrences and failures in the ledger.
2. Prefer `full_day` for the settlement value. A same-date intermediate + full-day pair is **accepted lineage**, not ambiguity: the intermediate is retained as provenance and the full-day body supplies the settlement max/min.
3. Multiple full-day bodies are safe only when they are byte-identical or semantically identical for the settlement fields and have a determinable revision order. Byte-identical occurrences are `duplicate_content`, not conflicting revisions.
4. If multiple non-identical full-day bodies disagree on settlement value, or revision order cannot resolve them, terminal status is `blocked_conflicting_full_day_revisions`.
5. If no full-day body exists, terminal status is `blocked_no_full_day` even if intermediates exist. Do not use an intermediate as the settlement value.
6. If exact station/date identity is absent, terminal status is `blocked_station_or_date_mismatch` (or `missing_no_cli` when no candidate exists). If report class cannot be distinguished from raw text, terminal status is `blocked_unresolved_lineage`.
7. Non-selected intermediate reports are never dropped: they are labeled `intermediate` and linked to the selected full-day via `lineage_key`/provenance fields. Selected body is marked `settlement_selected=true`; all other accepted bodies are `settlement_selected=false`.

A station/date is `accepted_full_day` only when exactly one coherent full-day semantic value remains after duplicate/revision handling. `found` is reserved for legacy/no-selection compatibility and is not used by the v2 QC output.

## Conflict examples required by focused tests

- KATL, KDFW, KAUS, KDEN, KHOU: same target date has intermediate and full-day reports. Select full-day; retain intermediate; do not mark ambiguous merely because preliminary values differ.
- Synthetic conflicting-full-day fixture: two non-identical full-day bodies with different max/min values for the same station/date must remain blocked.
- Synthetic unclassifiable fixture: raw text lacking `TODAY`, `YESTERDAY`, or an explicit equivalent must remain blocked; do not infer class from byte length.

## Audit outputs and GO gate

The v2 bounded audit must report station count (20), selected full-day count, intermediate retained count, conflicting-full-day count, missing/no-full-day count, accepted CLI rows (selected settlement rows), rejected/mismatched count, duplicate-content count, and any three-way metrics. Existing GHCN/Kalshi values may be compared descriptively only where identity/uniqueness gates pass; ambiguous settlement rows are excluded and retained in unresolved accounting.

This QC does **not** authorize full backfill. Full backfill disposition is `NO-GO / separate GO required` unless all station/date lineage is classifiable, no unresolved/conflicting full-day lineage remains, no proxy substitution is proposed, raw/manifests are reproducible, and a separate approval is issued. Accuracy-focused suggestion: after the source gate is approved, compare selected full-day max/min against a delayed finalized independent official record and retain any discrepancy as an explicit revision/provenance flag rather than overwriting the selected raw body.
