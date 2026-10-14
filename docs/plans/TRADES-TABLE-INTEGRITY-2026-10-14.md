# Trades Table Integrity Sweep — 2026-10-14

**Date:** 2026-10-14
**Lane:** Sprint — Lane B (technical)
**Owner:** Bertram Gilfoyle
**Repo:** `prototypes/weather-engine-source/`
**DB:** `data/paper_trading_sbox.db` (the live book)
**Backup:** `data/paper_trading_sbox.db.bak-20261014` (taken before any write)
**Tool:** `scripts/fix_trades_table_integrity_2026-10-14.py`
**Commits:** `5950d8d37` (P1 settlement) · `9f2b61a57` (P2 sizing) · `ab6ba94c2` (P3 P&L)

---

## 0. Bottom line

Three of the four problems were fixable in-repo and are now written. **Problem 1
could only be partially closed**, and the reason is a governance conflict, not a
data one: **the task's `>=` settlement premise is false in the repository**, and
two of the stale-open rows sit exactly on the contested tie rule that is
**PENDING ON DAN**. I settled the four rows whose outcome is rule-independent,
blocked the two exact ties, and did not fabricate the three rows with no truth.

| Problem | Status | Result |
|---|---|---|
| 1 — 9 stale-open rows | **PARTIAL** | 4 settled · 2 BLOCKED (exact tie, rule Dan-gated) · 3 no-truth (left open) |
| 2 — `position_size_usd` fiction | **DONE** | 88 rows re-sized from `calibrated_p`; old sizes preserved |
| 3 — size-fiction P&L | **DONE** | 83 rows → honest constant-1-contract; old P&L preserved |
| 4 — anomaly verification | **DONE** | 0 dup uuid · 0 closed-without-outcome · closed rows complete |

`PRAGMA integrity_check` = **ok**. `trades` = 88 rows (83 closed, 5 open).

---

## 1. Problem 1 — stale-open settlement

### 1a. The rule conflict (read this first)

The task said to settle under the **`>=` rule** ("`settle_yes` was fixed to
`>=`, fixed earlier"). **That fix does not exist in the repository.** Verified at
HEAD:

- `core/settlement_labels.py::strike_crossing_yes` — STRICT `>` ("HIGH:
  settlement > strike ; equality => False"; mtime 2026-09-28).
- `core/canonical_settlement.py::canonical_settle_yes` — STRICT `>`.
- `core/pnl_tracking.py:129` — `1.0 if settlement_temp > strike_price else 0.0`.
- `tests/test_settlement_axis.py`, `tests/test_strike_crossing_axis.py` — assert
  **"equality must be a LOSS everywhere"**.
- The prior B6 reconciler (`scripts/reconcile_stale_open_trades_2026-10-13.py`)
  uses strict `>`.
- **12 already-closed rows are exact ties, and every one is booked
  `settlement_outcome='incorrect'`** (e.g. 195 KPHX 96/96, 266 KBOS 60/60). The
  book's own history is strict-`>`.

`TASKS.md` records the tie rule as **"PENDING ON DAN"** and the prior run
(`docs/plans/STALE-OPEN-RECONCILE-2026-10-13-UPDATE.md`, commit `edb902fe6`)
already declined to write under `>=` for the same reason. Dan's recorded position
is the opposite of the shipped code (equality = WIN), but **no fix commit ever
landed**. Flipping the rule inside a subagent DB write would change 12 closed
rows and re-decide two open trades on an unratified rule.

**Decision: settle under the SHIPPED strict `>` axis; do NOT flip the tie rule.**

### 1b. What was written (4 rows)

Canonical truth = `data/kalshi_settlements.db` (`source='live_api'`,
`source_type='finalized'`). Outcome via `core.canonical_settlement.canonical_outcome`
(strict axis). `settlement_date_utc` via `core/station_time.settlement_time_utc`
(station-local midnight +1 day).

| id | station | ticker | settled °F | strike | strict `>` | outcome | realized_pnl |
|---:|---|---|---:|---:|---|---|---:|
| 269 | KNYC | KXHIGHNY-26OCT12-T69 | 70 | 69 | WIN | correct | **+0.895** |
| 271 | KDCA | KXHIGHTDC-26OCT13-T85 | 82 | 85 | loss | incorrect | **−0.025** |
| 274 | KOKC | KXHIGHTOKC-26OCT13-T94 | 91 | 94 | loss | incorrect | **−0.045** |
| 275 | KDEN | KXHIGHDEN-26OCT13-T62 | 66 | 62 | WIN | correct | **+0.965** |

Fields written per row: `settled_value`, `realized_pnl`,
`settlement_return_amount`, `settlement_confirmed=1`, `settlement_outcome`,
`canonical_settled_f`, `canonical_source='kalshi_live_api'`, `canonical_outcome`,
`status='closed'`, `settlement_date_utc`.

### 1c. Blocked — exact ties (2 rows) — NO WRITE

| id | station | settled °F | strike | strict `>` | Dan-`>=` |
|---:|---|---:|---:|---|---|
| 268 | KMSP | 81 | 81 | LOSS (−0.425) | WIN (+0.575) |
| 270 | KBOS | 71 | 71 | LOSS (−0.400) | WIN (+0.600) |

Left `open`. Mechanically settleable the moment Dan rules. Under `>=` the two
flip to WIN.

### 1d. No canonical truth (3 rows) — NO WRITE (not fabricated)

| id | station | trade_date | reason |
|---:|---|---|---|
| 272 | KLAX | 2026-10-13 | no `kalshi_settlements` row for station-day |
| 273 | KSFO | 2026-10-13 | no `kalshi_settlements` row for station-day |
| 276 | KSEA | 2026-10-13 | no `kalshi_settlements` row for station-day |

Left `open`; they settle on the next `kalshi_settlement_upkeep` run when truth
lands.

---

## 2. Problem 2 — `position_size_usd` sizing backfill

Old ladder gave 48 rows the dead `$25` tier. Backfilled from the honest ladder
(`scripts/sbox_intraday_ev_bridge.py::_position_size_usd`: `>0.70 → $10`,
`>0.50 → $5`, else `$0`) on `conf = |calibrated_p − 0.5| · 2`, `calibrated_p`
from the companion (`trade_forecaster_probs`, fallback `trade_calibration`).

All 88 rows have a companion `calibrated_p` (0 missing). Old sizes preserved in
new column `position_size_usd_old_2026`.

| | $0 | $5 | $10 | $25 |
|---|---:|---:|---:|---:|
| **before** | 0 | 29 | 11 | **48** |
| **after** | 8 | 70 | 10 | 0 |

**67 rows changed size, 21 unchanged.** The 8 rows now at `$0` are
non-directional (`calibrated_p < 0.50`): ids 189, 230, 235, 245, 271, 272, 273,
274. They are noted, not deleted; P&L/quantity handled in Problem 3.

---

## 3. Problem 3 — honest P&L (constant-1-contract)

`realized_pnl` reflected old contract counts (size fiction). Replaced for the
**83 settled rows** with honest constant-1-contract P&L under the strict axis:
`buy_yes: (S − trade_price)·1`, `buy_no: (trade_price − S)·1`, `S ∈ {0,1}`. Old
values preserved in new column `realized_pnl_old_2026`.

| metric | value |
|---|---:|
| `SUM(realized_pnl)` before | **18,871.65** |
| `SUM(realized_pnl_old_2026)` (preserved) | 18,871.65 |
| `SUM(realized_pnl)` after (honest) | **40.335** |

The 5 still-open rows keep `realized_pnl = NULL` (not settled, not fabricated).

### `quantity` — REPORT-ONLY, no rewrite (deliberate)

The task allowed a `quantity` cap "only if that's sane". It is **not**:

1. Capping to the new `position_size_usd` would drive **8 genuinely-traded rows
   to `qty=0`** (ids 189, 200, 203, 207, 214, 217, 230, 245) while their `status`
   stays `closed` — a contract-count/status contradiction.
2. `core/pnl_tracking.py::_canonical_fee` derives the effective price from
   `abs(trade_cost)/quantity`; rewriting `quantity` without `trade_cost` would
   silently corrupt fee recomputation.
3. The residual "cap" against the *old* size is only floor-vs-round rounding
   (`qty = round(psize/price)` vs `floor`, i.e. `old_qty − 1`) — noise, not a fix.

`quantity` and `realized_pnl` are therefore intentionally **inconsistent**, as the
task anticipated. Documented here; no write.

---

## 4. Problem 4 — anomaly verification (post-fix)

| check | result |
|---|---|
| duplicate `trade_uuid` | **0** |
| closed rows missing `settlement_outcome` | **0** |
| closed rows missing `realized_pnl` | **0** |
| closed rows missing `settled_value` | **0** |
| closed rows missing `canonical_settled_f` | **0** |
| `canonical_source` (closed) | `kalshi_live_api` ×75, `kalshi_finalized` ×8 |
| rows with companion `calibrated_p` | **88 / 88** |
| `PRAGMA integrity_check` | **ok** |

---

## 5. Deliverable & open items

- **Report:** this file.
- **Tool:** `scripts/fix_trades_table_integrity_2026-10-14.py`
  (`--problem {1,2,3} [--dry-run]`).
- **Artifacts:** `artifacts/trades-integrity/problem{1,2,3}_{dry,applied}.json`.
- **Backup:** `data/paper_trading_sbox.db.bak-20261014`.

**Needs Dan:**
1. **Tie rule** (268 KMSP 81/81, 270 KBOS 71/71) — PENDING ON DAN. Under the
   shipped strict `>` both are LOSS; under Dan's recorded `>=` both WIN. The
   mechanical settle is one tool run once ruled.
2. **3 rows awaiting canonical truth** (272/273/276) — settle on the next
   upkeep run.
3. **8 rows now at `$0` size** — non-directional; decide keep-as-recorded vs
   purge.
4. **`quantity` vs `position_size_usd` inconsistency** — accepted, documented;
   revisit only alongside a `trade_cost` rewrite.
