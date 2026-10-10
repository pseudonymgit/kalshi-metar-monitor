# Custody + Gate-Ledger Reconciliation for the 64 Replay Rows

**Date:** 2026-10-10 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Predecessor:** `docs/plans/GR5-WIRING-RECEIPT-2026-10-10.md`
**Scope:** backfill the settlement-admission audit surfaces for the 64 settled
historical-replay rows. No live trading. No `trades`/P&L mutation. Append-only.

---

## The gap (as reported) and its true shape

The task framed the gap as: 64 settled closed rows in `data/paper_trading_sbox.db`
were admitted at settlement but never produced `data/gate_ledger.db` /
`data/trade_custody.db` rows (surfaces sat at 29 / 27).

Investigation found the gap is **narrower and more specific than described**, and
there were **two distinct missing links**:

1. **The standalone audit surfaces are missing the settlement-admission verdicts.**
   Every one of the 64 rows lacked a standalone `trade_custody` row (0/64) and a
   standalone `gate_ledger` verdict (0/64). This is the real gap and is now closed.

2. **The standalone surfaces are fossils of the pre-purge book.** All 29 ledger
   rows and all 27 custody rows reference `trade_uuid`s that no longer exist in
   the SBOX book at all (0/29 and 0/27 match live `trades`). They are the
   append-only archives the purge deliberately preserved
   (`.meta/continuity/weather-engine/2026-10-10-purge-and-historical-replay.md`).
   The 29 = 27 admitted + **2 G5-rejected** closed rows that the purge removed.

3. **The settlement boundary has never run against the current book.**
   `core/booking_admission.admit_settled_trades()` has no production caller
   (only `tests/test_booking_wiring.py`). So no settled row — old or replay —
   ever got a *standard* (full G0–G5) admission verdict; the booking-time
   decidable subset (G0/G1/G4) was all the live path wrote.

### Correction to one premise

The SBOX book carries its **own embedded** `gate_ledger` / `trade_custody`
tables (created in `data/paper_trading_sbox.db` by the wire-up proof). Those hold
the replay's *booking-time* rows: ledger = 76 rows, custody = 69 rows. All 64
closed rows DO have an embedded custody row (64/64) — but it is
`settlement_confirmed=0` (a booking-time snapshot), and the embedded ledger keys
are synthetic `SBOX-<station>-<date>` strings, not the real `trade_uuid`, so the
two surfaces **cannot be joined on identity**. The replay wrote the booking-time
substrate; it never wrote the settlement-admission substrate. That is exactly why
the 64 rows needed the admission boundary run.

## Remedy chosen

**(a) Backfill** — run the real admission boundary
(`candidate_from_trades_row` -> full composed gate G0–G5 -> ledger append ->
`record_custody_for_trade`) over the 64 settled rows, driven by the new
idempotent script `scripts/reconcile_custody_gate_ledger.py`. No fields were
missing that blocked the chain: every row supplied the real `created_at_utc`
(pinned 13:30-local entry), a real `settlement_snapshot.settled_at_utc`
finalization anchor, the canonical strike-crossing outcome, and
`trade_version=v3.1_sbox` — so G1–G5 all passed.

Option (b) from the task (field-shortfall documentation) did **not** apply: no
booking-time-writer field (`market_ticker`, etc.) was required and absent.

### Gate result over the 64 rows

**64/64 ADMITTED, 0 rejected, 0 excluded.** G3 now passes because the
temperature-vs-binary reconciliation documented as "carry forward #1" in the
wiring receipt has since landed (`candidate_from_trades_row` surfaces
`canonical_outcome`; `settled_value` is no longer required to be `{0,1}`).

## Counts before / after

| Surface | Before | After | Δ |
|---|---|---|---|
| `data/gate_ledger.db` (rows) | 29 | **93** | +64 |
| `data/trade_custody.db` (rows) | 27 | **91** | +64 |

* Full custody for the 64: **64/64** (custody_id 28–91; chain extends from the
  existing row 27, `prev_hash` links intact).
* Rows excluded: **0**. No fabricated hashes — every `record_hash` is computed
  from the row's own stored values by the existing writer.

### Chain integrity

* `trade_custody.db` chain: **ok** (stdlib `verify_chain`, 0 problems) — the
  original 27-row chain is preserved and extended.
* Embedded SBOX custody chain: **ok** (unchanged).
* `trades` table: **no mutation** — read-only; P&L and settlement columns
  untouched.

## Safety / idempotency

* `trades` opened `mode=ro`; driver refuses any trades DB whose path lacks
  `sbox`.
* Append-only by construction: existing ledger/custody rows are never updated or
  deleted (immutability triggers intact).
* Custody writes are idempotent (UNIQUE `idempotency_key`); the ledger append is
  guarded by a per-`trade_uuid` existing-ADMITTED check, so a re-run is a no-op:
  verified re-run → custody 91 -> 91, ledger 93 -> 93, `custody_written: 0`.
* Backup before write: `data/backups/pre-custody-gate-reconcile-20261010T174522Z/`
  (sbox + both standalone DBs).

## Finding carried forward (not fixed here — out of scope)

`admit_settled_trades()` appends a ledger verdict on every invocation with **no
idempotency guard**; re-running it doubles ledger rows (measured: 93 -> 157 on a
second call). The new driver closes that for this backfill (guarded append), but
the production writer still needs a ledger-side idempotency key before it is
scheduled/called repeatedly. Flagged, not hidden.

## Artifacts

* `scripts/reconcile_custody_gate_ledger.py` (new) — idempotent backfill driver.
* Backups: `data/backups/pre-custody-gate-reconcile-20261010T174522Z/`.
* DB files are gitignored (`*.db`); the reconciliation is reproduced by re-running
  the script, not by committing a binary.
