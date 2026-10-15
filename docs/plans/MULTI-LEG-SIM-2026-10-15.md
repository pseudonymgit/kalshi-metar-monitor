# Multi-Leg Simulation — two strikes, evolving om_gfs, 1/3/6h

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/multi_leg_sim_2026-10-15.py` · **Artifact:** `data/multi_leg_sim_2026-10-15.json`

**Simulation, not a system test.** Read-only. No book writes.

## Setup

- Cohort: honest HIGH cohort with ASOS recs + om_gfs + grid truth, `2026-04-21..2026-10-09`, 13:30 station-local pin, **n = 2927** (2455 also carry Kalshi truth).
- Legs: `T_lo = floor(running_high@13:30) + 1` (om_gfs best-guess strike), `T_hi = T_lo + 5` (Dan's T75/T80 spacing).
- Evolving forecast at tau: `mu_final = max(running_high(tau), om_gfs_bc_ps)`; `P(final > T) = 1` if `running_high(tau) > T`, else the Gaussian crossing prob.
- Truth: strict crossing `y = 1{settled_max > T}` (GHCN-based grid); Kalshi variant alongside.
- Policy: `rh > T_hi` -> hold T_hi / exit T_lo; `rh > T_lo` -> hold T_lo / exit T_hi; else hold both.

## Primary — fixed om_gfs sigma

| holding time | n rows | retained legs | accuracy | confidence | Brier | P&L 2-leg | P&L 1-leg | gain |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0h after pin | 2927 | 5854 | 0.4722 | 0.5032 | 0.0705 | -0.1065 | -0.0285 | -0.078 |
| 1h after pin | 2927 | 4396 | 0.5883 | 0.6256 | 0.0653 | -0.0613 | -0.0285 | -0.0328 |
| 3h after pin | 2927 | 3971 | 0.6404 | 0.6926 | 0.0662 | -0.0492 | -0.0285 | -0.0207 |
| 6h after pin | 2927 | 3912 | 0.6426 | 0.6985 | 0.0668 | -0.0467 | -0.0285 | -0.0182 |

Policy split (hold-both / hold-lo / hold-hi): 
`0h: 2927/0/0 · 1h: 1469/1432/26 · 3h: 1044/1754/129 · 6h: 985/1757/185`

## Sensitivity — sigma decays with the remaining peak window

| holding time | n rows | retained legs | accuracy | confidence | Brier | P&L 2-leg | P&L 1-leg | gain |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0h after pin | 2927 | 5854 | 0.4722 | 0.5032 | 0.0705 | -0.1065 | -0.0285 | -0.078 |
| 1h after pin | 2927 | 4396 | 0.5883 | 0.6258 | 0.0655 | -0.0631 | -0.0285 | -0.0346 |
| 3h after pin | 2927 | 3971 | 0.6404 | 0.6926 | 0.0677 | -0.057 | -0.0285 | -0.0285 |
| 6h after pin | 2927 | 3912 | 0.6426 | 0.6984 | 0.071 | -0.064 | -0.0285 | -0.0355 |

Policy split (hold-both / hold-lo / hold-hi): 
`0h: 2927/0/0 · 1h: 1469/1432/26 · 3h: 1044/1754/129 · 6h: 985/1757/185`

## Kalshi-truth variant (fixed sigma, overlap sub-cohort)

| holding time | n rows | retained legs | accuracy | confidence | Brier |
|---|---:|---:|---:|---:|---:|
| 0h after pin | 2455 | 4910 | 0.4483 | 0.4989 | 0.0861 |
| 1h after pin | 2455 | 3690 | 0.5645 | 0.6248 | 0.0839 |
| 3h after pin | 2455 | 3335 | 0.6123 | 0.6911 | 0.0855 |
| 6h after pin | 2455 | 3287 | 0.6127 | 0.6969 | 0.0873 |

## Reading

- **accuracy** = fraction of retained legs that settle YES (the winner).
- **confidence** = mean decision-time P(cross) of the retained legs.
- **P&L** = implied per-contract dollars, entry at model p@13:30 and exit at model p@tau (fees via `core.fees`). It measures whether the two-leg + exit policy is additive *under the model's own prices*, not market-realized P&L.
