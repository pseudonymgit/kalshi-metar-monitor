# Settlement Epochs Rewrite Plan (Option A) — STAGED / GATED

**Date:** 2026-09-17
**Author:** Ray Donovan (subagent — preparation/staging phase)
**Branch:** `temp-sprint-branch`
**Status:** PREPARATION ONLY. No data mutated. Gated on METAR parser fix + Dan approval.

---

## 0. TL;DR — the number Dan must approve

Of the **616** settlement_epochs rows flagged corrupt by the recovery
receipt's broad filter (`settlement_bucket > 150 OR prior_settlement_bucket > 150`),
the corruption decomposes into two very different populations:

| Population | Count | Action |
|---|---|---|
| **Current-bucket corrupt** (the real corruption) | **419** HIGH + **5** LOW | need a clean external source |
| **Prior-bucket-only corrupt** (artifact of the above) | **197** HIGH + **4** LOW | auto-fixed by cascade, no source needed |

**For the 419 HIGH rows that actually need a clean source:**

| Kalshi source | Rows |
|---|---|
| `finalized` (live Kalshi API, authoritative) | 337 |
| `live_api` (live API, not yet marked finalized) | 19 |
| `historical_backfill` (Kalshi historical settlement API) | 37 |
| **Recoverable via Kalshi** | **393** |
| **Unresolved (no clean Kalshi HIGH source)** | **26** |

**For the 5 LOW rows that actually need a clean source:**

| Kalshi LOW source | Rows |
|---|---|
| `historical_backfill` | 3 |
| **Unresolved (no clean Kalshi LOW source)** | **2** (KLAS 2026-09-12, KSFO 2026-09-12) |

> **Approval gate:** Dan approves (or amends) the split
> **393 recoverable vs 26 unresolved (HIGH)** and **3 recoverable vs 2
> unresolved (LOW)**. Execution proceeds only after (a) this approval and
> (b) the METAR parser fix has landed (so the corruption cannot recur).

---

## 1. Two material corrections to the prior recovery receipt

The `SETTLEMENT-EPOCHS-RECOVERY-2026-09-17.md` receipt was directionally
correct but contained two errors that materially change the plan:

### Correction A — "min_temp_f is clean" is FALSE
The receipt asserted `min_temp_f` has "0 corrupt across all 58,703 rows."
In fact **5 `min_temp_f` rows are corrupt** (same METAR string-concat bug),
which produced the 9 corrupt LOW epochs:

| Station | Date | corrupt `min_temp_f` |
|---|---|---|
| KSFO | 2026-07-07 | 50482.4 |
| KSFO | 2026-07-09 | 52280.6 |
| KSFO | 2026-09-12 | 46891.4 |
| KLAS | 2026-09-12 | 34287.8 |
| KOKC | 2026-07-09 | 34278.8 |

So LOW buckets are **not** trivially regenerable from "clean `min_temp_f`";
5 dates are corrupt at source and need a Kalshi LOW fallback (3 found) or an
`unresolved` marker (2 not found).

### Correction B — the "616" conflates two populations
The receipt's `>150 OR prior>150` filter counts both (a) the day whose own
bucket is garbage and (b) the *next* day, whose own value is clean but whose
`prior_settlement_bucket` points at the garbage day. Only (a) needs a source.
(b) is fixed automatically once (a) is fixed (recompute prior in date order).

Concretely: 419 = number of corrupt `max_temp_f` rows = number of HIGH rows
whose `settlement_bucket` itself is corrupt. The other 197 are prior-bucket
artifacts.

---

## 2. Exact coverage math (verified read-only, 2026-09-17)

### HIGH

```
616  = corrupt rows under doc filter (settlement>150 OR prior>150)
 ├─ 419  current-bucket corrupt  (== 419 corrupt daily_stats.max_temp_f)
 │    ├─ 337  finalized         → recoverable
 │    ├─  19  live_api          → recoverable
 │    ├─  37  historical_backfill → recoverable
 │    └─  26  UNRESOLVED
 └─ 197  prior-bucket-only corrupt → auto-fixed by cascade
```

**HIGH unresolved (26) — by station:**

| Station | Count |
|---|---|
| KSLC | 4 |
| KSAT | 3 |
| KSFO | 3 |
| KLAS | 2 |
| KMIA | 2 |
| KOKC | 2 |
| KPHX | 2 |
| PHNL | 2 |
| KCLT | 1 |
| KDEN | 1 |
| KDTW | 1 |
| KMSP | 1 |
| KMSY | 1 |
| KTPA | 1 |

Date range: 2026-07-26 → 2026-09-17. These are late-window dates (mostly
2026-09-06 through 2026-09-17) where Kalshi has not yet finalized, plus a
cluster of `KSLC`/`KDTW`/`KCLT`/`KTPA`/`PHNL` early-July dates where Kalshi
simply has no settlement history at all.

### LOW

```
9  = corrupt rows under doc filter
 ├─ 5  current-bucket corrupt  (== 5 corrupt daily_stats.min_temp_f)
 │    ├─ 3  historical_backfill → recoverable
 │    └─ 2  UNRESOLVED (KLAS 2026-09-12, KSFO 2026-09-12)
 └─ 4  prior-bucket-only corrupt → auto-fixed by cascade
```

---

## 3. Station-alias resolution (required, not optional)

The Kalshi settlement DB uses different station codes than `daily_stats` for
four stations. The generator MUST apply the production alias map from
`scripts/kalshi_settlement_upkeep.py`:

```python
STATION_ALIASES = {"KORD": "KMDW", "KJFK": "KNYC", "KLGA": "KNYC", "KDAL": "KDFW"}
```

Six corrupt HIGH rows are recoverable only through this alias resolution.

Also, Kalshi's `series` field distinguishes HIGH vs LOW (`KXHIGH*` vs
`KXLOW*`). The join MUST filter on `series LIKE 'KXHIGH%'` for HIGH recovery
and `series LIKE 'KXLOW%'` for LOW recovery. Joining on `station + target_date`
alone silently matches HIGH epochs against LOW settlement temps (the same bug
the R8 settlement JOIN fix documented).

---

## 4. Source-priority and trust determination

`kalshi_settlements` rows carry a `source` and `source_type`. Only the
following are trustworthy for reconstruction:

| Priority | source | source_type | Rationale |
|---|---|---|---|
| 1 | (any) | `finalized` | live Kalshi API finalized settlement — authoritative |
| 2 | `live_api` | (any) | live Kalshi API, clean HIGH value, not yet finalized |
| 3 | `historical_backfill` | `historical_api` | Kalshi historical settlement API — verified clean HIGH values |

**Excluded as untrustworthy (circular):**
- `metar_fill` — derived from METAR (the corrupt source). Confirmed pre-2026
  (2021-08-06 → 2025-12-31), so it does not intersect the corruption window,
  but it is excluded on principle.
- `historical_backfill` LOW values were spot-checked and are **genuine HIGH
  temps** (e.g. KMIA 94.0 vs max 93.2; KSFO 70.0 where max was corrupt
  52278.8), confirming they are Kalshi-derived, not METAR-derived.

`historical_backfill` and `live_api` HIGH-series values were sanity-checked:
ranges 70–110°F and 58–109°F respectively, all clean (no values outside
[-60, 150]).

---

## 5. The generator (staged, not run)

`scripts/regenerate_settlement_epochs.py` — written but **not executed**.

Behavior:
- **Read-only by default.** `--coverage` / `--dry-run` print the split above.
- `--execute` refuses to run unless a pre-flight backup of
  `data/metar_backfill.db` exists (`...pre-settlement-fix-YYYYMMDD`).
- Rewrites only the corrupt rows via `INSERT OR REPLACE` / `UPDATE` keyed on
  the existing `UNIQUE(station, market_type, local_trading_date)`.
- Recomputes `prior_settlement_bucket` and `reversion_occurred` in
  `local_trading_date` order so the cascade propagates correctly.
- Writes explicit provenance into `epoch_close_reason`:
  - `kalshi_finalized`, `kalshi_live_api`, `kalshi_historical_backfill`,
    `clean_min_temp_f`, or `unresolved`.
- Rows with no clean source are marked `epoch_status='unresolved'` and left
  with their (still-corrupt) bucket, so downstream backtests can exclude them
  rather than silently consume fabricated values.

**Provisioning detail — prior-bucket cascade:**
Rows whose *current* bucket is clean but whose *prior* bucket pointed at a
garbage day are corrected by the ordered recompute. Their `epoch_close_reason`
is set to `clean_min_temp_f` (their own value was already correct; only the
linkage was repaired).

---

## 6. Approval gate (blocking)

Execution is **blocked** on two independent conditions, both required:

1. **METAR parser fix landed** — the `max_temp_f`/`avg_temp_f`/`min_temp_f`
   string-concatenation bug in the METAR parser must be fixed and verified, so
   that re-running the (corrected) epoch generator does not immediately
   re-introduce corruption on the next ingestion cycle. Owner: Gilfoyle
   (technical). This is a *hard* dependency, not a nice-to-have.

2. **Dan approves the coverage split** — specifically:
   - HIGH: **393 recoverable / 26 unresolved** (from 419 genuinely-corrupt
     current buckets; 197 prior-artifacts auto-fix).
   - LOW: **3 recoverable / 2 unresolved** (from 5 genuinely-corrupt current
     buckets; 4 prior-artifacts auto-fix).
   Dan may also direct how to handle the 28 total `unresolved` rows
   (leave-marked vs. attempt a diurnal-adjustment reconstruction — the latter
   is explicitly **out of scope** for Option A and would be a new decision).

---

## 7. Write scope vs. read scope (this phase)

**Read-only (performed this phase):**
- `metar_backfill.db` — read `settlement_epochs`, `daily_stats`,
  `six_hour_aggregates`, `metar_observations` schemas and data.
- `kalshi_settlements.db` — read `kalshi_settlements`.

**No data was mutated.** `settlement_epochs` untouched. `metar_backfill.db`
and `kalshi_settlements.db` not written. No backfill. No `metar_backfill.db`
backup created (that is an execution-phase step).

**Staged (this phase):**
- `scripts/regenerate_settlement_epochs.py` (corrected generator, gated).
- This plan doc.

---

## 8. Execution sequence (future, NOT performed)

1. Confirm METAR parser fix landed + verified clean re-ingestion.
2. Dan approves the coverage split above.
3. `cp data/metar_backfill.db data/metar_backfill.db.pre-settlement-fix-$(date +%Y%m%d)`
4. `python3 scripts/regenerate_settlement_epochs.py --execute`
5. Verify: no `settlement_bucket` or `prior_settlement_bucket` outside
   [-60, 150]; `reversion_occurred` consistent; 28 rows marked `unresolved`.
6. Commit to `temp-sprint-branch`; handoff for Dan's merge approval.
