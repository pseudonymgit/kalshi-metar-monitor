# S6 — GHCN Proxy Disposition Decision (REBUILD / ARCHIVE / REPLACE)

**Date:** 2026-09-17
**Item ID:** S6 (Workstream S — "GHCN proxy rebuild/archive/replace")
**Owner:** Gilfoyle (architecture/QA lane) — disposition only
**Implementation owner:** Ray Donovan (deferred; not executed here)
**Scope:** Read-only analysis + recommendation. **No code, config, data, or DB mutation.**
**Status:** DISPOSITION COMPLETE — recommendation issued, no mutation performed.

---

## 1. Recommendation

**REBUILD is ALREADY DONE — the canonical asset is `data/ghcn_settlements.db`, and it stays.**

The three-way framing (REBUILD / ARCHIVE / REPLACE) is not mutually exclusive; the correct
disposition is a **hybrid**, applied per-asset:

| Asset | Disposition | Confidence |
|---|---|---|
| `data/ghcn_settlements.db` (64 MB, 619,428 rows, 21 stations) | **REBUILD — DONE, KEEP CANONICAL** | High |
| `data/ghcn_settlement.db` (symlink → canonical) | **REPLACE — keep as harmless alias** (or remove) | High |
| `data/ghcn_daily.db` (4 KB, zero tables — empty stub) | **ARCHIVE** | High |
| `data/isd_lite_raw.db` (33,002 rows — raw ISD, *not* GHCN) | **ARCHIVE** (superseded by GHCN build) | Medium |
| `data/isd_raw.db` (0 rows) / `data/isd_log.db` (log table only) | **ARCHIVE** | High |

Net recommendation: **the REBUILD leg is complete and correct; the residual work is ARCHIVE of
superseded ISD/legacy artifacts, not further rebuild or replace.**

---

## 2. Current state (verified read-only, 2026-09-17)

Verified against the live filesystem in `weather-engine-source` on `temp-sprint-branch`:

- `data/ghcn_settlements.db` — **64 MB**, single table `ghcn_settlements`:
  - **619,428 rows**, **21 stations**, date range **1869-01-01 → 2026-09-12**.
  - Schema: `station TEXT, date TEXT, high_f REAL, low_f REAL, source TEXT DEFAULT 'ghcn_daily'`.
  - `source` is uniformly `'ghcn_daily'` (clean, single-provenance tag).
  - Row count grew from the 619,353 noted on 2026-09-16 → 619,428 (live backfill advanced ~75 rows).
- `data/ghcn_settlement.db` — **symlink** → `ghcn_settlements.db` (legacy name alias, 0 bytes).
- `data/ghcn_daily.db` — **4,096 bytes, zero tables** (empty stub, superseded).
- `data/isd_lite_raw.db` — 33,002 rows in `isd_lite_raw` (+ empty `isd_backfill_log`). Raw ISD
  ingestion artifact, **not** GHCN — pre-dates the consolidated GHCN build.
- `data/isd_raw.db` — 0 rows (`isd_raw` empty).
- `data/isd_log.db` — 5 rows in `isd_backfill_log` only.

No existing `docs/plans/S6-*` or `docs/plans/*GHCN*` plan document (confirmed). Prior S-track
continuity notes live under `.meta/continuity/weather-engine/` (S3–S7, 2026-09-16).

---

## 3. Reasoning

1. **The rebuild is already complete and healthy.** The canonical DB went from 2 → 21 stations
   and 87,560 → 619K rows (S6 architecture note, 09-16). It is current through 2026-09-12 and
   still advancing. Schema is clean; provenance tag is singular and correct (`ghcn_daily`).
   There is no defect to "rebuild" away.

2. **GHCN is the longest-horizon, highest-coverage ground-truth *weather* source** (1869→present),
   but it is **third** in truth precedence after `kalshi` and `settlement_epochs` (S5/S6 notes).
   S5 confirmed Kalshi↔GHCN agree to **mean 0.35°F / median 0.00°F** — so GHCN is a *safe
   fallback*, not authoritative for market settlement. This constrains its role but does not
   justify replacing or discarding it.

3. **The "proxy" framing is a misnomer for the canonical file.** `ghcn_settlements.db` is not a
   proxy — it is a first-party ground-truth rebuild with a clean provenance tag. The *proxy*
   concern actually lives in the Kalshi `source_type='historical_api'` rows (S7) and the
   `settlement_epochs.settlement_bucket` corruption (S5), neither of which is a GHCN asset.
   Replacing GHCN would be solving the wrong problem.

4. **Residual work is ARCHIVE, not rebuild/replace.** The only stale artifacts are the empty
   `ghcn_daily.db` stub and the superseded ISD raw/log files. These should be moved to the
   archive dir (`data/archive-stale-20260907/` or the next dated archive dir), never deleted.

---

## 4. Risk / benefit per option

### REBUILD
- **Benefit:** (already realized) 21-station, 619K-row, 1869→present clean rebuild; clean schema;
  singular provenance tag.
- **Risk:** low-to-none remaining. The only outstanding item is confirming the rebuild command is
  deterministic and documented (manifest with row-count/shape/date-range). No re-rebuild needed.

### ARCHIVE (residual stale assets)
- **Benefit:** removes the empty `ghcn_daily.db` stub and superseded ISD artifacts, eliminating
  ambiguity about which DB is canonical; frees ~8 MB.
- **Risk:** **low**, provided it is *archive-only* (physical move to `data/archive-*`), never
  `git clean` or delete. The `gefs_work` guard applies: do not delete data assets.

### REPLACE
- **Benefit:** none over the current state. The only candidate "replace" is the `ghcn_settlement.db`
  symlink, which is already a harmless alias and could simply be removed (or left).
- **Risk:** **high if misread** as "replace the canonical GHCN source." Replacing ground-truth
  GHCN with ISD or another source would *lose* the 1869→present horizon and the 0.35°F Kalshi
  agreement. Not warranted.

---

## 5. Concrete next step (deferred to Ray — not executed here)

1. **Document determinism:** record the rebuild command + a manifest (row count 619,428, 21
   stations, date range 1869-01-01 → 2026-09-12, sha256 of the DB file) so the rebuild is
   reproducible and auditable.
2. **Archive stale assets (archive-only, never delete):**
   - `data/ghcn_daily.db` → `data/archive-stale-YYYYMMDD/`
   - `data/isd_lite_raw.db`, `data/isd_raw.db`, `data/isd_log.db` → same archive dir.
3. **Decide the symlink:** either remove `data/ghcn_settlement.db` or leave it documented as
   `alias-of-canonical` (no data impact either way).
4. **De-conflict with the real proxy problems (already owned by S4/S5/S7):** the Kalshi
   `historical_api` provenance policy (S7) and the `settlement_epochs` outlier sanity-band fix
   (S5) are the actual "proxy" issues — GHCN itself needs none.

**Stop conditions honored:** no rebuild/replace implemented; no backtests run; no live DB or
daemon touched; read-only inspection only.

---

## 6. Receipt

- **Disposition:** REBUILD (complete) + ARCHIVE (residual stubs) + keep `ghcn_settlements.db` canonical.
- **No code/data mutation:** confirmed — this task produced only this document; `git status` of
  tracked files unchanged by this work.
- **Canonical truth asset:** `data/ghcn_settlements.db` (64 MB, 619,428 rows, 21 stations, 1869→2026-09-12).
