# GEFSv12 Reforecast Collector — Spec (Item 4, Brier improvement sprint)

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Status:** collector PREPARED, NOT RUN
**Script:** `scripts/gefsv12_reforecast_collector.py` (read-only on the live book)
**Target DB (not yet created):** `data/gefsv12_reforecast.db`

---

## Why this source

GEFSv12 reforecast is the best free resource we lack for **bias-correction and analog
post-processing**. It is the same model version and resolution as the operational GEFS
of its era, run retrospectively over 2000–2019, so it gives a **station-climate-
consistent, multi-member** history to fit debiasing and analog weights against —
without the discontinuities of stitching operational archives.

It is **not** a decorrelated predictor (it is still GEFS); its value is calibration,
not diversity. Pair with the standing finding that our NWP sources are one forecast
sampled many ways.

---

## Source path (verified 2026-10-13)

| Item | Value |
|---|---|
| Bucket | `noaa-gefs-retrospective` (AWS Open Data, keyless, no token) |
| Base | `s3://noaa-gefs-retrospective/GEFSv12/reforecast/` |
| Layout | `GEFSv12/reforecast/<YYYY>/<YYYYMMDD>00/<member>/Days:1-10/tmax_2m_<YYYYMMDD>00_<member>.grib2(.idx)` |
| Init cadence | **1 cycle/day (00Z)** |
| Members | **`c00` (control) + `p01`..`p04` → 5 members** |
| Horizon | `Days:1-10` file + `Days:10-16` file |
| Grid | global **0.25°** (721 × 1440), lat 90..−90, lon 0..359.75 |
| Record | `TMAX:2 m above ground`, stepped 3 h and 6 h forecast-window maxima |
| Units | Kelvin → °F = K·9/5 − 459.67 |
| Years present | 2000–2020 (2020 is partial: Jan–Jul) |

### ⚠️ Correction to the task brief
The task described GEFSv12 reforecast as **"31-member."** That is the **operational
GEFS** ensemble size. The **reforecast product carries 5 members (c00 + p01–p04).**
This materially affects sample-size planning for any analog/debias fit. Verified by
listing `<YYYYMMDD>00/` — exactly 5 member prefixes.

### Efficient fetch (no 59 MB whole-file pull)
Like RTMA, the `.idx` sidecar gives exact byte offsets. The `tmax_2m` file is
**~59 MB / member / init**; each 6-hour-window TMAX message is **~0.7 MB**. The
collector fetches **only the four 6-h window records per forecast day** (plus the
`.idx`, 6.7 KB), i.e. **~3 MB / member / init** for a 10-day window — a ~20× reduction.

```
2:744029:d=2010010100:TMAX:2 m above ground:0-6 hour max fcst:ENS=low-res ctl
4:2221756:d=2010010100:TMAX:2 m above ground:6-12 hour max fcst:ENS=low-res ctl
6:3673936:d=2010010100:TMAX:2 m above ground:12-18 hour max fcst:ENS=low-res ctl
8:5144122:d=2010010100:TMAX:2 m above ground:18-24 hour max fcst:ENS=low-res ctl
```

**Daily max** for forecast day `d` = max of the 6-h-window records whose window falls
in `((d−1)·24, d·24]` hours from init. The 3-h records are subsets and are skipped.

---

## Schema

```sql
CREATE TABLE gefsv12_reforecast (
    station        TEXT NOT NULL,
    init_date      TEXT NOT NULL,   -- YYYY-MM-DD (00Z init)
    member         TEXT NOT NULL,   -- c00 | p01..p04
    forecast_day   INTEGER NOT NULL,-- 1-based 24h window from init
    tmax_f         REAL,            -- daily max 2m temp, °F
    window_start_h INTEGER,         -- first forecast hour contributing
    window_end_h   INTEGER,         -- last forecast hour contributing
    n_records      INTEGER,         -- 6h records folded in
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (station, init_date, member, forecast_day)
);
CREATE TABLE gefsv12_collect_log (init_date, member, status, n_rows, note, ts_utc,
    PRIMARY KEY (init_date, member));
```

---

## Usage / operations

```bash
# 1) validate the path (no DB write)
python3 scripts/gefsv12_reforecast_collector.py probe --start 2010-01-01

# 2) bulk catch-up (all 5 members, all 20 stations, 10-day windows)
.venv-gefs/bin/python scripts/gefsv12_reforecast_collector.py \
    collect --start 2000-01-01 --end 2019-12-31

# 3) incremental / daily backfill (safe to re-run; skips existing rows)
.venv-gefs/bin/python scripts/gefsv12_reforecast_collector.py \
    collect --start 2019-12-01 --end 2019-12-31 --members c00

# 4) progress
python3 scripts/gefsv12_reforecast_collector.py status
```

**Decoder requirement:** `sample_stations` needs `xarray` + `cfgrib` → run the
collector with **`.venv-gefs/bin/python`** (the system `python3` lacks them). The
`probe` subcommand is pure-`urllib` and runs on either.

### Idempotency & rate limiting
- **Idempotent:** the primary key is `(station, init_date, member, forecast_day)`;
  `INSERT OR REPLACE` + a pre-check skips already-present days, so daily incremental
  runs or a re-run after interruption are safe.
- **Rate-limited:** `REQ_DELAY_S = 2.0` between any two requests; 3 retries with
  linear backoff; `User-Agent` set. One init = 1 `.idx` + up to 4 record fetches per
  member ≈ 5 requests → ~10 s/member.

### Volume estimate (full 2000–2019, 20 stations, 5 members)
- 7,305 days × 5 members = **36,525 inits**; ~3 MB temp fetched each ≈ **110 GB** of
  transient `/tmp` traffic, **~105 h wall** at 10 s/member inits (dominated by the
  2 s spacing). The DB itself is tiny: 36,525 × 20 × 10 = **7.3 M rows ≈ 0.5–1 GB**
  (10 days per init × 20 stations; store only days 1–16 as needed).
- **Recommended strategy:** backfill **`c00` first** (control; 1/5 the cost), then
  `p01..p04` only if the analog/debias refit shows member spread matters. A daily cron
  adding one init/day keeps the archive current with zero bulk cost.

---

## Alignment caveat (must be handled by the consumer)

Windows are **forecast hours from the 00Z init (UTC)**, not local calendar days. A US
station's local max falls across the UTC forecast-day boundary. Before use:

1. Convert the station-local calendar day to a UTC window,
2. Take the max of the overlapping 6-h records,
3. Compare against **canonical `settlement_truth`** (GHCN/Kalshi), exactly as the
   NWP path does.

For daily-max fitting this is a bounded ±1-record edge effect; document it in any
consumer so the local-day join is not silently mis-dated (the same class of bug the
P-workstream loader review fixed).

---

## Status of deliverables

| Item | Status |
|---|---|
| Source path identified + verified | ✅ |
| `.idx` byte-range decode verified (tmax_2m → °F, 2010-01-01) | ✅ |
| Collector script (`probe` / `collect` / `status`) | ✅ prepared |
| Schema + idempotency + rate limiting | ✅ |
| **Bulk backfill** | ⛔ **NOT STARTED** (per instruction) |
| Member-count correction (5, not 31) | ✅ documented |

**Next action (owner: Dan):** decide whether to start a `c00`-only backfill. Effort
estimate above; no code changes needed.
