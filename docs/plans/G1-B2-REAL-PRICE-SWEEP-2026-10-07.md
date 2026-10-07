# G1 — B2 Real-Price Sweep (first honest measurement)

**Sprint G** · **Item:** G1 (P0) · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Harness:** `scripts/b2_real_price_sweep.py` (new) · **Result:** `data/b2_real_price_results.json`
**Status:** DONE — pending Gilfoyle review before results are quoted internally.

---

## Why a new harness

The frozen sweep's entry price is **synthetic** — `scripts/sweep/kalshi_sweep_eval.py:140`
sets `market_price = min(0.95, 0.5 + confidence*0.4)`, derived from the model's own
confidence, and never reads a market price. This harness replaces that with the **actual
Kalshi mid-price** via an as-of join into `data/kalshi_market_snapshots.db::market_snapshots`,
and reads the realized outcome from `kalshi_strike_ladder.db::strike_ladder.result` on the
**strict strike-crossing axis**. This is the first measurement where both the price and the
label are real.

## Method

Per station × settlement date, over **2026-08-01 .. 2026-10-06** (67 days):

1. **Model** — `mu` = GEFS ensemble mean (step=24, **converted °C→°F**); `sigma = max(0.8, (max−min)/4)` (in °F). `P(high ≥ T) = 1 − Φ((T−μ)/σ)` (`core.strike_selection_ev.p_high_ge`).
2. **Ladder** — threshold `-T` strikes for `(station, HIGH, date)` from `strike_ladder`, each carrying the realized `result` (`yes`/`no`).
3. **Price (as-of, no lookahead)** — `mid_price` for the market ticker at the latest `snapshot_time ≤ t_eval`. `t_eval` = **event-day 09:00Z** (local morning, before the afternoon max).
4. **Liquidity filter** — mirror `sbox_intraday_ev_bridge.strike_ladder`: keep `0.02 < mid < 0.98` and `volume_24h ≥ 50`. Removes structural `mid=0` placeholder rows.
5. **Trade** — pick the strike with the larger EV leg (yes/no) over net edge; one trade per station-day.

## Bugs found and fixed in the harness (each would have inflated the result)

| # | Bug | Effect | Fix |
|---|---|---|---|
| 1 | GEFS mean used raw | ensemble is **°C**, strikes/settlement are **°F**; `p` collapsed to ≈0 | `mu, sigma = c*9/5+32` (repo convention, `kalshi_sweep_eval.py:263`) |
| 2 | `mid = 0` placeholder rows treated as tradable | fake free lotteries; 97% "accuracy" | liquidity filter `0.02 < mid < 0.98` |
| 3 | end-of-day (23:59Z) entry cutoff | market still quoted *after* the event → lookahead | morning cutoff (09:00Z), before the local max |
| 4 | no volume floor | thin/extreme strikes traded | `volume_24h ≥ 50` |

Before fixes the sweep reported 96.9–97.5% accuracy — all four artifacts. The corrected sweep
is below.

## Result — 67-day real-price sweep (entry 09:00Z)

| Metric | Value |
|---|---|
| Trades | **95** |
| Directional accuracy | **57.89%** (95% CI ≈ ±9.9 pp) |
| Mean P&L / contract | **+$0.01751** |
| Sharpe (annualized, daily units) | **1.17** |
| Mean model−market edge | 0.201 |
| Sides | yes 41 / no 54 |

**Signal-level breakdown (per station):**

| Station | n | acc | Σ P&L/contract |
|---|---:|---:|---:|
| KDEN | 32 | 0.688 | +2.04 |
| KDCA | 13 | 0.615 | +1.08 |
| KBOS | 16 | 0.563 | −0.04 |
| KATL | 12 | 0.417 | +0.03 |
| KMIA | 4 | 1.000 | +0.16 |
| KLAX | 4 | 0.750 | −0.43 |
| KPHX | 2 | 1.000 | +0.11 |
| KMDW | 2 | 0.000 | −1.03 |
| KAUS | 2 | 0.000 | −0.11 |
| KMSP/KSEA/KOKC/KMSY/KDFW/KHOU | 1–2 each | mixed | −0.10 … +0.03 |

**Entry-time sensitivity (accuracy):** 06:00Z 0.593 · 09:00Z 0.579 · 12:00Z 0.571 · 15:00Z 0.590.
Stable — the result is not an artifact of the cutoff.

### Interpretation (honest)

- Accuracy ≈ **57–59%** — modestly above the coin-flip base rate in a 2-sided strike market, but the 95% CI (±10 pp on n=95) **does not clear** the Gray-Room ≥58% directional gate with confidence.
- The **+$0.0175/contract** positive P&L is driven by a small number of far-from-money "no" wins; near-money strikes (0.15–0.85) show **53.8%** on n=13. Edge is **thin and not yet significant**.
- Sample is small (n=95 across 14 stations; Aug has only ~108 tickers/day so early-month strikes thin out). **Do not treat as a durable edge.**

## ⚠️ Caveat (required by the order)

> **B2 results constrained to the Aug–Oct window; the 144 days Mar–Jul are unreachable from the
> public Kalshi candlestick API — summer regime may differ.**

The window (2026-08-01 → 2026-10-06, 67 days) is B2's accepted scope per the F3 formal gap
acceptance (`F3-B2-MARKET-BACKFILL-WINDOW-2026-10-06.md`).

## Review gate

**Gilfoyle reviews this sweep before any number is quoted internally.** Items to confirm:
(1) the °C→°F conversion matches the frozen sweep; (2) the morning cutoff is the intended
decision time (the live bridge settles on an intraday snapshot — a 13:30Z live-price analog
may be the closer match); (3) whether the sweep should restrict to near-money strikes.

## Files

| path | change |
|---|---|
| `scripts/b2_real_price_sweep.py` | new — real-price B2 harness |
| `data/b2_real_price_results.json` | new — canonical run (09:00Z), 95 trades |
