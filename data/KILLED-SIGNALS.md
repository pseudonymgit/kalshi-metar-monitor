# KILLED Signals — Big Sweep Redundancy Registry

**Generated:** 2026-08-07T05:11:32Z
**Source:** `data/signal_correlation_matrix.json` + `data/signal_disposition.csv`
**Method:** Pairwise Spearman ρ on prediction-direction vectors across 7 stations (KNYC, KATL, KBOS, KDCA, KDEN, KLAX, KMDW)

## Summary

| Total signals | 40 |
|---|---|
| Evaluable (≥30 predictions) | 12 |
| Kill candidates (ρ ≥ 0.95) | 2 |
| ADVANCE | 10 |
| PARK (insufficient data to evaluate) | 28 |

## Killed Signals

### 1. `simple_trend` — KILLED (redundant with `persistence`)

| Metric | Value |
|---|---|
| Correlation pair | `persistence` |
| ρ | **1.0000** (perfect correlation) |
| Mean |ρ| | 0.1270 |
| Max |ρ| | 1.0000 |
| Accuracy | 0.519 |
| nTrades | 3138 |
| nStations | 7 |

**Rationale:** `simple_trend` produces the identical prediction-direction vector as `persistence` across 3,138 trades and 7 stations — ρ = 1.0000. The two signals are functionally identical in this configuration: `simple_trend` with its default lookback degenerates to a persistence forecast (predicts the same direction as the prior day's move). Keeping both adds zero information and only dilutes ensemble weighting. `persistence` is kept (it is the canonical climatological baseline and is used in Brier-skill scoring); `simple_trend` is killed.

### 2. `persistence` — KILLED (redundant with `simple_trend`)

| Metric | Value |
|---|---|
| Correlation pair | `simple_trend` |
| ρ | **1.0000** (perfect correlation) |
| Mean |ρ| | 0.1270 |
| Max |ρ| | 1.0000 |
| Accuracy | 0.519 |
| nTrades | 3138 |
| nStations | 7 |

**Rationale:** Mirror image of the above. Both signals are symmetric duplicates. Per the redundancy protocol, exactly **one** of the pair must be dropped. We drop `simple_trend` (see above) and retain `persistence` as the reference baseline. `persistence` is listed here for registry completeness: the disposition CSV flags both, but the effective registry kill is `simple_trend` only.

> **NOTE:** The disposition CSV will list both as KILL because the classifier is pair-symmetric. Effective registry change: **remove `simple_trend` only**. `persistence` stays as the skill baseline.

## ADVANCE Signals (10)

All ρ < 0.3 vs. every other signal — orthogonal, independent information:

| Signal | Accuracy | nTrades | Max |ρ| |
|---|---|---|---|---|
| ecmwf_bias_corrected | 0.845 | 3138 | 0.092 |
| calendar_climatology | 0.646 | 697 | 0.086 |
| pressure_delta | 0.641 | 1991 | 0.119 |
| gaussian | 0.631 | 1290 | 0.154 |
| forecast_disagreement | 0.609 | 1663 | 0.086 |
| gaussian_v2 | 0.609 | 2151 | 0.222 |
| wind_direction_shift | 0.515 | 487 | 0.291 |
| goldilocks | 0.477 | 2155 | 0.113 |
| spike_reversion | 0.446 | 1138 | 0.163 |
| radiational_cooling | 0.184 | 38 | 0.291 |

## PARK — Insufficient Data for Correlation (28)

These signals produced <30 usable predictions in the quick-evaluation pass. They are NOT killed — they were simply not evaluable on the fast subset. They remain registered for the full Phase 1 sweep:

`ai_composite`, `cloud_cover_index`, `corrected_pressure_delta`, `cross_model_divergence`, `dual_polarity`, `eighty_two_member_ensemble`, `eighty_two_member_ensemble_ece`, `eighty_two_member_ensemble_pooled`, `esdr`, `feels_like_delta`, `fogr_reversion`, `frontal_detector`, `frontal_passage_intraday`, `frontal_passage_nowcast`, `intraday_metar_confirmation`, `metar_dtdt`, `metar_nowcast`, `nwp_analog`, `nwp_direct`, `nwp_dtdt_fusion`, `pressure_tendency`, `regime`, `settlement_arbitrage`, `spread_based_entry`, `temperature_advection`, `volume_momentum`, `frontal_passage_detector`, `dewpoint_depression`

**Reason for PARK:** these signals are predominantly ensemble/NWP/regime-based and either (a) require multi-day NWP lookback windows that exceed the settlement overlap window, (b) are gated on regimes/events not present in the fast evaluation subset, or (c) are deprecated wrappers that delegate to newer implementations. The full Phase 1 sweep evaluates all 40 signals on the full date range and will produce definitive per-signal dispositions.

## Files

- `data/signal_correlation_matrix.json` — full 12×12 pairwise ρ matrix
- `data/signal_correlation_summary.md` — human-readable report
- `data/signal_disposition.csv` — per-signal disposition table
- `data/KILLED-SIGNALS.md` — this file
