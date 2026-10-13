# LOW Market Entry-Price Backfill — 2026-10-13

**Script:** `scripts/low_market_price_backfill_2026-10-13.py`  
**Machine artifact:** `artifacts/low-market-backfill/low_market_price_backfill_2026-10-13.json`  
**Series:** `artifacts/low-market-backfill/low_entry_price_series_2026-10-13.csv`  
**Status:** backfill only. Holdout is **unblocked**, NOT wired.

## Bottom line

The LOW entry-price universe was **17 station-days on 1 snapshot day(s)** as measured by the holdout design doc (via `market_snapshots.market_type='LOW'`). It is **178 station-days** once keyed on the `KXLOWT*` ticker prefix (the real LOW books are ingested as `market_type='binary'`). **84 of those carry canonical LOW settlement truth** — enough for a 30-trade holdout with +54 margin.

## Why the design doc saw one day

`market_snapshots.market_type` is a mislabeled *strike-shape* column: for KXLOWT rows it reads `HIGH` on T-strikes and `LOW` on B-strikes, so filtering on it fragments and mostly drops the LOW books. The `KXLOWT*` ticker prefix is the reliable key. (The 2026-09-07 rows are a one-off ingest pass that happened to stamp real `market_type` values.)

## Coverage: before → after

| metric | before | after |
|---|---:|---:|
| snapshot days | 1 | 10 |
| station-days priced at the pin | 17 | 178 |
| ...with LOW label | — | 104 |
| ...with LOW settlement truth | — | 84 |
| priced cells (station-day-ticker) | — | 848 |
| cells direct at the pin (≤30 min) | — | 316 |
| cells same-day fallback (nearest snapshot) | — | 532 |
| station-days with a near-money pin quote (mid∈[0.2,0.8]) | — | 37 |
| station-days with a near-money same-day quote | — | 87 |

Distance from each cell's snapshot to the 13:30 pin (minutes): median 220, p90 720, max 780.

**Entry-price caveat (measured, not assumed):** on the 2026-09-15→23 days the live watch lists are sparse at the pin — a handful of strikes per station-day, frequently tails (mid ≈ 0 or 1) rather than near-money. Only the station-days in the near-money row above give a *directly usable* 50/50-ish entry price; the rest need the ladder proxy below or a rank-2 strike, and the proxy is only trustworthy when near-money neighbours actually exist that day.

### By target date (station-days priced / with truth)

| target date | priced | with truth |
|---|---:|---:|
| 2026-09-07 | 11 | 11 |
| 2026-09-15 | 17 | 17 |
| 2026-09-16 | 18 | 18 |
| 2026-09-17 | 18 | 18 |
| 2026-09-18 | 20 | 20 |
| 2026-09-19 | 20 | 0 |
| 2026-09-20 | 20 | 0 |
| 2026-09-21 | 20 | 0 |
| 2026-09-22 | 20 | 0 |
| 2026-09-23 | 14 | 0 |

## Ladder proxy — honest error (leave-one-strike-out)

For each station-day, every quoted strike was dropped and re-derived from its neighbours (linear interpolation between the bracketing strikes; nearest-quote when outside the quoted range). Absolute error vs the direct quote:

| slice | n | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| overall | 843 | 0.3225 | 0.18 | 0.87 | 0.97 |
| interp (bracketed) | 480 | 0.3022 | 0.196 | 0.76 | 0.943 |
| extrap_near (one-sided) | 345 | 0.3544 | 0.145 | 0.92 | 0.97 |
| moneyness high(>0.8) | 132 | 0.7648 | 0.832 | 0.9375 | 0.97 |
| moneyness low(<0.2) | 574 | 0.2297 | 0.0675 | 0.759 | 0.97 |
| moneyness near(0.2-0.8) | 137 | 0.285 | 0.215 | 0.62 | 0.74 |

**Proxy read:** median absolute mid error **0.18** (mean 0.3225, p90 0.87) over 843 leave-one-out cells. Bracketed interpolation is better than one-sided extrapolation on the median, but both are **large relative to a traded price** (a 0.18–0.32 absolute mid error is ~18–32 cents). The proxy is therefore only acceptable where near-money neighbours actually exist (see the moneyness rows); the one-sided branch and the deep-tail cells should be flagged or dropped, not silently interpolated.

## What is now unblocked

- A **84-station-day** LOW entry-price series with canonical truth and labels — comfortably ≥30 for the pre-registered holdout.
- Entry price = YES mid at the nearest-to-13:30 station-local snapshot, same frozen quote filter and LOCAL date alignment as the HIGH benchmark.
- For any strike lacking a direct pin quote, the ladder proxy supplies a fill estimate with a documented error distribution (above).

## Explicit non-goals (still true)

- No `market_type='LOW'` booking path enabled; no gate/sizing/exposure change; no live-book write.
- The 30 holdout trades are NOT selected or frozen here — that is the next step, gated on this backfill being accepted.

## Reproduce

```bash
.venv-gefs/bin/python scripts/low_market_price_backfill_2026-10-13.py
```

