# Sub-hourly P(cross)=1.0000 leak — root-cause diagnosis

**Date:** 2026-09-19
**Branch:** `temp-sprint-branch`
**Investigator:** Gilfoyle subagent (deterministic, read-only, script-only)
**Scope:** diagnose — not fix. The bug is documented below; correction is proposed but NOT applied.

---

## TL;DR — the answer

The `P(cross)=1.0000` cells are **NOT** a thermometer mismatch, **NOT** a direct
settlement leak in the gap feature, and **NOT** a slope lookahead. They are a
**structural lookahead-by-construction**: the gap feature is computed from
`cur_max` — the maximum temperature observed up to time `T`, where `T` is
**3–4 hours before the daily hot-of-day (HOTD)** — while the settlement label
(`settlement_temp_f` = GHCN TMAX) is the **daily maximum** temperature, which by
definition occurs *after* `T`.

Because temperature is still rising during those 3–4 pre-peak hours, `cur_max@T`
systematically understates the eventual settlement, so `gap = strike − cur_max`
systematically *overstates* the headroom. The "near-strike" cells (`gap<1`,
`gap1-3`) select events whose temperature is only 1–2°F below the strike **and
still climbing**. They resolve YES because the diurnal warming carries the
temperature past the strike *after* the bet would be placed.

**This is a leak, not an edge.** The signal is predicting the daily warming
cycle, not a market inefficiency.

---

## Evidence

### 1. The gap feature is contemporaneous (rules out hypothesis #1)

`scripts/validate_subhourly_oos.py`:

```python
198:  cur_max = max(v for _, v in past)     # past = obs with m <= t (contemporaneous)
206:  "gap": strike - cur_max,              # uses cur_max, NOT settlement
207:  "already_crossed": 1 if cur_max > strike else 0,
```

`gap` uses `cur_max` (ASOS obs ≤ T), **not** `settlement_temp_f`. Hypothesis #1
("gap uses settlement = lookahead") is **FALSE**. The label is correctly kept
separate: `cross = 1 if settle > strike else 0` (line ~201).

### 2. Slope is also contemporaneous (rules out hypothesis #2)

```python
194:  win = [(m, v) for (m, v) in past if m >= t - TRAJECTORY_WINDOW_MIN]
```

Slope is fit on `past` (obs ≤ T) only. No future obs. Hypothesis #2 **FALSE**.

### 3. "Already-crossed" does NOT explain the perfect stations (rejects hypothesis #3)

Cross-tabulation of `already_crossed` against the P=1.0000 stations:

| station | n | already-crossed | P(cross) |
|---|---|---|---|
| KSAT | 35 | 20 (57%) | 1.0000 |
| KBOS | 24 | 6 (25%) | 1.0000 |
| KDEN | 17 | **0 (0%)** | 1.0000 |
| KMSP | 9 | **0 (0%)** | 1.0000 |
| KLAS | 6 | **0 (0%)** | 1.0000 |
| KPHX | 4 | **0 (0%)** | 1.0000 |
| KHOU | 4 | **0 (0%)** | 1.0000 |
| KMDW | 3 | **0 (0%)** | 1.0000 |
| KOKC | 3 | **0 (0%)** | 1.0000 |
| KDFW | 2 | **0 (0%)** | 1.0000 |

Eight of the ten perfect stations have **zero** already-crossed events, yet are
still perfectly "predicted." The mechanical "already happened" story in the
original report is **wrong** for the perfect cells. Hypothesis #3 **REJECTED**
as the root cause (it is a *secondary* effect, only ~21.8% of bets).

### 4. The settlement is the daily max, measured AFTER the feature time T

`settlement_temp_f` is sourced from **GHCN-Daily TMAX** (the true daily high).
The feature window is `t ∈ [hi_mod − 240, hi_mod − 180]` — i.e. **3–4 hours
before** the observed hot-of-day minute `hi_mod`.

Confirmed against ASOS completeness: for the perfect-station bettable dates the
ASOS 1-min series is near-complete and its full-day max closely matches the
GHCN settlement (e.g. KBOS 2026-02-26 ASOS max=43.0 vs settle=43.0; KDEN
2025-12-24 ASOS max=71.0 vs settle=71.1). The labels are sound; the **timing**
is the problem.

### 5. Quantified leak

Over all 275 bettable test events:

- **100.0%** (275/275) have `cur_max@T < full-day ASOS max − 0.5°F`.
- Temperature rises **mean 4.57°F, median 4.00°F, max 11°F** *after* time T.
- **56.7%** (156/275) of bettable events crossed the strike **after T**:
  `cur_max@T ≤ strike` yet `settle > strike`. These "wins" were not determinable
  at T from contemporaneous temperature alone.

---

## Root cause (exact)

**File:** `scripts/validate_subhourly_oos.py`
**Lines 186–187** (the window is pinned to the hot-of-day, before it):

```python
t_lo = max(0, hi_mod - WINDOW_BEFORE_HOTD[1])   # hi_mod - 240
t_hi = max(0, hi_mod - WINDOW_BEFORE_HOTD[0])   # hi_mod - 180
```

**Line 198** (`cur_max` is the running max up to a time that is itself before the peak):

```python
cur_max = max(v for _, v in past)
```

**Line 206** (the feature that inherits the bias):

```python
"gap": strike - cur_max,
```

The `gap` feature is not wrong *internally* — it correctly reflects the
temperature at time T. It is wrong *as a betting signal* because the settlement
is defined on the **daily maximum**, which is unobserved at T and will climb
toward (and past) the strike during the remaining 3–4 hours of diurnal warming.
The validator's own `already_crossed` guard (`cur_max > strike`) only catches the
minority of events already past the strike at T; it does **not** catch the 56.7%
that cross *after* T.

The original report's "decisive" slope-gradient test (section 5) inherits the
same bias: monotonic P(cross) 0.115 → 0.442 → 0.560 → 0.781 → 1.000 is largely
a restatement of "warmer + still warming ⇒ will hit the daily high," not
independent predictive power.

---

## Why it produces P=1.0000 specifically

The `gap<1` and `gap1-3` cells select the sub-population where the temperature
at T is *nearly* at (or just past) the strike. For those events the remaining
diurnal rise (mean ~4.6°F) almost always pushes the daily max past the strike,
so `settle > strike` is a near-certainty. Smaller-station cells (KSAT, KBOS,
KDEN, KMSP, …) concentrate on a handful of such "nearly-there and still-rising"
days, collapsing to a perfect hit rate. The larger, more diverse stations (KMIA,
KLAX, KPHL) dilute the effect because they mix in days where the temperature at
T was not so close to the strike.

---

## The fix (proposed, NOT applied)

The feature must be computed from **contemporaneous forecast/current temperature
relative to strike**, evaluated at the *actual trade time*, not from the running
daily max at a fixed lead before the settlement peak. Concretely:

1. **Replace `cur_max` (running daily max) with the current/forecast temperature
   at time T**, or at minimum use `temp(T)` — the instantaneous temperature at T —
   rather than `max(obs ≤ T)`. The running max is the most biased choice because
   it is monotonically non-decreasing toward the settlement quantity.
2. **Do not derive the label from the same 3–4h lead window.** If the market is
   "daily high > strike," then any feature that says "how far below the strike
   are we *right now, hours before the high*" is structurally leaky. Use a
   *fixed* reference time (e.g. a specific hour of day) or an explicit forecast
   of the daily high, and require that the feature be measured strictly at trade
   time with no knowledge of `hi_mod`.
3. **Gate the whole sub-hourly trajectory claim behind a proper "no peeking at
   HOTD" reconstruction.** `hi_mod` (the hot-of-day minute) is computed from the
   full day's ASOS data and used to *choose* the sample window — that is itself
   lookahead: at trade time you do not yet know when the high will occur. The
   `t_lo/t_hi` window is only known after the fact.

The cleanest correction is to fix `T` to a wall-clock time (e.g. "3 hours before
market settlement" or a named hour) rather than "3 hours before the observed
daily max," and to use `temp(T)` (or a contemporaneous NWP forecast of the daily
high) instead of the running daily max.

---

## Verdict

**Genuine leak.** The P=1.0000 cells are an artifact of measuring the gap at a
fixed lead *before* the daily peak while the settlement is the daily peak itself.
The "survives OOS" conclusion in
`reports/subhourly_oos_walkforward_2026-09-19.md` is **invalid** for the perfect
cells and, more broadly, for the slope-gradient "decisive" test, which inherits
the same diurnal-warming bias.

Not fixed here — per instructions, diagnosis + documentation only.
