# Ingestion Coverage Manifest

**Date:** 2026-09-17 10:24 UTC
**Author:** Ray Donovan (subagent — read-only audit)
**Scope:** Task C — every data source, expected cadence, current last-write, PASS/STALE/MISSING verdict.

---

## Summary

| Verdict | Count |
|---|---|
| PASS | 7 |
| STALE | 2 |
| MISSING / BROKEN | 2 |

**Critical finding:** the daily LOOP **producer** is missing (rebuilt — see Task A),
and `daily_stats.max_temp_f`/`avg_temp_f` are **corrupt** (see Task B). Two ingestion
paths are stale (GEFS archive, ECMWF bias) and the `WE-auth-verification` +
`weekly-backup` crons are in `error` state.

---

## Per-source detail

### 1. METAR backfill (`metar_backfill.db`) — **PASS** (with caveat)
- **Cadence:** continuous (live collector + daily backfill).
- **Last write:** 2026-09-17 10:23 (live).
- **Table:** `metar_observations` 12.8M rows through 2026-09-17; `daily_stats` fresh through 2026-09-17.
- **Verdict: PASS** for ingestion cadence; **data-quality caveat** — `max_temp_f`/`avg_temp_f` corrupt since ~2026-09-15 (see Task B). `min_temp_f` clean.

### 2. NWP forecasts (`nwp_forecasts.db`) — **PASS**
- **Cadence:** daily (cron `NWP Forecast Collection` `0 6 * * *` UTC).
- **Last write:** 2026-09-17 06:45.
- **Coverage:** GFS/ECMWF/ICON/GEM target_date through 2026-09-23 (forward forecasts present). 20 stations.
- **Verdict: PASS.**

### 3. Kalshi settlements (`kalshi_settlements.db`) — **PASS**
- **Cadence:** daily (cron `kalshi-settlement-upkeep` `0 8 * * *` UTC).
- **Last write:** 2026-09-17 08:00.
- **Coverage:** 13,755 rows (1,650 finalized), 21 stations, 2021-08-06 → 2026-09-15.
- **Verdict: PASS** — authoritative clean settlement source.

### 4. Kalshi market snapshots (`kalshi_market_snapshots.db`) — **PASS**
- **Cadence:** hourly (cron `z8-kalshi-market-cron` `30 * * * *` UTC).
- **Last write:** 2026-09-17 09:31.
- **Verdict: PASS.**

### 5. ASOS / IEM (`iem_asos_1min.db`) — **PASS**
- **Cadence:** 1-min (cron `z5-asos-1min-cron` `40 * * * *` UTC) + daily backfill.
- **Last write:** 2026-09-17 09:40.
- **Note:** `iem_asos.db` (static archive) last written 2026-09-02 — expected (archival).
- **Verdict: PASS** (live 1-min feed active).

### 6. WeatherAPI (`weatherapi_archive.db`) — **PASS**
- **Cadence:** daily (cron `WE-weatherapi-daily-refresh` `0 6 * * *` UTC, 20 stations).
- **Last write:** 2026-09-17 06:00.
- **Verdict: PASS.**

### 7. Settlement epochs (`settlement_epochs` in metar_backfill.db) — **PASS** (with caveat)
- **Cadence:** daily (cron `WE-settlement-epochs-regenerate` `0 4 * * *` UTC).
- **Last write:** current through 2026-09-17.
- **Verdict: PASS** for cadence; **data-quality caveat** — 625 corrupt buckets (see Task B).

### 8. GEFS archive (`gefs_archive.db`) — **STALE**
- **Cadence:** daily (cron `WE-GEFS-daily-backfill` `0 3 * * *` UTC).
- **Last write:** 2026-09-15 03:48.
- **Verdict: STALE** — 2 days behind expected daily cadence. Cron still `ok` status but output lags.

### 9. ECMWF bias (`ecmwf_bias.db`) — **STALE**
- **Cadence:** tied to NWP/tigge daily updates (cron `ecmwf-tigge-daily-update` `0 7 * * *` UTC).
- **Last write:** 2026-09-16 16:08.
- **Verdict: STALE** — ~1 day behind; borderline.

### 10. Daily LOOP producer (`daily_loop_output.json`) — **MISSING** (now REBUILT)
- **Cadence:** daily, feeding `intraday_bridge.py` at 11:00 UTC.
- **Prior state:** producer script lost in wipe; output stale since 2026-09-06 (247h).
- **Cron:** `WE-intraday-bridge-daily` `0 11 * * *` ET — in **`error`** status (fail-closed on missing/stale producer).
- **Verdict: was MISSING → REBUILT** (see Task A, `scripts/daily_loop_producer.py`). Bridge cron will recover once producer runs on schedule.

### 11. Kalshi fee verify / auth — **BROKEN**
- **Cron:** `WE-auth-verification` `0 * * * *` UTC — **`error`** status.
- **Cron:** `weather-engine-weekly-backup` `0 3 * * 0` — **`error`** status.
- **Verdict: MISSING/BROKEN** — two crons in error state need triage (out of scope for this recovery; flagged for Ray/Gilfoyle).

---

## Sources *supposed* to be pulling but flagged

1. **Daily LOOP producer** — was silently dead since Sep 6 (fail-closed bridge was hiding it). **Rebuilt.**
2. **GEFS archive** — 2 days stale; cron running but not keeping up. Investigate backfill.
3. **ECMWF bias** — ~1 day stale; likely cascading from tigge/GEFS lag.
4. **`WE-auth-verification` + weekly backup crons** — in `error`, need ownership/triage.

---

## Notes / caveats

- `iem_asos.db` (2026-09-02) and `ghcn_settlements.db` (2026-09-14) are **archival**
  snapshots, not live feeds — their staleness is expected, not a defect.
- `gefs_refresh_receipt.json` and `gefs_backfill_checkpoint.json` are from 2026-09-14,
  consistent with the GEFS archive lag.
- This is a **read-only** audit. No files or DBs were modified.
