# D-Track Dispositions — CONFIRMED (2026-10-04)

**Owner (ruling):** Bertram Gilfoyle (CTIO) · **Requested by:** Sprint C, Item 1 (P2, this week)
**Repo:** `prototypes/weather-engine-source/` · **Mode:** read-only disposition, no mutation
**Supersedes:** the `READY` rows in the roadmap control register (reconciliation lag) and
re-confirms `docs/plans/WEATHER-ENGINE-D-TRACK-DISPOSITIONS-2026-09-14.md` +
`D1-disposition-2026-09-16.md` with fresh evidence.

## Rulings (one line each)

| Item | Ruling | One-line rationale |
|---|---|---|
| **GHCN** | **RETAIN canonical — CONFIRMED** | `data/ghcn_settlements.db` = 619,827 rows across 20 stations, current (mtime 2026-10-04 06:30); REBUILD leg long complete (S6). No change. |
| **WeatherAPI** | **KEEP ENABLED — no action** | `weatherapi_archive.db` (315 MB) = 2,406,727 hourly + 121,400 daily rows, current through 2026-09-16+; live consumer `core/additive/context.py` (cloud cover, imported by `scripts/intraday_runner.py`). Neither DISABLED nor ARCHIVE is correct. |
| **Series monitor** | **KILL (script absent) — residual work is a consumer fix, not a REPLACE of the monitor** | `scripts/kalshi_series_monitor.py` does not exist on disk or in git history (only a stale `.pyc`); series discovery now lives in `core/kalshi_monitor.py` + `z8_kalshi_market_cron.py`. Separate finding below: one phantom consumer reference to fix. |
| **z6 (METAR archive)** | **REDUNDANT — KEEP DISABLED (confirmed)** | No `z6*.py` source anywhere (only `.pyc`); live METAR via `metar_collect_live.py` + `metar_43_backfill.py`/`metar_gap_backfill.py`. |
| **z7 (NWP refresh)** | **REDUNDANT — KEEP DISABLED (confirmed)** | No `z7*.py` source anywhere (only `.pyc`); `nwp_collect.py` is the surviving daily NWP writer (fresh: max fetch 2026-10-04T18:00Z). |

## Evidence (verified 2026-10-04)

- GHCN: `sqlite3 data/ghcn_settlements.db` → table `ghcn_settlements`, 619,827 rows.
- WeatherAPI: table `hourly` 2,406,727 / `daily` 121,400 / `checkpoint` 20.
- z6/z7/series: `find` + `git log --all` find no tracked source file for any of the three;
  only compiled `.pyc` caches remain.
- NWP: `data/nwp_forecasts.db` max `fetch_timestamp` = 2026-10-04T18:00:01Z (healthy).

## Separate finding surfaced during this disposition (NOT a D-track item)

**Phantom consumer — `weatherapi_archive_hourly` (F5-class schema drift).**
`core/additive/context.py` (`_load_cloud`, ~L302; and ~L413) queries
`FROM weatherapi_archive_hourly`, but the DB contains only a table named `hourly`
(columns include `cloud`). No view/alias named `weatherapi_archive_hourly` exists.
The query is wrapped in `except (sqlite3.OperationalError, FileNotFoundError): pass`,
so the cloud-cover load has been **silently returning zero rows** — the exact silent-zero-row
class the Data Integrity Register calls F5.

- **Impact:** WeatherAPI's cloud-cover contribution to additive context is not actually
  being applied (silent). Does not affect the "retain, don't kill" ruling — the archive
  is valid and still needed — but the consumer needs a one-line table-name fix.
- **Recommendation:** route to Ray (1-line fix: `hourly` not `weatherapi_archive_hourly`),
  plus a non-silent log on `OperationalError` so the next drift is loud.
- **Not decided here:** whether to keep the outer bare `pass` (fail-soft) or escalate to warn.

## Standing callout (register, Dan 2026-09-19)
The pre/post-wipe reconciliation already ruled the "disabled GHCN/WeatherAPI/series/z6/z7
paths" a red herring — they are keep-enabled/current, and the post-wipe delta was the
**axis correction**, not a recovery gap. These rulings are consistent with that verdict.

**Status:** per-item rulings produced; no mutation performed.
