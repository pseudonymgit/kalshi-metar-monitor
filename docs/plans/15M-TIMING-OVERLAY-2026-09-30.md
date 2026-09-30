# 15m Entry/Exit Timing Overlay — Design Note

**Date:** 2026-09-30 · **Author:** Ray Donovan · **Item:** 2 of the 2026-09-30 WE work order
**Status:** BUILT + TESTED (13/13 pass) · **Code:** `core/intraday/timing_15m.py` · **Tests:** `tests/test_timing_15m.py`

---

## 1. What this is (and is not)

The daily distribution layer (`core/intraday/distribution_adapter.py`, WS-P emOS)
answers **what** to trade: P(high ≥ X) for a station-day. This overlay answers
**when** within the day to act, from the 1-minute ASOS archive.

```
distribution (strike + P)  →  15m ASOS overlay  →  trigger  →  entry signal
                                                    (station, timestamp,
                                                     trigger, direction, confidence)
```

It does **not** pick strikes, size positions, or call any model. Deterministic
math only — no AI/ML in the loop.

## 2. Data

`data/iem_asos_1min.db` — 20.77M rows, 20 stations, 2024-01-01 → 2026-09-30,
**exactly 1440 rows/station-day** on populated days (verified: dense, no gaps).

**Units correction (new finding, 2026-09-30):** `observations.tmpf` is **already
Fahrenheit** (IEM ASOS convention) — global range −49→122, KNYC June midnight reads
80, KPHX July reads 98–99. `scripts/intraday_multifreq_sweep.py:load_intraday_hsf`
applies `tmpf*9/5+32` to this column, so its `w_hsf` term saw ~175–200 "°F" and is
effectively corrupt. That script was **not** modified (out of scope); the bug is
flagged here and in the module docstring. This module applies **no** C→F conversion.

## 3. Resampling (strict 15-minute grid)

`resample_15m()` maps each 1-min row to `bucket = hour*4 + minute//15` → **96
buckets/day**. Per bucket:

| field | definition |
|---|---|
| `tmpf_mean` | mean of the bucket's 1-min temps (F) |
| `high_so_far` | running max of bucket means up to and including this bucket |
| `rate_15m` | `tmpf_mean` − previous bucket mean |
| `rate_1h` | `tmpf_mean` − mean 4 buckets back (the momentum axis) |
| `vol_15m` | sample std of the last 8 × 15m deltas |

Empty buckets are skipped but indices stay on the true grid, so gaps are visible.
This produces the full 96-bucket day, unlike the sweep's hour-truncated max.

## 4. The trigger (derived from the data)

Thresholds were **measured**, not guessed. On 7 stations × ~92 days of real 1-min
ASOS (36,579 bucket-states), scoring P(day's high ≥ next strike above high-so-far):

| condition | n | P(cross) |
|---|---|---|
| unconditional | 36,579 | **0.684** |
| `rate_1h ≥ +0.5 F` | 15,726 | **0.725** |
| `rate_1h ≤ −0.5 F` | 15,540 | **0.634** |
| `rate_1h ≥ +1.0 F` | 14,363 | 0.738 |
| proximity ≤1F & `rate_1h ≥ +0.5` | 8,274 | 0.733 |

**Finding:** the discriminating axis is the **1-hour rate-of-change (~9pp
separation)**, not proximity to the strike (0.691 vs 0.684 baseline). Proximity
says *where* we are; momentum says *whether we are still going*. The trigger
therefore requires **both**: near the strike **and** moving toward it. Median
daily-peak bucket is ~19:00 UTC, so afternoon momentum is exactly the window where
the strike is undecided.

Two mirror triggers, both gated to trading hours:

- **`approach_up`** (→ direction `UP`): `gap = strike − high_so_far ∈ [0, proximity_F]`
  **and** `rate_1h ≥ +mom_thr_F · vol_mult`. The high is still climbing toward the
  strike — enter before it crosses.
- **`fade_down`** (→ direction `DOWN`): `gap > proximity_F` **and**
  `rate_1h ≤ −mom_thr_F · vol_mult`. The high has peaked short of the strike and is
  falling — the strike is unlikely to be reached.

**Defaults** (`Timing15mParams`): `proximity_F=1.5`, `mom_thr_F=0.5`,
`vol_mult=1.0` (the gate can be scaled by observed 15m volatility), `mom_lookback=4`,
`vol_lookback=8`. Trading window `14:00–21:00 UTC`, identical to `EntryTimingEngine`.

**Confidence** (deterministic, [0,1]): `0.6 · momentum_strength + 0.4 · proximity`,
where momentum strength = `min(1, |rate_1h| / (2·gate))` and proximity = `1 − |gap|/proximity_F`.

Signal schema: `Timing15mSignal(station, timestamp, trigger, direction, confidence,
high_so_far, strike, gap, rate_1h, vol_15m)` with `.as_dict()`.

## 5. Composition with the hourly EntryTimingEngine

`EntryTimingEngine.evaluate_hour` decides entry from probability divergence +
confidence gate + Kelly sizing, but is blind to the intraday trajectory. This
overlay is a **gate/enricher in front of it**:

- the hourly engine still owns sizing and the probability/edge gates;
- the overlay owns timing — a call is only actionable when a trigger is live;
- the overlay's `confidence` folds into the engine's `confidence` argument;
- both share the same trading-hours window, so they never disagree on the boundary.

```python
sig = latest_signal(buckets, strike, station)
if sig is not None and sig.direction == engine_direction:
    conf = 0.5 * engine_conf + 0.5 * sig.confidence
    entry = EntryTimingEngine().evaluate_hour(hour, hb, p, market_price, conf)
```

Zero new imports are required by `entry_engine.py`; the overlay is additive.

## 6. Verification

- `.venv-gefs/bin/python -m py_compile core/intraday/timing_15m.py` → clean.
- `pytest tests/test_timing_15m.py` → **13 passed** (96-bucket strict grid, monotone
  high-so-far, F-sanity guard, trigger fires on a real KNYC day, triggers confined to
  trading hours, schema, exact synthetic gate logic, determinism).
- Manual replay on KNYC 2026-06-15 (1440 real rows): 96 buckets, high 79.53 F; strike
  80/81 each fire `approach_up` inside 14:00–20:45 UTC; strike 79 fires nothing.

## 7. Notes / non-goals

- `big_sweep.py` and `PER_SIGNAL_BUDGETS` untouched; existing modules not rewritten.
- Read-only on every DB; no paper-trading DBs touched.
- The sweep's C→F bug is real but left in place per scope — flag to Gilfoyle before
  trusting any `intraday_multifreq_results.json` `w_hsf`-weighted CRPS.
