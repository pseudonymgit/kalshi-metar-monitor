# Forward Settle Path — Read Canonical Truth at Settlement Time

**Date:** 2026-10-10 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Predecessor:** `94e6603bc` (settlement-truth re-source — read path + invariant)
**Ruling:** `.meta/continuity/weather-engine/settlement-truth-reconciliation-2026-10-10.md` (Donna Paulsen)
**Canonical source:** Kalshi live API `expiration_value` (GHCN cross-check), frozen in `settlement_snapshot`.

---

## Why

`94e6603bc` fixed the **read** path: old trades now carry canonical truth in
`trades.canonical_settled_f`. But the **write** path still booked `settled_value`
from the epoch `settlement_bucket` — the unreliable settle-time partial-METAR
extreme the ruling flagged as primary fault #1. New trades would keep settling
wrong; only old ones were corrected. This change fixes the writer.

## The fix

| # | Change | File |
|---|---|---|
| 1 | Settle writer reads canonical truth from the frozen `settlement_snapshot` (Kalshi/ GHCN) instead of the epoch `settlement_bucket` | `core/pnl_tracking.py::process_settlements_for_date` |
| 2 | Writes `canonical_settled_f` + `canonical_source` + `canonical_outcome` on **every** new settlement | `core/pnl_tracking.py` |
| 3 | Booked `settled_value` heading forward **is** canonical truth; `realized_pnl`/strike/payout mechanics unchanged | `core/pnl_tracking.py` |
| 4 | Fail closed: no frozen canonical row for a station-day ⇒ trade stays **OPEN**, never booked from the raw/epoch value | `core/pnl_tracking.py` |
| 5 | Immutability assertion at settle time: a fetch that would move an existing canonical value raises `ImmutableSettlementError` (**fail loud**) and the trade is not written | `core/pnl_tracking.py` + `core/canonical_settlement.py::assert_canonical_immutable` |
| 6 | New helpers: `load_canonical_snapshots`, `resolve_canonical_settlement`, `assert_canonical_immutable` | `core/canonical_settlement.py` |
| 7 | Idempotent column migration `_ensure_canonical_trade_columns` (+ folded into `_migrate_settlement_columns`) | `core/pnl_tracking.py` |
| 8 | Outcome single-sourced from the **strict strike-crossing** axis (`canonical_outcome`); removed the redundant UP/DOWN-direction derivation so label and outcome cannot diverge | `core/pnl_tracking.py` |
| 9 | Regression tests (6) | `tests/test_forward_settle_canonical.py` |

**Not mutated:** historical `settled_value` / `realized_pnl` / `status` of the 29
already-closed rows — untouched. No in-place restatement of historical P&L.

## Verification

### New-trade settle (end-to-end, on a copy of `paper_trading_sbox.db`)

The 4 open Oct-9 trades settled via the patched path:

| id | st | date | strike | qty | fill | settled_value | canonical_settled_f | source | outcome | realized_pnl |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|
| 187 | KSEA | 10-09 | 59 | 833 | 0.06 | **59.72** | **59.72** | weatherapi | correct | +783.02 |
| 188 | KMDW | 10-09 | 71 | 18 | 0.55 | **74.66** | **74.66** | weatherapi | correct | +8.10 |
| 189 | KDFW | 10-09 | 93 | 182 | 0.055 | **89.42** | **89.42** | weatherapi | incorrect | −10.01 |
| 190 | KSFO | 10-09 | 70 | 71 | 0.70 | **64.94** | **64.94** | weatherapi | incorrect | −49.70 |

`canonical_settled_f` is populated on every new settlement (not just
`realized_pnl`). The booked value going forward equals canonical truth: after the
run, `settled_value == canonical_settled_f` on **33/33** closed rows
(`booked != canonical` = 0).

### Re-run a settled date → no change (invariant holds)

Re-running `process_settlements_for_date` for `2026-10-09`, `10-08`, `10-07`:
`settlements_processed count=0` each; **zero column diffs** across all 33 rows;
row count 33 → 33. Nothing moved.

### Fail-loud invariant

- A settlement that would move a pre-existing canonical value raises
  `ImmutableSettlementError` and the trade is left open with the value unchanged
  (`test_moving_existing_canonical_value_fails_loud`).
- No frozen canonical row ⇒ trade stays open, `canonical_settled_f` NULL
  (`test_no_canonical_row_leaves_trade_open`).
- `settlement_snapshot` UPDATE/DELETE still abort (test-locked).

### Tests

- `tests/test_forward_settle_canonical.py` — **6 pass**.
- Settlement/pnl/canonical/gate surface: **101 pass, 1 skip** (no regressions).

## Flags

1. **Forward-population dependency.** The writer now *requires* a frozen
   `settlement_snapshot` row per station-day. `settlement_snapshot` was populated
   through Oct 9 by the one-time backfill; **nothing currently populates it
   forward**. Until a daily keep-fresh step freezes new days (Kalshi finalizes a
   HIGH market T+1 ~05:00Z), new trades will correctly stay OPEN and will not
   settle. Candidate: extend `scripts/kalshi_settlement_upkeep.py` (08:00Z daily)
   to also freeze the snapshot. Recommended as its own change.
2. **LOW markets.** The snapshot is HIGH-only; the settle writer keys on
   `(station, market_type, date)`, so a LOW trade would fail closed (stay open)
   rather than mis-settle. No LOW trades exist in the book. Symmetric LOW
   population is out of scope here.
3. **Oct 9 source.** Canonical for Oct 9 is `weatherapi` (Kalshi high market not
   yet finalized on the API) and is frozen write-once. A later Kalshi
   finalization mismatch surfaces via the invariant rather than silently
   overwriting. `kalshi_settlements.db` lags (max 10-07); `ghcn_settlements.db`
   lags (max 10-06) — the writer reads the frozen snapshot, not those tables, so
   the lag does not affect settle correctness.
4. **G5 unchanged** — same-day `settlement_date_utc` still trips the as-of gate;
   orthogonal to settlement truth.

## Not done

- No AI/ML. Deterministic. No live trading enabled. G3/G5 unchanged. No
  historical P&L mutated. The live `data/paper_trading_sbox.db` was not written
  by this verification (ran against a copy).
