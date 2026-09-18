# Settlement Epochs Recovery Receipt

**Date:** 2026-09-17
**Author:** Ray Donovan (subagent — recovery)
**Scope:** Task B — verify settlement epoch generation path, identify corruption, document exact recovery steps.
**Read-only.** No live data was overwritten. No DB was modified.

---

## 1. What `settlement_epochs` is and how it's generated

`settlement_epochs` is a **table** (not a separate DB) inside `data/metar_backfill.db`.
It is the ground-truth source for all backtesting: per-station, per-day settlement
buckets for the Kalshi HIGH and LOW temperature markets.

**Generator:** `scripts/generate_settlement_epochs.py` (intact, survives the wipe).

**Generation path (verified intact):**

```
daily_stats (per-station daily max/min/avg temp)
      │
      ▼
generate_settlement_epochs.py
   ─ HIGH bucket = get_temp_bucket(max_temp_f)   ← ROUNDS max_temp_f
   ─ LOW  bucket = get_temp_bucket(min_temp_f)
   ─ prior bucket = previous day's bucket
   ─ reversion = 1 if current > prior else 0
      │
      ▼
settlement_epochs table (UNIQUE(station, market_type, local_trading_date))
```

The generator is deterministic and correct **in structure**. The problem is its
**input**, not its logic.

---

## 2. What's recoverable (ground truth state)

### 2a. `settlement_epochs` table — present, but partially corrupt

| Metric | Value |
|---|---|
| Total epochs | 117,517 |
| HIGH epochs | 58,762 (2021-01-01 → 2026-09-17) |
| LOW epochs | 58,755 (2021-01-01 → 2026-09-17) |
| Distinct stations | 31 |
| **Corrupt rows** (bucket >150 or < -50) | **625** |
| — corrupt HIGH | 616 |
| — corrupt LOW | 9 |
| Corruption onset | **2026-07-07** |

### 2b. Root cause — `daily_stats` temperature corruption

`max_temp_f` and `avg_temp_f` in `daily_stats` contain garbage values (e.g.
`64893.2`, `9346.3`, `7357.6`) starting ~2026-09-15 (with a smaller earlier
wave around 2026-07-07). `min_temp_f` is **clean** (0 corrupt across all 58,703
rows). The garbage is consistent with a METAR raw-text parsing bug that
concatenates strings into the numeric column (e.g. `64.8` + `932` → `64893.2`).

**Consequence:** every HIGH settlement bucket derived from `max_temp_f` on a
corrupt day is wrong. 616 HIGH epochs are affected since 2026-07-07.

### 2c. Authoritative clean source — `kalshi_settlements.db`

`data/kalshi_settlements.db` (table `kalshi_settlements`) holds Kalshi's own
**finalized** settlement temperatures — the correct ground truth.

| Metric | Value |
|---|---|
| Total rows | 13,755 |
| `finalized` (live API) | 1,650 |
| `historical_api` | 12,105 |
| Distinct stations | 21 |
| Coverage | 2021-08-06 → 2026-09-15 |
| Schema | `kalshi_temp` = clean float (e.g. 73.0, 68.0, 101.0) |
| Last write | 2026-09-17 08:00 (cron `kalshi-settlement-upkeep`) |

This table is populated by `scripts/kalshi_settlement_upkeep.py` (cron
`0 8 * * *` UTC), which queries the Kalshi API for finalized events and is the
authoritative settlement source. `kalshi_temp` is clean.

---

## 3. Recovery steps (exact, in order)

> **Do NOT run these without Dan's approval.** They rewrite `settlement_epochs`,
> which is the backtesting ground truth. This receipt documents *how*, not a
> directive to execute.

1. **Confirm the corruption window.** Query:
   ```sql
   SELECT MIN(local_trading_date), MAX(local_trading_date), COUNT(*)
   FROM settlement_epochs
   WHERE settlement_bucket > 150 OR prior_settlement_bucket > 150;
   ```
   Expect: 625 rows, onset 2026-07-07.

2. **Build a clean temperature source.** Join `daily_stats.min_temp_f` (clean)
   for LOW markets, and for HIGH markets use `kalshi_settlements.kalshi_temp`
   where available (finalized), falling back to `min_temp_f` + a documented
   diurnal adjustment (or mark the day `epoch_status='unresolved'` if no clean
   source exists).

3. **Regenerate settlement buckets** with a corrected generator that:
   - reads `kalshi_temp` (not `max_temp_f`) for HIGH buckets;
   - reads `min_temp_f` (clean) for LOW buckets;
   - preserves `reversion_occurred` semantics (current > prior).

4. **Back up `metar_backfill.db`** before any rewrite:
   ```bash
   cp data/metar_backfill.db data/metar_backfill.db.pre-settlement-fix-$(date +%Y%m%d)
   ```

5. **Re-run** the corrected generator, then **verify** no bucket is outside
   [-60, 140] and `reversion_occurred` is consistent.

### What is NOT recoverable without further action
- The 616 HIGH epochs' original correct values are **not** in `daily_stats`
  (corrupted at source). They *are* recoverable from `kalshi_settlements.db`
  for the ~21 stations + dates that Kalshi finalized (1650 finalized rows).
  Any HIGH epoch with no `finalized` Kalshi row and a corrupt `max_temp_f` has
  **no clean ground truth available** and should be marked `unresolved` rather
  than fabricated.

---

## 4. Conclusion

- **Generator intact** — `generate_settlement_epochs.py` survives and is
  structurally correct.
- **Ground truth partially recoverable** — 117,517 epochs exist; 625 are corrupt
  due to a `daily_stats.max_temp_f` parsing bug, onset 2026-07-07.
- **Authoritative recovery source exists** — `kalshi_settlements.db`
  (`kalshi_temp`, finalized) is clean and covers the corruption window.
- **Fix is a data rewrite**, not a script rewrite. The generator needs a
  corrected temperature input (Kalshi `kalshi_temp` for HIGH, clean `min_temp_f`
  for LOW), plus a backfill of `unresolved` markers for any epoch with no clean
  source.
