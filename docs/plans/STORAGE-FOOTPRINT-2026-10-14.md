# Storage Footprint — DB inventory, GEFSv12 backfill projection, optimization (2026-10-14)

**Date:** 2026-10-14 · **Agent:** Gilfoyle · **Status:** MEASURED — recommendations ready
**Question answered:** Dan (2026-10-14) — *"Make sure backfill doesn't fuck our storage. What
footprint? Optimize, compress."*
**Scope:** read-only measurement. No DB writes, no temp DB files. Nothing deleted.

---

## TL;DR

- **Disk:** `/dev/nvme0n1p3` **914 GB total, 748 GB used, 120 GB free (87%)**. The weather-engine
  `data/` tree is **21.9 GB**; the live top-level DBs are **8.5 GB**; *all* `.db` files (incl.
  superseded siblings and cold archives) are **15.7 GB**.
- **The GEFSv12 backfill is a non-issue for storage.** Full 2000–2019, 5 members, 20 stations,
  days 1–10 = **7.3 M rows ≈ 0.76 GB** on the current schema, or **≈ 0.27 GB** with the optimized
  schema. Even the whole GEFS archive is **<1 GB**.
- **The real storage problem is not the backfill — it is accumulated superseded backups**: two
  pre-settlement copies of `metar_backfill.db` (**4.7 GB**), `lineage_freeze/` (**3.5 GB**), and
  `_a4_backup_20261011/` (**2.4 GB**) alone are **10.6 GB** — more than every live DB combined.
- **Compression works extremely well on these DBs:** `metar_backfill.db` compresses **6.7:1**
  (150 MB → 22.5 MB with gzip-6). The superseded set can shrink from ~12 GB to ~2 GB with near-zero
  information loss. **No zstd/xz binary is installed** (only gzip).
- **VACUUM is mostly unnecessary:** every live DB measured `freelist_count=0`. Only `tigge_archive.db`
  (6,503 free pages ≈ 26 MB) and `ghcn_settlements.db` (28 pages) have reclaimable free space.

---

## 1. Disk + DB inventory (measured)

### Host
```
/dev/nvme0n1p3  914G  748G  120G  87%  /home/node/.openclaw/workspace
```

### Live DBs (`data/`, top-level .db = 8.54 GB)
| DB | Size | Rows | journal | freelist |
|---|---:|---:|---|---:|
| `iem_asos_1min.db` | **2,901 MB** | 20,856,512 (`observations`) | wal | 0 |
| `metar_backfill.db` | **2,451 MB** | 12,850,627 (`metar_observations`) + 1,239,739 (`settlement_truth`) + 118,267 (`settlement_epochs`) | wal | 0 |
| `kalshi_market_snapshots.db` | **465 MB** | 3,404,658 (`market_snapshots`) | **delete** | 0 |
| `nwp_forecasts.db` | **405 MB** | 1,195,435 (`nwp_forecasts`) | wal | 2 |
| `weatherapi_archive.db` | 301 MB | 2,406,927 (`hourly`) + 121,600 (`daily`) | wal | 0 |
| `ghcn_settlements.db` | 65 MB | 620,036 | wal | 28 |
| `tigge_archive.db` | 56 MB | 102,175 | wal | **6,503** |
| `gefs_archive.db` | 7.2 MB | 44,740 | wal | 0 |
| `gefsv12_reforecast.db` (pilot) | 76 KB | 517 | delete | 0 |

Page size is **4096** on every DB. `iem_asos_hourly.db` (793 MB) and `iem_asos.db` (632 MB) are
additional live-ish ASOS stores in `data/`.

### Superseded / cold (the actual hogs — ~12 GB)
| Path | Size | Disposition |
|---|---:|---|
| `data/metar_backfill.db.pre-settlement-fix-20260917` | 2,357 MB | **superseded** duplicate |
| `data/metar_backfill.db.pre-settlement-fix-20260917-165826` | 2,357 MB | **superseded** duplicate |
| `data/lineage_freeze/` (SHA256SUMS + frozen DB copies) | 3.5 GB | integrity freeze (compressed cold store viable) |
| `data/_a4_backup_20261011T005429Z/` | 2.4 GB | pre-A4 backup |
| `data/snapshots/` (metar_backfill_00{1,2}.db.gz) | 540 MB | already gzipped |
| `data/_backup_pre_gefsgap_20260925_015651/` | 384 MB | pre-change backup |
| `data/_backup_pre_gefsfix_20260924_225045/` | 378 MB | pre-change backup |
| `data/gefs_archive.db.{pred2,prerepair,corrupt-backup,rebuild-degraded}-*` | 213 MB | 4 stale repair leftovers |
| `data/z8-frozen/` | 83 MB | frozen |
| `data/tigge_archive.db.bak-prefixcleanup-*` | 56 MB | superseded |
| `data/backups/`, `data/archive/`, `data/quarantine/` | ~64 MB | cold |

**Observation:** the weather-engine `data/` tree (21.9 GB) holds **more cold/superseded bytes (~12 GB)
than live bytes (~9 GB)**. The storage risk is archival hygiene, not the backfill.

---

## 2. GEFSv12 backfill footprint projection

Schema: `gefsv12_reforecast(station, init_date, member, forecast_day, tmax_f, window_start_h,
window_end_h, n_records, fetched_at_utc)` PK `(station, init_date, member, forecast_day)`.

**Per-row cost (measured, `dbstat`, in-memory, 200 k-row synthetic fits):**

| Schema | B/row (table + PK index) |
|---|---:|
| Current (TEXT station/date/member + REAL + `fetched_at_utc` TEXT + 2 window cols) | **104.5** |
| Pilot actual (517 rows incl. PK autoindex) | 126.8 |
| Optimized (INTEGER station/member ids, INTEGER day, scaled-INT tmax, PK only) | **36.7** |

**Projection two ways** (5 members × 20 stations × 7,305 days):

| Scenario | Rows | Current schema | Optimized schema |
|---|---:|---:|---:|
| **True sizing** (days 1–10 per init = 10 values/station-init) | 7,305,000 | **763 MB** | **268 MB** |
| **Task-brief framing** (1 Tmax/station-day) | 730,500 | **76 MB** | **27 MB** |

→ Scaled from the pilot's 0.08 MB / 517 rows: **0.08 × (7.305 M/517) ≈ 1.13 GB** on the current
schema with pilot-level per-row overhead — consistent with the dbstat estimate above (<1.3 GB).

**Verdict: the full backfill is ≤ ~1 GB. It is not a storage threat.** The transient `/tmp` GRIB
traffic is the larger concern (the collector spec estimates ~110 GB of *transient* fetch over 2000–2019),
but that is fetched-and-discarded, not stored.

---

## 3. PRAGMA / optimization status

| DB | page_size | journal_mode | freelist | auto_vacuum | VACUUM needed? |
|---|---:|---|---:|---:|---|
| `iem_asos_1min.db` | 4096 | wal | 0 | 0 | No (0 free) |
| `metar_backfill.db` | 4096 | wal | 0 | 0 | No |
| `kalshi_market_snapshots.db` | 4096 | **delete** | 0 | 0 | No (but see §4) |
| `nwp_forecasts.db` | 4096 | wal | 2 | 0 | No (8 KB) |
| `weatherapi_archive.db` | 4096 | wal | 0 | 0 | No |
| `ghcn_settlements.db` | 4096 | wal | 28 | 0 | Marginal (112 KB) |
| `tigge_archive.db` | 4096 | wal | **6,503** | 0 | **Yes (~26 MB reclaimable)** |

- Page size 4096 is the correct default for this workload; no change recommended.
- WAL is fine; `kalshi_market_snapshots.db` uses `delete` (rollback) — acceptable but converting to
  WAL would improve writer concurrency if snapshot ingestion ever contends. Not a footprint item.
- `auto_vacuum=0` everywhere: incremental vacuum is off. Fine given freelist≈0.
- **Only `tigge_archive.db` merits a VACUUM** (~26 MB), and `ghcn_settlements.db` marginally.

---

## 4. Recommendations

### (a) GEFSv12 storage — keep SQLite, but optimize the schema
SQLite is the right home (random-access joins to settlement truth; ~0.27–0.76 GB is trivial). Apply
the **optimized schema** when the bulk backfill lands:
- `station_id INTEGER` (dictionary table `station(id, code)`) instead of TEXT `station`
- `member_id INTEGER` (0=c00, 1–4=p01–p04) instead of TEXT `member`
- `init_day INTEGER` (days-since-epoch) instead of TEXT `init_date`
- `tmax_c10 INTEGER` (tenths of °C) instead of REAL °F — **half the mpemba-able precision, exact**
- **drop** `fetched_at_utc` (redundant — `gefsv12_collect_log` already carries ingest time) and the
  two `window_*_h` columns (derivable from `forecast_day`)
- Result: **≈ 268 MB instead of ≈ 763 MB (−65%)**, and faster PK lookups.

**Do NOT** switch to packed binary/`.csv.gz` for the primary store: it breaks the ad-hoc
SQL join to `settlement_truth` that the debias/analog refit needs. Reserve packed formats for
**export snapshots only**.

### (b) VACUUM
- `tigge_archive.db` — **VACUUM** (~26 MB reclaim). Run outside active writers.
- `ghcn_settlements.db` — optional (~112 KB).
- **All other DBs: do not VACUUM** (freelist 0; a VACUUM would rewrite hundreds of MB for zero
  reclaim and block the live book). Confirm with `PRAGMA freelist_count` immediately before any run.

### (c) Cold archives → compress (gzip, since zstd/xz are absent)
Measured: `metar_backfill.db` compresses **6.7:1** at gzip-6. Recommended actions, **in order of
payoff** (all reversible; none deletes data without Dan's sign-off):
1. **gzip the two `metar_backfill.db.pre-settlement-fix-*` copies** — 4.71 GB → **~0.70 GB** (−4.0 GB).
2. **gzip `lineage_freeze/` DB copies** — 3.5 GB → **~0.5 GB** (−3.0 GB). Keep `SHA256SUMS` plain.
3. **gzip + prune `_a4_backup_20261011/`** — 2.4 GB → ~0.36 GB (−2.0 GB).
4. **Remove the 4 stale `gefs_archive.db.{pred2,prerepair,corrupt,rebuild}-*` siblings** (213 MB;
   they are failed-repair leftovers, not restores) — gzip or delete.
5. gzip `_backup_pre_gefs{fix,gap}-*` (762 MB → ~0.11 GB) and `tigge_archive.db.bak-*` (56 MB→8 MB).

**Net recovery ≈ 9–10 GB**, taking the `data/` tree from 21.9 GB to ~12 GB, with no live-data loss.
(The `snapshots/` dir is already gzipped — leave it.)

### (d) Backfill guardrail
The GEFSv12 bulk backfill writes ≤1 GB and fetches ~110 GB transiently to `/tmp`. **Bind the
collector's spill directory and add a preflight disk check** so a runaway `/tmp` fill cannot take the
120 GB-free host to full — the backfill itself is fine; it is the temp GRIB traffic that needs a cap.
`/tmp` currently has the same 120 GB free pool as the workspace (single overlay fs).

---

## 5. Status of deliverables

| Item | Status |
|---|---|
| Disk + DB inventory | ✅ — §1, measured |
| GEFSv12 footprint projection (rows, MB) | ✅ — §2 (763 MB current / 268 MB optimized) |
| PRAGMA audit (page_size, journal_mode, freelist, autovacuum) | ✅ — §3 |
| Optimization recommendations (a/b/c/d) | ✅ — §4 |
| Applied | ▶ **NONE** — read-only pass; nothing deleted, moved, or VACUUMed |

**Bottom line for Dan:** the backfill is **under 1 GB** and is not a storage problem. The
storage story is the **~10 GB of superseded DB backups** that compress ~6.7:1 — gzip those and the
tree nearly halves. Only `tigge_archive.db` needs a VACUUM.
