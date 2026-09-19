# Sub-hourly Trajectory Signal — Build Spec

**Date:** 2026-09-19
**Author:** Gilfoyle (build lane, spec→build)
**Status:** authoritative build spec (derived from OOS validation `ef1e9d8c0`)
**Target module:** `core/signals/subhourly_trajectory_signal.py`

---

## 1. Purpose

A sub-hourly **temperature-trajectory slope nowcast** that predicts whether the
settlement temperature will **cross the strike** (settle strictly above strike on a
"greater-than" market). This is a *predictive* directional signal (cross / no-cross),
NOT the mechanical `gap<1°F` near-realized cell.

The OOS-validated, genuinely anticipatory cell is:

> **gap-to-strike ∈ [1, 3) °F  AND  slope ≥ 0.05 °F/min**

This is the cell the signal promotes. The mechanical `gap<1°F` cell is explicitly
excluded (it is "already happened" — 21.8% already crossed at evaluation time).

---

## 2. Inputs

### 2.1 Temperature observations (contemporaneous-only)
- **Source:** `data/iem_asos_1min.db` → table `observations`
  - Columns: `station`, `valid` (ISO-8601 UTC), `tmpf` (°F)
- **Resolution:** genuine 1-minute ASOS (NCEI source, `asos1min.py`).
- **Contemporaneous-only:** only observations with `valid ≤ nowcast_time T` are used.
  No lookahead. Settlement truth is label-only (never a feature).

### 2.2 Strike / settlement truth (label-only)
- **Source:** `data/metar_backfill.db` → table `settlement_truth`
  - Columns: `station`, `market_type`, `local_trading_date`,
    `settlement_temp_f`, `strike_price`, `strike_type`, `trustworthy`
- **Filter:** `strike_type = 'greater'`, `trustworthy = 1`, strike + settlement non-null.
- **Axis:** settlement_temp_f vs strike_price, strict `>` → `cross = settlement_temp_f > strike_price`.

### 2.3 Station timezone map
- Station → IANA local timezone (same `_ICAO_TZ` map used by the OOS validator).
- Used to compute local trading day, the minute-of-day for the trajectory window, and
  the daily-high ("HOTD") anchor.

---

## 3. Slope computation (deterministic)

Slope is an **ordinary least-squares (OLS) linear regression** of temperature (`y`) on
minute-of-day (`x`) over a trailing trajectory window. Matches the OOS validator's
`ramp_slope()` exactly:

```
slope = Σ (xᵢ − x̄)(yᵢ − ȳ) / Σ (xᵢ − x̄)²        (units: °F/min)
```

- `x` = minute-of-day (local), `y` = temperature `tmpf` (°F).
- If `denom ≤ 0` or `< 2` points → slope = 0.0 (no fit).

### 3.1 Trajectory window
- `TRAJECTORY_WINDOW_MIN = 90` minutes (look back from nowcast time T).
- `MIN_OBS_FOR_FIT = 4` points required to compute a slope.
- Resolution: `RES = 5` minutes (downsample 1-min observations to 5-min steps to
  reduce autocorrelation noise, matching the validator).

---

## 4. Gap computation

```
gap_to_strike (°F) = strike_price − cur_max_temp
cur_max_temp       = max(tmpf over all contemporaneous observations so far that day)
```

- `cur_max_temp` is the running daily max as of nowcast time T (contemporaneous-only).
- `gap` is positive when temperature is *below* strike (has not yet crossed).

### 4.1 Already-crossed guard
- `already_crossed = (cur_max_temp > strike_price)`.
- The signal **does not fire** a cross prediction when `already_crossed` is true —
  this is the "already happened" regime the OOS validation flagged as mechanical.

---

## 5. Decision rule (the anticipatory cell)

The signal fires a **cross** prediction only when BOTH hold:

1. **gap ∈ [1.0, 3.0) °F**  (strict: `1.0 ≤ gap < 3.0`)
2. **slope ≥ 0.05 °F/min**

Otherwise the signal fires **no-cross** (or abstains, per §7).

| Condition | Direction | Rationale |
|---|---|---|
| gap ∈ [1,3) AND slope ≥ 0.05 | **cross** | OOS P=0.781, Wilson LB=0.705 (n=137), the genuinely anticipatory cell |
| already_crossed (cur_max > strike) | abstain (no-cross) | mechanical "already happened" — excluded by OOS validation |
| gap < 1 | abstain | mechanical near-realized cell — deliberately excluded |
| gap ≥ 3 | no-cross | too far from strike to be anticipatory |
| slope < 0.05 | no-cross | insufficient trajectory momentum |

---

## 6. Confidence

Confidence is a **deterministic** function of the slope, bounded to [0, 1]. It is
monotonic in slope (per the OOS slope-gradient: 0.115 → 0.442 → 0.560 → 0.781 → 1.000),
but is NOT a trained probability — it is a ranking score so the ultimate sweep can score
and calibrate it.

```
confidence = clip( slope / SLOPE_CONF_REF , 0.0, 1.0 )
SLOPE_CONF_REF = 0.15   # °F/min  (slope 0.15 → confidence 1.0)
```

- `SLOPE_CONF_REF = 0.15 °F/min` — chosen because the OOS slope-gradient plateaus to
  P=1.0 at the `0.10–0.15` bin.
- For a **no-cross** direction, confidence is `1 − confidence_cross`-style (mirror), but
  the module's *primary* output is the cross/no-cross label + a cross-directional
  confidence. No-cross confidence is reported as `1.0 − p_cross_confidence` (deterministic).

---

## 7. Abstention vs. no-cross

The `evaluate` contract returns `(direction, confidence)` where direction ∈ {`'up'`,
`'down'`, `None`}.

- `'up'`  → cross prediction (gap∈[1,3) AND slope≥0.05).
- `'down'`→ no-cross prediction (in a valid evaluation window, but the anticipatory
  cross cell did not trigger).
- `None` → abstain (insufficient data / no valid window / already-crossed).

The **primary** nowcast entry-point (`evaluate_nowcast(station, trading_date, t)`),
used by the sweep, returns a richer result object (§10) including `gap`, `slope`,
`already_crossed`, `prediction` (`'cross'`/`'no-cross'`/`None`), and `confidence`.

---

## 8. Time window / HOTD anchor

Matching the OOS validator:

- `TYPICAL_HOTD_MIN = 15*60 + 30 = 930` (approx 15:30 local) — reference only.
- The nowcast window is the **3–4 hours before the daily high (HOTD)**:
  - `WINDOW_BEFORE_HOTD = (180, 240)` minutes → `t ∈ [hotd − 240, hotd − 180]`.
- The daily high (`hotd`) is computed from the day's own ASOS observations
  (contemporaneous daily max minute), NOT a fixed clock.

For the deterministic module, the nowcast is evaluated at each candidate minute `t`
within that pre-HOTD window (step `RES = 5`), using only observations `≤ t`.

---

## 9. Constraints (non-negotiable)

1. **Deterministic** — pure arithmetic, no randomness, no ML, no AI in the loop.
2. **Contemporaneous-only** — `valid ≤ t` only; settlement is label-only.
3. **Correct axis** — `settlement_temp_f > strike_price` (strict), strike_type='greater'.
4. **No lookahead** — no future observations, no settlement, no post-hoc strike info.
5. **No live/paper wiring** — this module is registered for scoring only; it is NOT
   wired into the live/paper trading path (separate Dan-gated step).
6. **No storage bloat** — read-only DB access; no new tables, no caches.

---

## 10. Module interface

```
class SubhourlyTrajectorySignal(BaseSignal):
    name = "subhourly_trajectory"
    min_lookback = 0  # sub-hourly; does not use the daily-day window interface

    def evaluate_nowcast(self, station, local_trading_date, t_minute) -> dict
    def compute_slope(self, obs_window) -> float
    def compute_gap(self, past_obs, strike) -> dict
```

`evaluate_nowcast(...)` returns:
```
{
  "station": str,
  "trading_date": str,
  "t_minute": int,
  "strike": float,
  "settle": float,          # label only (not used to predict)
  "cur_max": float,
  "gap": float,
  "slope": float,
  "already_crossed": bool,
  "prediction": "cross" | "no-cross" | None,
  "confidence": float,       # 0..1
  "outcome": 0 | 1,          # label: 1 if settle > strike (for scoring)
}
```

The standard `evaluate(idx, days)` / `evaluate_for_station` daily-day interface is NOT
used for this signal (sub-hourly axis). It returns `(None, 0.0)` by default; the sweep
scores via `evaluate_nowcast`.

---

## 11. Reproduction target (self-test acceptance)

The self-test must demonstrate, on the **held-out period (2025-07-01 → 2026-09-18)**:

1. **FIRES** — `evaluate_nowcast` emits a non-`None` prediction on `> 0` labels.
2. **Reproduces the OOS anticipatory cell** — the `gap∈[1,3) AND slope≥0.05` cell
   yields P(cross) ≈ 0.78 (Wilson LB ≈ 0.705, n≈137), matching `ef1e9d8c0`.
3. **No lookahead** — `already_crossed` events are abstained (0 cross predictions with
   `cur_max > strike`).
4. **Correct axis** — outcomes computed as `settlement_temp_f > strike_price`.

Exact numeric parity with the validator is expected (same data, same arithmetic); any
divergence is a bug to flag.

---

## 12. Out of scope (explicit)

- Live/paper-trading wiring (separate Dan-gated step).
- Re-baselining the non-stationary cross base rate (noted caveat; handled at sizing).
- The mechanical `gap<1°F` cell (excluded by design).
- Confidence calibration to true probabilities (the sweep calibrates; module emits a
  deterministic ranking confidence).
