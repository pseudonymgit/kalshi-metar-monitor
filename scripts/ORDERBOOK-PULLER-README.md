# Kalshi Order-Book Puller (Predexon)

Pulls historical Kalshi order-book history for **weather markets only** and
collapses it to a compact per-contract bid/ask **spread** summary. Answers the
question: *"could we actually enter below fair value?"* — the trade VWAP alone
doesn't tell you whether a favorable price was ever resting on the book.

## Source of truth (verified 2026-09-20)

Kalshi's own API has **no historical orderbook** (historical partition covers
markets/candlesticks/trades/orders/positions, not orderbook; orderbook is
live-only). Third-party is required.

| Provider  | Coverage            | Cost  | Notes                                        |
|-----------|---------------------|-------|----------------------------------------------|
| Predexon  | Tick-level since Jan 2026 | Free tier (REST orderbook endpoints) | **Chosen** — free + unlimited orderbook history |
| Lychee    | Full launch→present | Paid  | Not needed for our weather-only scope         |

## Exact endpoint + auth

- **Endpoint** (active, use this):
  `GET https://api.predexon.com/v2/kalshi/orderbooks-subcent`
- **Legacy** (deprecated soon, rounds to whole cents → widens spreads):
  `GET /v2/kalshi/orderbooks`
- **Auth**: header `x-api-key: <key>` — free signup, no credit card, at
  `https://dashboard.predexon.com`.

### Query params (`/orderbooks-subcent`)

| Param            | Type    | Required | Notes                                   |
|------------------|---------|----------|------------------------------------------|
| `ticker`         | string  | yes      | Kalshi market ticker                     |
| `start_time`     | int     | yes      | epoch **milliseconds** UTC               |
| `end_time`       | int     | yes      | epoch **milliseconds** UTC               |
| `limit`          | int     | no       | 1–2000 (default 100)                    |
| `pagination_key` | string  | no       | base64 cursor for next page              |
| `sources`        | string  | no       | `spine` (default), `all`, or CSV list    |

### Response snapshot fields (per snapshot)

`ticker`, `timestamp` (ms), `yes_bids[]`, `yes_asks[]` (each `{price, size}` as
decimal-**strings**, e.g. `"36.50"`, `"196.52"`), `best_bid`, `best_ask`
(decimal-cent strings), `bid_depth`, `ask_depth`, `sequence`, `source`.

Pagination is cursor-based over `(timestamp, sequence)` via `pagination.has_more`
/ `pagination.pagination_key`.

## Free-tier access — confirmed YES

Orderbook-history endpoints are **"Free & Unlimited" on all plans** — they do
**not** count against the monthly request quota (per Predexon
`rate-limits.md`). The free tier is:
- `$0/month`, **1 req/sec**, 1,000 requests/month (quota **not** consumed by
  orderbook endpoints).
- Signup needs no credit card.

The **tick-level Parquet** firehose (`/v2/data/ticks?venue=kalshi`) is a
*separate, paid* path (`$80/GiB`, ~$5 minimum) and is **not** required for our
spread summary. We use the free REST `orderbooks-subcent` endpoint instead.

## What the script does

`scripts/pull_kalshi_orderbook.py`:

1. **Scope** = weather contracts only, read from `data/kalshi_trades.db`
   (`contract_trades`). 9,783 contracts have a valid trade window (155 have NULL
   timestamps and are skipped).
2. For each contract, streams orderbook snapshots within its
   `[first_trade_time, last_trade_time]` window (entry-relevant times).
3. **Aggregates** (no tick replay → no bloat): computes yes-side bid/ask
   `spread = best_ask - best_bid` per snapshot and stores only
   `spread_min / spread_p10 / spread_median / spread_p90 / spread_max`,
   `n_ticks`, `first_book_time`, `last_book_time`.
4. Writes one row per contract to a **new** `data/kalshi_orderbook.db`
   (table `contract_spreads`). Idempotent — `INSERT OR REPLACE` keyed on
   `ticker`, and already-completed tickers are skipped on re-run.
5. Respects the 1 req/sec free-tier limit with a margin + exponential backoff
   on `429`.

Output is a few MB (one row × ~9.8k contracts), not GB.

## Run it

```bash
export PREDEXON_API_KEY=<your-key>          # free signup: dashboard.predexon.com
python3 scripts/pull_kalshi_orderbook.py     # full scope
python3 scripts/pull_kalshi_orderbook.py --limit 50   # smoke-test first 50
python3 scripts/pull_kalshi_orderbook.py --dry-run    # resolve scope, no API call
```

## Blocker (what Dan must provide)

The puller is **built and committed** but **cannot run** until an API key is
supplied. There is **no key in the environment**. Dan must:

1. Sign up free at `https://dashboard.predexon.com` (no credit card).
2. Provide the resulting API key (set `PREDEXON_API_KEY`).

Without the key the script exits cleanly with instructions and writes nothing.
It never fakes data and never touches a paid endpoint.
