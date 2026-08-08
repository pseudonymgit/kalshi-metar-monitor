# A2 Wiring Diff — Bias Correction Module Integration

**Author:** Gilfoyle
**Date:** 2026-08-08
**Status:** Review-ready for Dan
**Gate:** Dan approves before merge to `bmode-r12-wiring` → main

---

## Executive Summary

The ensemble bias correction wiring (A2) is **substantially complete**. Both scripts already import and use `apply_bias_correction()` and `compute_ensemble_fraction()` from `core.ensemble_fraction.py`. The R12-CLEANUP-SWEEP-PLAN.md's claim that these scripts use "inline UHI correction" is **stale** — the code was updated before being documented.

**Remaining gap:** `ForecastConfidenceModulator` is imported in both scripts but never instantiated or called. This is a separate feature (multi-model agreement modulation) that depends on Edge 20 NWP data being available — it's not ready to wire yet.

---

## What's Already Wired

### `scripts/gefs_paper_trading_cron.py` ✅

| Component | Status | Lines |
|-----------|--------|-------|
| Import from `core.ensemble_fraction` | ✅ Done | 48-52 |
| Import `ForecastConfidenceModulator` | ✅ Imported (unused) | 53 |
| `_load_ensemble_bias()` — module-level loader with UHI fallback | ✅ Done | 66-80 |
| `_apply_bias_correction()` on mean temperature | ✅ Done | 291-304 in `compute_ensemble_signal()` |
| `_apply_bias_correction()` on 31 member values | ✅ Done | 316-318 |
| `_compute_ensemble_fraction()` | ✅ Done | 319 |
| Pre-load bias table in `run_paper_trading()` | ✅ Done | 486 |

**Code path in `compute_ensemble_signal()`:**
```python
# P1.2: Bias correction (prefer ensemble seasonal bias over UHI)
try:
    ensemble_corrected = _apply_bias_correction(mean_f_arr, station, target_date)
    if ensemble_corrected is not None:
        corrected_mean_f = float(ensemble_corrected[0])
        if abs(corrected_mean_f - gefs_mean_f) < 50.0:
            gefs_mean_f = corrected_mean_f
except Exception:
    gefs_mean_f = apply_uhi_correction(station, gefs_mean_f, target_date)
```

### `scripts/bmode_p1_backtest.py` ✅

| Component | Status | Lines |
|-----------|--------|-------|
| Import from `core.ensemble_fraction` | ✅ Done | 58-62 |
| Import `ForecastConfidenceModulator` | ✅ Imported (unused) | 63 |
| `_apply_bias_correction()` on mean temperature | ✅ Done | 163-172 in `compute_signal()` |
| `_apply_bias_correction()` on 31 member values | ✅ Done | 185-188 |
| `_compute_ensemble_fraction()` | ✅ Done | 186 |
| Walk-forward UHI as second fallback | ✅ Done | 174-176 |

**Same pattern:** tries ensemble seasonal bias first, falls back to walk-forward UHI.

---

## Remaining Gap: ForecastConfidenceModulator (NOT WIRED)

### Why it's not wired yet

The `ForecastConfidenceModulator` modulates ensemble probability using multi-model agreement from 4 deterministic models (GFS, ECMWF, ICON, GEM). This depends on:

1. **Edge 20 NWP data availability** — The modulator queries `nwp_forecasts.db` at inference time. For the cron script, this requires day-1 forecast data (available). For the backtest, this requires historical NWP data (which is 29 days incomplete — Edge 20 is still collecting).
2. **Multi-model collection** — The cron at 06:00 UTC collects all 4 models. But the modulator needs real-time or near-real-time NWP data to work during live trading.
3. **Backtest compatibility** — Historical NWP data doesn't exist beyond ~30 days ago (Open-Meteo real-time only). The modulator can't be backtested until NWP archive accumulates enough data (est. 2026-09+).

### What wiring would look like

When ready, the wiring in both scripts is straightforward:

```python
# In cron script run_paper_trading():
modulator = ForecastConfidenceModulator()
# ... then in compute_ensemble_signal():
p_adjusted, meta = modulator.modulate(
    station=station, 
    p_ens=confidence, 
    prev_actual=prev_temp_f,
    lat=..., lon=...,  # from station_registry
)
```

### Recommendation

**Do NOT wire ForecastConfidenceModulator yet.** It's a live-only feature until the NWP archive accumulates enough data for backtesting. The staging import is harmless and avoids future import path changes.

---

## Delta from Plan Document

| Plan Claim | Actual State | Verdict |
|-----------|-------------|---------|
| "Both scripts use inline UHI correction, not the module" | Both scripts use ensemble seasonal bias FIRST, UHI as fallback | ✅ Stale claim — code is already wired |
| "Needs wiring" | Already wired | ✅ Update plan doc |
| `ForecastConfidenceModulator` needs wiring | Imported, unused — depends on NWP data availability | ⏸️ Defer until Edge 20 complete |
| "Load bias corrections once at module level" | `_load_ensemble_bias()` exists in cron; backtest lazily loads in `_apply_bias_correction` | ✅ Both paths work |

---

## Action Required

1. **Dan reviews this diff** — confirm the wiring is correct as-is
2. **Update R12-CLEANUP-SWEEP-PLAN.md** — mark A2 bias wiring as ✅ Complete, update ForecastConfidenceModulator to ⏸️ Deferred
3. **No code changes needed** — the wiring is already on main, just needs a `bmode-r12-wiring` branch from main for Dan to approve

---

## Verification Checklist

- [x] `gefs_paper_trading_cron.py` imports from `core.ensemble_fraction`
- [x] Bias correction applied to mean temperature in cron
- [x] Bias correction applied to member values in cron
- [x] `_compute_ensemble_fraction()` used in cron
- [x] UHI fallback preserved in cron (station not in bias table)
- [x] `bmode_p1_backtest.py` imports from `core.ensemble_fraction`
- [x] Bias correction applied to mean in backtest
- [x] Bias correction applied to members in backtest
- [x] `_compute_ensemble_fraction()` used in backtest
- [x] Walk-forward UHI fallback preserved in backtest
- [x] `ForecastConfidenceModulator` deferred (NWP data incomplete)
- [x] No look-ahead bias from bias correction (seasonal table is pre-computed from historical matched pairs)