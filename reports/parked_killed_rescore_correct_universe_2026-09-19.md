# Parked/Killed Signal Re-Score — Correct R/T-1 Universe

**Generated:** 2026-09-19T15:08:12Z
**Axis:** strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction
**Truth:** settlement_truth.strike_price — 14130 labels (HIGH up=0.1013, LOW down=0.0536)

**Unit note:** R/T-1 `ultimate_sweep.load_daily_series()` fed `temp_c` into
these °F-native in-memory signals — a unit mismatch that produced NO_FIRES
for `trajectory_state_vector`/`nwp_analog`/`goldilocks`. This re-score uses
`temp_f` (correct native unit).

## Re-Scored Table (comparable universe)

| signal | accuracy | MCC | n | HIGH-up prec/EV | LOW-down prec/EV |
|---|---|---|---|---|---|
| goldilocks | 49.41% | -0.0299 | 9243 | 0.0954 / -0.0364 | 0.0675 / -0.0166 |
| late_day_momentum | 52.94% | +0.1177 | 17 | 0.2857 / +0.1539 | 0.0000 / -0.0841 |
| nwp_analog | 49.15% | -0.0210 | 4716 | 0.0417 / -0.0901 | 0.0106 / -0.0735 |
| persistence | 51.61% | +0.0279 | 14020 | 0.1115 / -0.0203 | 0.0467 / -0.0373 |
| rdae_mos | 44.38% | -0.0022 | 13227 | 0.0921 / -0.0397 | 0.0556 / -0.0285 |
| sensor_bias_correction | 49.84% | -0.0418 | 11269 | 0.0969 / -0.0349 | 0.0560 / -0.0281 |
| trajectory_state_vector | 49.07% | +0.0019 | 12212 | 0.1144 / -0.0174 | 0.0310 / -0.0531 |

## Verdict

No parked/killed signal shows minority-class lift that survives fees + OOS.

_Read-only. No signal modification, no wiring, no DROP/backfill._