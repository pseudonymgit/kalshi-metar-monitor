# E2 — Market Snapshot Backfill: RECEIPT (2026-10-05)

**Sprint E** · **Item:** E2 (P1) · **Owner:** Ray Donovan · committed, pending review
**Repo:** `prototypes/weather-engine-source/`

---

## The gap
`data/kalshi_market_snapshots.db::market_snapshots` spans **2026-09-07 → 2026-10-05**
(29 days, 26 distinct days present). The B2 real-price sweep needs a **210-day**
window (starts **2026-03-09**). Gap = **181 missing days**.

## Backfill strategy — one-shot historical pull, from the Kalshi API
Source of record: Kalshi public API `candlesticks`
(`/series/{series}/markets/{ticker}/candlesticks`), which returns per-interval
OHLC + `yes_bid`/`yes_ask` **even for settled markets**. Reconstruction maps each
candle `end_period_ts` → `snapshot_time`, `yes_bid/ask.close_dollars` → bid/ask,
`mid_price = (bid+ask)/2` (identical to the live z8 collector's definition).

New script: `scripts/market_snapshot_backfill.py`.
- Enumerates settled markets via `/markets` (client-side `close_time` window
  filter — `/historical/markets` **ignores** `min/max_close_ts`, verified).
- Idempotent: `DELETE` + `INSERT` per (market_ticker, snapshot_time).
- Backfilled rows tagged `ltv='E2BACKFILL'` for provenance separation from live
  z8 snapshots.

## CRITICAL finding: Kalshi's API history is bounded to ~90 days
Candlesticks **404 for markets older than ~2026-08-01** (verified 2026-10-05):

| month tried | candles |
|---|---|
| 2026-05 | 404 (0) |
| 2026-06 | 404 (0) |
| 2026-07 | 404 (0) |
| 2026-08-04 | OK (3 daily) |
| 2026-09-07 | OK |

`/historical/markets` lists old **tickers** back to at least 2022, but the
**price/candlestick** endpoint does not serve them. So the reachable window is
**~2026-08-01 → 2026-09-06 (~37 days)** via API. The remaining ~144 days
(2026-03-09 → 2026-07-31) are **not recoverable from the public Kalshi API**.

## Days available: before → after
| | days | window |
|---|---|---|
| before | 29 | 2026-09-07 → 2026-10-05 |
| after (projected) | ~66 | 2026-08-01 → 2026-10-05 |

**This does NOT reach 210 days.** The residual 144-day gap needs a different
source (Kalshi orderbook/trades archive, a data vendor, or an official Kalshi
bulk-history export). Flagged for Gilfoyle/Dan — do not assume E2 alone unblocks
the 210-day B2 sweep.

## Verification (5-minute)
```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python -c "
import sqlite3
c=sqlite3.connect('file:data/kalshi_market_snapshots.db?mode=ro',uri=True)
print('distinct days:', c.execute('select count(distinct substr(snapshot_time,1,10)) from market_snapshots').fetchone()[0])
print('E2BACKFILL rows:', c.execute(\"select count(*) from market_snapshots where ltv='E2BACKFILL'\").fetchone()[0])
print('min date:', c.execute('select min(substr(snapshot_time,1,10)) from market_snapshots').fetchone()[0])
"
```

## Proof points
- Dry-run: KOKC 2026-09-01..09-03 → 18 markets, 430 candle rows.
- Real write: KOKC 2026-09-01..09-03 → **430 rows persisted** (`ltv=E2BACKFILL`),
  e.g. `2026-09-01T00:00:00 KXHIGHTOKC-26SEP01-T97 bid 0.03 ask 0.04 mid 0.035`.
- KATL dry-run 2026-09-01..09-06 → 36 markets, 1110 rows.

## Run
```
.venv-gefs/bin/python -u scripts/market_snapshot_backfill.py \
    --start 2026-08-01 --end 2026-09-06 --interval 60
```
(Do NOT pass `--include-historical` for this window — `/markets` already covers
it, and the historical endpoint's ignored date filter caused an unbounded crawl.)

## Status
Run in progress (background). Row totals to be appended on completion.
