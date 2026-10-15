# Custody / Gate-Ledger vs Blank-Slate Book — Reconciliation (2026-10-15)

**Date:** 2026-10-15 · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Origin:** PWW Bug Hunt v3 (2026-10-15) finding #4 — `trade_custody` (91 rows)
/ `gate_ledger` (100 rows) reference `trade_uuid`s absent from `trades` (0 rows).
**Script:** `scripts/reconcile_custody_book_2026-10-15.py` · **Artifact:**
`artifacts/custody-integrity/custody_book_reconcile_2026-10-15.json`

**Verdict: BY DESIGN — KEEP. No rows were cleared.**

---

## 1. Are the 91 custody rows orphans, or real?

They are the **append-only archive of the pre-blank-slate book**. Workstream AG-0
(register commit `0f5edf2`, 2026-10-14) cleared the SBOX book to 0 rows as a fresh
start, preserving the prior books in dated `data/*.bak-*` backups. The
chain-of-custody surfaces are *intended* to outlive the book: the purge removed
`trades` rows, and the custody/ledger rows are the deliberate forensic record of
what was booked before the clear.

Evidence (read-only):

| fact | value |
|---|---|
| `data/paper_trading_sbox.db` `trades` | **0** |
| embedded `trade_custody` | **91** (0/91 join `trades`) |
| embedded `gate_ledger` | **100** |
| custody `created_at_utc` range | 2026-10-10T14:15 → 2026-10-14T18:15 (all pre-blank-slate) |
| `trade_custody` chain `verify_chain` | **ok (0 problems)** |

This matches the earlier, narrower reconciliation
(`docs/plans/CUSTODY-GATE-RECONCILE-2026-10-10.md`), which already established the
same class: the standalone surfaces are "fossils of the pre-purge book… the
append-only archives the purge deliberately preserved."

**Do NOT clear them.** The tables are immutable by design — `trg_trade_custody_no_delete`
/ `_no_update` and the gate-ledger equivalents `RAISE(ABORT)`. A "clear" would have
to disable those triggers and would break the hash chain
(`record_hash = sha256(prev_hash || payload)`, GENESIS → row N). If a clear is ever
wanted it must be an explicit **archive-out** (copy to a dated backup, drop triggers
in that copy), signed off by Dan — never in place.

## 2. `disposition` NULL — expected, not a wiring gap

- The `trade_custody` **booking writer** (`core/trade_custody.record_from_trade`)
  never writes `disposition`; the column is an **optional governance annotation**.
- The only writer that sets it is `scripts/reconcile_stale_open_trades_2026-10-13.py`
  (`AUTO`/`MANUAL` on a stale-open reconcile).
- `gate_ledger` **has no `disposition` column at all** (its axis is
  `verdict` + `failed_gate`; all 100 embedded rows are `ADMITTED`).

So `disposition IS NULL` on all 91 rows is the correct default for booking-time
rows, and there is no gate-ledger "disposition" to be NULL.

## 3. Which DB does the live path write?

- **Live bridge** (`scripts/sbox_intraday_ev_bridge.py::run`) calls
  `record_custody(conn, …)` with `conn` = the **SBOX book**, i.e. the embedded
  `trade_custody`/`gate_ledger` tables in `data/paper_trading_sbox.db`.
- The **standalone** `data/trade_custody.db` (95 rows) and `data/gate_ledger.db`
  (97 rows) are written by the **settled-admission boundary**
  (`core/booking_admission.record_custody_for_trade` / `admit_settled_trades`) and
  the 2026-10-10 reconcile driver — **not** by the live booking path.

There is therefore no "writers target different DBs" ambiguity: the two surfaces
have different writers by design (booking-time vs settlement-admission).

## Reproduce

```bash
python3 scripts/reconcile_custody_book_2026-10-15.py
```
