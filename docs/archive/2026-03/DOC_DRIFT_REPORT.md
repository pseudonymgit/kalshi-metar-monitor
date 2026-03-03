# DOCUMENTATION DRIFT AUDIT REPORT

Date: 2026-03-02
Scope audited: `README.md`, architecture docs, governance docs, observability docs.

## Executive Summary

The documentation set is broadly aligned with deterministic execution and observability-domain separation. Main drift is not contradictory behavior text; it is **operational ambiguity** around dynamic market discovery failure modes and hydration/coverage interpretation when Kalshi connectivity is unavailable.

## Drift Findings

## 1) Market-authoritative station language vs fallback behavior
- Documentation states that markets are source-of-truth for active stations.
- Runtime `app._canonical_live_station_universe()` uses market-derived stations **only when** `build_market_polling_station_universe()` succeeds; otherwise it falls back to config/state/watchlist + discovered series union.
- Drift class: **partial mismatch in strictness wording**.
- Impact: operator may expect fully market-authoritative station set even during market-derivation failure.

## 2) Hydration completeness interpretation gap
- Observability docs emphasize hydration readiness surfaces, but practical semantics across endpoints rely on cache status (`cache_missing`, `cache_stale`, `cache_valid`) and per-row coverage reasons.
- Drift class: **incomplete cross-doc invariant**.
- Missing invariant to document explicitly: market coverage rows can be `market_data_unknown` even when scheduler/ingestion is healthy.

## 3) Bootstrap window detail dispersion
- Bootstrap/lookback constants are in execution code (`BOOTSTRAP_LOOKBACK_MINUTES`, overlap/cushion/publishing lag), but top-level docs do not consolidate these exact values in one canonical operations table.
- Drift class: **detail fragmentation** (not contradiction).

## 4) Execution-domain separation accuracy
- Governance and API docs correctly state observability endpoints must not perform live Kalshi calls.
- Code-level domain guard in Kalshi public GET path aligns with documentation.
- Drift class: **no mismatch detected**.

## 5) Outdated hydration description check
- No direct outdated hydration-state token list was found in audited docs; however, operational docs do not clearly bind endpoint-level `coverage_reason` to hydration prerequisites for triage ordering.
- Drift class: **triage guidance gap**, not behavior mismatch.

## Confirmed Accurate Areas

- Transition-driven alert model and Phase-1 semantic immutability are consistently documented.
- Read-only observability boundary is consistently documented.
- HIGH/LOW symmetry intent is consistently documented.

## Recommended Documentation Corrections (documentation-only)

1. Add a single canonical "station-universe resolution order" section in README/Architecture:
   - market-derived stations when available,
   - fallback union path when unavailable.
2. Add a canonical "coverage_reason interpretation" matrix in observability docs.
3. Add a bootstrap-window constants table in operations docs with direct code-value linkage.
4. Add explicit invariant: discovery/hydration failures must not be inferred from alert absence alone; use runtime-authority + market-coverage surfaces together.

## Non-goals respected
- No execution logic change proposed.
- No ingestion/hydration/eligibility behavior change proposed.
- Structural accuracy and governance hardening only.
