# METAR max_temp_f Parser Root-Cause Fix

**Date:** 2026-09-17
**Author:** Gilfoyle (subagent — root-cause fix)
**Scope:** Task — root-cause and fix the `daily_stats.max_temp_f` / `avg_temp_f`
corruption. Option A (fix parser first, then regenerate).
**Status:** COMPLETE (writer fixed; no data mutated; staged for Dan's review)

---

## 1. Root cause (exact)

**File:** `scripts/metar_collect_live.py`
**Function:** `parse_metar()`, the temperature/dewpoint token matcher
(plus `_parse_temp_c()`).

The temperature/dewpoint branch matched **any** token containing a `/`:

```python
if '/' in token and len(token) >= 3:
    parts = token.split('/')
    if len(parts) == 2:
        temp_c = _parse_temp_c(parts[0])
        ...
        temp_f = temp_c * 9/5 + 32
```

A METAR **peak-wind** token — `PK WND 36034/2032` (wind direction/speed + time)
— also contains a `/`. The parser split it into `['36034', '2032']` and called
`_parse_temp_c('36034')`, which returned `36034.0` (a valid float). That was
then treated as Celsius and converted:

```
36034.0 °C × 9/5 + 32 = 64893.2 °F
```

Because `PK WND` appears **after** the real temperature group in the raw string,
this garbage value **overwrote** the correct `temp_f` (e.g. `29/26` → 84.2 °F
was overwritten by 64893.2 °F). The corrupt `temp_f` then flowed through
`update_daily_stats()`'s `MAX(temp_f)` / `AVG(temp_f)` SQL into
`daily_stats.max_temp_f` / `avg_temp_f`, and onward into `settlement_epochs`
HIGH buckets.

**Corroboration against live corrupt rows** (all `temp_f` values map exactly to
the `PK WND DDDSSS/HHMM` wind token's `DDDSSS` field):

| Raw token | `DDDSSS` | `temp_f` stored | `DDDSSS × 1.8 + 32` |
|---|---|---|---|
| `PK WND 36034/2032` | 36034 | 64893.2 | 64893.2 ✓ |
| `PK WND 36032/0049` | 36032 | 64889.6 | 64889.6 ✓ |
| `PK WND 36030/1934` | 36030 | 64886.0 | 64886.0 ✓ |
| `PK WND 36029/1936` | 36029 | 64884.2 | 64884.2 ✓ |
| `PK WND 36028/0900` | 36028 | 64882.4 | 64882.4 ✓ |
| `PK WND 36027/1432` | 36027 | 64880.6 | 64880.6 ✓ |

This also explains the `temp*1000+day` garbage seen in `settlement_bucket`
(S5): the `DDDSSS` wind value is `360` + `speed`, so the magnitude is ~36000,
and day-of-month suffixes appear because `DDDSSS` varies with the report time
(`2032`, `0049`, `1934`, ... are the `HHMM` wind-peak time — coincidentally
readable as day-like digits).

## 2. The fix

Three changes in `scripts/metar_collect_live.py`:

1. **Narrow the temp/dewpoint matcher** — added `_is_temp_dewpoint_token()`,
   which only matches genuine `[M]DD/[M]DD` temperature groups and explicitly
   rejects peak-wind (`36034/2032`), runway-visual-range (`R01/5000VP6000FT`),
   and fractional-visibility (`1/2SM`) tokens.

2. **Sanity guard at parse time** — `temp_f` / `dewpoint_f` outside
   `[-60, 150]°F` are set to `None` (rejected) rather than stored.

3. **Sanity guard at write time** — `save_observation()` refuses to persist any
   `temp_f` outside `[-60, 150]°F` (defense in depth).

## 3. Verification

Regression test added: `tests/test_metar_temp_parser_fix.py` (6 tests, all pass).

Re-parse of known-bad production rows (before → after):

| Station | Raw temp group | Before (stored) | After (fixed) |
|---|---|---|---|
| KMSY | `29/26` | 64893.2 | **84.2** |
| KDCA | `21/19` | 64893.2 | **69.8** |
| KDEN | `15/04` | 64893.2 | **59.0** |
| KMSY | `26/23` | 64889.6 | **78.8** |
| KPHX | `34/18` | 64880.6 | **93.2** |
| KDEN | `14/M01` | 64882.4 | **57.2** |

The `-60..150°F` guard also ensures that even if a future false-positive token
slips past the narrow matcher, it is rejected before any write.

## 4. Non-mutation confirmation

- **No DB or data was mutated.** All DB access was read-only
  (`sqlite3` URI `mode=ro`) against `data/metar_backfill.db`.
- `settlement_epochs` was **not** rewritten. `daily_stats` was **not** touched.
- The daemon was **not** restarted. No backfill was run.
- Files changed (staged for Dan's review, **not committed**):
  - `scripts/metar_collect_live.py` (the fix)
  - `tests/test_metar_temp_parser_fix.py` (new)
  - `docs/plans/METAR-MAXTEMP-PARSER-FIX-2026-09-17.md` (this receipt)

## 5. Next steps (NOT executed — require Dan's approval)

1. Re-run `generate_settlement_epochs.py` (or the corrected HIGH-bucket
   generator reading Kalshi `kalshi_temp`) to regenerate the 616 corrupt HIGH
   epochs from a clean source. See
   `SETTLEMENT-EPOCHS-RECOVERY-2026-09-17.md` §3.
2. Existing corrupt `metar_observations.temp_f` rows and `daily_stats` rows are
   **not** fixed by this writer-only change — they must be backfilled/cleaned
   separately (read-time sanity band already guards consumers).
