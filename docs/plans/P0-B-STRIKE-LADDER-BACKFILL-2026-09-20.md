# P0-B Strike-Ladder Backfill — Receipt & Verification

Date: 2026-09-20
Author: Gilfoyle (subagent)
Task: P0-B — finish the strike-ladder backfill (full historical strike ladder per station/day).
Deterministic, script-only, parallel, no AI in the loop.

## Objective

The prior `kalshi_historical_strike_backfill.py` (rev-2, 2026-09-18) backfilled the
**single** representative strike per `(station, market_type, local_trading_date)`,
collapsing each day's full ladder to the median `greater`/`less` rung. Only **54
`between` rungs survived** of ~14k strike rows — the ladder was effectively destroyed.

P0-B required the **full ladder** — every rung (`greater` threshold, `less`
threshold, and the 1-degree `between` buckets) — so bucket/rung edges and NO-side
entry can be scored, not just the single-strike HIGH/LOW crossing.

## What was done

New script: `scripts/kalshi_strike_ladder_backfill.py` (deterministic, public GET only).

- Fetches `/historical/markets` (deep, 2021→present) + `/markets` (recent tail) for
  all 40 alive series (20 stations × HIGH/LOW) via the canonical
  `scripts/kalshi_series_map.py` mapping.
- Stores **every rung** (not collapsed) into a NEW dedicated DB
  `data/kalshi_strike_ladder.db` (table `strike_ladder`), fully separate from the
  running orderbook pull (`kalshi_orderbook.db`) and the mispricing re-test
  (`kalshi_trades.db`).
- Rung schema: `(station, market_type, local_trading_date, strike_value,
  strike_type, floor_strike, cap_strike, settlement_value_dollars, result, ticker)`.
  One row per rung — aggregate, not raw orderbook (no bloat).

## Before / After strike coverage

| Metric | Before (single-strike) | After (full ladder) |
|---|---|---|
| Strike rows total | 14,305 | **85,846 rungs** |
| `greater` rungs | 10,139 | 14,290 |
| `less` rungs | 4,112 | 14,337 |
| `between` rungs | **54** | **57,219** |
| Date range | 2021-01-01..2026-09-19 (epochs) | 2022-12-22..2026-09-19 (settled markets) |
| Stations covered | 20 active (11 retired = 0) | 20 active (11 retired unreachable, same) |

Per-date ladder depth: 5–12 rungs/date (typically 6: 1 less + 4 between + 1 greater),
vs. 1 collapsed strike before.

## Completeness verification

Every `(station, market_type, local_trading_date)` that the single-strike backfill
populated is **also** covered by the ladder (ladder_dates == se_single_struck for
all 40 series — verified exact match). The ladder is a strict superset in *content*
(full rungs) while matching coverage in *date span*.

`between` bucket resolution: 45,661 `result='no'` + 11,355 `result='yes'`, **0 NULL**
— fully resolved for NO-side and bucket-edge scoring.

## Gap flags (expected, not bugs)

- **11 retired stations** (KBNA, KCLT, KDAL, KDTW, KJFK, KORD, KPDX, KSLC, KTPA,
  PHNL, TEST) remain at 0 strikes — their tickers no longer resolve on Kalshi.
  Unrecoverable.
- **Pre-launch dates**: each station's `settlement_epochs` spans 2021-01-01→present,
  but Kalshi listed each city's HIGH/LOW series at different launch dates (2 cities
  in 2021, most 2023–2026). The ladder only covers dates Kalshi actually listed.
  This is why `ladder_dates` (~167–1364) < `se_dates` (~2085). Not a gap — those
  days were never listed.

## Verdict

The full ladder is now **complete enough to score bucket/rung edges and NO-side
entry**. The 57,219 `between` (1-degree bucket) rungs carry authoritative
`settlement_value_dollars` + `result`, enabling bucket-level P vs VWAP scoring and
NO-side entry analysis that the single-strike backfill could not support.

## Safety / boundaries honored

- Deterministic script-only; no AI/ML in the loop.
- Read-only on all existing data (settlement_epochs, settlement_truth, orderbook,
  trades all untouched).
- New separate DB — no collision with the running orderbook pull or mispricing re-test.
- No live trading, no order endpoints, no auth (public GET).
- `data/` is gitignored; only the script + this receipt are committed.

## Commit

branch: `temp-sprint-branch`
files: `scripts/kalshi_strike_ladder_backfill.py`, `docs/plans/P0-B-STRIKE-LADDER-BACKFILL-2026-09-20.md`
