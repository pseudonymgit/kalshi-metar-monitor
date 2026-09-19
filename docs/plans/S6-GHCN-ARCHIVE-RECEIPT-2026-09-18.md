# S6 — GHCN Proxy Residual ARCHIVE — Execution Receipt

**Date:** 2026-09-18
**Owner:** Gilfoyle (architecture/QA lane) — execution of 9/17 disposition
**Disposition source:** `S6-GHCN-PROXY-DISPOSITION-2026-09-17.md` (REBUILD done + residual ARCHIVE)
**Action:** Non-destructive MOVE of superseded ISD/legacy artifacts to dated archive dir. **No delete. No commit. Dirty tree preserved.**
**Status:** COMPLETE — 4 DB files + 2 sidecars archived; canonical assets intact.

---

## 1. Assets archived (MOVE, not delete)

Target: `data/archive-stale-20260918/`

| Asset | Pre-move size | sha256 (pre == post) | Disposition |
|---|---|---|---|
| `data/ghcn_daily.db` | 4,096 B | `44e9b382070d7cf97c2d422aaa250eee7edbe9a9fa39516c42c54ccea43cae81` | empty stub (0 tables) → ARCHIVE |
| `data/isd_lite_raw.db` | 8,019,968 B | `cd108343a2b9283dcac4d8d3b2f512908d37d22996f09d7cccedda2e86385926` | 33,002-row raw ISD → ARCHIVE |
| `data/isd_raw.db` | 28,672 B | `883657c0500e269de5d819e2239ecbf21861d5a4b563e0a4eba598625fe7e98f` | 0-row empty → ARCHIVE |
| `data/isd_log.db` | 12,288 B | `437125b905a75708c20ecf8cbd79a87ac61ca972a87b94a213a53c18aaf78be4` | log-only → ARCHIVE |
| `data/isd_lite_raw.db-shm` | 32,768 B | (sidecar) | WAL/SHM sidecar → ARCHIVE |
| `data/isd_lite_raw.db-wal` | 0 B | (sidecar) | WAL/SHM sidecar → ARCHIVE |

**Content-preservation proof:** sha256 of each moved DB is byte-identical to the manifest captured before the move (§2 of pre-execution inventory). Move = `mv`, no rewrite.

---

## 2. Canonical assets — verified intact after archive

| Asset | Status |
|---|---|
| `data/ghcn_settlements.db` (canonical GHCN, 67,031,040 B, 619,428 rows) | ✅ intact |
| `data/ghcn_settlement.db` → symlink → `ghcn_settlements.db` | ✅ intact (kept as harmless alias per disposition §5.3) |
| `data/kalshi_settlements.db` | ✅ intact |
| `data/metar_backfill.db` | ✅ intact |
| `data/weatherapi_archive.db` | ✅ intact |
| `data/kalshi_market_snapshots.db` | ✅ intact |

---

## 3. Not archived (flagged, not executed)

- **ISD scripts** (`scripts/backfill_isd.py`, `backfill_isd_fast.py`, `backfill_isd_lite.py`, `download_isd_stations.py`, `edge9_isd_backfill.py`, `simple_directional_backtest_isd.py`): these are **git-tracked** files still in `scripts/` root. A duplicate set already lives in `scripts/archive/isd-retired/` (also tracked). Moving the root copies is a *tracked-file deletion+addition* that would expand the dirty-tree surface beyond the disposition's explicit "archive the DB assets" list — **deferred to a decision on whether the tracked root duplicates should be `git rm`'d or left**. No live code imports them (verified: only graphify-generated manifest/cache artifacts reference them). Flag for Dan/Gerri, do not self-resolve.
- `data/isd_downloads/` (empty dir): left as-is (empty, harmless).

---

## 4. Receipt

- **Disposition executed:** residual ARCHIVE of 4 superseded DBs + 2 sidecars → `data/archive-stale-20260918/`.
- **Non-destructive:** `mv` only; sha256 proves byte-identical content; nothing deleted.
- **No commit:** `git status` dirty-tree state preserved (no files staged/committed).
- **Canonical GHCN + all truth sources untouched.**
- **Deferred:** tracked root ISD script duplicates (flag, not moved).
