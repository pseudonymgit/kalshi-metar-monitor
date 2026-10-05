# E6 — Trajectory Cell Scoring: RECEIPT (2026-10-05)

**Sprint E** · **Item:** E6 (P2) · **Owner:** Ray Donovan · committed, pending review
**Repo:** `prototypes/weather-engine-source/`
**File:** `scripts/sweep_validation.py`

---

## What was asked
Score the anticipatory **cross-cell only**, not the day-dominant label. The
`reason="traj:no-cross"` on every row carried base-rate noise; strip it so the
trajectory reports a real, non-base-rate number. Verify the figure becomes
cell-only (not 91%).

## Root cause
`_subhourly_scoring_rows()` collapsed each station-day to its dominant nowcast
label over every pre-HOTD minute. Since the signal predicts `no-cross` on any
minute not in the cell, almost every day is labelled `no-cross`. Scoring that
label measured the base rate (most days don't cross the strike), not skill.

## Before / after (same signal, same data)
| metric | before | after |
|---|---|---|
| headline accuracy | **91.42%** | **77.78%** |
| rows scored | 7,148 (day-dominant) | **63 (cell-only)** |
| stations | 20 | 17 |
| "no-cross" share | 7,133 / 7,148 = 99.8% | 0 (cell only) |

Reproduce:
```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python -c "
import sys; sys.path.insert(0,'.')
from core.signals.subhourly_trajectory_signal import SubhourlyTrajectorySignal
import scripts.sweep_validation as sv
sig = SubhourlyTrajectorySignal('data/metar_backfill.db')
res = sv.score_subhourly_trajectory('subhourly_trajectory', sig)
print(res['aggregate']['n_trades'], res['aggregate']['accuracy'])
print(res['subhourly'])
"
# -> 63 0.7778 ; cell_accuracy 0.7778, legacy_day_dominant_accuracy 0.9142
```

## Change
- `_subhourly_scoring_rows(signal_obj, cell_only=True)`: default now keeps only
  station-days with **≥1 cell (cross) prediction**, direction fixed to `cross`.
- `score_subhourly_trajectory`: `aggregate` is the cell-only metric (headline).
  Legacy day-dominant kept as `subhourly.legacy_day_dominant_*` for comparison.
- Diagnostics keys renamed: `n_cell_days` / `cell_accuracy` (were `n_cross_days` /
  `cross_accuracy` alongside `n_nocross_days` / `nocross_accuracy`).

## Honest caveat for reviewers
- n=63 is **small**; 77.78% has a wide CI (Wilson 95% ≈ [66%, 86%]). It clears the
  Gray Room ≥58% directional bar but it is a **cell-conditional** number, not a
  trade-population accuracy. Do not quote it as an overall accuracy.
- No trades are emitted; scoring-only, unchanged (Dan-gated).
