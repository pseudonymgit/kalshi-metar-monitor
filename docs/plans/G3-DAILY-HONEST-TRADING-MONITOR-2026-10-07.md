# G3 — Daily Honest Trading Monitor (read-only)

**Sprint G** · **Item:** G3 (ongoing) · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Status:** DONE (monitoring only — no live behavior changed)

---

## What was asked

With pricing, labels, and the ledger clean, let honest trading run. Monitor the first days of
post-wipe trading: total post-wipe trades, price provenance (all should be MATCH, no
STALE_WALK), and P&L trajectory. **No live system changes — monitoring only.**

## Snapshot (2026-10-07 15:0x UTC)

**Ledger:** 22 trades total — 21 `closed`, 1 `open`.

| Date | Trades | Wins | Losses | Realized P&L |
|---|---:|---:|---:|---:|
| 2026-10-02 | 5 | 3 | 2 | +$861.90 |
| 2026-10-03 | 7 | 2 | 5 | +$1,871.88 |
| 2026-10-04 | 4 | 0 | 4 | −$149.77 |
| 2026-10-05 | 3 | 0 | 3 | −$29.97 |
| 2026-10-06 | 2 | 1 | 1 | +$146.97 |
| **Cumulative** | **21** | **6** | **15** | **+$2,701.01** |

Open: id 179 (KOKC HIGH 2026-10-07, qty 714 @ 0.035).

**Price provenance** — `scripts/verify_price_provenance.py`:

```
trades scanned: 22
  MATCH       : 22
  STALE_WALK  : 0
  MISMATCH    : 0
POST-FIX cohort (created_at >= 2026-10-05T22:59:07Z): n=3
  MATCH: 3  STALE_WALK: 0  MISMATCH: 0  price_source NULL: 0
RESULT: all trades priced within their decision-time snapshots.
```

- **0 STALE_WALK, 0 MISMATCH, 0 NO_SNAPSHOT.** The E1 price-integrity fix holds.
- The 3 **post-fix** trades (ids 177, 178, 179 — 10-06/10-07) carry an explicit
  `price_source = kalshi_market_snapshots.db:market_snapshots.mid_price@…|mid=…`. The 19 older
  rows have `price_source=NULL` (pre-E1) but still verify as MATCH.

## Reading

- Post-fix path works end-to-end: 2 closed post-fix trades (177 −$25.03, 178 +$171.99) plus 1 open.
- Cumulative +$2,701 is **not** a clean edge signal — 8 of those dollars come from one large
  low-price KBOS/KPHL-style win, and 6 wins / 15 losses is a sub-50% hit rate on this tiny book.
  Consistent with the G1 finding (thin, not-yet-significant edge) and the G2 label caveat.
- **Do not draw edge conclusions from 22 trades.**

## Actions

**None.** Monitoring only, per the order. No daemon restart, no calibration/gamma/trajectory/cron change.

## Files

| path | change |
|---|---|
| `docs/plans/G3-DAILY-HONEST-TRADING-MONITOR-2026-10-07.md` | this snapshot |
