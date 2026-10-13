# B6 — Stale Open Rows → Settlement Reconciliation (draft)

**Date:** 2026-10-13
**Lane:** Sprint 2 — Lane B (technical)
**Owner:** Gilfoyle
**Repo:** `prototypes/weather-engine-source/`
**Status:** READ-ONLY investigation + prepared (NOT executed) reconciliation. No write to `trades`.

---

## 0. Bottom line

The exit pass flagged **14 open trades** in `data/paper_trading_sbox.db` with
`hours_to_settlement < 0` (past their settlement date) — entered but never
reconciled. Cross-referencing canonical settlement truth:

* **11 can be reconciled automatically** (canonical Kalshi `live_api/finalized`
  temp exists for the station-day; strike parseable from the trade's notes).
* **3 need manual review** — all three are `2026-10-12` trades whose
  **Kalshi settlement has not yet been written** to `kalshi_settlements.db`
  (canonical truth lagging; not a data error, just not-yet-ingested).

Recommended action: a one-time reconcile script (shape below), gated by Dan,
that writes only the 11 auto rows. The 3 laggards settle on the next
`kalshi_settlement_upkeep` run.

**⚠️ One source disagreement is surfaced (§3): KPHX 2026-10-10 flips outcome
between canonical Kalshi (96°F) and GHCN (96.1°F) because the settled value
lands exactly on the strike (96). Strict `>` means the trade lost. Canonical
wins; flagged, not hidden.**

---

## 1. The 14 open rows

Read-only:
```sql
SELECT id, station, market_ticker, trade_date_utc, created_at_utc, status
  FROM trades WHERE status='open' ORDER BY created_at_utc;
```

| id | station | market_ticker | trade_date | strike | dir | qty | price |
|---:|---|---|---|---:|---|---:|---:|
| 193 | KDCA | KXHIGHTDC-26OCT10-T70 | 2026-10-10 | 70 | UP | 44 | 0.115 |
| 194 | KNYC | KXHIGHNY-26OCT10-T66 | 2026-10-10 | 66 | UP | 143 | 0.035 |
| 195 | KPHX | KXHIGHTPHX-26OCT10-T96 | 2026-10-10 | 96 | UP | 57 | 0.175 |
| 196 | KATL | KXHIGHTATL-26OCT10-T69 | 2026-10-10 | 69 | UP | 9 | 0.565 |
| 261 | KLAX | KXHIGHLAX-26OCT10-T75 | 2026-10-10 | 75 | DOWN | 91 | 0.055 (no) |
| 262 | KHOU | KXHIGHTHOU-26OCT10-T88 | 2026-10-10 | 88 | UP | 26 | 0.19 |
| 263 | KDCA | KXHIGHTDC-26OCT11-T68 | 2026-10-11 | 68 | UP | 12 | 0.43 |
| 264 | KLAS | KXHIGHTLV-26OCT11-T73 | 2026-10-11 | 73 | UP | 30 | 0.815 |
| 265 | KPHL | KXHIGHPHIL-26OCT11-T64 | 2026-10-11 | 64 | UP | 154 | 0.065 |
| 266 | KBOS | KXHIGHTBOS-26OCT11-T60 | 2026-10-11 | 60 | UP | 10 | 0.47 |
| 267 | KMDW | KXHIGHCHI-26OCT11-T73 | 2026-10-11 | 73 | UP | 200 | 0.025 |
| 268 | KMSP | KXHIGHTMIN-26OCT12-T81 | 2026-10-12 | 81 | UP | 12 | 0.425 |
| 269 | KNYC | KXHIGHNY-26OCT12-T69 | 2026-10-12 | 69 | UP | 238 | 0.105 |
| 270 | KBOS | KXHIGHTBOS-26OCT12-T71 | 2026-10-12 | 71 | UP | 62 | 0.40 |

All are `HIGH`/`buy_yes` except id 261 (`HIGH`/`buy_no`, i.e. the DOWN side).

---

## 2. Settlement truth sources

| Source | DB | Authority |
|---|---|---|
| Kalshi `live_api` / `finalized` | `data/kalshi_settlements.db` (`kalshi_temp`, `target_date`) | **CANONICAL** (`core.canonical_settlement.CANONICAL_TEMP_SOURCES`) |
| GHCN daily high | `data/ghcn_settlements.db` (`high_f`, `date`) | provisional |
| Kalshi results snapshot | `data/kalshi_settlement_results.db` | **useless here** — static 2026-09-20 snapshot (9783 rows, `fetched_at` all 2026-09-20); no ticker for this window. Tickered join returns MISSING. |

**Canonical coverage:** `kalshi_settlements.target_date` maxes at **2026-10-11**.
There is therefore **no canonical row for `2026-10-12`** → ids 268/269/270 are
the three laggards.

GHCN provisional covers only through **2026-10-10** (3-day NOAA lag) → the
2026-10-11 rows have canonical truth but no provisional cross-check.

---

## 3. Per-row disposition (computed)

Settlement rule (single source `core.canonical_settlement.canonical_outcome`,
verified against the 68 already-closed rows):

```
HIGH : S = 1 iff settlement_temp > strike        (strict; equality == not-ITM)
buy_yes: profit = (S - price) * qty   return = S * qty
buy_no : profit = (price - S) * qty   return = (1 - S) * qty
settled_value = 1.0 if S else 0.0     (contract value, NOT the temperature)
canonical_settled_f = canonical temp  settlement_outcome/canonical_outcome = 'correct'|'incorrect'
```

| id | station | canonical temp (°F) | strike | S | outcome | realized P&L | note |
|---:|---|---:|---:|---:|---|---:|---|
| 193 | KDCA | 72 (live_api) | 70 | 1 | correct | **+38.94** | |
| 194 | KNYC | 69 (live_api) | 66 | 1 | correct | **+138.00** | |
| 195 | KPHX | 96 (live_api) | 96 | 0 | incorrect | **−9.98** | ⚠️ **SRC-DISAGREE**: GHCN 96.1 → S=1. Canonical strict `>` binds. |
| 196 | KATL | 68 (live_api) | 69 | 0 | incorrect | **−5.09** | |
| 261 | KLAX | 79 (live_api) | 75 | 0 | incorrect | **−86.00** | DOWN/buy_no: 79>75 → UP won → NO lost |
| 262 | KHOU | 86 (live_api) | 88 | 0 | incorrect | **−4.94** | |
| 263 | KDCA | 67 (live_api) | 68 | 0 | incorrect | **−5.16** | |
| 264 | KLAS | 74 (live_api) | 73 | 1 | correct | **+5.55** | |
| 265 | KPHL | 65 (live_api) | 64 | 1 | correct | **+143.99** | |
| 266 | KBOS | 60 (live_api) | 60 | 0 | incorrect | **−4.70** | equality == not-ITM |
| 267 | KMDW | 78 (live_api) | 73 | 1 | correct | **+195.00** | |
| 268 | KMSP | — | 81 | — | **MANUAL** | — | no canonical 2026-10-12 row yet |
| 269 | KNYC | — | 69 | — | **MANUAL** | — | no canonical 2026-10-12 row yet |
| 270 | KBOS | — | 71 | — | **MANUAL** | — | no canonical 2026-10-12 row yet |

**Auto: 11** (5 correct, 6 incorrect). **Manual: 3** (2026-10-12 laggards).

P&L convention caveat: `trade_cost` in this book = `qty*price` (the engine's
`record_trade` writes `trade_cost = net_cost`, i.e. execution cost), so
`realized_pnl` is gross of the settled Kalshi fee; the live settle path applies
the per-contract canonical fee separately. The reconciled P&L above matches the
TABLE convention of the 68 closed rows (verified: id 256 buy_yes correct
`(1-0.21)*24=18.96`; id 246 buy_no correct `(0.94-0)*26=24.44`).

---

## 4. Recommended one-time reconcile script (NOT executed)

Artifact: `scripts/reconcile_stale_open_trades_2026-10-13.py` — **read-only**.
It re-derives the table above and can `--emit-sql` the prepared statements.
Recommended gate: Dan runs it with an explicit `--execute` flag added, or applies
the SQL below by hand. Prepared UPDATE shape (per auto row):

```sql
UPDATE trades
   SET settled_value            = <1.0|0.0>,
       settlement_date_utc      = '<trade_date>T<HH:MM:SS>Z',   -- per-station convention from closed rows
       realized_pnl             = <pnl>,
       settlement_return_amount = <return>,
       settlement_confirmed     = 1,
       settlement_outcome       = '<correct|incorrect>',
       canonical_settled_f      = <canonical_temp>,
       canonical_source         = 'kalshi_live_api',
       canonical_outcome        = '<correct|incorrect>',
       status                   = 'closed'
 WHERE id = <id> AND status = 'open';
```

Notes:
* `settlement_date_utc` hour is taken from the **per-station convention already
  present in the 68 closed rows** (e.g. KLAX/KSFO/KSEA `08:00:00Z`,
  KPHX `07:00:00Z`, most eastern/central `05:00:00Z`). A station with no closed
  precedent defaults to `05:00:00Z` and is flagged `settlement_date_source`.
* `canonical_source` is written **only** because the value is
  `live_api/finalized` — the `backfill_canonical_settlement` invariant.
* The 3 manual rows must **not** be touched until a canonical 2026-10-12 row
  exists (otherwise a GHCN provisional would be laundered as canonical).
* No position-table mutation is proposed. The engine's settle path also reduces
  the matching `positions` row; those are **empty for these trades** — a
  read-only check found no `positions` row referencing any of the 14 trade
  uuids, so the proposed `trades` UPDATE is complete on its own for closing.
  *(If Dan wants balance parity, `daily_balances` should be recomputed after.)*

---

## 5. Evidence / artifacts

* Read-only script: `scripts/reconcile_stale_open_trades_2026-10-13.py`
* JSON artifact: `artifacts/stale-open-reconcile/stale_open_reconcile_2026-10-13.json`
* No DB writes. No temp DBs. (Cache DBs untouched.)

---

## 6. Do-not-wire

Nothing wired. No `trades` write. No P&L mutation. No live-book change.
