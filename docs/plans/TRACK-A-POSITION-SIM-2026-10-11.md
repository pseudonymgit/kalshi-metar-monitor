# Track A — Position-Trading Simulation (2026-10-11)

**Script:** `scripts/trackA_position_sim.py`
**Machine artifact:** `data/trackA_position_sim.json`
**Population:** full ASOS station-day cohort — 17,160 station-days, 20 stations, 2024-01-01 → 2026-10-11

## Hypothesis

`rate_1h` (1-hour temperature momentum, native °F) identifies the strike the
temperature is heading toward. Buy that strike early (mid 5-15¢), sell into the
repricing wave as the mid rises (+20% take-profit, sell at .95, or run to
settlement) — profit WITHOUT needing to beat the market's settlement accuracy,
just by being early to a revaluation.

## Standalone result — NOT profitable under realistic costs

| monitor | thr | N | net P&L | gross | win rate | avg/trade |
|---|---|---|---|---|---|---|
| windows (14/17/20Z) | 0.5 | 209 | **−$839.69** | −$478.79 | 24.4% | −$4.02 |
| windows (14/17/20Z) | 1.0 | 192 | **−$471.04** | −$147.22 | 26.6% | −$2.45 |
| continuous | 0.5 | 217 | **−$794.71** | −$256.09 | 19.8% | −$3.66 |
| continuous | 1.0 | 200 | **−$729.97** | −$209.17 | 19.5% | −$3.65 |

Realistic = buy above mid by half the quoted spread, fees
`ceil(0.07·P·(1−P)·100)/100` both sides, forced exits pay `mid − half-spread`.

## The friction bracket — the edge exists but is smaller than the round-trip cost

Idealized model (fill at mid, no spread, no fees) on the same trades:

| monitor | thr | N | net P&L | avg/trade |
|---|---|---|---|---|
| windows | 0.5 | 209 | **+$187.63** | +$0.90 |
| windows | 1.0 | 192 | **+$457.71** | +$2.38 |
| continuous | 0.5 | 217 | **+$721.07** | +$3.32 |
| continuous | 1.0 | 200 | **+$667.49** | +$3.34 |

The revaluation edge is real (positive before friction) but on a ~21¢ contract
a single round trip costs ~$0.06/contract (two fees) plus ~$0.13/contract (two
half-spreads) ≈ **$0.19 — roughly 90% of the average position's entire entry
cost**. Friction eats the edge and then some. The strategy is a *break-even-to-
slightly-negative* bet on this cohort, not a profitable one.

## Why the profitable legs don't carry it

Exit breakdown (realistic / windows / thr 0.5, N=209):

| exit | count | net P&L |
|---|---|---|
| take_profit_20 | 37 | **+$873.23** |
| sell_095 | 2 | **+$44.52** |
| stop_loss | 48 | −$354.58 |
| settlement | 122 | **−$1,402.85** |

The repricing-wave legs (take-profit + sell@.95) are strongly positive — the
premise is directionally correct. But only **39/209 = 19%** of positions reach a
profitable exit; **58% ride to settlement**, and the settlement leg alone loses
≈$1,403 (a win pays ≈(1−entry−fee); a loss pays ≈−(entry+fee), and most
settlement positions lose). The wave *usually doesn't arrive* before the day
settles.

Under the `continuous` monitor (exit ladder re-evaluated at every 15-min bucket)
the stop-loss rule churns **164 stops**, converting marginal losers into
realized −spread losses; net stays negative.

## Funnel — where the cohort thins (thr 0.5)

| stage | count |
|---|---|
| full ASOS station-days | 17,160 |
| station-days with a Kalshi HIGH ladder | 8,110 |
| `rate_1h ≥ 0.5` triggers at 14/17/20Z | **13,302** |
| … with a mapped ladder target | 13,030 |
| … with a real non-degenerate market quote | **217 entered** |
| … with a canonical settlement label | 209 (8 unresolved, excluded) |

Threshold 1.0: **9,420** triggers → 200 entered.

## E3's "0 candidates" is a joining artifact, not just thin data

The prior sweep targets the exact integer `floor(high_so_far)+1`. The Kalshi
HIGH ladder is **coarse** — typically one `less`, four `.5` `between`, one
`greater` per station-day. So `floor(hi)+1` names a real listed strike only
**1,176 / 13,302 = 8.8%** of the time; E3's market join found ~0 because it
required that exact integer as a ticker.

Track A snaps the integer target **up to the nearest real ladder entry** (the
`between` whose floor ≥ the integer, else the containing `between`). That raises
the market-reachable trade count from ~0 to 217 without inventing markets. So
the thin cohort is (partly) a **joining/snapping error**; with correct snapping
the deep ASOS cohort is trade-able on ~200 real market prices — still small, but
non-zero.

## Verification (all four checks pass)

- Every simulated trade has a real market price from `kalshi_market_snapshots.db`
  (`yes_bid>0 AND yes_ask<1`, buy side >0): **209/209**.
- Every settlement outcome is from the canonical settlement truth
  (`kalshi_strike_ladder.result`), cross-checked against
  `z8_settlement_truth.kalshi_temp` strict-crossing: **87.6% agreement** (n=193;
  the disagreement is the documented GHCN boundary effect, see
  `MARKET-BENCHMARK-CONTRACT-2026-10-11.md`); **0 settlement exits lack a
  canonical label**.
- Position never exceeds 25% of the strike's open interest:
  `max(contracts / OI) = 0.25`.
- No lookahead: entry price = last snapshot **≤** decision instant;
  `high_so_far` = max bucket mean with epoch **≤** decision instant.

## Read this before believing the number

**N ≈ 200.** The market snapshot archive only covers **67 local dates
(2026-08-01 → 2026-10-11)** and 637 station-days; the tradeable cohort is
therefore a ~200-position strip, not the 16.5k ASOS cohort. The P&L is a small
sample; treat the sign (negative under realistic costs, marginally positive
under idealization) as the finding, not the magnitude.

## Reproduce

```bash
.venv-gefs/bin/python scripts/trackA_position_sim.py            # full ASOS scan
.venv-gefs/bin/python scripts/trackA_position_sim.py --friction realistic
.venv-gefs/bin/python scripts/trackA_position_sim.py --monitor continuous
```

Read-only. No live wiring. No SBOX/gate-ledger/trade-custody mutation.
