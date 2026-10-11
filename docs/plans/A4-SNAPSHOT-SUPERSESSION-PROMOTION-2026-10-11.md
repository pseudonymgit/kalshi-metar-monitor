# A4 Promotion — Settle Oct 9 From Finalized Canonical Truth

**Date:** 2026-10-11 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Ruling:** `.meta/continuity/weather-engine/governance-provenance-drift-disposition-2026-10-10.md` (A4)
**Predecessor:** `c8241e1a6` (A4 code) · forward-settle `FORWARD-SETTLE-CANONICAL-2026-10-10.md`
**Canonical source:** Kalshi finalized settlement (`data/kalshi_settlements.db`, `source_type='finalized'`)

---

## Problem

`data/metar_backfill.db::settlement_snapshot` froze 20 Oct-9 HIGH rows from the
`weatherapi` provisional source write-once (immutable; UPDATE/DELETE abort). The
A2 pass annotated them `provisional_source` (pending). Kalshi has since finalized
Oct 9 (KATL 73, KBOS 69, KSEA 58, KDFW 91, KMDW 74, KSFO 71, …), leaving **4 open
SBOX trades (KDFW, KMDW, KSEA, KSFO on 2026-10-09) unable to settle** — the settle
path only accepted canonical snapshot rows and correctly refused the provisional
ones.

## A4 delivered

| # | Change | File |
|---|---|---|
| 1 | Settlement-truth resolution order: canonical snapshot row → finalized `kalshi_settlements` row (`source_type='finalized'`) → **fail closed** | `core/canonical_settlement.py::resolve_settlement_truth`, `::resolve_finalized_settlement` |
| 2 | Settle writer uses that order; never books a provisional snapshot proxy | `core/pnl_tracking.py::process_settlements_for_date` |
| 3 | Annotate, never overwrite: `superseded_by_canonical` rows in an additive side table (`settlement_snapshot_supersession`), frozen values byte-identical | `scripts/promote_snapshot_supersession.py` |
| 4 | Regression tests: resolution order, fail-closed, supersession idempotence | `tests/test_snapshot_supersession_promotion.py` (8) |

The frozen snapshot rows are **never** UPDATE/DELETE-ed. The promotion records the
disposition in a side table keyed by the snapshot primary key — the same pattern
as A1/A2 (`settlement_snapshot_annotation`). The A2 `provisional_source`
annotation is retained; the two together read "pending" → "superseded by canonical".

## Live application (backed up to `data/_a4_backup_20261011T005429Z/`)

The 4 open Oct-9 trades, before → after:

| Station | trade_uuid | before | after | canonical_settled_f | canonical_source | canonical_outcome | realized_pnl |
|---|---|---|---|---|---|---|---|
| KDFW | bddcc7b9… | open, conf=0 | closed, conf=1 | 91.0 | kalshi_finalized | incorrect | −10.01 |
| KMDW | 2813eced… | open, conf=0 | closed, conf=1 | 74.0 | kalshi_finalized | correct | +8.10 |
| KSEA | c8e3dc6b… | open, conf=0 | closed, conf=1 | 58.0 | kalshi_finalized | incorrect | −49.98 |
| KSFO | 173b3fe4… | open, conf=0 | closed, conf=1 | 71.0 | kalshi_finalized | correct | +21.30 |

P&L = buy_yes payout `(1.0 − fill) × qty` (all 4 are `buy_yes`): KDFW
(1−0.055)·182, KMDW (1−0.55)·18, KSEA (1−0.06)·833, KSFO (1−0.70)·71.

## Verification

- **Frozen snapshot unchanged:** sha256 `ac2bf4fa…9e957` identical before/after
  (1400 rows). No frozen value moved.
- **Annotations:** 20 Oct-9 `superseded_by_canonical`; the 20 A2
  `provisional_source` rows retained.
- **No historical mutation:** cell-diff of all 74 trades vs the backup shows 40
  changed cells, **all on `2026-10-09` open rows**; every historical closed row
  byte-identical. No restatement.
- **Gate:** all 4 ADMITTED with full G0–G5 PASS (G3 PASS, G5 PASS — pinned 13:30
  local entry < Oct-10 finalization anchor).
- **Custody + ledger:** gate ledger 93→97 (+4, no dups); custody 91→95 (+4);
  `verify_chain` ok.
- **Idempotent:** re-running settle for 10-09/10-08/10-07 → `count=0` each; no
  column drift.
- **Tests:** `tests/` settle/canonical/gate/custody/booking/pnl/provenance/prereg
  surface — 216 passed, 1 skipped, 0 failed.

## Flags

1. **Forward population still manual** — nothing freezes new station-days into
   `settlement_snapshot` on a schedule. New trades will settle only once a
   finalized `kalshi_settlements` row exists (the new step-2 fallback), which is
   the intended forward path; the keep-fresh snapshot freeze remains a separate
   recommended change.
2. **Oct-8 drift rows (5)** are now also annotated `superseded_by_canonical`
   (finalized truth exists for them). No trade depends on them.
3. **LOW markets** fail closed (finalized table is HIGH-only). No LOW trades exist.

## Not done

No AI/ML. Deterministic. No live trading enabled. No historical P&L mutated.
