# Wire the Measurement Substrate into the Live Booking Path

**Date:** 2026-10-10 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Predecessor:** `94ba9a099` (E2, suite green + CI) · **Spec:** `docs/plans/GRAY-ROOM-5-SYNTHESIS-2026-10-09.md` §1/§2/§6
**Scope:** call the built substrate from the live path — pre-ingest gate (G0–G5),
TradeCustodyRecord writer, pre-registration epoch. No live trading enabled; no
substrate module rewritten; no historical P&L mutated.

---

## Why

The substrate (`scripts/gate_composer.py`, `core/trade_custody.py`,
`core/preregistration.py`) was built, tested, and green — but **nothing in the
live path called it** (GR5-D receipt blocker #3; GR1). This change makes the gate
the thing that decides admission and gives every accepted trade a custody row.

## What landed

| # | Change | File |
|---|---|---|
| 1 | New composition module: pre-registration epoch + gate + TCR writer, and the settled-admission boundary | `core/booking_admission.py` (new) |
| 2 | Booking-path wiring: gate before `record_trade`; custody row after; `trade_uuid` returned | `scripts/sbox_intraday_ev_bridge.py` |
| 3 | Regression tests (13) | `tests/test_booking_wiring.py` (new) |

### `core/booking_admission.py`
- `pinned_entry_time_utc(station, date)` — the **real H3 pin** (13:30 station-local
  → UTC; KNYC 2026-10-04 = 17:30Z, KSFO = 20:30Z). Fixes the 13:00-default
  provenance failure the G5 ruling records.
- `admit_decision_candidate(...)` — booking-time gate: decidable subset (G0
  window-green · G1 identity/version · G4 axis); G2/G3/G5 recorded **DEFERRED**
  with reason. Verdict appended to the append-only gate ledger.
- `record_custody(...)` / `record_custody_for_trade(...)` — one TradeCustodyRecord
  row per accepted trade, idempotent, chain-verifying.
- `admit_settled_trades(...)` — **admission boundary**: full composed G0–G5 over
  settled rows, every verdict logged, custody written for each admit; refuses on a
  pre-registration hash mismatch.
- `candidate_from_trades_row(...)` — builds the candidate from a real `trades` row
  with the real pin + real `settlement_snapshot.settled_at_utc` anchor.
- `verify_run_relies_on_prereg(...)` — delegates to `require_preregistration`.

### `scripts/sbox_intraday_ev_bridge.py`
- Gate runs before booking (a REJECTED/HALT candidate is **not booked**; skip reason
  `booking_gate_<VERDICT>`, ledger row written).
- `record_custody(...)` runs after a successful book; `record_trade` now returns
  `trade_uuid` so the writer chains off the real row.
- Guarded import + `WE_BOOKING_GATE=0` off-switch; a substrate failure can never
  break the bridge's other jobs. **Dry-run writes nothing** (the block is inside
  the `else:` record branch).

## Honest coupling disclosure (the reason the gate is not a naive per-book filter)

The gate's blocking set is {G0..G5}. **G2, G3 and G5 are not decidable at booking
time on this codebase**:

- **G3** needs `settlement_confirmed=1`; the book records an open row
  (`settlement_confirmed=0`) at decision time and fills settlement later. A fresh
  candidate fails G3 *by construction* — reproduced: a settled-state candidate with
  `settlement_confirmed=0` yields `REJECTED (G3)`.
- **G2** needs a stored canonical fee; `record_trade` books `trade_cost = qty*price`
  (execution cost), not `kalshi_fee` — the R2/E1 "fee unwired" finding.
- **G5** needs `finalization_utc` (the frozen snapshot anchor), which exists only
  after settlement.

G0–G5 is therefore an **admission filter on SETTLED candidates** — it binds at the
measurement set / N=640 cohort boundary, not at order write. The live bridge is
wired for the decidable subset (G0/G1/G4) + TCR at booking; the full gate runs at
admission. This was surfaced, not hidden — the substrate is measure-quality, not a
live-order risk stop.

## Verification (evidence)

| Claim | Evidence |
|---|---|
| Rejected candidate **not booked**, logged REJECTED with failing check | `test_booking_gate_rejects_wrong_axis_and_logs_failing_check` → `gate_ledger = ('REJECTED','G4')` |
| Version mismatch **HALT**, logged G1 | `test_booking_gate_halts_on_version_mismatch` |
| Full gate rejects lookahead | `test_full_gate_rejects_lookahead_and_raises` → `failed_gate='G5'` |
| Clean candidate **booked AND has a custody row** | `test_booked_trade_gets_custody_row_via_wiring` |
| Two same-day trades get **distinct** custody rows | `test_two_trades_same_day_get_distinct_custody_rows` |
| TCR idempotent + chain verifies, 48 columns | `test_record_custody_one_row_idempotent_and_chain_ok` |
| Real 13:30 pin (never 13:00) | `test_pinned_entry_is_1330_station_local` |
| Prereg epoch verifies | `test_prereg_verifies_for_governed_experiment` |
| **Live bridge proof**: 2 trades booked, each with a custody hash | `.venv-gefs/bin/python scripts/_sbox_bridge_proof.py` |

- `verify_gate.py` exit **0**; `gate_composer.py` exit **0**; `preregistration verify` exit **0**;
  `substrate_gate.py` exit 4 (PROVISIONAL, pre-existing).
- Full suite: **1499 passed, 16 skipped, 6 xfailed, 0 failed** (was 1486 passed; +13).
- **No historical P&L change**: re-running `process_settlements_for_date` for
  2026-10-09/08/07 on a copy left the 33-row fingerprint **byte-identical**
  (`34b5371e…`, P&L $2591.33).

## Defect found and fixed while wiring

`trades` has **no `market_ticker` column**, so `record_from_trade`'s idempotency
key fell back to the literal `"None"`; two same-day trades collapsed onto ONE
custody row (observed: KMSP + KPHX → 1 row, identical hash). Fixed in
`record_custody` by falling back to `trade_uuid`, with an `IntegrityError`
backstop; locked by `test_two_trades_same_day_get_distinct_custody_rows`.

## Book hygiene (disclosed)

`scripts/_sbox_bridge_proof.py` is a **pre-existing live diagnostic** that calls
`run(dry_run=False)` and writes the real SBOX book — it was exercised to prove the
wiring and booked 2 open 2026-09-29 trades + created `trade_custody`/`gate_ledger`
in `data/paper_trading_sbox.db`. **Restored**: the 2 proof rows removed and the two
proof-created tables dropped; the 33-row fingerprint is now identical to pre-proof
(`34b5371e…`, P&L $2591.33). The errant write is flagged here, not hidden.

## Findings carried forward (not this change)

1. **SBOX settled rows all fail G3** (29/29 in a real run): the book stores
   `settled_value` as the settlement **temperature** (°F), while the frozen G3
   contract expects the binary `{0,1}`. This is the documented booked-vs-canonical
   divergence (GR5-D finding #1) — a settlement-truth/data-contract item, **not**
   mutated here. Until reconciled, the settled-admission path correctly refuses to
   admit any current row.
2. **G2 fee** remains unwired at `record_trade` (booked `trade_cost` is execution
   cost, not `kalshi_fee`) — the R2/E1 item; G2 is DEFERRED at booking and enforced
   with the canonical recompute at admission.
3. **TCR in the live book.** Rows are written to `data/trade_custody.db` (default)
   or, in the bridge, the SBOX book; merge/environment policy is Dan's call.

## Not done

- No AI/ML. Deterministic. No live trading enabled (bridge stays a paper book; the
  cron already runs `dry_run=False` by design — unchanged). No substrate module
  rewritten. No historical P&L mutated. G3/G5 semantics unchanged.
