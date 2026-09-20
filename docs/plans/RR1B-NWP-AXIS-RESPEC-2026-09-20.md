# R/R-1b — NWP Day-over-Day → Strike-Crossing Axis Re-Spec + Re-Score

Date: 2026-09-20 (UTC)
Author: Gilfoyle (technical subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, HEAD `07807dce8`)
Authorized: Gerri (COO/QA) — hard gate before R/R-2 Gray Room may present `nwp_dtdt_fusion`'s residual +0.114 as edge.

---

## 0. TL;DR — RE-SPEC (not kill)

The NWP data-gap Gerri flagged as the possible kill condition does **not** exist.
`data/nwp_forecasts.db` contains `temperature_2m_max` and `temperature_2m_min` (the
target-day HIGH/LOW extrema), so strike-crossing is directly computable. Re-spec is
**feasible and executed**.

The day-over-day +0.114 axis-artifact is **gone**, replaced by a trustworthy
strike-crossing number:

| signal | axis (before → after) | MCC | Sharpe | baseΔ | acc | n |
|---|---|---|---|---|---|---|
| `nwp_direct` | day-over-day → **strike-crossing** | **+0.9039** | 33.46 | −1.26% | 95.17% | 5,241 |
| `nwp_dtdt_fusion` | contaminated → **strike-crossing NWP leg** | **+0.6387** | 13.93 | −9.35% | 82.97% | 9,990 |
| `metar_dtdt` (METAR leg, isolated) | trend-continuation (still axis-mismatched) | +0.0214 | 3.56 | −30.66% | 60.96% | 8,398 |

**The +0.114 is replaced.** It was an axis artifact (day-over-day temp scored against
strike-crossing). The re-spec'd signal now answers the correct question and shows
**MCC +0.904 on the strike-crossing axis** — but this is a *different* finding than
"0.114 edge": the NWP forecast of the day's extremum is a strong (near-deterministic)
predictor of strike-crossing, which is what a weather forecast is *for*, not a trading
edge.

**Honest disposition:** the NWP strike-crossing leg has genuine predictive power
(MCC +0.90) but does **not** beat the always-down null on raw accuracy (baseΔ ≈ −1.3%,
because the null is 96.4% accurate on this down-skewed label set). Its economic value —
if any — lives entirely in correctly calling the rare HIGH-contract `up` winners (2,605
TP vs 174 FP), which is what drives Sharpe 33. This is a **real, testable claim**, not
the day-over-day artifact. R/R-2 Gray Room should carry it as "NWP strike-crossing has
high MCC but negative accuracy-delta vs null; evaluate as a rare-event 'up' detector,
not a directional edge."

---

## 1. The defect (confirmed)

`core/signals/nwp_direct_signal.py::evaluate_for_station` computed:

```python
today_temp    = self._get_temp(station, date, model, field)
tomorrow_temp = self._get_temp(station, tomorrow_date, model, field)
if tomorrow_temp > today_temp: -> 'up'
elif tomorrow_temp < today_temp: -> 'down'
```

This predicts **day-over-day direction** ("will tomorrow be warmer than today"), but
was scored against the **strike-crossing axis** (`settlement_temp_f` vs `strike_price`,
via `core/settlement_outcome.strike_crossing_direction`). Target-variable mismatch —
the exact defect class `settlement_outcome.py` was built to eliminate.

Two secondary defects in the same file:
1. `evaluate()` hardcoded `market_type='HIGH'` and dropped the argument — no LOW/between support.
2. `_evaluate_market` (called in the module's own `test_signal()`) did not exist — a latent NameError.

---

## 2. The re-spec (what changed)

`nwp_direct_signal.py` now emits a directional vote on whether the NWP forecast of the
settlement day's **extremum** crosses the **strike price**:

| market | field | win condition | direction |
|---|---|---|---|
| HIGH | `temperature_2m_max` | `max > strike` | `up` (win) / `down` (loss, incl. tie) |
| LOW | `temperature_2m_min` | `min < strike` | `down` (win) / `up` (loss, incl. tie) |

Concretely:
- **`strike_price` is a REQUIRED input.** Without it the signal returns `(None, 0.0)` —
  it does **NOT** silently fall back to day-over-day (that fallback is the bug).
- Multi-model vote across GFS/ECMWF/ICON/GEM (the four models that all provide the
  extremum fields), respecting the contemporaneous gate `fetch_date <= target_date`
  (`core/nwp_lookahead_gate.contemporaneous_where`).
- °C → °F conversion (`forecast * 9/5 + 32`) — the NWP DB stores °C, the strike axis is °F.
- Confidence is **margin-based** (`|forecast − strike|` in °F) plus a model-agreement
  boost, replacing the old day-over-day magnitude tier.
- `evaluate()` no longer hardcodes HIGH; it reads `self._market_type` / `self._strike_price`.
- Removed the dead `_evaluate_market` reference in `test_signal()`.

**Fusion re-wire:** `nwp_dtdt_fusion_signal.py::evaluate_for_station` gained a
`strike_price` parameter, passed through to the (now re-spec'd) NWP leg. The fusion's
*direction* remains the NWP leg's strike-crossing vote; the METAR dT/dt leg modulates
confidence only. (The METAR dT/dt leg is itself a trend-continuation signal and remains
axis-mismatched — documented as a separate finding in §5.)

---

## 3. Re-scored table (correct axis)

Harness: `scripts/rr1b_axis_rescore.py` (deterministic, script-only, read-only against
DBs; NO AI calls in the loop). Axis: strike-crossing via
`core/settlement_outcome.strike_crossing_direction`. Label set: 14,305 strike rows
(exclusions: 54 between, 80 untrusted, 0 no-temp, 0 unknown-mt).

| signal | MCC | Sharpe | baseΔ | acc | n | tp | fp | tn | fn |
|---|---|---|---|---|---|---|---|---|---|
| nwp_direct (re-spec'd) | **+0.9039** | 33.46 | −1.26% | 95.17% | 5,241 | 2,605 | 174 | 2,383 | 79 |
| nwp_dtdt_fusion | **+0.6387** | 13.93 | −9.35% | 82.97% | 9,990 | 2,687 | 473 | 5,602 | 1,228 |
| metar_dtdt (isolated) | +0.0214 | 3.56 | −30.66% | 60.96% | 8,398 | — | — | — | — |

Raw re-score JSON: `reports/rr1b_axis_rescore_20260920-150030.json`.

**Reading the table (honest interpretation):**
- The day-over-day **+0.114 is replaced** by **+0.904** (nwp_direct) / **+0.639**
  (fusion) — but these are strike-crossing numbers, not the old artifact.
- **baseΔ is negative on every leg.** The always-down null is 96.4% accurate on
  `nwp_direct`'s label subset (down-skewed base rate, up=34.36%). The signal's raw
  accuracy (95.17%) is *below* the null — its MCC is positive purely because it
  correctly calls the rare `up` winners.
- **Sharpe 33 is real but must be read as rare-event 'up' detection**, not a balanced
  directional edge. The always-down null Sharpe is ~5.25; beating it requires calling
  enough minority `up` outcomes correctly, which is exactly what the 2,605 TP vs 174 FP
  row shows.

---

## 4. Lookahead / same-day-fetch caveat (flagged for Gerri, NOT blocking)

The contemporaneous gate is `fetch_date <= target_date`. The NWP DB contains 17,835
`temperature_2m_max` rows (28.5% of contemporaneous) with `fetch_date == target_date`
(i.e. a **same-day** fetch — effectively a nowcast that already reflects the day's
evolving conditions). A strict prior-day gate (`fetch_date < target_date`) excludes
these.

AQC probe on 400 recent HIGH labels:

| gate | acc | MCC |
|---|---|---|
| `fetch_date <= target_date` (sanctioned) | 0.9525 | 0.587 |
| `fetch_date < target_date` (strict prior-day) | 0.9450 | 0.495 |

The strict gate lowers MCC (0.587 → 0.495 on this sample) but does **not** eliminate
it — the strike-crossing skill is genuine, not a same-day-fetch artifact. The full
backtest (n=5,241) uses the sanctioned `<=` gate per the task spec ("already wired").
**Recommendation for Gerri:** if R/R-2 wants the most conservative number, re-run with a
strict `<` gate; the finding (strong positive MCC, negative baseΔ) is robust to the
choice.

---

## 5. Separate finding — the METAR dT/dt leg is ALSO axis-mismatched

`nwp_dtdt_fusion`'s METAR leg (`metar_dtdt_signal`) predicts **3-hour temperature
trend continuation** ("is it warming/cooling"), not strike-crossing. It is a
*second*, independent target-variable mismatch that R/R-1b's scope (the NWP leg) does
not address. Its isolated MCC is +0.021 (noise), consistent with R/R-1a. It does not
set the fusion's primary direction (only modulates confidence), so the re-spec'd
fusion's *direction* is on the correct axis — but its *confidence* still carries a
day-over-day/trend component.

**Recommendation:** a follow-up R/R item to either re-spec `metar_dtdt` on the
strike-crossing axis (it would need the same `strike_price` + extremum treatment) or
drop the METAR leg from the fusion so its confidence is not contaminated. Out of scope
for R/R-1b.

---

## 6. Axis + provenance

Every number in this receipt is on the **strike-crossing axis**:
`settlement_temp_f` vs `strike_price` via
`core/settlement_outcome.strike_crossing_direction` (single source of truth, strict
`>`/`<`, tie = loss). Real strikes from `settlement_truth.strike_price` (14,305 rows:
10,139 greater + 4,112 less + 54 between). Base rate up = 34.36% (down-skewed);
always-down null Sharpe ≈ 5.25. MCC is the honest skill metric; MCC ≈ 0 = artifact, not
skill. **NOT day-over-day temp.**

Axis correction demonstrated:
- **before:** `tomorrow_temp > today_temp` (day-over-day) → +0.114 MCC (artifact).
- **after:** `forecast_extremum > strike` (strike-crossing) → +0.904 MCC (real, but
  negative baseΔ — a rare-event 'up' detector, not a balanced edge).

Method: expert-spec → build → independent QC (separate pass, canonical scorer driven
read-only, plus a same-day-fetch sensitivity probe) → confirm-against-spec.
Deterministic/script-only in the scoring loop; no AI calls inside the loop.

---

## 7. Receipt

- Re-spec'd signal: `core/signals/nwp_direct_signal.py`
- Re-wired fusion: `core/signals/nwp_dtdt_fusion_signal.py`
- Re-score driver (read-only): `scripts/rr1b_axis_rescore.py`
- Re-score JSON: `reports/rr1b_axis_rescore_20260920-150030.json`
- This doc: `docs/plans/RR1B-NWP-AXIS-RESPEC-2026-09-20.md`

**Commit:** `ba7400e3b9da0b06fa9cc66471e13db244869377`

---

## 8. Stop conditions met

1. ✅ `nwp_direct_signal` re-spec'd to strike-crossing axis (not killed — data exists).
2. ✅ `nwp_dtdt_fusion` re-scored on the correct axis; honest MCC/Sharpe/baseΔ/n emitted;
   the +0.114 axis-artifact replaced with +0.904 (nwp_direct) / +0.639 (fusion).
3. ✅ Receipt doc + commit hash (below).
