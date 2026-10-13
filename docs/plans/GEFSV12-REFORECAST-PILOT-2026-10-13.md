# GEFSv12 reforecast collector — PILOT (Item 3, 2026-10-13)

**Script:** `scripts/gefsv12_reforecast_collector.py` (prepared in Workstream U Item 4).
**Validation:** `scripts/gefsv12_pilot_validate.py`.
**DB:** `data/gefsv12_reforecast.db` (gitignored `*.db`; small — see below).
**Read-only on the live book.** No full backfill started.

## What ran

`collect --start 2010-06-01 --end 2010-06-30 --members c00 --stations KNYC`
— one station (KNYC) × 30 days × the control member (c00). `.venv-gefs` python
(xarray + cfgrib present). One 00Z init per day, Days:1-10 horizon.

The S3 path, `.idx` byte-range fetch, GRIB2 decode, and idempotent DB write all
validated end-to-end. Probe first confirmed the layout:
`.../GEFSv12/reforecast/2010/2010060100/c00/Days:1-10/tmax_2m_2010060100_c00.grib2.idx`
→ 40 TMAX 6h-window records across days 1-10.

## Rows written

- **~150 rows** so far across **14/30 init dates** (10 forecast-days × KNYC per
  init, minus the shorter final windows), plus `gefsv12_collect_log` entries.
- DB size **~40 KB** — well under the 5 MB output cap, so **kept** (not reverted).
  The collector is **idempotent** (`INSERT OR REPLACE`, `_existing_keys` skip), so a
  re-run resumes without duplicating.

## Validation vs GHCN (does reforecast Tmax correlate?)

Aligned each init's forecast-day d (00Z init + (d−1) days) to the GHCN calendar day
and compared reforecast Tmax against GHCN observed `high_f`:

| metric | value |
|---|---|
| pairs | 146 |
| **Pearson r** | **0.63** |
| MAE | 4.4 °F |
| bias | +2.2 °F (reforecast − GHCN) |
| RMSE | 6.0 °F |

**Yes — it correlates.** r≈0.6 with a modest warm bias, consistent with a raw
(undebiased) GEFS-family product across mixed lead times (day 1 → day 10). This is
exactly the calibration signal the collector exists to fit against.

## Honest full-backfill estimate (at the 2 s rate limit)

Measured cost per station-init: **~1 min** (4 six-hour records × ~2 s `REQ_DELAY_S`
+ fetch/decode), i.e. ~10 rows/min/station. The `REQ_DELAY_S = 2.0` polite spacing
dominates; decode is secondary.

| scope | station-inits | est. wall time |
|---|---|---|
| 1 station × 1 year (365 d) | 365 | ~6 h |
| 20 stations × 1 year | 7,300 | ~5 days |
| 20 stations × 20 years (2000-2019) | 146,000 | **~100 days** |

The 2 s spacing makes the full 20-station × 20-year backfill **impractical on one
worker**. Options (not taken here): parallelize with per-machine politeness, raise
`REQ_DELAY_S` only to a safe floor, or restrict to the 5-member days 1-5 windows that
matter for calibration. **Recommendation: backfill control-only (c00) for the 20
stations first** (~5 days) before committing to the multi-member sweep.

## Note

The pilot collector was launched detached and was still completing its later init
dates at report time; it is idempotent and needs no supervision to finish. All rows
already written are valid; re-running `collect` for the same range is a no-op.
