# Kalshi truth-table date-offset repair — 623 rows fixed

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Repair script:** `scripts/fix_kalshi_settlement_dates_2026-10-15.py`
**Artifacts:** `attention/kalshi_datefix_2026-10-15.json`, `artifacts/brier-datefix-impact-2026-10-15.json`
**Approved by:** Dan (2026-10-15)

## What was wrong

`data/kalshi_settlements.db` held **623 rows** whose `target_date` was one day
**later** than the contract's own `-YYMMMDD` ticker date. All 623 were
`source_type='historical_api'` (the Jan–May 2026 backfill); every other row
(10,903 HIGH + 787 LOW) was already correct.

Joined on `target_date`, those rows land on the wrong station-day and inject a
~7°F error into any truth join. The defect was found by the truth-switch analysis
(commit `1da1e4ea0`).

## Second defect found during the repair — the unique index was stale

The repair could not be applied as planned: `kalshi_settlements` carried

```
uq_kalshi_settlements_station_date   UNIQUE(station, target_date)
```

created by `scripts/dedup_kalshi_settlements.py` on **2026-09-28**, when the
table held HIGH series only. The LOW series (`KXLOWT…`, 787 rows) was added
**later**, so `(station, target_date)` is no longer a valid key: shifting a
mis-dated HIGH row back one day can legitimately land on the same
`(station, date)` as a LOW row. There are exactly **3** such legitimate
HIGH/LOW pairs:

| station | date | HIGH row | LOW row |
|---|---|---|---|
| KLAX | 2026-01-13 | id 734 (KXHIGHLAX, 79.0) | id 12763 (KXLOWTLAX, NULL) |
| KNYC | 2026-01-13 | id 869 (KXHIGHNY, 48.0) | id 13505 (KXLOWTNYC, NULL) |
| KSEA | 2026-04-16 | id 1222 (KXHIGHTSEA, 56.0) | id 14284 (KXLOWTSEA, NULL) |

The correct key is **`(station, series, target_date)`**, which has **zero**
collisions both before and after the repair. So the fix also had to replace the
stale index.

## What was done

Single transaction, private backup first (`data/_backup_pre_datefix_20261015T012634Z.db`,
md5 `620661216db07a1637fb9c487dbcbd15`, byte-identical to the pre-fix DB):

1. Dropped the stale `uq_kalshi_settlements_station_date (station, target_date)`.
2. `UPDATE … SET target_date = <ticker date>` for the 623 mismatched rows.
3. Created `uq_kalshi_settlements_station_series_date UNIQUE(station, series, target_date)`.

Idempotent: a re-run reports `need_fix=0`, `still_mismatched=0`.

## Verification

| check | result |
|---|---|
| rows total | 11,690 (unchanged) |
| rows fixed | 623 (all `historical_api`) |
| rows where ticker-date ≠ target_date | **0** |
| `(station, series, target_date)` duplicates | 0 |
| `PRAGMA integrity_check` | ok |
| `PRAGMA foreign_key_check` errors | 0 |

## Impact on om_gfs (the spurious penalty, removed)

Reproduced the honest axis (`T = floor(running_high@13:30)+1`,
`y = 1{settled_high > T}`) and scored om_gfs (`bc_ps`) under the **naive**
`(station, target_date)` join against the pre-fix backup and the post-fix DB:

| DB | n | Brier | dir acc | base rate |
|---|---:|---:|---:|---:|
| pre-fix (naive join) | 2700 | **0.1215** | 0.8326 | 0.7663 |
| post-fix (naive join) | 2695 | **0.1031** | 0.8545 | 0.7788 |

**Δ = −0.0184 Brier**, direction +2.2 pts. This is the spurious penalty the
mis-dated rows imposed on the naive join — slightly larger than the −0.008…0.016
the truth-switch analysis estimated. The repaired table now carries the correct
truth on all 623 rows.

## Note for the register

Same class as P4: *metrics are real only after the joins are verified.* The
number moved; the arithmetic here is the post-fix one.
