# F4 — Trajectory Cell Climatology Baseline: RECEIPT (2026-10-06)

**Sprint:** F · **Item:** F4 (P2) — add a climatology baseline (MCC) to the
trajectory cell score so the 77.78% headline has a comparison point.
**Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Status:** probe done, code change committed, pending Gilfoyle review.

---

## 1. What was asked

The E6 cell-only trajectory headline (77.78%) had **no comparison point** — the
receipt even flagged "cell-conditional, not a trade-population accuracy". Add a
climatology baseline so we can see whether the signal beats its own base rate.

## 2. One-pass probe (read-only)

Single `iter_nowcast_events()` pass over 13 months of METAR (97,167 events),
both labelings derived from the cached events (no model calls):

| metric | value |
|---|---|
| cell-only rows (E6 headline) | **63** |
| cell-only accuracy | **0.7778** |
| **P(cross \| cell)** = cell-conditioned climatology | **0.7778** |
| **lift over climatology** | **0.0000** |
| legacy day-dominant rows | 7,148 |
| legacy day-dominant accuracy | 0.9142 (base-rate artifact) |
| unconditional cross rate over all station-days | 0.0873 |
| cell Wilson 95% CI | [0.661, 0.863] |

### The finding

**The signal predicts `cross` on every scored cell row, so its 77.78% equals the
cell-conditioned base rate exactly — lift vs climatology is 0.0pp.** The cell
gate selects days that cross ~77.78% of the time; the answer it emits adds no
skill *given the gate*. Per-station cell cross-rate varies widely (KMDW 9/9,
KBOS/KHOU/KMSP/KPHX 100%, vs KLAX 1/3, KNYC 1/3, KMIA 4/9), so the aggregate is
a blend of the gate's station mix, not a stable edge.

This does **not** contradict E6 (which correctly stripped the no-cross base rate
and reported the cell number); it supplies the missing denominator-comparison.
Read together: E6's 77.78% is a **base rate of the firing population**, not
demonstrated predictive skill.

## 3. Change

`scripts/sweep_validation.py` → `score_subhourly_trajectory()`:
- computes `cell_climatology_accuracy` = P(cross | cell) over the same 63 rows;
- `cell_edge_vs_climatology` = cell_accuracy − cell_climatology;
- `unconditional_cross_rate` = P(cross) over all station-days;
- all three exposed under `subhourly.*` and named in `notes`.

Read-only, scoring-only. **No trades, no fusion, no SBOX wiring** (Dan-gated).

## 4. Verification (reproduces the numbers through the real scorer)

```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python -u -c "
import sys,json; sys.path.insert(0,'.')
from core.signals.subhourly_trajectory_signal import SubhourlyTrajectorySignal
import scripts.sweep_validation as sv
res=sv.score_subhourly_trajectory('subhourly_trajectory', SubhourlyTrajectorySignal('data/metar_backfill.db'))
print(json.dumps(res['subhourly'],indent=2))
"
# -> cell_accuracy 0.7778 ; cell_climatology_accuracy 0.7778 ;
#    cell_edge_vs_climatology 0.0 ; unconditional_cross_rate 0.0873
```

## 5. Reviewer caveat

n=63, CI [0.661, 0.863]. The zero-lift result is the headline of F4: **treat the
trajectory cell signal as base-rate-calibrated, not as an edge, until a
comparison that controls for station mix and cell selectivity shows lift.**
