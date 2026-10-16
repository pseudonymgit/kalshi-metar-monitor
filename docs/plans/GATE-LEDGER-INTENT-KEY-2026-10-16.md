# Gate-Ledger Intent Key (ITEM4.3) — Why the ledger `trade_uuid` is synthetic

**Date:** 2026-10-16 · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Origin:** v4 live-book findings (2026-10-16) — `gate_ledger.trade_uuid` carries a
synthetic uuid (`SBOX-{station}-{day}`) with **0 direct joins** to `trades`.

---

## Finding

`scripts/sbox_intraday_ev_bridge.py` calls the GR5 §2 booking gate with

```python
admit_decision_candidate(trade_uuid=f"{INSTANCE}-{st}-{day_str}", ...)
```

i.e. `SBOX-KBOS-2026-10-15`. The booked `trades` row instead carries a random
`uuid4` (`143210bd-…`). A naive `gate_ledger JOIN trades ON trade_uuid` returns 0,
which looks like an orphaned ledger.

## Verdict: BY DESIGN — the intent key is the correct ledger key

The ledger is **append-only** with a UNIQUE `idempotency_key =
sha256(trade_uuid|trade_version|verdict|checked_at)`
(`scripts/gate_composer.py::verdict_idempotency_key`). The key MUST be
**reproducible across a cron re-run**, or every re-invoke would enqueue a fresh
duplicate verdict under a new random uuid4. The deterministic decision-intent key
`SBOX-{station}-{day}` is exactly what makes the append idempotent — a re-run of
the same booking intent reproduces the same key and is a true no-op.

This is the same pattern the custody surface already uses: `_ensure_trade_uniqueness`
keys the book on `(station, market_ticker, created_at_utc)` — a deterministic
DECISION intent — precisely because a random per-run token is race-prone. The
ledger mirrors that discipline.

## How to join the ledger to the book

Join on the **decision intent**, not the random row id:

```
gate_ledger.trade_uuid  ==  'SBOX-' || trades.station || '-' || trades.trade_date_utc
```

`(station, trade_date_utc)` maps 1:1 to the booked row for the intraday family
(the book is unique on `(station, trade_date_utc, functionality)` and, for the
intraday leg, on `(station, market_ticker, created_at_utc)`).

## What was NOT done (and why)

- **No** uuid4 was written back into the ledger rows: the ledger is immutable
  (`trg_gate_ledger_no_update` / `_no_delete` `RAISE(ABORT)`) and the idempotency
  key is derived from the intent uuid — rewriting it would break the key's
  reproducibility and the append-only contract.
- The booked row keeps its own uuid4 (the row identity the custody chain and the
  settlement closer both use).

Both identities are intentional and live at different layers: **intent key** (ledger,
reproducible) vs **row id** (book/custody, unique). No reconciliation is needed.
