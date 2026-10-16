# Positions Table vs the SBOX Book (ITEM4.4) — Why `UPDATE positions` matches 0 rows

**Date:** 2026-10-16 · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Origin:** v4 live-book findings (2026-10-16) — the settlement closer's
`UPDATE positions ... WHERE trade_uuids LIKE '%<uuid>%'` silently matches **0**
rows because the `positions` table is empty.

---

## Finding

`core/pnl_tracking.py` (the ONLY settlement path; `process_settlements_for_date`
delegates to it) closes a trade by updating `trades` **and** reducing the matching
`positions` row. The SBOX `paper_trading_sbox.db` `positions` table holds **0**
rows, so the position-reduction UPDATE is a silent no-op every cycle.

## Verdict: FINE for the current book — positions are DERIVED from `trades`

The SBOX book is **trade-row-first**. Rows are booked directly into `trades` by
`scripts/sbox_intraday_ev_bridge.py::record_trade` (and the LOW/replay writers),
which never inserts into `positions`. `positions` is the legacy
`core/paper_trading_engine.py` aggregate (a fold of executions into holdings) that
the SBOX bridge does not drive. The book's live state, P&L and settlement are all
computed from `trades`:

- **open book:** `SELECT ... FROM trades WHERE status='open'` (the exit pass,
  exposure gate, and reports all read `trades`);
- **P&L:** `core/pnl_tracking.py` writes `realized_pnl` on the `trades` row
  (contract-notional, qty-scaled) at settlement;
- **mark-to-market / reconciliation:** `daily_reconciliation` MTMs `positions`
  (empty → 0 rows) and derives the bankroll from `trades` + `daily_balances`.

So the empty `positions` table is **consistent, not corrupt**: there is no
position writer to feed it, and every consumer that matters reads `trades`.

## What was NOT done (and why)

- **No** `positions` writer was invented. Adding one would create a SECOND source
  of truth for the same holdings — the exact divergence class the book-keyed
  uniqueness constraint (`_ensure_trade_uniqueness`, E4-b) exists to prevent. If
  a positions aggregate is ever wanted it must be a deterministic **derived view**
  of `trades`, regenerated, never a parallel writer.
- The settlement closer's position UPDATE is left in place: it is harmless (0
  rows), it keeps the delegated `pnl_tracking` path uniform with the engine, and
  removing it would fork the shared settlement code.

## Follow-up (not blocking)

If a derived positions view is wanted for reporting, add a read-only builder
(fold `trades` by `(station, market_type, side)`); do not add an execution-time
writer. Track it as a reporting nicety, not a correctness fix.
