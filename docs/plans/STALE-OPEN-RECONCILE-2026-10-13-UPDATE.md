# Stale-open reconcile — remaining 3 rows (Item 2, 2026-10-13)

**Read-only.** No write to `trades`. Truth for `target_date=2026-10-12` HAS landed
(`data/kalshi_settlements.db`, `source='live_api'`, `source_type='finalized'`,
max target_date = 2026-10-12). The 3 remaining Oct-12 rows (ids 268/269/270) are
therefore reconcilable — **but two of the three are EXACT TIES, i.e. they sit on the
contested settlement rule. This is blocked pending Dan's rule call (see below).**

## Truth (canonical, live_api/finalized)

| id | station | ticker | settled | strike | relation | strict `>` | `>=` |
|---|---|---|---|---|---|---|---|
| 268 | KMSP | KXHIGHTMIN-26OCT12-T81 | 81.0 | 81.0 | **TIE** | LOSS (−5.10) | WIN (+6.90) |
| 269 | KNYC | KXHIGHNY-26OCT12-T69 | 70.0 | 69.0 | > | WIN (+213.01) | WIN (+213.01) |
| 270 | KBOS | KXHIGHTBOS-26OCT12-T71 | 71.0 | 71.0 | **TIE** | LOSS (−24.80) | WIN (+37.20) |

## The blocker — the settlement rule is NOT `>=` in the repo

The task states "`settle_yes` was fixed to `>=` (Workstream T/V)". **That fix is NOT
in the repository.** Verified at HEAD:

- `core/settlement_labels.py::strike_crossing_yes` — strict `>` ("HIGH: settlement >
  strike ; equality => False"), mtime 2026-09-28.
- `core/canonical_settlement.py::canonical_settle_yes` — strict `>`.
- `core/pnl_tracking.py:129` — `1.0 if settlement_temp > strike_price else 0.0`.
- `tests/test_settlement_axis.py`, `tests/test_strike_crossing_axis.py` — **assert
  equality is a LOSS** ("the core discriminator: equality must be a LOSS everywhere").
- The prior B6 reconciler (`scripts/reconcile_stale_open_trades_2026-10-13.py`) uses
  strict `>`.
- `PROJECTS.md`/`TASKS.md` (18:01 delta): "B6 tie-rule NOT durable
  (`strike_crossing_yes` mtime 2026-09-28, strict `>`); no fix commit". Owner-gated.

Dan's position (recorded by the owner, PROJECTS.md 10:01) is the opposite: *"Kalshi
temp-HIGH equality is a WIN (`high >= strike`)"*, with 12 exact-tie rows booked loss
(+$1,695 swing) as the pending correction — **owner-gated, never landed.**

So writing this item under "`>=`" would:
1. contradict the shipped engine + the test suite's explicit strict-`>` contract, and
2. flip 268/270 to WIN on an unratified rule change.

## Decision

**No write.** Reported only. The 3 rows remain `open`. When Dan rules on the tie
rule, the reconcile is mechanical (same fields as the earlier 11-row write:
`settled_value`, `settlement_date_utc`, `realized_pnl`, `settlement_return_amount`,
`canonical_settled_f`, `canonical_source='kalshi_live_api'`, `canonical_outcome`,
`settlement_confirmed=1`, `status='closed'`). Artifact (prepared, strict-`>` axis):
`artifacts/stale-open-reconcile/stale_open_reconcile_2026-10-13.json`.

## ids 271–274 (Oct-13) — correctly NOT reconciled

`max(target_date)=2026-10-12`; no 2026-10-13 settlement rows exist. Ids 271 (KDCA),
272 (KLAX), 273 (KSFO), 274 (KOKC) are correctly still `open` — not stale.
