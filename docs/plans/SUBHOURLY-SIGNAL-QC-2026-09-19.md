# Sub-hourly Trajectory Signal — Independent QC Report

**Date:** 2026-09-19
**QC by:** Independent QC lane (not the builder)
**Scope:** spec→build→QC→confirm — verify build against spec, reproduce OOS edge.
**Build under review:**
- spec `88437c545` — `docs/plans/SUBHOURLY-SIGNAL-SPEC-2026-09-19.md`
- module `f7a653861` — `core/signals/subhourly_trajectory_signal.py` (+ registry edit)
- self-test `8034b49a7` — `scripts/test_subhourly_trajectory_signal.py`

---

## Verdict

**CONDITIONAL PASS — one blocking registry defect must be fixed before wire-in.**

The module **conforms to the spec's arithmetic and reproduces the OOS anticipatory
cell exactly** (n=155, P(cross)=0.7871, Wilson LB=0.7161). Slope, gap, unbounded-slope
threshold, already-crossed exclusion, strict `>` axis, and °F units are all correct and
deterministic, with no feature-level lookahead. **However, the signal is NOT actually
registered in any scoring sweep** — it will be silently dropped (the exact NO_FIRES /
NO_SCORE failure mode). Fix the registry before wire-in.

---

## Checklist results

### 1. SPEC CONFORMANCE — **PASS** (with one minor interface deviation)

| Spec item | Result |
|---|---|
| Slope = 90-min trailing OLS, 5-min downsample, ≥4 obs | ✅ matches validator `ramp_slope()` / `downsample()` exactly (verified char-for-char against `ef1e9d8c0`) |
| `MIN_OBS_FOR_FIT = 4`, `TRAJECTORY_WINDOW_MIN = 90`, `RES = 5` | ✅ constants identical to validator |
| gap = strike − cur_max, `[1.0, 3.0)` strict | ✅ `GAP_LO=1.0` (inclusive), `GAP_HI=3.0` (exclusive), implemented as `GAP_LO <= gap < GAP_HI` |
| slope ≥ 0.05 UNBOUNDED upper | ✅ `SLOPE_THRESHOLD=0.05`, no upper cap — correct (the "n=137" is a validator slope-BIN artifact, not a threshold) |
| already_crossed exclusion (`cur_max > strike` → abstain) | ✅ implemented as `prediction = None` |
| strict `>` axis (`settlement_temp_f > strike_price`) | ✅ `outcome = 1 if settle > strike else 0`; `strike_type='greater'` + `trustworthy=1` filter matches validator |
| §10 `compute_gap(...)` method | ⚠️ **MINOR DEVIATION** — spec lists `compute_gap(self, past_obs, strike) -> dict` as a method; module inlines gap in `evaluate_nowcast` and does NOT expose `compute_gap`. Correctness unaffected (logic is right), but interface is not spec-literal. |

### 2. NO LOOKAHEAD — **PASS** (feature-level airtight)

- Independently confirmed `evaluate_nowcast` filters `past = [(m,v) for m <= t_minute]`
  for **both** slope (`win`) and `cur_max`.
- Constructed a synthetic day with a future 96°F spike; at `t=20` the module returned
  `cur_max=51.5` (not 96.0), `already_crossed=False`, `gap=8.5` — no leak.
- Settlement truth is label-only (`outcome`), never a feature.
- **Note (non-blocking):** `iter_nowcast_events` selects *which minutes* to evaluate using
  the full-day daily-high time (`_hotd_minute` over the whole day) to bound the pre-HOTD
  window `[hi−240, hi−180]`. This is a mild *event-selection* lookahead (inherited
  identically from the validator `ef1e9d8c0`, and matches spec §8 verbatim). Features at
  each `t` are still contemporaneous-only. Flagging for awareness; not a spec violation.

### 3. OOS REPRODUCTION — **PASS** (exact parity)

Independent re-score of the module on held-out `2025-07-01 → 2026-09-18`:

| Metric | QC (module) | Target | Status |
|---|---|---|---|
| anticipatory cell n | **155** | ≈155 | ✅ |
| P(cross) | **0.7871** | ≈0.787 | ✅ |
| Wilson LB | **0.7161** | ≈0.716 | ✅ |
| cross preds | 155 | — | ✅ |

Slope-bin breakdown within gap∈[1,3) reproduces the validator's slope-gradient table
exactly (`0.05-0.10`: n=137 P=0.781; `0.10-0.15`: n=15 P=1.0; `>=0.15`: n=3 P=0.0),
confirming the n=155 is the UNBOUNDED `slope≥0.05` cell (137 + 15 + 3 = 155) and the
"n=137" reference in the spec is the validator's `0.05–0.10` bin-cap artifact. Build
correctly implements the unbounded threshold per the confirmed reading.

Minor divergence (cosmetic, diagnostic-only): the validator's `0-0.02` slope bin shows
n=156 vs my independent n=163. Cause: the validator's `emit_events` *skips* `len(win)<2`
events; the module emits them with slope=0.0. This is a 7-event difference confined to
the near-zero slope bin and does **not** touch the anticipatory cell (slope≥0.05).

### 4. UNIT CORRECTNESS — **PASS**

- ASOS source column is `tmpf` (°F); observed range −49..122 °F — unambiguously Fahrenheit.
- `settlement_temp_f` range −34.1..122 °F; `strike_price` range −14..120 °F — all °F.
- Module reads only `tmpf`/`settlement_temp_f`/`strike_price` (all `_f`), slope is
  °F/min. No °C/°F mixing.

### 5. REGISTRY — **FAIL** (blocking: silently dropped)

The signal is **NOT registered in any scoring sweep**:

- In `core/signals/__init__.py`, `SubhourlyTrajectorySignal` is imported and set as an
  **instance attribute** (`self.subhourly_trajectory = ...`) but is **NOT added to the
  `self.signals` dict** — so `get_all_signals()` does **not** return it.
- The ultimate sweep's canonical enumeration (`scripts/ultimate_sweep.py`,
  `scripts/ultimate_suite.py`, `scripts/sweep_engine.py` `build_signal_registry()`) does
  **not** include `subhourly_trajectory` (verified: zero references to
  `subhourly_trajectory` / `SubhourlyTrajectory` / `evaluate_nowcast` / `iter_nowcast`).
- Only `test_subhourly_trajectory_signal.py`, `measure_subhourly_trajectory.py`, and
  `measure_subhourly_sweet_spot.py` reference it — none is the ultimate sweep.

Consequence: the ultimate sweep will score **zero** sub-hourly trajectory events —
exactly the NO_FIRES / silently-dropped failure mode the QC checklist warns against.
The spec §10 states "the sweep scores via `evaluate_nowcast`," but no sweep currently
calls `evaluate_nowcast` or iterates `iter_nowcast_events`.

---

## Required fix before wire-in

1. **Register the signal in the ultimate sweep's signal enumeration** — add
   `_reg("subhourly_trajectory", "subhourly_trajectory_signal", "SubhourlyTrajectorySignal")`
   to `build_signal_registry()` in `scripts/ultimate_sweep.py` (and `ultimate_suite.py` /
   `sweep_engine.py` as applicable), AND wire a sweep path that iterates
   `iter_nowcast_events()` (the daily-day `evaluate(idx, days)` interface returns
   `(None, 0.0)` and is NOT the scoring path for this signal).
2. **Optionally** also add `subhourly_trajectory` to the `self.signals` dict in
   `core/signals/__init__.py` for registry-consistency (in addition to the current
   attribute-only exposure), so `get_all_signals()` includes it.
3. **Minor (non-blocking):** implement `compute_gap(...)` per spec §10, or amend the
   spec to drop the method (the inline gap logic is already correct).

## Bottom line

The **math and data path are correct and OOS-reproducing** — no arithmetic, lookahead,
axis, or unit defect. The **single blocking issue is the missing registry wiring**: as
committed, the signal would score nothing in the ultimate sweep and silently vanish.
Fix the registry (item 5), then wire-in may proceed (still behind the separate
Dan-gated live/paper-trading step per spec §9.5).
