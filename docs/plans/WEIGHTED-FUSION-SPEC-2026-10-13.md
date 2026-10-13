# Weighted Fusion Spec — Per-Station Model Weights

**Date:** 2026-10-13
**Author:** Gilfoyle (technical lane)
**Status:** DESIGN — not wired, not production. Bounded analysis off the read-only SBOX book.
**Companion artifacts:** `data/model_consensus.json`, `data/calibration_sketch.json`,
`scripts/model_consensus_2026-10-13.py`, `scripts/calibration_sketch_2026-10-13.py`
**Context:** `docs/plans/EXPERT-METHODOLOGY-REVIEW-2026-10-13.md` §0/§2.3 (E2 saturation,
no calibration gate at entry).

---

## 0. Bottom line

The book **does** store the component forecasts separately (`p_gefs`, `p_analog`,
`p_trajectory`, `p_fused` on `trades`; plus `n_analog`, `n_traj`, `agreement`, `edge_mult`,
`traj_verdict`, `traj_mult`, `fusion_rule` on `trade_forecaster_probs`). So per-station
weighted fusion is **possible today** — what is missing is a place to *store the weights*
and a per-trade stamp of the weights that were applied.

But the measurement also shows the components are **collinear to the point of being
redundant**: on the 68 closed trades, `p_gefs` and `p_analog` agree on direction **100%**
of the time, both at **72.1%** directional accuracy, and the 2nd-most-confident model has a
**0% rescue rate** when the top model is wrong. Re-weighting three copies of the same signal
buys nothing. The spec below is therefore written as a *mechanism* — it is honest about the
fact that its first-order payoff is small until the components are made genuinely
independent.

---

## 1. Current fusion (what we are replacing)

`core/intraday/multi_forecaster_fusion.py::fuse()`:

```
gap = |p_gefs - p_analog|
agree   (gap <= 0.10) -> p_core = 0.55*p_gefs + 0.45*p_analog,  edge x1.0
split   (gap <= 0.20) -> p_core = 0.50*p_gefs + 0.50*p_analog,  edge x1.25
disagree(gap  > 0.20) -> p_core = 0.5 + 0.5*(0.5*pg+0.5*pa - 0.5)  (shrink), edge x1.50
p_fused = 0.5 + traj_mult*(p_core - 0.5),  traj_mult in {0.75 discount, 0.95 soft, 1.05 confirm}
FUSION_RULE_ID = "fuse_v1:blend(gefs.55/analog.45)@agree|.5/.5@split|.5x@disagree;traj(x.75d|1.05c|.95s)"
```

Observations that motivate the redesign:

1. **Weights are global constants** (0.55/0.45). There is no per-station or per-region
   variation, even though the review's E11 flags that East/West skill splits (0.816 / 0.316)
   may be window/staleness artifacts — i.e. exactly the kind of thing per-station weights
   would need to be stratified by window to handle.
2. **Market is not a fusion component.** `market_price` enters only downstream in the
   entry-EV comparison, never as a weighted vote. The task asks for a `market_weight`.
3. **No weight provenance.** A booked trade records `fusion_rule` (the string id) but not the
   numeric weights applied. You cannot reproduce which weights produced `p_fused`.
4. **Selection bias in the measurement.** Because the pipeline books only `agree`/`split`
   signals, the closed-trade sample shows 100% component agreement — a property of the
   *selection*, not of the raw signal. Any weight tuning must be done on the **pre-gate
   candidate population**, not on booked trades, or it will learn from a censored sample.

---

## 2. Proposed fusion

### 2.1 Form

For station `s`, region `r(s)`, with components `GEFS`, `analog`, `trajectory`, `market`:

```
w_s = (w_gefs, w_analog, w_traj, w_market)          # non-overlap: sum(w_s) = 1
p_core(s) = w_gefs * p_gefs + w_analog * p_analog
          + w_traj  * p_traj  + w_market * p_market
```

Keep the existing trajectory gating semantics as a **separate multiplier** (it encodes
*agreement of the trajectory gate*, not a probability vote) until a station has enough
history to fold it into a weight:

```
p_fused(s) = clamp(0.5 + traj_mult * (p_core(s) - 0.5), 0.01, 0.99)
```

> Note: the trajectory component is currently used *twice* — as a `p_traj` vote inside the
> 3-model blend and as the `traj_mult` gate. The weighted form above makes the double-use
> explicit; pick **one** role per station once the sweep has data. Default recommendation:
> keep `traj` as the multiplier and set `w_traj = 0` initially, because the trajectory gate
> already carries directional information via `traj_verdict`.

### 2.2 Non-overlap constraint

`w_gefs + w_analog + w_traj + w_market = 1`, each `w >= 0`. This is the only hard constraint;
no upper bound per component (a single component may own the station if it is the only one
with skill there).

### 2.3 Component confidence and grading

Component **confidence** is derived (the store holds probabilities, not confidences):

```
conf_c = |p_c - 0.5| * 2          # 0 at coin-flip, 1 at certainty
```

Composite confidence for the station is the **weighted sum of component confidences**:

```
composite_conf(s) = w_gefs*conf_gefs + w_analog*conf_analog
                  + w_traj*conf_traj  + w_market*conf_market
```

Then apply **Dan's 2026-10-12 grade ladder** to the *composite confidence* (NOT to the raw
`p_fused`, which is the saturation bug):

| Grade | Threshold | Cap (USD) |
|---|---|---|
| S | `composite_conf >= 0.90` | 25 |
| A | `>= 0.80` | 15 |
| B | `>= 0.70` | 10 |
| C | `>= 0.60` | 5 |
| F | `< 0.60` | 0 (no entry) |

Entry gate: `grade >= C` **and** `p_core - market_price - round_trip_cost > 0`.

---

## 3. Tuning procedure

Tune on the **pre-gate candidate population** (all evaluated strikes, not just booked
trades) so the sample is not censored by the agreement gate.

1. **Assemble per-station history.** For each `(station, settlement_date, strike)` candidate:
   `p_gefs`, `p_analog`, `p_traj`, `p_market`, and the binary outcome. Requires a
   `strike_candidates` (or equivalent) table — see §4. Booked trades alone are insufficient.
2. **Score a weight grid.** For each station, sweep `w` on a simplex grid
   (e.g. step 0.05 over 4 components) and score by **directional accuracy** and/or **Brier
   score** on the *outcome* (not on whether the trade was booked).
3. **Guard against n.** A station with `< 30` candidates gets **equal weights** (or the
   region prior). Do not fit a 4-parameter simplex on 3 observations. Current per-station
   `n` ranges 1–13 (see `data/model_consensus.json`) — every station is below the floor.
4. **Regularize toward a region prior.** Fit region-level weights first (East / West / by
   decision-window), then shrink station weights toward the region mean by `n/(n+k)`.
5. **Re-tune on a rolling window** (e.g. 30 days) with the same OOS discipline already used
   for `p_fused` calibration; never tune and grade on the same window.
6. **Freeze the chosen weights** per station with a hash, and stamp them on every booked
   trade (§4).

---

## 4. Schema changes required

To make the above reproducible and auditable:

| Change | Where | Why |
|---|---|---|
| `market_weight` (and/or fold `market_price` into `p_core`) | fusion config | market is currently not a fusion vote |
| `weight_store` table: `(station, region, w_gefs, w_analog, w_traj, w_market, window_start, window_end, n, fit_metric, weight_hash)` | new table | per-station weights must be versioned and frozen |
| `weights_applied` JSON column on `trade_forecaster_probs` / `trade_custody` | existing tables | stamp the exact weights that produced `p_fused` for reproducibility |
| `composite_conf` REAL on `trade_forecaster_probs` | existing table | grade from weighted confidence, not saturated `p_fused` |
| `strike_candidates` table: `(station, date, strike, p_gefs, p_analog, p_traj, p_market, outcome)` | new table | tuning needs the *un-censored* candidate population |
| `component_conf_*` columns | optional | if confidences are ever computed upstream rather than derived as `|p-0.5|*2` |

All changes are **additive** (`ALTER TABLE ... ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`),
consistent with the repo's existing additive-schema pattern in
`multi_forecaster_fusion.ensure_schema()`.

---

## 5. Evidence from the current book (read-only)

Source: `data/model_consensus.json` (68 closed trades, 82 total).

- **Separate components stored:** YES — `p_gefs`, `p_analog`, `p_trajectory`, `p_fused`; 0
  nulls on closed rows.
- **GEFS vs analog agreement:** **100%** (68/68) — an artifact of the agreement gate that
  only books `agree`/`split` signals. Raw agreement must be measured pre-gate.
- **GEFS vs trajectory:** 95.6%; **analog vs trajectory:** 95.6%.
- **Component directional accuracy:** GEFS 72.1%, analog 72.1% (identical), trajectory 70.6%.
- **Does the 2nd/3rd model add anything?** No measurable lift: top-1 = 72.1%, confidence-
  weighted majority = 72.1%, fused = 72.1%; the 2nd-ranked model is *worse* (69.1%) and its
  rescue rate when the top model is wrong is **0%** (0/19).
- **Consensus tiers:** unanimous 65 (72.3% correct), 2-of-3 only 3 (66.7%), split 0.
- **Conclusion:** the components are collinear. Weighting is worth building as *mechanism*
  (per-station, auditable, market-inclusive), but the real gain requires **decorrelating the
  components** — e.g. different horizons, different data sources — before weight tuning can
  find signal. Do not expect a weighting change alone to move accuracy.

### Link to the entry-gate rebuild (`data/calibration_sketch.json`)

The calibration sketch shows why weights alone will not fix the selection problem: squashing
`p_fused` (0.50–0.99 → 0.50–0.75) drops the book to at most a **B** grade, yet the entry gate
still re-admits **63/68** trades (92.6%) because `market_price << 0.50` keeps EV positive.
The squash lowers confidence but does **not** change the selection *ranking*. A
cost/liquidity filter (`spread/mid`, depth) is the missing complement (review E3/E5). The
weighted-fusion change should ship **with** that filter, not instead of it.

---

## 6. Open questions for the C-suite

1. Is `market` a *vote* (weight) or a *benchmark* (only used at the entry-EV gate)? The task
   asks for a `market_weight`; folding the market into `p_core` risks double-counting it in
   the EV comparison. **Recommendation:** keep market out of `p_core` (weight 0) and use it
   only at the gate, unless the sweep shows a market vote adds skill.
2. Do we have the **pre-gate candidate population** stored anywhere? Without it, tuning is
   censored and the weights will be fit to the selection. This is the gating dependency.
3. Is the trajectory gate a vote or a multiplier? (Recommendation: multiplier, `w_traj = 0`.)
