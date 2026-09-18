# GHCN-Daily vs Kalshi Target Equivalence Audit — 2026-08-15

## Purpose and guardrails

This is a post-freeze, descriptive, read-only equivalence audit. It does not fit calibration, tune or replace labels, enable calibration, alter evaluator/production/trading paths, or mutate frozen GHCN inputs. GHCN labels remain the source labels; the existing Kalshi settlement table is audit-only.

## Frozen scope and mapping

The frozen NCEI GHCN-Daily archive is under `data/external/ghcn_daily/`, governed by `manifests/acquisition_manifest_20260815.json`. Exactly 20 station keys are used from `data/station_mapping.json`; the critical mapping is `KDFW -> USW00003927` (Dallas/Fort Worth). All raw and manifest hashes are recorded in the JSON artifact.

## Semantics

* Raw `TMAX` is the GHCN-Daily integer in tenths of degrees Celsius. It is converted only for comparison as `(raw/10) * 9/5 + 32`, with no alteration of the raw label.
* The GHCN YYYY-MM-DD record is treated as the station's local calendar date; no UTC shift is applied.
* A directional change is `up` when change is greater than 0.5°F, `down` when less than -0.5°F, and `flat` otherwise. The epsilon is fixed before execution.
* Each target date is paired with the immediately prior local calendar date for the same station. No missing-date imputation is performed.
* Only unique Kalshi station/date rows with a non-null settlement temperature are compared. Duplicate/ambiguous settlement dates are retained as QC findings, not silently selected.
* Training-period overlap ends at 2026-02-28 inclusive. The audit reports coverage and missingness separately from the paired comparison.

## Outputs

The deterministic script `scripts/audit_ghcn_kalshi_target_equivalence.py` emits `data/external/ghcn_daily/derived/ghcn_kalshi_target_equivalence_audit_20260815.json`, including source and settlement hashes, station/date coverage, missingness, flat outcomes, disagreement matrix, agreement and directional agreement, temperature differences, and station/season strata.

## QC and disposition

QC must verify 20 raw files, manifest integrity, exact mapping, no GHCN duplicate dates, no mutation, and explicit treatment of settlement ambiguity. The result may be `ACCEPTED_FOR_CALIBRATION`, `REJECTED_AS_PROXY`, or `BLOCKED/NEEDS_REVIEW`. Official provenance alone cannot pass: CLI/NWS settlement semantics, station identity, date basis, rounding, and cutoff behavior must be demonstrated. This audit therefore defaults to `BLOCKED/NEEDS_REVIEW` unless those semantics are proven by the frozen evidence.

## Determinism / receipt

Run the script twice against unchanged inputs. The two JSON files must be byte-identical and have identical SHA-256 hashes. The continuity receipt records commands, hashes, source integrity, QC, disposition, and the accuracy-focused next improvement. No network fetch or write to frozen/raw/production data is permitted.
