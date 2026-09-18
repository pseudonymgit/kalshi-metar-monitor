# GHCN/Kalshi Target-Equivalence QC Specification — Five-Station Pilot

**Date:** 2026-08-15  
**Lane:** independent quality control (read-only)  
**Pilot stations:** KATL, KDFW, KMDW, KPHX, KSEA  
**Disposition at design time:** `BLOCKED/NEEDS_REVIEW` until every gate below has evidence.

## 1. Purpose and non-goals

This QC lane tests whether a frozen GHCN-Daily TMAX target is the same target used by the Kalshi/NWS settlement path. It is an independent evidence and reproducibility check, not a calibration exercise. It must not fetch or alter source data, change labels, tune thresholds, enable calibration, or modify evaluator, production, or trading behavior. Kalshi rows are audit-only.

The QC reviewer works from the existing acquisition manifest and equivalence-audit artifacts, plus frozen settlement/report evidence supplied to the lane. A result is never promoted merely because the numerical agreement rate is high.

## 2. Frozen inputs and receipt

The baseline inputs are:

* `data/external/ghcn_daily/manifests/acquisition_manifest_20260815.json` (manifest version `2026-08-15`; retrieved `2026-08-15T07:02:52.257898+00:00`; manifest SHA-256 `274497d806e6524035dbf99ec6e761b4980220213fcba095a04b22c6dd39b8d1`).
* GHCN metadata (`ghcnd-stations.txt`, `ghcnd-inventory.txt`, `readme.txt`) and the five raw `.dly` files identified by the manifest.
* `data/kalshi_settlements.db`, only as a frozen audit input; its baseline full-file SHA-256 is `aa6a2de93261c51dc9674f002652dfe63497d59895430504f2e248f31d401f75`.
* `data/external/ghcn_daily/derived/ghcn_kalshi_target_equivalence_audit_20260815.json`, whose baseline audit reports 3,138 paired rows overall, 247 ambiguous station/date keys overall, and `BLOCKED/NEEDS_REVIEW`.

The reviewer records path, byte count, SHA-256, command/version, UTC start/end, and output hash in a QC receipt. Missing hash, changed hash, or unpinned input is a block—not a refresh opportunity.

## 3. Independent checks

Each check has an owner-independent evidence requirement and a disposition. The reviewer must inspect raw evidence and the derived output; a summary statistic cannot substitute for a failed evidence check.

### QC-01 — Raw report provenance

**Question:** Is each value traceable to the official report that defines the settlement target?

* For each pilot station/date, retain the raw GHCN record location (GHCN ID, element `TMAX`, year/month/day, raw integer, measurement/quality/source/time flags) and the corresponding raw NWS CLI daily climate report or other explicitly identified settlement report.
* Record report product name, issuing office, observation station, report date, retrieval timestamp, URL or archive locator, and whether it is preliminary/final/revised.
* Verify the report is the actual settlement authority, not a nearby station, forecast, model output, or an unverified secondary table.
* **Pass:** all sampled rows have a provenance chain from report to parsed value to target row. **Block:** any missing authority, untraceable row, or claim based only on “official GHCN” provenance.

### QC-02 — URL, retrieval, byte, and hash integrity

For every raw report, metadata file, settlement extract, and derived input, verify URL/archive locator, HTTP/file retrieval receipt, UTC timestamp, byte count, SHA-256, and the manifest entry. Recompute hashes independently; do not trust a producer-supplied hash alone. Confirm the audit output records the canonical settlement-row hash and source hashes.

* **Pass:** all hashes and sizes match the frozen receipt and all URLs resolve to the stated artifact (or an immutable archive locator).
* **Block:** mismatch, mutable unversioned source without a frozen copy, missing retrieval receipt, or any network refresh during QC.

### QC-03 — Exact station mapping

Verify the complete chain `Kalshi market station key -> NWS/CLI station identifier -> GHCN ID -> metadata coordinates/name -> raw file`. For the pilot the expected GHCN mapping is:

| Kalshi key | GHCN ID | Required identity evidence |
|---|---|---|
| KATL | USW00013874 | Atlanta airport/observatory metadata and report station agree |
| KDFW | USW00003927 | Dallas/Fort Worth airport/observatory metadata and report station agree |
| KMDW | USW00094846 | Chicago Midway metadata and report station agree |
| KPHX | USW00023183 | Phoenix airport/observatory metadata and report station agree |
| KSEA | USW00024233 | Seattle airport/observatory metadata and report station agree |

Coordinates, name, ICAO/WMO aliases, and station history must be compared, not inferred from city names. A station alias or relocation requires explicit documented equivalence. **Pass:** one exact, justified mapping per station. **Reject:** wrong airport/nearby station. **Block:** unresolved alias, relocation, or conflicting identifiers.

### QC-04 — Report/product/date/cutoff semantics

For every pilot row, independently answer:

1. Which report product is authoritative (e.g., CLI daily climate report versus another NWS product)?
2. Which report observation day is the target's local calendar date?
3. Is the GHCN `.dly` date compared to the same local date, with no accidental UTC shift?
4. What report issuance, settlement, and data cutoff apply? Is the target frozen at the stated cutoff (`2026-02-28` for the baseline audit), inclusive/exclusive?
5. Are late reports, missing reports, preliminary reports, and post-settlement revisions handled explicitly?
6. Is the directional target paired with the immediately prior local calendar day, and is missing prior-day data left missing rather than imputed?

The reviewer constructs a row-level semantic table with `target_date`, `prior_date`, product, report date, cutoff status, and inclusion/exclusion reason. **Pass:** the same written rule explains every included row and every excluded row. **Block:** any undocumented date shift, cutoff ambiguity, or report/product substitution.

### QC-05 — Units, rounding, and revision semantics

Recompute GHCN TMAX from the raw tenths-of-degrees-C integer without mutating the raw label: `(raw / 10) * 9/5 + 32`. Capture missing sentinel and all flags. Independently document how the settlement report converts, rounds, truncates, or displays temperature, and whether Kalshi stores the displayed or underlying value. Verify the directional epsilon is frozen at `0.5°F`: `up > +0.5`, `down < -0.5`, otherwise `flat`.

Compare both absolute values and prior-day deltas before and after the documented rounding rule. Revisions must be classified as preliminary-to-final, corrected report, or unchanged; never silently overwrite the originally observed value. **Pass:** rounding and revision behavior is demonstrated on boundary examples. **Reject:** a proven unit, rounding, or target-definition mismatch. **Block:** semantics not demonstrated.

### QC-06 — Duplicate settlement classification

Group settlement rows by `(station, target_date)` and retain every member, including event ticker/series, value, market count, finalization count, source, and source type. Classify each group as:

* **Exact duplicate:** same semantic event and same value/receipt; retain both in evidence but count once only under a declared de-duplication rule.
* **Revision:** same event/date with changed value or status; retain versions and apply the documented settlement-finality rule.
* **Distinct market/product:** different series, contract, cutoff, or target definition; do not merge.
* **Unresolved ambiguity:** cannot prove which row settles the target; exclude from comparison and `BLOCK` the affected station/date.

The baseline audit found 247 ambiguous station/date keys overall. In the five-station pilot, known ambiguous keys include KMDW `2024-02-28` and every date from `2026-01-04` through `2026-02-28` present with two rows. These are test fixtures for this check, not rows to be selected by “first,” latest insertion order, or non-null preference. **Pass:** every duplicate has an evidence-backed classification. **Reject:** a duplicate is proven to represent a different target than GHCN. **Block:** unresolved.

### QC-07 — No silent row selection

Run a row-accounting reconciliation: raw settlement rows read = rows retained in the evidence ledger + rows explicitly excluded, with an exclusion reason for each. The comparison input must be generated only after the ledger is reviewed. Any selection operation must use an explicit key and documented rule; arbitrary SQL ordering, `LIMIT 1`, dictionary overwrite, or “latest row” behavior is prohibited unless that rule is the proven settlement-finality rule.

Emit counts by station/date for included, missing, duplicate, revised, invalid, and out-of-cutoff rows. **Pass:** zero unexplained row loss and zero silent overwrite. Any unexplained loss is a **block**.

### QC-08 — Deterministic rerun and immutability

Using unchanged frozen inputs, run the audit twice in a clean environment. Compare canonical JSON bytes, output SHA-256, row counts, source hashes, ambiguity ledger, and disposition. Also verify raw GHCN files, manifest, settlement database, and existing audit artifact have unchanged hashes before and after.

**Pass:** both runs are byte-identical and all input hashes are unchanged. **Block:** nondeterministic output, clock/network dependence, unordered row behavior, or mutation. A rerun that changes only a timestamp is not deterministic unless timestamps are excluded from the canonical payload and separately recorded in the receipt.

## 4. Pilot matrix

The matrix deliberately covers three seasonal regimes for every station and includes normal, boundary, missing/prior-day, and ambiguous cases. Dates are fixtures to be selected from the frozen inputs; if a requested fixture is unavailable, record `BLOCKED/MISSING FIXTURE` rather than substituting silently.

| Station | Exact mapping | Summer fixture (JJA) | Winter fixture (DJF) | Transition fixture (MAM/SON) | Known ambiguity / adversarial fixture |
|---|---|---|---|---|---|
| KATL | USW00013874 | 2025-07-15 target + 07-14 prior | 2026-02-05 + 02-04 | 2025-03-20 + 03-19 | duplicate group if present; otherwise explicit no-duplicate proof |
| KDFW | USW00003927 | 2025-07-15 + 07-14 | 2026-02-05 + 02-04 | 2025-03-20 + 03-19 | duplicate group if present; test missing prior-day handling |
| KMDW | USW00094846 | 2025-07-15 + 07-14 | 2026-01-15 + 01-14 | 2025-03-20 + 03-19 | **2024-02-28** and **2026-01-04..2026-02-28** duplicate rows; include at least first, middle, last |
| KPHX | USW00023183 | 2025-07-15 + 07-14 | 2026-02-05 + 02-04 | 2025-03-20 + 03-19 | duplicate group if present; test rounding near ±0.5°F |
| KSEA | USW00024233 | 2025-07-15 + 07-14 | 2026-02-05 + 02-04 | 2025-03-20 + 03-19 | duplicate group if present; test report/date and missingness |

For each station/regime, the minimum evidence packet is one raw GHCN excerpt, one authoritative report excerpt, parsed values, prior-day values, settlement ledger entries, semantic decision, and independent reviewer sign-off. Expand each cell to at least three rows when available: one `up`, one `down`, and one `flat`/near-boundary case. Do not manufacture a class by changing epsilon.

## 5. Decision criteria

### ACCEPT — `ACCEPTED_FOR_DESCRIPTIVE_EQUIVALENCE_ONLY`

All QC-01 through QC-08 pass for all five stations and all matrix cells; exact mapping and settlement authority are proven; duplicate groups are classified; no row is silently selected; hashes and reruns are deterministic; and no semantic disagreement remains. This status does **not** by itself enable calibration or trading. Any calibration use requires a separately approved gate.

### REJECT — `REJECTED_AS_PROXY`

Use when evidence proves GHCN and Kalshi are different targets: wrong station, different report product, incompatible date/cutoff, irreconcilable rounding/unit rule, or a revision/finality rule that changes the target definition. Preserve the evidence and quantify affected rows; do not “repair” labels.

### BLOCK — `BLOCKED/NEEDS_REVIEW`

Use for missing or mutable provenance, hash mismatch, unresolved station alias, unresolved duplicate/revision, missing report/cutoff semantics, silent row loss, unavailable fixture, nondeterministic rerun, or any source mutation. The baseline is blocked because exact CLI/NWS settlement semantics are not demonstrated and duplicate settlement rows remain ambiguous.

## 6. QC receipt and handoff

The final receipt must contain: scope and cutoff; input inventory and hashes; station mapping table; matrix completion; per-check PASS/REJECT/BLOCK with evidence pointers; duplicate ledger; row reconciliation; deterministic rerun hashes; before/after immutability hashes; unresolved questions; disposition; and reviewer/date. A blocked receipt must name the smallest evidence needed to unblock it. No calibration, trading, or source mutation is permitted as part of remediation.

## 7. Accuracy-focused improvement

**Recommendation:** freeze the corresponding NWS CLI daily climate reports for the same five stations and target dates, including preliminary/final/revised versions and issuance timestamps, then perform a row-level three-way comparison: GHCN raw TMAX → CLI reported maximum → Kalshi settlement. This is the highest-value accuracy improvement because it tests the actual settlement product, local date, rounding, and revision path rather than treating GHCN's official provenance or airport proximity as evidence of target identity. It should precede any calibration decision and remain descriptive/read-only.
