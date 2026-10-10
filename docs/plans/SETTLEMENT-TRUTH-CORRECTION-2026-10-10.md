# Settlement-Truth Correction — Canonical Re-Source + Deterministic Epochs

**Date:** 2026-10-10 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Ruling:** `.meta/continuity/weather-engine/settlement-truth-reconciliation-2026-10-10.md` (Donna Paulsen)
**Canonical source:** Kalshi live API `expiration_value` (api.elections.kalshi.com/trade-api/v2), GHCN cross-check.
**Constraint honored:** no in-place mutation of historical P&L; G3 not weakened; surgical.

---

## What was done

| # | Ruling step | Delivered |
|---|---|---|
| 1 | Re-source canonical truth into a separate column | `trades.canonical_settled_f` (+ `canonical_source`, `canonical_outcome`) — historical `settled_value` untouched |
| 2 | Repopulate confirmation fields on the strike-crossing axis | `settlement_confirmed=1` + `settlement_outcome` for all 29 closed rows, from canonical truth, strict HIGH: temp > strike |
| 3 | Deterministic, as-of-anchored epoch regeneration | `settlement_snapshot` (write-once, immutable, `settled_at_utc` anchor) + invariant enforced in the epoch generator **fail-closed** |
| 4 | Backfill `settlement_truth` Oct 1 → present | no rows after 2026-09-19 → now 180 rows (Oct 1–9, all stations) |
| 5 | Invariant: settlement immutable across epoch regeneration | `core/canonical_settlement.py` + `tests/test_canonical_settlement.py` (15 tests) |

### New / changed files

| path | change |
|---|---|
| `core/canonical_settlement.py` | **new** — canonical label + frozen snapshot + immutability invariant |
| `scripts/backfill_canonical_settlement.py` | **new** — dry-run-by-default re-source + repair |
| `scripts/generate_settlement_epochs.py` | `insert_epochs()` now refuses (fail-closed) to move a frozen value |
| `scripts/regenerate_settlement_epochs.py` | pre-write invariant check on the rewrite path |
| `tests/test_canonical_settlement.py` | **new** — 15 tests |

## Verification

**Canonical map:** 180 station-days (20 stations × Oct 1–9) frozen with source provenance.

**Per-station before/after — the 10 flipped trades** (booked outcome derived from the booked `settled_value`, since `settlement_outcome` was NULL on every row):

| id | st | date | strike | booked | canonical | booked→canonical outcome |
|---|---|---:|---:|---:|---:|---|
| 138 | KSAT | 10-02 | 79 | 79 | **80** | incorrect → **correct** |
| 141 | KDCA | 10-02 | 85 | 84 | **86** | incorrect → **correct** |
| 146 | KDCA | 10-03 | 69 | 73 | **67** | correct → **incorrect** |
| 150 | KMSP | 10-03 | 64 | 57 | **71** | incorrect → **correct** |
| 151 | KATL | 10-03 | 82 | 79 | **86** | incorrect → **correct** |
| 156 | KSEA | 10-03 | 66 | 66 | **67** | incorrect → **correct** |
| 161 | KBOS | 10-04 | 61 | 57 | **63** | incorrect → **correct** |
| 162 | KMSY | 10-04 | 80 | 77 | **82** | incorrect → **correct** |
| 171 | KLAX | 10-05 | 97 | 88 | **101** | incorrect → **correct** |
| 173 | KMSP | 10-05 | 66 | 61 | **68** | incorrect → **correct** |

Matches the ruling's `outcome_flips = 10` exactly. `booked != canonical` = **27/29** (ruling said 23/29 for >0.6°F; at the ±0.5°F rounding unit it is 27 — the two extra are single-degree rounding-boundary rows, e.g. KHOU 10-08 booked 91 vs canonical 92).

**G3 now ADMITS corrected rows:** before correction, `settlement_confirmed=0` → G3 REJECT on **29/29**. After: G3 **PASS on 29/29**; a corrected row with `settlement_date` set to the T+1 finalization day gives a full **ADMITTED** verdict (all six gates pass). G3 itself is unchanged — `settlement_confirmed=0` still rejects (test `test_g3_still_rejects_uncorrected_row`).

> Note: presented with the raw same-day `settlement_date_utc`, the composer returns REJECTED at **G5** (as-of), not G3 — the trade's entry timestamp is same-day vs the local trading day. That is G5 working correctly (same-day lookahead), orthogonal to settlement truth, and outside this fix's scope per the ruling.

**P&L integrity:** recomputing `realized_pnl` from each row's *booked* `settled_value` reproduces the stored value on **29/29** rows → the booked value ↔ P&L pairing is intact and untouched. Historical `realized_pnl` / `settled_value` / `status` were never written by this change.

## Invariant

> A settlement value cannot change when the epoch table is regenerated.

- `settlement_snapshot` rows are append-only (`BEFORE UPDATE`/`BEFORE DELETE` → `RAISE(ABORT)`).
- `freeze_snapshot()` is write-once: re-freezing an identical value is a no-op; re-freezing a **different** value raises `ImmutableSettlementError`.
- `verify_regeneration_invariants()` is called by both `generate_settlement_epochs.py` and `regenerate_settlement_epochs.py`; a regeneration that would move a frozen value **fails closed** (RuntimeError / exit 3) and writes nothing.

## Flags

1. **Upstream settle path (ruling #1 mechanism) not patched.** The engine still books `settled_value` from an unreliable settle-time METAR extreme. This change corrects the *truth read* and adds the immutability invariant; the fix to `process_settlements_for_date` to read canonical truth **forwards** should be its own reviewed change.
2. **Governance (Gerri's call):** 10 closed trades flipped outcome; booked `realized_pnl` was left as-is (default `settled_value`/P&L). Only `settlement_confirmed`/`settlement_outcome` were repopulated. Restate vs freeze-with-annotation remains a governance decision.
3. **Oct 9:** the Kalshi high market is not yet finalized on the API; Oct 9 canonical comes from weatherapi (`max_temp_f`) until finalization, then re-freezes... **no** — it is frozen write-once, so it will NOT update. If Oct 9 was frozen from weatherapi it is pinned; a later Kalshi finalization mismatch will surface via the invariant rather than silently overwrite. (Window frozen 10-01..10-09.)

## Not done

- No AI/ML. Deterministic. No live trading enabled. G3 unchanged.
