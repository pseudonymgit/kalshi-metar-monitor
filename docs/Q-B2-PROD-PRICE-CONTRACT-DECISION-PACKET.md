# Q-B2-PROD-PRICE-CONTRACT — approved monitor contract

**Status:** `APPROVED_MONITOR_CONTRACT`  
**Scope:** read-only B2 input contract; recovery worktree docs/tests/fixture only  
**Prepared:** 2026-09-12 UTC  
**Worktree:** `prototypes/recovery-worktrees/we-recovery-2`

## Approved contract

- **API contract identifier:** `kalshi-predictions-rest-v2`
- **Base URL:** `https://api.elections.kalshi.com/trade-api/v2`
- **Endpoint:** `GET /trade-api/v2/markets`
- **Permitted source fields:** paired unsuffixed `yes_bid` and `yes_ask` only
  (`bid_source_field` and `ask_source_field` are persisted exactly)
- **Unit:** cents (the API contract declares this; no unit inference or magnitude inference)
- **Derived value:** `mid_price = (yes_bid + yes_ask) / 2`, in cents
- **Fallback:** `reject_suffixed_or_unpaired`; reject dollar-suffixed, mixed-source,
  absent, one-sided, malformed, non-finite, reversed, or out-of-range quotes.
  No conversion or fallback inference.
- **New-row metadata:** persist `price_unit`, exact source fields, API contract
  identifier, and provenance status.

Rows captured under this established surface are verified monitor-mode rows and
may be considered by B2 only when their persisted provenance is `verified` and
contract identifier is nonempty. This does not authorize trading or deployment.

## Fail-closed uncertainty

When the source/endpoint contract cannot be established, monitor mode preserves
capture as `api_contract_version = UNKNOWN_UNVERIFIED` and
`provenance_status = unverified`. B2 must reject those rows. Existing rows are
not relabeled, converted, backfilled, or rewritten.

## Operational boundary

This decision and implementation change only the recovery worktree. It does not
change the canonical checkout, live DB, scheduler, credentials, deployment, or
trading mode.
