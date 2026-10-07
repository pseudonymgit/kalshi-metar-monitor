# G4 — Historical Fills Probe (one shot)

**Sprint G** · **Item:** G4 (P2) · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Status:** DONE — endpoint works, but returns a different axis (fills, not mid-prices)

---

## What was asked

Probe the Kalshi `/historical/trades` endpoint for a few known old markets (pre-July-31) to
confirm whether it returns useful **price** data (bid/ask mid-prices) or just **trade prints**
(fills). If it works, scope the backfill. If it 404s / returns junk, formally document it as
B2's 144-day gap acceptance.

## Live probe (2026-10-07, `https://api.elections.kalshi.com/trade-api/v2`)

| ticker | status | prints | oldest `created_time` |
|---|---|---|---|
| `KXHIGHTBOS-26MAY31-T73` | **200** | 5 (+cursor) | 2026-06-01 |
| `KXHIGHTBOS-26FEB20-T43` | **200** | 5 (+cursor) | 2026-02-20 |
| `KXHIGHNY-26FEB20-T46` | **200** | 5 (+cursor) | 2026-02-21 |
| `KXHIGHTATL-26FEB20-T80` | **200** | 5 (+cursor) | 2026-02-21 |

**Reach confirmed ≥ 2026-02-20** for `KXHIGH*` tickers, paginated via `cursor`.

**Response shape** (per print):
`count_fp, created_time, is_block_trade, no_price_dollars, taker_book_side, taker_outcome_side,
taker_side, ticker, trade_id, yes_price_dollars`
(e.g. `yes_price_dollars=0.0100`, `no_price_dollars=0.9900`, `count_fp=11.00`, `taker_side=yes`).

Confirmed: pre-boundary **candlesticks still 404** (`KXHIGHTBOS-26JUL29-T75` and
`KXHIGHTBOS-26JUL30-T75` → `{"error":{"code":"not_found"}}`). The candlestick path stays bounded.

## Verdict — different axis (fills, not mid-prices)

The endpoint **works** but does **not** supply a mid-price series:

1. Prints are **taker fills** (`yes_price_dollars`/`no_price_dollars`), not bid/ask **mid**
   snapshots. The live collector defines `mid_price = (yes_bid+yes_ask)/2`; the prints carry
   **no bid/ask at all**.
2. **Sparse** — a print exists only when a trade executes; no continuous series.
3. **Spread unobservable** — you get the executed price, not the book.
4. Candlesticks remain bounded → the `mid_price` series cannot be reconstructed from Kalshi pre-2026-07-31.

## Scoping recommendation (proposed, Dan/Gilfoyle call)

Build a **separate** store `data/kalshi_historical_trade_prints.db` with `provenance=trade_print`
rows `(ticker, trade_id, created_time, yes_price, no_price, size, taker_side)`.
**Do not merge into `market_snapshots.mid_price`** — keeps the axis distinction intact.
B2 may then treat **last-trade price** as an *alternate* price source for the missing 144 days,
clearly labelled *not-mid*. Until Dan/Gilfoyle accept a trade-print axis, **B2 stays scoped to 67 days.**

## Formal gap acceptance

This confirms the F3 / register §B2 gap acceptance: the candlestick mid-price source is bounded
at **2026-07-31**; the pre-boundary window is reachable only as **trade prints**, a different axis.

## Files

| path | change |
|---|---|
| `docs/plans/G4-HISTORICAL-FILLS-PROBE-2026-10-07.md` | this probe + scoping recommendation |
