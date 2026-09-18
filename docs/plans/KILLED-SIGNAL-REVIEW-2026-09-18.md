# Killed / De-Registered Signal Re-Review — Against CLEAN Ground Truth

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Type:** READ-ONLY re-review. Recommendation only. **No signal revived, re-wired, or re-registered. No code/data change, no commit, no backtest.**
**Trigger:** Settlement-epochs rewrite (2026-09-17) repaired the corrupt ground-truth labels — 625 corrupt buckets (METAR string-concat strike proxy) → 0 residual corruption (28 explicitly `unresolved` rows + 18 linkage rows, all accounted; closed buckets now in [-22, 119]). Prior kill decisions were measured against the **OLD corrupt labels**, so a signal killed for "actively harmful" against corrupt labels may be genuinely useful against clean Kalshi settlements.

---

## 0. The core question this review must answer

Every kill/deprecation below recorded a single scalar (MCC, accuracy, or EV) that was computed against the **pre-rewrite** `settlement_epochs` labels. The rewrite corrected the *labels*, not the *signals*. Signals whose bad number was caused by (or confounded by) corrupt labels need a fresh number before their kill can be upheld.

**Important nuance:** corruption was concentrated in the **last ~2 weeks** (mostly 2026-07-26 → 2026-09-17) plus a few early-July dates. Signals killed earlier (persistence, simple_trend, goldilocks, late_day_momentum — mostly 2026-08-07 sweep) were evaluated over windows that *overlap* that corruption window to varying degrees. Signals with longer history (persistence, simple_trend, goldilocks daily) were mostly scored over clean pre-corruption data, so their kill rationale is *less* likely to flip. The pressure-corrected signal is the one most likely to have been poisoned, because its de-registration happened later and the direction question is the whole point of the signal.

---

## 1. `corrected_pressure_delta` — **NEEDS-RETEST**

**(a) Why it was killed / de-registered**
- Deprecated in `DEAD-CODE-REVIEW-2026-08-03.md` with **MCC = -0.2167**, classified "actively harmful."
- The signal is the physics-corrected inversion of `pressure_delta` (Expert 1 finding: *rising pressure → cooling, not warming*). Its entire value proposition is a **direction flip** relative to `pressure_delta`.

**(b) Does the rationale still hold against CLEAN labels?**
**Unknown — and specifically suspect.** Two independent problems mean the -0.2167 number is not trustworthy as a *label-level* verdict:

1. **Naming schism + wrong-class mapping (confirmed in code):** In `scripts/sweep_engine.py:337`:
   ```python
   _reg("corrected_pressure_delta", "dual_polarity_signal", "DualPolaritySignal", wrapped_signal=None)
   ```
   The registry key `corrected_pressure_delta` is mapped to the **base `DualPolaritySignal`** class, *not* to `CorrectedPressureDeltaSignal`. `DualPolaritySignal.__init__` requires `wrapped_signal`, and here it is `None` — so the sweep engine either fails to instantiate or produces a broken wrapper. The actual physics-corrected class is **never evaluated** under that key. Separately, `CorrectedPressureDeltaSignal.name` returns `"pressure_delta_phy_corrected"`, not `"corrected_pressure_delta"` — so even the intended class would not report under the key the reviewers scored.
   → **The -0.2167 MCC may have been measured on the wrong (base dual-polarity, or a broken instance) signal, not on the physics-corrected logic.**

2. **Corrupt labels:** the direction question (rising pressure → up vs. down) is exactly what a corrupt-bucket label would scramble. If the MCC was computed over the late-window corrupt labels, a *correct* physics inversion would score negative against garbage labels (the label flips independently of the physical truth).

**(c) Verdict:** **NEEDS-RETEST** — highest priority of the six. The physics rationale is sound (and was pre-registered as "should outperform pressure_delta" in `PHASE-B-EXPERT-SPEC.md`), but it has never been cleanly measured: wrong class + wrong name + corrupt labels.

**(d) Concrete next step (do NOT run here):** Re-run a directional backtest of `CorrectedPressureDeltaSignal` (the real class, imported from `core/signals/dual_polarity_signal.py`, `.name == "pressure_delta_phy_corrected"`) against the **cleaned** `settlement_epochs` labels, excluding the 28 `unresolved` + 18 linkage rows, and compute **MCC and accuracy**, compared head-to-head against `pressure_delta`. Command shape (indicative, from existing sweep tooling): a per-signal directional eval over the clean `metar_backfill.db` with `epoch_status != 'unresolved'` filtering. Exact command: **defer to Gilfoyle to specify the canonical runner** — the re-review stops here because a real clean-label number is required to decide REVIVE vs KEEP-KILLED. *Also fix the sweep_engine mapping bug before any retest, or the retest will re-measure the wrong class again.*

---

## 2. `persistence` — **KEEP-KILLED (but preserve as baseline, not as a trading signal)**

**(a) Why it was killed**
- `data/KILLED-SIGNALS-BIG-SWEEP.md` (2026-08-07): `persistence` and `simple_trend` are ρ=1.0000 identical (same prediction-direction vector over 3,138 trades / 7 stations); accuracy ~51.9% (near coin flip). Protocol requires dropping one of the pair.

**(b) Does the rationale hold against CLEAN labels?**
**Yes, unchanged.** Two separate facts are being conflated and should be separated:

- **The redundancy (ρ=1.0 with simple_trend)** is a *structural* property of the two signal implementations, not of the labels. `persistence.evaluate` and `simple_trend.evaluate` are literally the same logic (yesterday's high vs. day-before's high → predict same direction) with a different confidence constant (0.3 vs 0.4). Cleaning labels does not un-correlate them. This part of the kill holds.
- **The "near coin flip" accuracy (~51.9%)** is the part that *could* theoretically shift under clean labels, but the overlap between the kill window and the corruption window is minor (persistence/simple_trend were scored over long history, mostly clean). Even if accuracy ticked a point or two, a day-over-day persistence forecast has no information content about tomorrow's Kalshi settlement direction — the canonical climatology baseline is not expected to beat 50% in any regime.

**(c) Verdict:** **KEEP-KILLED** as a *trading* signal. **BUT** the review flag from the task is correct in one respect: `persistence` should **not be fully dropped** as a *concept*. It is the canonical climatological baseline used in Brier-skill scoring. The `KILLED-SIGNALS.md` note already acknowledges this ("`persistence` stays as the skill baseline").

**(d) Concrete next step:** None required for revival. Optional hygiene: ensure any future skill-scoring code still references a persistence *reference forecast* (can be a 50/50 or yesterday-direction baseline) independent of whether `PersistenceSignal` is in the live registry. No retest needed to decide this.

---

## 3. `goldilocks` — **NEEDS-RETEST (only the separated real-time microstructure edge)**

**(a) Why it was killed**
- Daily `goldilocks` signal killed (2026-08-07 area + later reconciliation) at **49.85% / negative EV**. The real-time microstructure spike-detector edge was **separated out** (into the LOW lane / `microstructure_spike_reversion`, see `tests/test_low_momentum_signals.py` referencing `microstructure_spike_reversion` and `microstructure_spike_momentum_down`).

**(b) Does the rationale still hold against CLEAN labels?**
**For the daily signal: mostly yes.** The daily goldilocks (C5 spike-direction asymmetry over day-over-day high deltas) was scored at ~49.85% — a coin flip with negative EV after fees. Its *daily* version is a reversion signal whose direction is defined against settlement labels; the corruption window is late and the daily signal was largely scored over clean history, so the flip risk is low. KEEP-KILLED for the daily variant.

**For the separated real-time microstructure edge: unknown.** The spike detector operates on *intraday microstructure* (settlement-up followed by reversion, etc.), which is precisely the kind of edge that (a) was evaluated on a different (real-time) label surface, and (b) may have been affected by the settlement-label corruption differently than the daily signal. The reconciliation doc does **not** record a clean-label accuracy/EV for the separated microstructure edge.

**(c) Verdict:** **NEEDS-RETEST** — but narrowly scoped to the *separated real-time microstructure spike-reversion edge only*, not the daily goldilocks signal.

**(d) Concrete next step:** Re-measure the microstructure spike-reversion edge against the cleaned settlement labels (exclude `unresolved` rows). Required metric: directional accuracy + net EV after the canonical fee source (`scripts.sweep.config.kalshi_fee`). Exact runner: defer to Gilfoyle; do not run here.

---

## 4. `late_day_momentum` — **KEEP-KILLED**

**(a) Why it was killed**
- Killed at **48% accuracy** (`WEATHER-ENGINE-APP-MAP.md`: "late_day_momentum.py … KILLED (48% acc)"; hourly variant also 48%).

**(b) Does the rationale still hold against CLEAN labels?**
**Yes.** 48% is *below* coin flip — a directional signal that is wrong more than half the time is not a corruption artifact; a label-corruption scramble would push a true 55% signal toward ~50%, not toward 48%. Below-50% on a *directional* signal is a structural failure of the logic (late-day 17–22 UTC momentum does not predict settlement direction), not a label-quality failure. Cleaning labels is unlikely to move it from 48% to the ≥58% Gray Room threshold.

**(c) Verdict:** **KEEP-KILLED.**

**(d) Concrete next step:** None. (If a cheap sanity check were ever desired, it would be a single directional-accuracy re-run against clean labels — but the prior magnitude makes revival implausible and not worth the run.)

---

## 5. `nwp_analog` — **NEEDS-RETEST (data now present)**

**(a) Why it was killed**
- Killed as **data-gated** (`NWP_ANALOG_ENABLED=False`, taxonomy `status="KILLED"`). Reconciliation records it at **32.63% accuracy** with the code path retained behind the env flag. Original block: needs NWP forecast data infra.

**(b) Does the rationale still hold against CLEAN labels?**
**The "data-gated" rationale has CHANGED — data now exists.** `data/nwp_forecasts.db` is present (324 MB, mtime 2026-09-17 06:45 — actively maintained), and there is a full NWP backfill/collect suite (`scripts/nwp_collect.py`, `nwp_era5_backfill.py`, `nwp_era5_upper_air_backfill.py`, `nwp_analog_ensemble.py`, `nwp_archive_backfill.py`, etc.). The signal's kill was explicitly conditional on missing data; that condition is now false.

Caveats:
- The **32.63% accuracy** is still a strong negative signal — but note the same class-mapping/name risk does *not* apply here (nwp_analog is registered as the correct `NwpAnalogSignal` class). 32.63% on a k-NN analog ensemble is bad, but it was scored (a) possibly against corrupt labels and (b) before the full NWP backfill was present, so the feature surface may have been incomplete.
- The architecture is the deterministic k-NN analog (no ML — `NWP_ANALOG_ENABLED` global, `data/nwp_forecasts.db` + `data/metar_backfill.db`). It is *not* the XGBoost version that was reverted.

**(c) Verdict:** **NEEDS-RETEST** — the gating condition (missing NWP data) has been lifted. Whether it is worth re-enabling depends on a fresh clean-label accuracy with the *now-complete* NWP feature surface.

**(d) Concrete next step:** Re-run the deterministic k-NN analog eval with `NWP_ANALOG_ENABLED=1` against the cleaned settlement labels, using the now-populated `data/nwp_forecasts.db`. Required metric: directional accuracy + MCC vs. the Gray Room ≥58% / MCC threshold. Exact runner: defer to Gilfoyle. Do not re-enable the flag or flip the taxonomy `status` until the clean retest is in.

---

## 6. `simple_trend` — **KEEP-KILLED**

**(a) Why it was killed**
- Killed as redundant with `persistence` (ρ=1.0000, identical direction vector, 3,138 trades / 7 stations). `data/KILLED-SIGNALS.md` clarifies the *effective* registry kill was `simple_trend` only, with `persistence` retained as baseline.

**(b) Does the rationale still hold against CLEAN labels?**
**Yes.** The ρ=1.0 redundancy is structural (identical `evaluate()` logic, differing only in the confidence constant 0.4 vs 0.3). Clean labels cannot decorrelate two functions that compute the same output. It is a genuine duplicate, not a near-duplicate.

**(c) Verdict:** **KEEP-KILLED.**

**(d) Concrete next step:** None. This is the correct, permanent kill of the pair. (Note for hygiene: `simple_trend_signal` still appears in the reconciliation table as `IMPL-NOT-WIRED` under `status=ACTIVE`, which is inconsistent with its KILLED disposition in the sweep docs — a taxonomy-metadata nit, not a revival.)

---

## 7. Summary verdict table

| # | Signal | Why killed | Rationale vs clean labels | Verdict | Next step |
|---|---|---|---|---|---|
| 1 | `corrected_pressure_delta` | MCC=-0.2167 "actively harmful" | Suspect: wrong-class mapping + naming schism + corrupt labels; physics rationale still sound | **NEEDS-RETEST** | Fix sweep_engine mapping (`DualPolaritySignal`→`CorrectedPressureDeltaSignal`), then re-score real class vs clean labels (MCC+acc) |
| 2 | `persistence` | ρ=1.0 w/ simple_trend; ~51.9% | Redundancy is structural; near-coin-flip independent of labels | **KEEP-KILLED** (retain as skill baseline concept) | None — preserve baseline in Brier scoring |
| 3 | `goldilocks` | 49.85% / negative EV (daily); real-time edge separated | Daily: holds. Separated microstructure edge: unmeasured vs clean | **NEEDS-RETEST** (microstructure edge only) | Re-score separated spike-reversion edge vs clean labels (acc + net EV) |
| 4 | `late_day_momentum` | 48% accuracy | Below coin flip = structural, not label artifact | **KEEP-KILLED** | None |
| 5 | `nwp_analog` | data-gated (needs NWP data); 32.63% | Gating condition now false — `nwp_forecasts.db` populated; but 32.63% still weak | **NEEDS-RETEST** | Re-run k-NN analog with `NWP_ANALOG_ENABLED=1` + complete NWP surface vs clean labels (acc + MCC) |
| 6 | `simple_trend` | ρ=1.0 w/ persistence | Structural duplicate, not label-dependent | **KEEP-KILLED** | None |

**Net:** 3 × KEEP-KILLED (persistence, late_day_momentum, simple_trend), 3 × NEEDS-RETEST (corrected_pressure_delta, goldilocks-microstructure-edge, nwp_analog). **Zero REVIVE** — no signal can be revived on the strength of this review alone, because the three candidates all require a real clean-label number that has not yet been computed.

---

## 8. Confirmation of scope compliance (no mutation)

- **No signal re-registered, re-wired, or re-enabled.** `NWP_ANALOG_ENABLED` still `False`; no registry edit.
- **No code change.** (The sweep_engine mapping bug at `scripts/sweep_engine.py:337` is *documented*, not fixed.)
- **No data/DB change.** `metar_backfill.db`, `nwp_forecasts.db`, `kalshi_settlements.db` untouched.
- **No backtest run.** The three NEEDS-RETEST verdicts explicitly defer the run; no result is fabricated.
- **No commit.** Working tree left as-found (already dirty from unrelated prior work).

---

## 9. Handoff / escalation

- **Owner of the retests:** Gilfoyle (technical — canonical runner + fix of the sweep_engine class-mapping bug as a prerequisite for the `corrected_pressure_delta` retest).
- **Decision gate for any eventual revival:** Dan (merge/approval), per the standard weather-engine merge policy; Gerri for exposure framing if a signal clears the retest and enters promotion consideration.
- **Order of operations:** (1) fix `corrected_pressure_delta` class mapping, (2) run the three clean-label retests, (3) re-review with real numbers before any REVIVE decision.
