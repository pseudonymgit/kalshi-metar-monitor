# E4 — Gamma G1-G6 Enable (scoring-only): RECEIPT (2026-10-05)

**Sprint E** · **Item:** E4 (P2) · **Owner:** Ray Donovan · committed, pending review
**Repo:** `prototypes/weather-engine-source/`

---

## The actual gate (not `ENABLED_BY_DEFAULT`)
G1-G6 are instantiated in `core/signals/__init__.py` `SignalRegistry.signals`
(`gamma_g1_uncertainty` … `gamma_g6_cloud_cover`). But the **live daemon never
evaluates them**:

- The daemon calls `HourlySignalEvaluator.evaluate_hour_with_pool_names()`, which
  runs `core/intraday/signal_evaluator.py`'s `_INTRADAY_SIGNALS` registry — a
  **different** set of METAR-vector detectors. Gamma is not in it.
- `core/additive/ENABLED_BY_DEFAULT` gates a different family (the
  `CloudClearingSignal`/`DrylineCooling`… additive set) — irrelevant to gamma.
- `core/signal_families.py` lists G1-G6 as `status="ACTIVE"`, but that registry
  (`get_active_signals`) is not the evaluator the daemon runs.

So the gate was structural: gamma had **no evaluator in the live scoring path**.

## Change
New `core/signals/gamma_scoring.py`: the scoring-only bridge (mirrors
`subhourly_scoring.py`). It:
- builds a `days` series with the `station` key G1 needs for its GEFS lookup,
- runs `evaluate(idx, days)` for all six at the target station-day,
- emits `AdditiveSignalResult` rows under the gamma names,
- persists into the existing M1 store (`data/signal_cache.db`,
  `additive_signal_results`) via `core.additive.persistence`.

`scripts/intraday_runner.py` block **4b-3** calls it each cycle (after the
subhourly block). SCORING-ONLY: never writes `fused_results`, entry, or SBOX.

## Verification (5-minute)
```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python -m core.signals.gamma_scoring
# -> "gamma scoring: 18 rows for 3 station-days"; prints fired/confidence per signal

.venv-gefs/bin/python -c "
import sqlite3
for r in sqlite3.connect('data/signal_cache.db').execute(
    \"select signal,count(*),max(date) from additive_signal_results \"
    \"where signal like 'gamma%' group by signal order by 1\"):
    print(r)
"
```

After the block runs, `additive_signal_results` shows gamma rows:
| signal | rows | max date |
|---|---|---|
| gamma_g1_uncertainty | 3 | 2026-10-05 |
| gamma_g2_regime | 3 | 2026-10-05 |
| gamma_g3_analog | 3 | 2026-10-05 |
| gamma_g4_pressure_adv | 3 | 2026-10-05 |
| gamma_g5_dewpoint | 3 | 2026-10-05 |
| gamma_g6_cloud_cover | 3 | 2026-10-05 |

Per-cycle sample (KATL 2026-10-05): G1 fired 0.528, G2 fired 0.367, G3 fired
0.596, G4/G5/G6 no-fire. G5/G6 fire only where dewpoint/cloud inputs exist.

## Notes for reviewers
- G2's calibrated confidence (0.367) is **below** the additive stack's default
  `min_confidence` — irrelevant here (this path is scoring-only and does not go
  through `apply_additive_offsets`). Flagged so it isn't mistaken for a bug.
- No live/paper wiring change; no change to fusion or entry.
