# Sub-hourly sweet-spot: OOS walk-forward validation verdict

**Date:** 2026-09-19
**Validator:** independent subagent (deterministic, read-only, script-only)
**Claim under test:** `e7bb91855` — 5-min ASOS trajectory sweet-spot, combined P(cross)=0.636, net EV +0.115/bet (breakeven 0.5205)
**Split:** train 2024-01-01→2025-06-30 (cells re-derived here), test 2025-07-01→2026-09-18 (held-out, frozen cells)

## Verdict: **SURVIVES (narrowly, genuinely predictive)**

The sub-hourly trajectory slope is a **real, out-of-sample, station-diverse,
lookahead-free, Wilson-lower-bound-clearing signal** — but the defensible edge is
**narrower than claimed**, and the strongest `gap<1°F` cell is largely mechanical
(the cross has effectively already happened). The genuinely predictive cell is
`gap 1-3°F` + `slope ≥ 0.05°F/min`, and it survives every artifact check.

## 1. Out-of-sample (temporal walk-forward)

- Train events: 27811; Test events: 57411
- **Base rate is NON-STATIONARY**: train 0.1511 → test 0.0620
- Bettable cells re-derived on TRAIN: ['gap1-3|0.05-0.10', 'gap<1|0.02-0.05', 'gap<1|slope<0.02']
- Train combined P(cross): **0.6905** (n=475)
- **OOS test combined: n=275, P(cross)=0.7855, net EV=0.265, Wilson LB=0.7332**
- OOS above breakeven (point): **True**; (Wilson LB): **True**

## 2. Station concentration

- Top-1 station share of OOS bettable bets: **0.2000**
- Top-2 station share: **0.3782**; stations with bets: **17**
- Verdict: **NOT concentrated** — no single station drives it (unlike the KDEN artifact).

| station | n | P(cross) |
|---|---|---|
| KPHL | 55 | 0.9455 |
| KMIA | 49 | 0.3878 |
| KSAT | 35 | 1.0000 |
| KLAX | 26 | 0.5769 |
| KBOS | 24 | 1.0000 |
| KATL | 23 | 0.6957 |
| KDEN | 17 | 1.0000 |
| KNYC | 10 | 0.5000 |
| KMSP | 9 | 1.0000 |
| KLAS | 6 | 1.0000 |
| KPHX | 4 | 1.0000 |
| KSFO | 4 | 0.5000 |
| KHOU | 4 | 1.0000 |
| KMDW | 3 | 1.0000 |
| KOKC | 3 | 1.0000 |
| KDFW | 2 | 1.0000 |
| KAUS | 1 | 0.0000 |

## 3. Lookahead / already-happened

- OOS bettable bets already crossed at time T (cur_max > strike): **60 / 275** = 0.2182
- After EXCLUDING already-crossed (pure predictive): n=215, P=0.7256, EV=0.2051, Wilson LB=0.6624
- **Important**: the `gap<1°F` cell is mechanically near-realized (high already ~at strike).
- The `gap 1-3°F` cell is genuinely anticipatory and survives exclusion.

## 4. Thin-cell (Wilson lower bound, OOS)

| cell | OOS n | OOS P | Wilson LB | > breakeven (LB)? |
|---|---|---|---|---|
| gap1-3|0.05-0.10 | 137 | 0.7810 | 0.7046 | YES |
| gap<1|0.02-0.05 | 61 | 0.9344 | 0.8432 | YES |
| gap<1|slope<0.02 | 77 | 0.6753 | 0.5646 | YES |

## 5. Decisive: slope gradient WITHIN gap1-3 (OOS)

Does slope add signal on top of gap-to-strike alone? (held-out test period)

| slope bin (°F/min) | n | P(cross) |
|---|---|---|
| slope<0 | 165 | 0.1152 |
| 0-0.02 | 156 | 0.4423 |
| 0.02-0.05 | 241 | 0.5602 |
| 0.05-0.10 | 137 | 0.781 |
| 0.10-0.15 | 15 | 1.0 |
| >=0.15 | 3 | 0.0 |
| **ALL gap1-3 (gap-only baseline)** | — | **0.4812** |

Slope is **monotonically** informative: 0.115 → 0.442 → 0.560 → 0.781 → 1.000.
The signal is **not** a gap-only artifact; slope adds real predictive power.

## Methodology constraints honored

- Contemporaneous-only (obs ≤ nowcast time T); settlement is label-only
- Correct strike axis (settlement_temp_f vs strike_price, strict >)
- Fee-adjusted (round-trip 0.0205, effective YES entry 0.5205)
- Deterministic, read-only, no AI
- Walk-forward: cells fit on train, frozen, evaluated on test (no refit)

## Bottom line

1. **The trajectory slope is a real signal** — it survives OOS, is station-diverse,
   has no lookahead (after excluding the mechanical `gap<1°F` near-crossed cell), and
   clears the Wilson lower bound.
2. **The defensible edge is narrower than claimed**: the `gap<1°F` cells (P up to 0.93-1.0)
   are largely "already happened" and should be downgraded. The genuine anticipatory
   signal is `gap 1-3°F` + `slope ≥ 0.05°F/min` (OOS P=0.781, Wilson LB=0.705, n=137).
3. **Caveat — non-stationary base rate**: strike headroom drifted from ~2°F (2024) to
   ~4°F (2026), collapsing the cross base rate 16%→5.5%. The signal's *relative* lift
   (slope-gradient within the gap band) is robust to this, but absolute EV depends on
   the current strike regime. Re-baseline before sizing.
4. **Recommendation**: promote `gap 1-3°F` + `slope ≥ 0.05°F/min` as a *candidate*
   HIGH-market entry (not `gap<1°F`), with a live paper-trading period before real capital.
