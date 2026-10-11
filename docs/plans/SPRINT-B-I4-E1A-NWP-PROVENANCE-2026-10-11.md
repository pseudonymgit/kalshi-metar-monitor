# Sprint B (I4 + E1-a) — NWP run provenance + HRRR mislabel fix

Date: 2026-10-11
Repo: `prototypes/weather-engine-source/`
Branch: `main`

## The hole (verified before fix)

`data/nwp_forecasts.db` table `nwp_forecasts` columns were:

```
[id, fetch_date, target_date, station, model, variable, value, fetch_timestamp]
```

`fetch_date` is a DATE and `fetch_timestamp` the retrieval instant, but there is
**no `run_init_utc`** — nothing records *which model cycle* a row came from. So a
backtest cannot prove which run was visible at decision time: lookahead by
construction, the same class as the A6 provisional-truth bug, in the NWP layer.

A separate probe also found that the module `core/observational_asof_gate.py`
docstring references a `core/nwp_lookahead_gate.py` that **does not exist on this
branch** (it lives only on the divergent `temp-sprint-branch`, commits
`6f3c1394c` / `a5fae98bb`, neither an ancestor of `main`). The claimed NWP
protection was therefore absent here.

`nwp_forecasts` row count: 1,164,012.

## Task 1 — run provenance (commit 1)

New seam `core/nwp_provenance.py`:

* Columns `run_init_utc` (model cycle instant) and `fetched_at` (retrieval
  instant), added by in-place `ALTER TABLE ADD COLUMN` — no row rewrite.
* `ensure_provenance_columns()` — idempotent migration, safe on the 400MB table.
* `backfill_fetched_at()` — fills `fetched_at` from the legacy `fetch_timestamp`
  column. **Ran on a copy: 1,164,012 / 1,164,012 rows backfilled.**
* `backfill_run_init_utc_derived()` — OPT-IN, dry-run capable derivation of a
  vintage from the fetch time (fails closed: can only hide data, never expose it
  early). Left off by default so unattributed rows stay honestly NULL.
* `run_init_utc_from_cycle()` — writer helper: floors fetch time to the last
  global cycle (00/06/12/18Z).
* As-of accessor `query_asof(conn, decision_instant, …)` + pure predicate
  `is_provenance_visible()`:
  - excludes runs whose `run_init_utc > decision instant` (lookahead);
  - excludes NULL-vintage rows (unknown vintage is not evidence);
  - `strict=True` default requires both columns present and `<= decision`;
    `strict=False` admits legacy rows carrying only `fetched_at`;
  - `require_provenance=True` **raises `ProvenanceMissingError`** when the
    provenance columns are absent, rather than silently narrowing.

Writers wired to populate provenance at write time:
`scripts/nwp_collect.py` (GFS/ECMWF/ICON/GEM daily + fetch log),
`scripts/gefs_ensemble_collect.py` (GEFS 31-member),
`core/signals/temperature_advection_signal.py` (`store_advection`).

### Schema before → after

```
BEFORE: [id, fetch_date, target_date, station, model, variable, value, fetch_timestamp]
AFTER : [id, fetch_date, target_date, station, model, variable, value, fetch_timestamp,
         run_init_utc, fetched_at]
```

### As-of accessor refusing a post-decision run (on a copy)

```
AS-OF @09Z -> [('temperature_2m_max', 50.0, '2026-10-11T06:00:00+00:00')]
PASS: 12Z post-decision run EXCLUDED at 09Z decision instant.
AS-OF @13Z -> [('temperature_2m_max', '2026-10-11T06:00:00+00:00'),
               ('temperature_2m_min', '2026-10-11T12:00:00+00:00')]
PASS: both runs visible at 13Z.
```

Known caveat (documented, not hidden): `INSERT OR REPLACE` under the
`UNIQUE(fetch_date,target_date,station,model,variable)` key means one row per key
— a same-`fetch_date` re-fetch from a later cycle **overwrites** the earlier run
on that key. Provenance fixes *attributability/visibility* of whatever row is
present; it does not by itself retain superseded runs. Retaining multiple runs
per key would require a key change (out of scope for I4).

## Task 2 — HRRR mislabel (E1-a, commit 2)

`core/signals/hrrr_bias_corrected_signal.py` called Open-Meteo `/v1/forecast`
**with no `models` parameter**, so the API served its `best_match` blend while
the module/class/DB all claimed "HRRR".

Fix (option **a**, pin the real model): `models=ncep_hrrr_conus` is now sent
(verified live 2026-10-11 — the bare `hrrr` string is rejected by the API;
`ncep_hrrr_conus` returns data). HRRR is CONUS-only, so a `us_location()` guard
plus an explicit API-error check make an out-of-domain request **fail closed**
(return `None`) instead of silently substituting a non-HRRR model.

```
HRRR_MODEL_ID = ncep_hrrr_conus
models= pinned in URL builder: True
US fetch OK: True n= 72
HRRR is CONUS-only; 51.500,-0.120 is outside the domain — refusing to substitute a non-HRRR model.
Non-US fetch (fail-closed): None
```

## Verify

* Schema shown before/after; as-of accessor refuses a post-decision run.
* Signal model parameter pinned to `ncep_hrrr_conus` after the fix.
* Full suite: **1566 tests collected, 0 failed** (exit 0).
* New `tests/test_nwp_provenance.py` (8 cases) + existing
  `tests/test_signals/test_hrrr_bias_corrected.py` (9) — 17 passed.
* Historical P&L / trades untouched. All schema experiments ran on `/tmp` copies.
