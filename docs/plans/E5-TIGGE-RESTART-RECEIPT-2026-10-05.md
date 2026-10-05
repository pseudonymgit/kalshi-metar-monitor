# E5 — TIGGE Re-backfill Restart: RECEIPT (2026-10-05)

**Sprint E** · **Item:** E5 (P2) · **Owner:** Ray Donovan · in progress
**Repo:** `prototypes/weather-engine-source/`

---

## Scope
Restart the TIGGE re-backfill with the fixed coordinate code (already on main),
writing only to `tigge_archive.db`.

## Pre-flight verification
- **Fix is on main:** `c9c81c022` — GRIB longitude wrap (`lons = [l-360 if l>180]`)
  + station-gridpoint collision guard + max-offset guard in
  `scripts/ecds_sequential_backfill.py`.
- **Collision check clears** (`scripts/verify_tigge_rebackfill.py`):

| pair | identical fraction | target |
|---|---|---|
| KATL/KPHX | 0.0% | 0–1% |
| KAUS/KMSY | 0.1% | 0–1% |
| KDEN/KPHL | 0.0% | 0–1% |
| KHOU/KSAT | 0.2% | 0–1% |

- **Live mapping proof** (from the ECDS run log): `Mapped 20 stations, max grid
  offset 0.33 deg, no collisions` — the exact line the fix was meant to produce.

## Gap identified
`KATL` was short by 270 rows (4,800 vs 5,070 for every other station). Precise
gap: **KATL missing target dates 2026-09-01 … 2026-09-29** (29 dates × 5 steps ×
~2 = the deficit; the rest of the archive had 95 rows on those dates = 19 stations).

## Why the runner alone was insufficient
`run_tigge_stale_rebackfill.sh` → `get_dates_to_backfill()` checks **date
presence**, not per-station completeness. Every affected date was "present" (19
stations), so a plain `--start/--end` run printed *"No missing dates to backfill.
All done!"* and exited. The fix: `--force` (idempotent `INSERT OR REPLACE` on the
PK) which re-ingests every date in range.

## Run
```
.venv-gefs/bin/python scripts/ecds_sequential_backfill.py \
    --start 2026-09-02 --end 2026-09-29 --batch-days 5 --force
```
(2026-09-01 already re-ingested during the ECDS connectivity probe.)

- Write target: `data/tigge_archive.db` only (no live system reads it).
- Per-batch GRIB cleanup runs inside the script (proven; the 124 MB orphan was
  pre-existing per the brief).
- ECDS worst case: rate-limit → shards retry with backoff (attempt N/3).

## Verification (on completion)
```bash
.venv-gefs/bin/python scripts/verify_tigge_rebackfill.py \
    --before data/tigge_repair/before_counts_20261005.json
```
Before-counts captured at `data/tigge_repair/before_counts_20261005.json`
(20 stations, total **101,130**). Post-run KATL should reach ~5,070 rows and the
2026-09-02…09-29 dates should show 100 rows each. Per-station row counts reported
below on completion.

## Status
Running (batch 3/6 as of this receipt; 10/28 dates, ~109 dates/hour). Row counts
per station to be appended when the run finishes.
