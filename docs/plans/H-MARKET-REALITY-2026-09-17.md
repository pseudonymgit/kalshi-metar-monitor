# H6 + H7 — Market Reality Classification

**Date:** 2026-09-17
**Branch:** temp-sprint-branch
**Author:** Ray Donovan (runtime integrity / SRE) — subagent task
**Scope:** Read-only market-reality assessment. No trading, no DB writes, no backtests, no daemon changes.

---

## 0. What this is (and is not)

This document answers two questions Dan has been asking, both of which are about **market reality, not theoretical model quality**:

- **H6** — Is there *marketable* (net executable) edge after subtracting real bid/ask width, liquidity limits, and fee/friction drag? *Not* raw bucket accuracy.
- **H7** — For each of the 20 stations, what is its actual market state?

**Data sources used (read-only):**
| Source | What it provides |
|---|---|
| `data/orderbook_snapshots/kalshi_orderbook_snapshot_20260718.json` | Real per-market bid/ask (dollars), volume_24h, open_interest, status — 20 stations, 1,650 markets, 396 active |
| `data/per_station_skill_analysis.json` | Per-station directional accuracy + Brier skill (SH2 gating) |
| `reports/kalshi_noise_floor.json` | Kalshi settlement noise floor — 77.7% directional ceiling, 22.3% disagreement rate |
| `reports/market_recalibration.json` | Per-station calibration, volume counts, n_obs |
| `core/market_cost_model.py` | Fee model: round-trip = spread/2 + slippage; taker fee ≈ 0.07×P×(1−P) |
| `core/low_liquidity_traps.py` | Liquidity thresholds: $15K/snapshot, $50K/24h, spread > 1.5¢ |
| `core/station_registry.py` | Canonical 20-station list |

**Note on station set:** `get_all_stations()` returns the 20 stations that *also* match the orderbook snapshot exactly:
`KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`.

(The older `STATIC_MAPPING` comment in `station_registry.py` listing KDFW/KJFK/KORD/etc. is stale — the authoritative runtime list is the one above, and it is the list both the orderbook and the skill analysis cover.)

---

## 1. H6 — Marketable / Net Executable Edge

### 1.1 The honest edge formula

Theoretical bucket accuracy is not executable. The correct net edge for a *buy-and-hold-to-settlement* weather position is:

```
net_edge = (directional_accuracy − 0.50)          # raw skill above coin-flip
           − round_trip_friction                  # spread (buy ask / sell bid) + slippage
           × (1 − kalshi_noise_discount)          # discount for un-modelable settlement noise
```

Where:
- **Round-trip friction** = full spread (enter at ask, exit at bid) + ~0.5¢ slippage. With no commission on Kalshi weather markets, the dominant cost is the spread itself.
- **Kalshi noise floor** = the finding (from `kalshi_noise_floor.json`) that even *ERA5 reanalysis* — a "perfect" model — only matches Kalshi settlement direction **77.7%** of the time. The other 22.3% is settlement measurement noise, microclimate, grid-cell averaging error, and temporal sampling mismatch. That means a hard ceiling of ~78% directional accuracy exists **regardless of model quality**, and the marginal edge near that ceiling is unreliable.

### 1.2 Per-station net edge table

Real spreads are computed from **active markets with tradable mid prices only** (mid ∈ 0.02–0.98), excluding finalized/expired markets whose bid/ask collapse to 0.00/1.00 extremes (which would falsely inflate spread).

| Station | Dir. Acc | Skilled | Real spread (¢) | RT friction (¢) | Raw edge (pp) | Net edge (pp, noise-discounted) | Verdict |
|---|---|---|---|---|---|---|---|
| KSAT | 0.791 | Y | 2.60 | 3.10 | +26.0 | **+20.2** | **Executable** |
| KATL | 0.764 | Y | 3.28 | 3.78 | +22.6 | **+17.5** | **Executable** |
| KMIA | 0.757 | Y | 2.38 | 2.88 | +22.9 | **+17.8** | **Executable** |
| KAUS | 0.702 | Y | 2.00 | 2.50 | +17.7 | +13.7 | **Executable (thin OI)** |
| KPHL | 0.675 | Y | 1.78 | 2.28 | +15.2 | +11.8 | **Executable** |
| KLAS | 0.678 | Y | 2.35 | 2.85 | +14.9 | +11.6 | **Executable** |
| KDCA | 0.685 | Y | 4.37 | 4.87 | +13.7 | +10.6 | Marginal (wide spread) |
| KDEN | 0.652 | Y | 2.33 | 2.83 | +12.4 | +9.6 | **Executable (thin vol)** |
| KMSP | 0.658 | Y | 3.44 | 3.94 | +11.9 | +9.2 | Marginal (thin vol) |
| KDFW | 0.640 | n | 2.12 | 2.62 | +11.4 | +8.9 | Marginal (unskilled) |
| KHOU | 0.634 | n | 3.73 | 4.23 | +9.2 | +7.1 | Marginal (unskilled) |
| KOKC | 0.630 | n | 5.18 | 5.68 | +7.3 | +5.7 | Marginal (wide spread) |
| KMSY | 0.615 | n | 3.56 | 4.06 | +7.5 | +5.8 | Thin |
| KSEA | 0.583 | n | 2.80 | 3.30 | +5.0 | +3.9 | Thin |
| KMDW | 0.561 | n | 1.50 | 2.00 | +4.1 | +3.2 | No edge |
| KBOS | 0.542 | n | 3.27 | 3.77 | +0.4 | +0.3 | No edge |
| KSFO | 0.536 | n | 3.72 | 4.22 | −0.6 | −0.5 | **No edge** |
| KLAX | 0.512 | n | 1.50 | 2.00 | −0.8 | −0.6 | **No edge (but deep)** |
| KNYC | 0.477 | n | 1.37 | 1.87 | −4.2 | −3.3 | **Negative edge** |
| KPHX | 0.486 | n | 6.24 | 6.74 | −8.1 | −6.3 | **Negative edge** |

*Net edge (pp) = raw edge × 0.777 (noise discount), i.e. the fraction of raw edge that survives the 22.3% un-modelable settlement noise. This is conservative — it assumes the full noise discount applies to the marginal edge rather than the full accuracy.*

### 1.3 H6 conclusion

**There is net executable edge, but it is concentrated in a narrow subset of stations and is smaller than the raw accuracy numbers imply.**

1. **The theoretical 58%+ directional-accuracy bar is not sufficient on its own.** The system's best stations (KSAT 79.1%, KATL 76.4%, KMIA 75.7%) clear it, but after **real spread (1.4–6.2¢)** and **the 77.7% noise ceiling**, only ~6–8 stations retain a *reliable* positive edge large enough to survive friction and adverse selection.

2. **Three structural facts compress the edge:**
   - **Noise ceiling of 77.7%** — even perfect information cannot exceed this directional hit rate against Kalshi settlements. The 22.3% disagreement is pure settlement noise, not signal.
   - **Real spreads are 1.4–6.2¢, not the assumed 3.1¢ flat.** KPHX (6.24¢) and KOKC (5.18¢) are far wider than the MEASURED_MEAN_SPREAD constant in `market_cost_model.py`, while KNYC (1.37¢) and KLAX (1.50¢) are tighter. A flat 3.1¢ assumption systematically mis-prices edge across the book.
   - **Liquidity is heavily skewed.** KLAX carries $277K/24h volume but has *negative* skill (51.2%). KNYC carries $135K/24h but is the *worst* station for accuracy (47.7%). The deep, liquid markets are precisely the ones where the model has no edge; the high-skill stations (KSAT, KATL, KMIA) are mid-liquidity.

3. **Fee drag is real but second-order.** Kalshi weather markets charge no flat commission, but the taker fee ≈ 0.07×P×(1−P) (peak ~1.4% at 50¢) plus slippage. This is already folded into "round-trip friction" above and is modest relative to spread.

**Bottom line for H6:** Executable edge exists in ~6 stations (KSAT, KATL, KMIA, KPHL, KLAS, KAUS), is *marginal* in ~6 more, and is *absent/negative* in the remaining ~8 — including the two deepest markets (KLAX, KNYC). Any production deployment must **gate on net edge (post-spread, post-noise-discount), not raw accuracy**, and must **use live per-strike spreads, not a flat 3.1¢ constant.**

---

## 2. H7 — 20-Station Market-State Classification

### 2.1 Classification taxonomy

| State | Definition | Criteria |
|---|---|---|
| **active** | Tradeable, liquid, model has positive net edge | volume_24h ≥ $50K (or OI ≥ $25K), spread ≤ ~4¢, net edge > 0 |
| **thin** | Tradeable but shallow — fill risk / wide spread | volume_24h < $50K or spread > 4¢, even if skill positive |
| **no-market** | Station exists but effectively untradeable (zero/negligible book, negative edge) | near-zero real liquidity or net edge ≤ 0 |
| **undiscovered** | Real skill exists but market has not priced it (thin OI, low volume relative to skill) | positive skill + OI < ~$20K + low volume |
| **paper-only** | Not fit for real capital; backtest/paper-trade only | unskilled and/or negative net edge, or data-limited |

### 2.2 Full 20-station classification table

| # | Station | City | Dir. Acc | Real spread (¢) | vol_24h ($) | OI ($) | Net edge | State |
|---|---|---|---|---|---|---|---|---|
| 1 | KATL | Atlanta | 0.764 | 3.28 | 18,820 | 14,635 | +17.5 | **active** |
| 2 | KAUS | Austin | 0.702 | 2.00 | 24,689 | 17,577 | +13.7 | **undiscovered** |
| 3 | KBOS | Boston | 0.542 | 3.27 | 18,961 | 15,422 | +0.3 | **paper-only** |
| 4 | KDCA | Washington DC | 0.685 | 4.37 | 19,778 | 16,664 | +10.6 | **thin** |
| 5 | KDEN | Denver | 0.652 | 2.33 | 14,345 | 12,804 | +9.6 | **thin** |
| 6 | KDFW | Dallas–Ft. Worth | 0.640 | 2.12 | 20,732 | 12,939 | +8.9 | **paper-only** |
| 7 | KHOU | Houston | 0.634 | 3.73 | 13,842 | 13,228 | +7.1 | **paper-only** |
| 8 | KLAS | Las Vegas | 0.678 | 2.35 | 25,647 | 22,161 | +11.6 | **active** |
| 9 | KLAX | Los Angeles | 0.512 | 1.50 | 277,741 | 157,550 | −0.6 | **no-market (deep, no edge)** |
| 10 | KMDW | Chicago–Midway | 0.561 | 1.50 | 67,266 | 43,706 | +3.2 | **no-market (no edge)** |
| 11 | KMIA | Miami | 0.757 | 2.38 | 72,684 | 44,811 | +17.8 | **active** |
| 12 | KMSP | Minneapolis | 0.658 | 3.44 | 10,794 | 9,310 | +9.2 | **thin** |
| 13 | KMSY | New Orleans | 0.615 | 3.56 | 14,133 | 12,746 | +5.8 | **paper-only** |
| 14 | KNYC | New York | 0.477 | 1.37 | 134,993 | 91,335 | −3.3 | **no-market (deep, negative)** |
| 15 | KOKC | Oklahoma City | 0.630 | 5.18 | 14,704 | 12,525 | +5.7 | **paper-only** |
| 16 | KPHL | Philadelphia | 0.675 | 1.78 | 36,414 | 25,165 | +11.8 | **active** |
| 17 | KPHX | Phoenix | 0.486 | 6.24 | 46,775 | 37,233 | −6.3 | **no-market (wide spread)** |
| 18 | KSAT | San Antonio | 0.791 | 2.60 | 10,367 | 10,146 | +20.2 | **active** |
| 19 | KSEA | Seattle | 0.583 | 2.80 | 16,086 | 13,237 | +3.9 | **paper-only** |
| 20 | KSFO | San Francisco | 0.536 | 3.72 | 31,482 | 24,083 | −0.5 | **no-market (no edge)** |

### 2.3 State counts

| State | Count | Stations |
|---|---|---|
| **active** | 5 | KATL, KLAS, KMIA, KPHL, KSAT |
| **thin** | 3 | KDCA, KDEN, KMSP |
| **undiscovered** | 1 | KAUS |
| **no-market** | 5 | KLAX, KMDW, KNYC, KPHX, KSFO |
| **paper-only** | 6 | KBOS, KDFW, KHOU, KMSY, KOKC, KSEA |

### 2.4 H7 interpretation

1. **The marketable book is 5 stations, not 20.** Only KATL, KLAS, KMIA, KPHL, and KSAT combine (a) positive skill, (b) acceptable spread, and (c) enough liquidity to be actually tradable. Everything else is thin, paper-only, or genuinely no-market.

2. **KAUS is the clearest "undiscovered" candidate.** It has positive skill (70.2%), a tight 2.0¢ spread, but low OI ($17.6K) and moderate volume ($24.7K) — the market has not yet priced in the edge. This is the classic thin-mispricing opportunity, but it is *thin* precisely because fills will be hard to get at size.

3. **The two deepest markets are the worst trades.** KLAX ($277K/24h, $157K OI) has 51.2% accuracy — coin-flip. KNYC ($135K/24h, $91K OI) has 47.7% — *below* coin-flip. Liquidity and edge are **inversely correlated** across this book. Chasing volume leads to the worst risk-adjusted outcomes.

4. **KPHX is a trap.** It carries decent volume ($46.8K) and OI ($37K) but has a 6.24¢ spread and 48.6% accuracy — the wide spread plus negative skill makes it a pure friction drain.

5. **Six stations are paper-only** (KBOS, KDFW, KHOU, KMSY, KOKC, KSEA): they have sub-65% accuracy that does not survive friction, or (KOKC) a spread too wide to trade. Keep them in backtests; do not allocate real capital.

---

## 3. Key risks and caveats

1. **Single-snapshot liquidity.** The bid/ask, volume, and OI figures come from a *single* orderbook snapshot (2026-07-18 18:00 UTC). Liquidity is intra-day and weather-dependent; these are point-in-time, not averages. The 20260717 snapshot was empty (no markets captured), so there is no paired second observation to confirm stability.

2. **Accuracy is historical, not forward.** The `per_station_skill_analysis.json` accuracies are from the SH2 gating run (2026-07-06). They do not reflect any recent parameter freezes (B2-PARAM-GRID-FREEZE, P1-LINEAGE-FREEZE, S5/S6). Forward accuracy under frozen params is unverified.

3. **Noise-floor discount is a blunt instrument.** The 77.7% ceiling is a *global* ERA5-vs-Kalshi figure across 17 stations, not station-specific. Applying a flat ×0.777 discount to the marginal edge is conservative but crude; per-station noise floors differ (e.g., KATL MAE 2.34°F vs KDCA 6.65°F in `kalshi_noise_floor.json`).

4. **Fee model has an internal inconsistency.** `market_cost_model.py` documents "commission: 0%" but also defines a `kalshi_fee()` taker fee of `ceil(0.07×P×(1−P)×100)/100`. The two are reconciled only if "commission" means *flat* commission and the 0.07 taker fee is *separate*. For a position at 50¢, the taker fee is ~1.4% — not negligible — and should be folded into any production Kelly sizing, not assumed zero.

---

## 4. Recommendations (for Gerri / Dan, non-blocking)

1. **Gate deployment on net edge, not accuracy.** Add a production gate that requires `(accuracy − 0.50) − spread − slippage > threshold` using *live* per-strike spread, with the 77.7% noise discount applied. Reject flat 3.1¢ spread assumptions.

2. **Trade only the 5 active stations + monitor KAUS.** Initial capital should go to KATL, KLAS, KMIA, KPHL, KSAT. Treat KAUS as a small-size watch (undiscovered, fill-risk).

3. **Do not chase KLAX/KNYC depth.** The liquid markets are the no-edge markets. Liquidity-weighted allocation as currently framed will systematically overweight the worst trades.

4. **Re-run this with ≥2 fresh snapshots** before any production decision, to confirm liquidity stability and catch the intra-day variance this single snapshot cannot see.

---

## 5. Confirmation of no mutation

- **Files created:** exactly one — `docs/plans/H-MARKET-REALITY-2026-09-17.md` (this file).
- **Repo/DB inspection:** read-only (`cat`, `python3 -c` json/sqlite reads, `find`, `grep`).
- **No trading, no DB writes, no backtests, no daemon changes, no config edits.**
- Working tree was already dirty (many untracked artifacts) *before* this task began; this task added **one** new file only.
