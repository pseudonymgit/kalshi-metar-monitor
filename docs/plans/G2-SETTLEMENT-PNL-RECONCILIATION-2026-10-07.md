# G2 — Settlement P&L Reconciliation (11 flagged rows)

**Sprint G** · **Item:** G2 (P1) · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/reconcile_settlement_pnl.py` (new) · **Status:** DONE (annotate-only; P&L unchanged)

---

## What was asked

Gilfoyle flagged 19 settled trades where P&L may contradict the settlement bucket/label. For
each settled trade, compare `realized_pnl` against `settlement_epochs.settlement_bucket`; flag
any row whose P&L sign contradicts the settlement. Fix `core/pnl_tracking.py` bugs if found.
If the discrepancy is **stale data**, document it and **do NOT retroactively change closed P&L**.

## Method

Strict strike-crossing axis. Strike parsed from the trade's own executed notes (`@ <X>F`).
Expected P&L checked against **two** settlement-label sources separately:

- **Check A (epoch):** `metar_backfill.db::settlement_epochs.settlement_bucket` (the order's reference)
- **Check B (truth):** `kalshi_settlements.kalshi_temp` (source `live_api`, `finalized`; 6 markets), else `ghcn_settlements.high_f`

`buy_yes`: exp = (payout − trade_price)·qty; `buy_no`: exp = ((1−payout) − (1−trade_price))·qty.

## Result

| | value |
|---|---|
| Settled trades | 21 |
| Clean (non-flagged) trades | 22 |
| **Check A — epoch contradictions (clean)** | **7** → ids 150, 151, 154, 161, 162, 171, 173 |
| Check A Σ P&L delta if corrected | +$3,063 |
| **Check B — truth contradictions (clean)** | **10** → 138, 141, 146, 150, 151, 156, 161, 162, 171, 173 |
| Check B Σ P&L delta if corrected | +$3,341 |
| Rows contradicting *either* check | 11 |
| Epoch vs truth label disagreements | 15 station-days |
| Trade `settled_value` matches epoch | 11/21 |
| Trade `settled_value` matches truth | **1/19** |

## Root cause — this is NOT a `pnl_tracking.py` arithmetic bug

`core/pnl_tracking.process_settlements_for_date` books P&L **internally consistently** from the
`settled_value` it read at settle time. The booked P&L matches each trade's own `settled_value`
(the trade's embedded settlement temp). The discrepancies are an **input-label problem**:

1. **`settlement_epochs` was re-derived 2026-10-07T04:00Z** (`created_at_utc`), i.e. **after**
   these trades settled (2026-10-03 … 10-06). The current epoch bucket no longer equals the temp
   the trade settled on.
2. **The epoch disagrees with independent truth** on 15 of the 10-02..10-05 station-days, and
   Kalshi truth is not yet published for 10-03..10-06 (`kalshi_settlements` is NULL there).
3. The trade's own `settled_value` **tracks the (old) epoch, not independent truth** (1/19 vs truth).

Worked examples:

| id | station | date | strike | epoch | truth | booked P&L | exp(epoch) | exp(truth) |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 150 | KMSP | 10-03 | 64 | 70 | 71 | −10.07 | +22.94 | +33.00 |
| 161 | KBOS | 10-04 | 61 | 63 | 63 | −50.00 | +1950.00 | +1950.00 |
| 171 | KLAX | 10-05 | 97 | 100 | 101 | −9.98 | +85.03 | +85.03 |
| 146 | KDCA | 10-03 | 69 | 73 | 67 | +11.78 | −50.22 | −50.22 |
| 141 | KDCA | 10-02 | 85 | 84 | 86 | −10.00 | −10.00 | +390.00 |

KBOS 10-04 is the sharpest: strike 61, both epoch (63) and truth (63) say the high breached the
strike → the buy_yes **won** → expected +$1,950, but it is booked **−$50** (settled_value = 57,
which is neither the epoch nor the truth — a bad settle in the writer at settle time).

## Action taken — ANNOTATE ONLY (per order)

Closed P&L **not changed** (verified: `realized_pnl`/`status` byte-identical before/after
annotation). 11 rows annotated:

- `reconciliation_log`: `check_name='settlement_pnl_contradiction'`, `verdict='stale_epoch_recompute'`, detail + evidence.
- `trades.notes`: appended `[RECON 2026-10-07] …` tag (idempotent).

## Escalation (needs Gilfoyle / Dan)

The reconciliation cannot fully adjudicate KBOS 10-04 without two fixes upstream:
1. **`settlement_epochs` rebuild reproducibility** — the table is wiped/re-derived (same pattern as
   the earlier settlement_truth rebuild) *after* trades settle, so the live reader disagrees with
   itself across time.
2. **`process_settlements_for_date` input validation** — it accepted a `settled_value` (57) that
   matched neither truth (63) nor the prior epoch. The eventual true settlement for 10-04..10-06
   is unverified pending Kalshi finalization.

**Do not repair** until the epoch regeneration path is fixed, or the same rows will re-break.

## Files

| path | change |
|---|---|
| `scripts/reconcile_settlement_pnl.py` | new — two-source reconciliation + annotate mode |
| `data/paper_trading_sbox.db` | 11 rows annotated (reconciliation_log + notes); no P&L change |
