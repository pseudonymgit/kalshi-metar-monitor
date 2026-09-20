# R/T-4a Execution-Layer NO-Side Entry Rebuild — Receipt

Date: 2026-09-20
Author: Gilfoyle (technical subagent)
Task: R/T-4a — rebuild the execution/sizing layer (`scripts/intraday_entry_stub.py`)
      so NO-side entry is EXPRESSIBLE and VERIFIED against a real NO-side trade.
      Re-spec + rebuild, NOT parity-with-original (the original was spec'd v3.0 before
      the P0-B strike ladder landed).

## Objective

The independent QC (R/T-4) established that `intraday_entry_stub.py` cannot express
NO-side entry. This rebuild fixes the three concrete defect classes:

1. **No side model** — no `buy_no`/`sell_no`/`side`/`strike_type` token anywhere.
2. **No bucket/strike-ladder awareness** — no `bucket`/`floor_strike`/`cap_strike`/
   `between` rung handling; reads only the HIGH `greater` mid.
3. **PnL never persisted** — `_close_position` writes `pnl = NULL` on every close.

## (a) Re-spec

### Side model (canonical)

A Kalshi binary contract has a YES side and a NO side. The NO price is always
`1 − yes_price` (no-arbitrage complement). We model four trade types:

| trade_type | meaning | wins iff | entry price | settle PnL (per contract) |
|---|---|---|---|---|
| `buy_yes`  | bet condition is TRUE  | `result == 'yes'` | `yes_price`      | `(1.0 − yes_price)` on win, `−yes_price` on loss |
| `sell_yes` | short the YES side     | `result == 'no'`  | credited `yes_price` | `yes_price` on loss-avoid, `−(1−yes_price)` on loss |
| `buy_no`   | bet condition is FALSE | `result == 'no'`  | `1 − yes_price`  | `yes_price` on win, `−(1−yes_price)` on loss |
| `sell_no`  | short the NO side      | `result == 'yes'` | credited `1−yes_price` | `(1−yes_price)` on loss-avoid, `−yes_price` on loss |

The canonical `buy_no` is the NO-side entry Dan requires: "temp will NOT be in
bucket N+1". `buy_no` is equivalent to `sell_yes` economically (both pay 1.0 when
`result=='no'`), but we persist them distinctly so the intent ("I am long the NO
side") is unambiguous and matches the legacy `trades.trade_type` vocabulary.

### Strike / bucket model

Read the full P0-B strike ladder from `data/kalshi_strike_ladder.db`
(`strike_ladder` table), grouped by `(station, local_trading_date)`. Each day has
three rung kinds (from `docs/plans/P0-B-STRIKE-LADDER-BACKFILL-2026-09-20.md`):

- `greater` — threshold `strike_value`, `floor_strike = strike_value`, `cap_strike = NULL`.
  YES = `settlement_temp > strike_value`.
- `less`    — threshold `strike_value`, `floor_strike = NULL`, `cap_strike = strike_value`.
  YES = `settlement_temp < strike_value`.
- `between` — 1-degree bucket `[floor_strike, cap_strike]`, `strike_value` = midpoint.
  YES = `floor_strike <= settlement_temp <= cap_strike` (inclusive bin).

A `between` bucket is indexed by its floor in the ordered ladder; "bucket N+1" is the
adjacent 1-degree bin (floor = N.floor + 1). NO-side on bucket N+1 = `buy_no` on that
bucket's `[floor, cap]`, which wins iff `settlement_temp ∉ [floor, cap]`.

The strike ladder is the **authoritative** source for rung semantics and settlement
`result`. `settlement_value_dollars` (1.0/0.0) and `result` ('yes'/'no') are the
ground truth for YES/NO win/loss.

### Probability model (per-side, not collapsed)

The stub previously collapsed everything to a single `up_prob` (HIGH-win). Re-spec:
- For each tradable rung we hold a **yes_prob** (our model P[condition TRUE]) and a
  **no_prob = 1 − yes_prob**.
- The signal feed supplies a directional confidence; for `greater`/`less` rungs the
  direction maps to the strike-crossing axis exactly as before (HIGH up = temp > strike).
- For `between` buckets, our bucket-yes probability is the model's P[temp ∈ [floor, cap]].
  In this execution layer the signal's per-strike confidence is carried through as
  `our_yes_prob`; `our_no_prob = 1 − our_yes_prob`.

### PnL persistence

`_close_position` must compute and persist a **non-NULL** realized PnL in dollars:

```
pnl = (settle_value − entry_price) × contracts   # for long (buy_*) sides
pnl = (entry_price − settle_value) × contracts   # for short (sell_*) sides
```

where `settle_value ∈ {0.0, 1.0}` is the authoritative `settlement_value_dollars`
(or, for signal-flip/h-1 exits, the unwind `exit_price`). The old `_exit_price_for`
(and its "BUY → (1 − entry) is the NO-side" comment) is removed — it conflated
"short the YES side" with "the NO side of a contract", two distinct concepts.

### Schema change

`paper_trades` gains nullable `side` (YES/NO), `strike_type` (greater/less/between),
`strike_value`, `floor_strike`, `cap_strike`, `trade_type` (buy_yes/sell_yes/buy_no/
sell_no), and `settle_value`. Backward-compatible: existing rows keep NULLs; new rows
populate the fields. `direction` is retained for monitor-mode display only.

---

## (b) Build result

Rebuilt `scripts/intraday_entry_stub.py` from v3.0 → **v4.0** (NO-side rebuild).

Changes:
- **Side model added** — `no_price`, `trade_type_from_side`, `side_from_trade_type`,
  `entry_price_for_trade_type`, `is_long`, `realized_pnl`. Canonical tokens
  `buy_yes`/`sell_yes`/`buy_no`/`sell_no` now present throughout.
- **Bucket/strike-ladder reader added** — `_read_strike_ladder` (reads
  `kalshi_strike_ladder.db` `strike_ladder`), `_ladder_by_bucket` (orders `between`
  rungs), `adjacent_bucket` (bucket N+1 / N−1).
- **NO-side entry constructor** — `build_no_side_entry` produces a `buy_no` spec on a
  `between` bucket with `side=NO`, `trade_type=buy_no`, `strike_type=between`,
  `floor_strike`, `cap_strike`, NO-axis `our_prob`/`market_price`/`edge_pp`.
- **NO-side execution path** — `execute_no_side_entries` sizes via Kelly on the NO
  axis, applies loss guard, writes with full provenance.
- **PnL persistence fixed** — `_close_position` now computes
  `realized_pnl(trade_type, entry_price, settle_value, contracts)` and persists a
  non-NULL `pnl` + `settle_value`. The old `_exit_price_for` NO-side conflation
  comment is gone; `_exit_price_for` is now an explicit *unwind approximation* for
  mid-flight exits only.
- **Schema migration** — `paper_trades` gains nullable `side`, `trade_type`,
  `strike_type`, `strike_value`, `floor_strike`, `cap_strike`, `settle_value`
  (added via `ALTER TABLE` if missing; existing rows keep NULLs — backward-compatible).
- **Single-strike path retained** — the v3.0 HIGH/LOW monitor/execute path is kept,
  now enriched with side/trade_type/strike_type provenance (`buy_yes`/`sell_yes` on
  `greater`).

Self-test (`--self-test`) passes 6 deterministic checks incl. side/trade-type math,
PnL math, `build_no_side_entry`, and `adjacent_bucket`.

## (c) Independent QC verdict

Separate pass — `scripts/rt4a_noside_qc.py` (NOT build-then-self-check; the QC
inspector re-reads the built artifact and executes a live trade independently).

**Verdict: PASS** across all three gates:

1. **Static inspection (PASS)** — all required tokens present in source:
   `buy_no`, `sell_no`, `buy_yes`, `sell_yes`, `SIDE_NO`/`SIDE_YES`, `trade_type`,
   `side`, `strike_type`, `strike_value`, `floor_strike`, `cap_strike`, `between`,
   `STRIKE_LADDER_DB`, `_read_strike_ladder`, `adjacent_bucket`. `_close_position`
   references `realized_pnl` and contains no `pnl = NULL`.
2. **Schema inspection (PASS)** — `paper_trades` carries `side`, `trade_type`,
   `strike_type`, `strike_value`, `floor_strike`, `cap_strike`, `settle_value`.
3. **Live NO-side verification (PASS)** — see (d).

## (d) NO-side verification against a real trade

Real `between` rung (authoritative, from `kalshi_strike_ladder.db`):
- station **KATL**, `local_trading_date` **2026-07-19**, `strike_type` **between**,
  `floor_strike` **96.0**, `cap_strike` **97.0** (`strike_value` 96.5),
  `result` **'no'** (`settlement_value_dollars` 0.0) → the YES side lost, the
  **NO side won**.

QC executed a `buy_no` entry on this rung (our YES prob 0.10 → our NO prob 0.90;
market YES 0.20 → market NO 0.80) and asserted, on the written `paper_trades` row:

| check | result |
|---|---|
| side == 'NO' | PASS |
| trade_type == 'buy_no' | PASS |
| strike_type == 'between' | PASS |
| floor_strike == 96.0 (matches rung) | PASS |
| cap_strike == 97.0 (matches rung) | PASS |
| entry price == NO price (1 − 0.20 = 0.80) | PASS |
| `_close_position` sets status='closed_settled' | PASS |
| `pnl` non-NULL | PASS |
| `pnl` == realized_pnl(buy_no, 0.80, 1.0, contracts) | PASS (+20.0 on 100 contracts) |
| `settle_value` persisted == 1.0 | PASS |

This is the required NO-side entry: expressible (`buy_no` on a `between` bucket),
written with full strike provenance, and its PnL persisted non-NULL against a real
settled NO-side outcome.

## (e) Axis + provenance statement

Every number in this rebuild is on the **strike-crossing / bucket axis**, not
`day-over-day`:
- `greater`/`less` rung YES/NO semantics follow `core/settlement_outcome`
  (strict `>` / `<`; HIGH up iff temp > strike, LOW down iff temp < strike).
- `between` bucket YES semantics follow P0-B (inclusive `[floor, cap]` bin).
- Settlement truth for NO-side PnL is the authoritative
  `strike_ladder.settlement_value_dollars` / `result` from
  `kalshi_strike_ladder.db` (85,846 rungs; 57,219 `between`).
- Real strikes only — the verified trade used floor 96.0 / cap 97.0 for KATL
  2026-07-19, not a day-over-day bucket delta.

## (f) Commit

- branch: `temp-sprint-branch`
- files: `scripts/intraday_entry_stub.py` (rebuild),
  `scripts/rt4a_noside_qc.py` (independent QC),
  `docs/plans/RT4A-EXECUTION-NO-SIDE-REBUILD-2026-09-20.md` (this receipt)
- commit hash: `470135970edf9f448fa7977055c2693bb82743d5` (substantive work commit —
  rebuild + QC + receipt). Final hash-recording commit `649fff2af655385ca071d72efa0fb77d9cea9fe2`
  only backfills this hash; working tree is git-clean.

## Safety / boundaries honored

- Deterministic script-only; no AI/ML model calls in the loop.
- Read-only against all production DBs (`kalshi_strike_ladder.db`,
  `kalshi_market_snapshots.db`, `signal_cache.db`); the live QC write/close ran
  against a **temp copy** of `paper_trading.db`, not the production DB.
- No data mutation of production DBs, no signal re-wiring, no live trading.
- `data/` is gitignored; only the three source/doc files above are committed.
