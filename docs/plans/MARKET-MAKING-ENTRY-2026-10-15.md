# Market-Making Entry Model — "trade anyway" (DESIGN ONLY)

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/market_making_entry_bridge.py` · **Status: DESIGN ONLY — nothing wired.**
**Companion:** `core/intraday/exit_logic.py` (shared), `core/fees.py` (canonical fee model)

Dan:
> *"Suppose I want to trade anyway on no edge. Just do the absolute best we can.
> Buy early-ish. Sell at a profit as much as we can. What changes would we make?"*

This is a **market-making** posture, not edge-prediction: when we have no positive expected
edge, the trade is still worth making *if* we can buy the two-sided spread and sell it back.
What follows is the design, and an honest statement of where the arithmetic does and does not
cooperate.

---

## 1. What changes vs the SBOX EV bridge

| element | SBOX EV bridge | market-making entry bridge |
|---|---|---|
| **entry gate** | `(p_calibrated − mid − cost) > min_edge` — **EV-based** | `spread ≤ spread_cap` **AND** `fill_p ≥ fill_gate` — **liquidity-based** |
| **om_gfs role** | the entry gate (a forecast) | a **curation signal** only — hold the strikes it favours, exit the ones it rejects as the day evolves |
| **entry timing** | whenever EV clears | the **liquid window** (13:30–20:00 station-local, "early-ish") |
| **sizing** | Kelly on the **edge** `f* = (p−m−fee)/m` | Kelly on the **spread**, and **never larger than the smallest leg already held** |
| **exit** | take_profit / stop_loss / signal_flip / auto_close_h1 | **the same rules**, delegated to `core/intraday/exit_logic.evaluate_exits` |

The whole point of the redesign is that the entry no longer requires an edge. The model buys
**inventory** (the cheapest 2, optionally 3, strikes with a tradable book) and lets the exit
rules harvest revaluation.

---

## 2. The four changes, precisely

### 2.1 Entry gate — spread, not EV
`evaluate_entry(quote, local_minute)` admits a leg when **all** hold:

- inside the liquid window (`LIQUID_WINDOW_OPEN=13:30`, `LIQUID_WINDOW_CLOSE=20:00` local);
- `ask − bid ≤ SPREAD_CAP` (0.01 — the Kalshi 1-tick minimum);
- `fill_probability(quote) ≥ FILL_GATE` (0.60).

`fill_probability` is depth-driven, `min(1, OI / depth_floor)` with `depth_floor = 100`. The
spread is a **hard gate**, so among admissible quotes the binding constraint is whether the book
is deep enough to actually fill.

### 2.2 om_gfs as curation, not a gate
`curation_score(mu, sigma, strike) = exp(−½ z²)` with `z = (strike − mu)/sigma` — the forecast
*density at the strike*, i.e. "is this the strike the day settles around". `curation_decide`
keeps the top-k legs and marks the rest for exit. A rejected leg is closed by the existing
`signal_flip` exit; om_gfs never blocks an entry.

### 2.3 Sizing — Kelly on the spread, capped by the book
`spread_kelly_fraction(entry, exit) = (exit − entry) / entry` (the spread capture as the
payout). `spread_position_size` then applies the same `bankroll × 5%` / `$10` caps as the SBOX
bridge **and** caps the leg at `min(size, smallest_current_leg_usd)`, so a spread book stays
balanced and no single leg dominates exposure. This is the "no position size larger than any
current leg" rule.

### 2.4 Exit — unchanged
`evaluate_mm_exits` builds `UP`/YES holdings and calls the shared
`core.intraday.exit_logic.evaluate_exits` with `{take_profit, stop_loss, signal_flip,
auto_close_h1}`. Nothing about the exit machinery is re-derived.

---

## 3. The arithmetic problem (the honest part)

Dan's specific instruction was *"buy early-ish, sell at a profit as much as we can."* The bridge
implements exactly that — but the **fee schedule forbids the literal 1¢-spread round trip**:

From `core/fees.py` (canonical, per-contract `ceil(0.07·P·(1−P)·100)`):

| entry price | per-side fee | round-trip fee | spread needed to break even |
|---:|---:|---:|---:|
| 0.05 | $0.01 | $0.02 | **4.0¢** |
| 0.10 | $0.01 | $0.02 | **4.0¢** |
| 0.30 | $0.02 | $0.04 | **8.0¢** |
| 0.50 | $0.02 | $0.04 | **8.0¢** |
| 0.90 | $0.01 | $0.02 | **4.0¢** |

A 1¢-wide market — which is the **most common real quote** (16,384 of 21,245 two-sided HIGH
snapshots, median spread 1.0¢) — round-trips at a **net −1¢ to −3¢ per contract**:

```
bid 0.30 / ask 0.31 → gross +0.010, fees −0.040, net −0.030
```

**So "spread ≤ 1¢ AND sell at a profit" is not a spread-capture trade — the fee eats the entire
spread.** The entry gate in §2.1 is correct as *liquidity selection*, but the profit cannot come
from the 1¢ round trip. It can only come from one of:

1. **Holding to settlement** — the leg pays its payoff ($1 or $0). Then the "profit" is
   directional/outcome exposure after all, which is exactly the edge we said we did not have.
2. **A wider revaluation move** — the mid travels >4–8¢ in your favour before exit, i.e. the exit
   rules (`take_profit` at 0.90/0.10, `signal_flip`) harvest a *price move*, not the spread.
3. **Buying the cheaper side of a wide book** — admissible only when the spread is *wider* than
   the break-even above, which contradicts the ≤1¢ gate.

### Verdict

- The **entry model is sound as inventory selection**: it will reliably acquire the cheapest
  tradable strikes early, which is what "buy early-ish" asks for.
- The **profit engine is the exit**, not the entry — and it is additive only when the mid moves
  >4–8¢. On a 1¢ book with a flat trajectory, the strategy is a slow fee bleed.
- Therefore the honest framing is: **this is a *revaluation* strategy dressed as market-making.**
  It is worth running only if (a) we accept the settlement/directional exposure the held legs
  carry, or (b) realized mid volatility over the window exceeds the fee floor. Neither is
  established on this cohort — and the pin sweep already found the realized decision edge
  **negative at every in-bounds pin** (−0.039 to −0.122/contract).

**Recommendation:** keep the script as the design artifact; **do not wire it**. If it is ever
armed, it must be behind a pre-registered test that measures *realized mid travel* vs the fee
floor, not spread capture — because on the current data the spread capture is fee-negative.

---

## 4. What the script does today

`--dry-run` evaluates the entry gate against read-only snapshots and **books nothing**:

```
$ python3 scripts/market_making_entry_bridge.py --dry-run --station KMSP --date 2026-09-26
[mm] MARKET_MAKING_BRIDGE_ENABLED=0 — design-only mode (no booking).
{ "n_quotes": 2, "n_picks": 1,
  "picks": [{"ticker": "KXHIGHTMIN-26SEP26-T69", "strike": 69.0,
             "bid": 0.01, "ask": 0.02, "spread": 0.01,
             "fill_p": 1.0, "enter": true, "reason": "enter"}],
  "booked": false }
```

- `MARKET_MAKING_BRIDGE_ENABLED` defaults **OFF**; non-dry-run passes refuse to book.
- Read-only on every DB; no book writes, no cron, no live path.
- Quotes are date-aligned to the ticker's own target date and reduced to the snapshot **nearest
  the 13:30 pin**, so no other hour or day leaks into the selection.

---

## 5. Guardrails

- **Design only.** No book writes. No cron. No live path. Arming requires an explicit Dan/approval
  step.
- Exit logic is **shared**, not forked — `core/intraday/exit_logic.py` is the single source.
- Fees are **shared** — `core/fees.py`; the break-even table above is computed from it, not
  re-derived.
- The finding in §3 is a **negative result**: report it as such, do not manufacture a positive
  framing for a fee-negative round trip.
