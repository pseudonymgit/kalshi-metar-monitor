# LOW Market Holdout — Design (draft)

**Date:** 2026-10-13
**Lane:** Sprint 2 — Lane B (technical)
**Owner:** Gilfoyle
**Repo:** `prototypes/weather-engine-source/`
**Status:** **DESIGN ONLY. Nothing wired.** No code path, gate, or booking change.
**Companion:** `docs/plans/F2-CALIBRATION-FUSION-SPRINT-LANE1-2026-10-13.md` (HIGH gate/fusion context)

---

## 0. Bottom line

The live book trades **HIGH only** (settlement when the day's high exceeds a
strike). **LOW** contracts settle when the day's temperature stays **below** a
strike. The calibration machinery is the same; the **entry-price gradient is
mirrored** (a LOW YES is ITM on the *down* side).

Design a **30-trade LOW holdout** under the **same gate as HIGH**, with a
**simpler exit rule**, and pre-register three pass conditions before any wiring:

* **30** trades in the holdout,
* **win rate ≥ 60%**,
* **net EV > 0** (per-contract edge survives fees) **before** wiring.

If all three pass → propose wiring. If any fails → park. No live change now.

---

## 1. Why LOW is not just HIGH with the sign flipped

Contract semantics (single source, `core.canonical_settlement`):

```
HIGH: YES (ITM) iff  settlement_temp > strike      (strict; equality == not-ITM)
LOW : YES (ITM) iff  settlement_temp < strike      (strict; equality == not-ITM)
```

Two asymmetries matter for a holdout:

1. **Mirrored price gradient.** For HIGH, the YES leg appreciates as temp
   climbs; for LOW, the YES leg appreciates as temp falls. A signal that is
   `UP`-correct on HIGH is, on a LOW market, a bet that temp **falls** below the
   strike. The entry side (`buy_yes` vs `buy_no`) therefore inverts against the
   *same* signal direction: HIGH `signal_direction=DOWN` → `buy_no`;
   LOW `signal_direction=DOWN` → `buy_yes`. Any reuse of the HIGH side-mapper
   must be **direction-aware per market_type**, not copied.

2. **Fewer revaluation events late in the day.** Diurnal temperature reaches its
   **minimum near dawn**, so by the afternoon entry pin (13:30 station-local —
   same pin as HIGH) the day's low is largely already established. The LOW
   outcome is therefore *more determined at entry* than the HIGH outcome, so the
   intraday price path has **fewer excursions to the take-profit/stop-loss
   bands** → the exit rule is simpler (auto-close dominates). This is the design
   premise to validate, not an assumption to bake in.

---

## 2. Gate — identical to HIGH

The LOW holdout reuses the **exact** HIGH gate stack (no new gate):

| Gate | Source | Rule |
|---|---|---|
| Pre-ingest G0–G5 | `core/booking_admission.py`, `scripts/gate_composer.py` | decidable subset at decision time; full gate at settled admission |
| Opportunity grade (F7) | `scripts/sbox_intraday_ev_bridge.py::compute_trade_grade` | edge ladder S≥0.12 / A≥0.09 / B≥0.06 / C≥0.03 / D≥0.01; F = no entry |
| Exposure caps (F9) | `exposure_skip_reason` | ≤3 open/station, ≤10 open/day |
| Sizing ladder | `_position_size_usd` | $5 / $10 / $25 by confidence; $0 below 0.5 |
| Entry pin | `pinned_decision_utc` | 13:30 station-local → UTC |
| Calibration | `core/intraday/multi_forecaster_fusion.py` | same `fuse()`, same per-station ceiling |

The only substitution: `market_type="LOW"` flows through the canonical
strike-crossing axis. Grading and fusion are direction-symmetric already
(`canonical_outcome` handles LOW), so **the gate needs no LOW-specific branch** —
that is the point of the shared substrate.

---

## 3. Exit rule — simpler than HIGH

Current production exit pass enables **only** `{"auto_close_h1"}` (see
`core/intraday/exit_logic.py` F3 note). The **holdout** uses the same single-rule
profile for LOW:

```
enabled_rules = {"auto_close_h1"}      # close at T-1h; no TP/SL/flip
```

Rationale (mirrored-gradient §1.2): on LOW, once the dawn minimum is in, the
position is close to its terminal value and the market price is not revalued
repeatedly against the 0.90/0.10 bands. Using auto-close-only keeps the holdout
measurement clean (the P&L is the *settlement* P&L, not a mid-path exit), and it
is the profile already running live for HIGH — so the holdout compares like for
like.

*Diagnostic only (not an exit rule):* record how often the LOW book *would* have
hit `take_profit` / `stop_loss`, to evidence the "fewer revaluations" premise.
If that count is materially **lower** than the HIGH book, the premise holds.

---

## 4. Holdout specification

**Unit:** one station-day = one candidate (strike chosen by the same
EV-strike selector as HIGH).

**Postconditions (pre-registered, frozen before evaluation):**

| # | Condition | Threshold |
|---|---|---|
| P1 | Holdout size | **exactly 30 trades** |
| P2 | Win rate | **≥ 60%** (≥18/30), win = `canonical_outcome == 'correct'` |
| P3 | Net EV | **> 0** per contract after the canonical Kalshi fee, summed over the 30 |

Net EV uses the engine's fee model
(`ceil(0.07 × P × (1−P) × 100)/100` per contract per side, round trip ×2) and the
**settlement** P&L (auto-close profile), consistent with §3.

**Selection of the 30 (frozen):** take the **first 30 qualifying LOW
station-days** by ascending `local_trading_date` that pass §2 against a **held-out
window not used for any HIGH calibration**. Do **not** cherry-pick by outcome.

**Data availability (measured, read-only):**

| Store | LOW coverage | Verdict |
|---|---|---|
| `data/kalshi_strike_ladder.db` | **4112** LOW station-days, `2025-12-13 … 2026-09-19` | **primary universe** |
| `data/kalshi_market_snapshots.db` | LOW = 672 rows, **single day 2026-09-07**, 17 stations | thin; mid-price at the pin only for that day |
| `data/kalshi_settlements.db` | `KXLOWT*` series present (17–148 rows/series) | canonical settlement for outcome |
| `data/paper_trading_sbox.db` | **0 LOW trades** (82 HIGH) | LOW has never been booked |

**Consequence:** the strike ladder + canonical settlements can supply ≥30
LOW station-days, but the **market mid at the 13:30 pin** is only thinly
available (one snapshot day). Before the holdout can score *entry price*, the
LOW entry-price series must be backfilled **or** the ladder's own strike prices
used as a proxy. **Flag this as the first blocker to clear**, not a solved item.

---

## 5. Pre-registration packet (what gets frozen)

Write a frozen packet before scoring, so the 30 are not selected post hoc:

```json
{
  "market_type": "LOW",
  "gate": "HIGH-parity (G0-G5, F7, F9, 13:30 pin)",
  "exit_profile": ["auto_close_h1"],
  "holdout_n": 30,
  "pass_conditions": {"min_win_rate": 0.60, "min_net_ev_per_contract": 0.0},
  "universe_source": "kalshi_strike_ladder where market_type='LOW'",
  "frozen_station_days": ["<station, local_trading_date, strike>", "..."],
  "frozen_at_utc": "<iso8601>"
}
```

Scoring runs only after `frozen_at_utc`; any station-day added later voids the
holdout and restarts it.

---

## 6. Explicit non-goals / do-not-wire

* No `market_monitor` / `sbox_intraday_ev_bridge` change.
* No `market_type="LOW"` booking path enabled.
* No gate, sizing, or exposure-matrix change.
* No live-book write. No P&L change. Design + pre-registration only.

---

## 7. Recommendation

**ADVANCE to a data-readiness spike** (not to wiring): clear the §4 entry-price
blocker (backfill LOW mid at the pin, or justify the ladder proxy), then freeze
the 30. Wiring is gated on P1–P3 passing.
