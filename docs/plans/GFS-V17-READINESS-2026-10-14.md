# GFS v17 — Ingest-Readiness Map + Transition Plan (2026-10-14)

**Date:** 2026-10-14 · **Agent:** Gilfoyle · **Status:** PREPARED — no action until the SCN lands
**Builds on:** `docs/plans/GFS-V17-WATCH-2026-10-14.md` (public-status watch) and
`docs/plans/CUTTING-EDGE-FORECASTING-RESEARCH-2026-10-13.md`
**Question answered:** Dan (2026-10-14) — *"how do we fix GFS v17?"*

---

## TL;DR — the honest answer

**There is nothing to "fix" today, and very little to fix when the SCN lands.** The single most
important finding of this readiness pass: **our engine does not fetch GFS from NOMADS by path.**
Our deterministic `gfs` rows are pulled from **Open-Meteo's `gfs` endpoint**, which is an
abstraction that will follow GFS v17 server-side. Our only NOMADS reference is a **stub that
returns `None`**. So the "directory rename breaks ingest" risk that the PNS warns about **does not
touch our production path** — it would only bite if/when we build a *direct* NOMADS GRIB fetcher.

The real, measurable risk is **not** ingest breakage — it is a **distribution shift**: GFS v17 is a
coupled, 9 km, re-initialised model era, and every GFS-anchored calibration/threshold we hold is
fitted on v16. That is the thing the readiness plan must gate.

**The "fix" = (1) an explicit model-era marker on the GFS rows + calibrations, (2) a `GFS_MODEL_ERA`
feature flag that keeps serving the v16-anchored calibration during the transition window, and
(3) a re-anchor pass on the honest baseline once v17 is live.** Prepared below; not activated.

---

## Part A — Our actual GFS dependency map (verified in-tree)

| Dependency | Path / URL | Source | v17 exposure |
|---|---|---|---|
| **`gfs` model rows** (`nwp_forecasts.db`, 190,302 rows, `temperature_2m_max`/`min` + hourly aggregates) | `https://api.open-meteo.com/v1/gfs` | Open-Meteo | **Indirect** — Open-Meteo follows v17; values shift, path unchanged |
| Historical GFS backfill | `https://archive-api.open-meteo.com/v1/archive` | Open-Meteo | Indirect (Open-Meteo reanalysis-era archive) |
| `om_gfs` fusion member | `https://api.open-meteo.com/v1/forecast` + `historical-forecast-api.open-meteo.com` (`models=gfs_seamless`) | Open-Meteo | Indirect — `gfs_seamless` = bias-corrected GFS |
| NOMADS GFS 0.25° GRIB filter | `https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl` | NOMADS | **STUB** — `core/forecast_disagreement.py::fetch_gfs_forecast()` returns `None`. Not wired. |
| Operational GEFS archive | `https://noaa-gefs-pds.s3.amazonaws.com` → `gefs.{YYYYMMDD}/{hh}/atmos/pgrb2sp25/{member}.t{hh}z.pgrb2s.0p25.f{NNN}` | AWS S3 | **None from v17** — GEFS has its own SCN track; layout already migrated to `atmos/pgrb2sp25` |
| GEFSv12 reforecast | `https://noaa-gefs-retrospective.s3.amazonaws.com` (`GEFSv12/reforecast/...`) | AWS S3 | None (retrospective, frozen) |
| RTMA/URMA | `https://noaa-rtma-pds.s3.amazonaws.com` | AWS S3 | None |

**Files holding the paths:** `scripts/nwp_collect.py` (live, `nwp_collect.py:94`),
`scripts/nwp_backfill_max.py:29`, `scripts/nwp_archive_backfill.py:83`,
`core/intraday/om_gfs_nwp.py:81-82`, `core/forecast_disagreement.py:151`,
`scripts/gefs_reforecast_backfill.py:94,98`, `scripts/gefsv12_reforecast_collector.py:68`.

### What this means
- **Directory/file-naming changes (PNS 26-29 warning): low impact.** We do not key off NOMADS GFS
  paths anywhere in production. The only `nomads.ncep.noaa.gov` reference is a documented stub.
- **Open-Meteo path stability:** Open-Meteo's `/v1/gfs` endpoint name is stable across model
  versions; they internally track GFS. Our exposure is *data*, not *plumbing*.
- **The genuine operational risk is GEFS, not GFS.** Our direct-GRIB dependency is the GEFS
  archive, and its layout (`atmos/pgrb2sp25`) is GEFS-v12-era. If v17's coupled GDASv17 forces a
  paired GEFS upgrade (GEFS v13), *that* is our ingest event — watch for a separate GEFS SCN, not
  the GFS one.

---

## Part B — What actually breaks (ranked)

| # | Break | Severity | Who | Trigger |
|---|---|---|---|---|
| 1 | **GFS calibration/threshold drift.** All GFS-anchored calibration, the honest single-source GFS Brier (~0.16), and the `gfs_seamless` bias correction are v16-era. After cutover they are mis-anchored. | **High** | fusion / calibration | v17 goes operational |
| 2 | **Silent era-mixing in backtests.** A backtest spanning the cutover mixes v16 and v17 rows with no marker → the P-workstream class of "silently mis-dated/mis-versioned" bug. | **High** | any backtest | cutover date crossed |
| 3 | **GEFS paired upgrade (v13?) ingest break.** If GEFS joins the coupled stack, `gefs.{date}/{hh}/atmos/pgrb2sp25/...` may rename. | Medium | GEFS collectors + cron backfill | paired GEFS SCN |
| 4 | **Removed lower-res products.** NOMADS filters for `gfs_0p50`/`gfs_1p00`, GRIB1, NetCDF removed (PNS 26-30). | **Low** | — | none — we use 0p25 only |
| 5 | **Direct NOMADS fetcher** (if ever built). Its hard-coded `filter_gfs_0p25.pl` path is exactly what renames. | Low (stub) | future work | if stub is wired |

★ **Items 1–2 are the real "fix GFS v17" work**, and they are about *versioning discipline*, not
URLs.

---

## Part C — The fix: model-era marker + feature flag

### 1. Model-era marker (write-path)
Stamp every GFS-sourced row with the model era it came from. Minimal schema addition (no live-book
writes in this pass — this is the design):

```sql
-- nwp_forecasts.db: add a nullable era tag; default NULL = pre-marker legacy (treat as v16)
ALTER TABLE nwp_forecasts ADD COLUMN model_era TEXT;   -- 'gfsv16' | 'gfsv17' | NULL
```

Writer sets `model_era` from `GFS_MODEL_ERA` (see flag). Readers that fit calibration **must**
partition on `model_era` so a v16 fit never trains over v17 rows.

### 2. `GFS_MODEL_ERA` feature flag (read-path)
```bash
GFS_MODEL_ERA=v16   # default — serve the v16-anchored calibration/thresholds (safe during transition)
GFS_MODEL_ERA=v17   # only after a post-cutover re-anchor pass proves the new distribution
```
Behaviour:
- `v16` (default): the existing calibration table is applied unchanged. This is the **transition-safe
  fallback** Dan asked for — the engine keeps trading on the last validated era until v17 is measured.
- `v17`: routes to a v17-anchored calibration table (does not exist until the re-anchor pass).
- Unset/unknown → `v16`, and logs a warning. Never silently mixes eras.

### 3. Re-anchor pass (the real fix, post-cutover)
After v17 is live and ≥30 days of v17 rows exist:
1. Re-run `docs/plans/HONEST-ARSENAL-REEVALUATION-2026-10-13.md` against v17 rows.
2. Re-fit the GFS/`gfs_seamless` bias correction on v17.
3. Compare v16-vs-v17 GFS Brier on the shared overlapping window; only flip `GFS_MODEL_ERA=v17`
   if v17 clears the same honest bar.
4. Record the era boundary in `docs/reference/` so no backtest silently spans it.

---

## Part D — Pre-flight checklist (run the day the SCN lands)

1. [ ] Confirm the SCN on `https://www.weather.gov/notification/` and record its **effective date**.
2. [ ] Diff the announced **directory structure + file naming** against Part A's map. Expect: no
       production impact (Open-Meteo abstraction); update the stub comment if NOMADS renames.
3. [ ] Scan for a **paired GEFS SCN** (GEFS v13 / coupled). If present, treat Part B #3 as live.
4. [ ] Verify Open-Meteo's `/v1/gfs` and `gfs_seamless` still serve (they will); note the cutover
       date to mark the v16→v17 row boundary.
5. [ ] Land the `model_era` column + `GFS_MODEL_ERA` flag **before** the cutover date (write-path
       must tag rows from day one; retro-tagging is unreliable).
6. [ ] Freeze the current GFS calibration as the `gfsv16` table; do not refit until v17 data exists.
7. [ ] Schedule the re-anchor pass for cutover + 30 days.
8. [ ] Do **not** wire the NOMADS stub during a transition window.

---

## Part E — Status of deliverables

| Item | Status |
|---|---|
| Dependency map (what we actually fetch) | ✅ — Part A |
| Break inventory ranked | ✅ — Part B |
| Model-era marker + `GFS_MODEL_ERA` flag design | ✅ — Part C (design; not applied — no live-book writes) |
| Pre-flight checklist | ✅ — Part D |
| Activation | ▶ **NOT ACTIVATED** — waiting on SCN (still unissued as of 2026-10-14) |

**No action required today.** This is the preparedness half of the answer; the flag is specified
and ready to land when the SCN issues. Flag to Dan only at SCN publication.
