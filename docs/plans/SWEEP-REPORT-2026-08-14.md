> ⚠️ **HISTORICAL — PRE-REMEDIATION RECORD.** Superseded by `docs/weather-engine/sweep-report-2026-08-14.md` (authoritative). The 3.01pp-above-baseline figure in this report is arithmetically incorrect (correct: 3.66pp). References the old pre-remediation artifact `sweep_directional_full_2026-08-14.json` (SHA256 571d2134...), not the post-remediation `_074943` artifact (SHA256 1949cb8c...). Do not use for current analysis. — 2026-08-14

# Layer 1 Directional Sweep Report — 2026-08-14

## Executive summary

The cleaned primary directional sweep completed successfully on the verified path. It evaluated **18/18 effective configurations** across **4 expanding temporal folds**, selected by mean fold directional accuracy, then evaluated the selected configuration once on the sealed outer holdout.

**Result:** the selected configuration achieved **59.83% mean inner-fold accuracy** and **56.91% outer-holdout accuracy** on 1,353 predictions. This is a real historical directional result under the current target/data path, not a claim of live profitability. The holdout result is 3.01 percentage points above the holdout always-UP baseline (53.25%), but the selection-to-holdout degradation is 2.92pp. No paper-trading conclusion is authorized.

The run is a clean Layer 1 baseline, not the comprehensive parameter/execution sweep originally discussed.

## Run manifest

| Field | Value |
|---|---|
| Generated | 2026-08-14 (artifact timestamp) |
| Script | `scripts/run_sweep.py` |
| Output | `data/sweep_directional_full_2026-08-14.json` |
| Mode | nested 4-fold |
| Requested configs | 100 |
| Effective configs evaluated | 18 |
| Ranking metric | `mean_fold_directional_accuracy` |
| Fusion methods | LLOP, UWC in grid generation |
| Calibration | isotonic only; Platt deferred |
| Confidence floors | 0.50, 0.55, 0.60 |
| Kelly values | 0.25, 0.50, 1.0; cosmetic for directional predictions |
| Active signals | 17 registered |
| Degenerate default | included (`exclude_degenerate=False`) |
| Degenerate comparison | excluded on same fold population |
| Calibration cutoff argument | 2026-04-30 passed; nested folds use fold-specific cutoffs |
| Outer holdout | 2026-05-15 through 2026-08-13 |
| P&L ranking | excluded |

## What was tested

The tested effective grid is:

```text
2 fusion methods × 1 calibration method × 3 confidence floors × 3 Kelly values
= 18 configurations
```

The request for 100 configs did not expand the grid; the script stopped after the 18 unique grid points. Therefore this report must not be described as a 100-config search.

### Configurations

All 18 rows are reproduced below from the machine-readable artifact. Accuracy is the ranking metric; `pseudo_pnl` is intentionally `None` for nested aggregates.

| # | Fusion | Calibration | Confidence floor | Kelly | Mean accuracy | Std | Min | Max | Fold 1 | Fold 2 | Fold 3 | Fold 4 | Folds | Calibrated |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | llop | isotonic | 0.60 | 1.00 | 0.5983 | 0.0379 | 0.5480 | 0.6548 | 0.6548 | 0.5942 | 0.5480 | 0.5963 | 4 | True |
| 2 | llop | isotonic | 0.60 | 0.25 | 0.5983 | 0.0379 | 0.5480 | 0.6548 | 0.6548 | 0.5942 | 0.5480 | 0.5963 | 4 | True |
| 3 | llop | isotonic | 0.60 | 0.50 | 0.5983 | 0.0379 | 0.5480 | 0.6548 | 0.6548 | 0.5942 | 0.5480 | 0.5963 | 4 | True |
| 4 | llop | isotonic | 0.55 | 0.50 | 0.5913 | 0.0241 | 0.5598 | 0.6156 | 0.6156 | 0.5760 | 0.5598 | 0.6140 | 4 | True |
| 5 | llop | isotonic | 0.55 | 1.00 | 0.5913 | 0.0241 | 0.5598 | 0.6156 | 0.6156 | 0.5760 | 0.5598 | 0.6140 | 4 | True |
| 6 | llop | isotonic | 0.55 | 0.25 | 0.5913 | 0.0241 | 0.5598 | 0.6156 | 0.6156 | 0.5760 | 0.5598 | 0.6140 | 4 | True |
| 7 | llop | isotonic | 0.50 | 1.00 | 0.5895 | 0.0220 | 0.5663 | 0.6146 | 0.6081 | 0.5688 | 0.5663 | 0.6146 | 4 | True |
| 8 | llop | isotonic | 0.50 | 0.25 | 0.5895 | 0.0220 | 0.5663 | 0.6146 | 0.6081 | 0.5688 | 0.5663 | 0.6146 | 4 | True |
| 9 | llop | isotonic | 0.50 | 0.50 | 0.5895 | 0.0220 | 0.5663 | 0.6146 | 0.6081 | 0.5688 | 0.5663 | 0.6146 | 4 | True |
| 10 | uwc | isotonic | 0.55 | 1.00 | 0.4242 | 0.2744 | 0.0000 | 0.6667 | 0.6667 | 0.6667 | 0.3636 | 0.0000 | 4 | True |
| 11 | uwc | isotonic | 0.55 | 0.50 | 0.4242 | 0.2744 | 0.0000 | 0.6667 | 0.6667 | 0.6667 | 0.3636 | 0.0000 | 4 | True |
| 12 | uwc | isotonic | 0.55 | 0.25 | 0.4242 | 0.2744 | 0.0000 | 0.6667 | 0.6667 | 0.6667 | 0.3636 | 0.0000 | 4 | True |
| 13 | uwc | isotonic | 0.50 | 0.25 | 0.4208 | 0.2851 | 0.0000 | 0.7500 | 0.7500 | 0.6000 | 0.3333 | 0.0000 | 4 | True |
| 14 | uwc | isotonic | 0.50 | 0.50 | 0.4208 | 0.2851 | 0.0000 | 0.7500 | 0.7500 | 0.6000 | 0.3333 | 0.0000 | 4 | True |
| 15 | uwc | isotonic | 0.50 | 1.00 | 0.4208 | 0.2851 | 0.0000 | 0.7500 | 0.7500 | 0.6000 | 0.3333 | 0.0000 | 4 | True |
| 16 | uwc | isotonic | 0.60 | 0.50 | 0.1666 | 0.1666 | 0.0000 | 0.3333 | 0.0000 | 0.3333 | 2 | True |
| 17 | uwc | isotonic | 0.60 | 0.25 | 0.1666 | 0.1666 | 0.0000 | 0.3333 | 0.0000 | 0.3333 | 2 | True |
| 18 | uwc | isotonic | 0.60 | 1.00 | 0.1666 | 0.1666 | 0.0000 | 0.3333 | 0.0000 | 0.3333 | 2 | True |

## Baselines

| Baseline | Eval accuracy | Holdout accuracy | Population |
|---|---:|---:|---:|
| Always UP | 0.5379 | 0.5325 | Eval 5717; holdout 1540 |
| Always DOWN | 0.4621 | 0.4675 | Eval 5717; holdout 1540 |
| Best selected configuration | not separately reported in eval baseline block | 0.5691 | 1,353 predictions |

## Selected configuration and holdout

- Selected parameters: `{'fusion': 'llop', 'calibration': 'isotonic', 'confidence_floor': 0.6, 'kelly_fraction': 1.0}`
- Mean inner-fold accuracy: **0.5983** (59.83%)
- Fold accuracies: `[0.6548, 0.5942, 0.548, 0.5963]`
- Fold standard deviation: **0.0379**
- Outer holdout accuracy: **0.5691** (56.91%)
- Holdout predictions: **1353**
- Selection-to-holdout gap: **2.92pp**
- Holdout proxy P&L: **$159.26** — not authoritative
- Holdout proxy Sharpe: **1.8860** — not authoritative
- Holdout proxy profit factor: **1.2677** — not authoritative

## Degenerate-signal comparison

The primary run included all four degenerate signals: `['spike_reversion', 'warm_momentum', 'cooling_failure', 'radiational_cooling']`.

- Included mean accuracy: **0.5983**
- Excluded mean accuracy: **0.5999**
- Delta (included − excluded): **-0.16pp**
- Excluded fold accuracies: `[0.6557, 0.601, 0.5377, 0.6053]`
- Same fold population: yes

## Calibration and per-signal reporting status

The run-level artifact records that isotonic calibration was used and that the primary ranking metric was mean fold directional accuracy. However, the nested aggregate does **not** persist the full per-signal/per-station calibration diagnostic payload requested in the roadmap. Specifically, this artifact does not contain complete nested values for:

- per-signal accuracy and fire rate by fold;
- per-station accuracy by fold;
- raw Brier and calibrated Brier by signal/station;
- Brier improvement;
- ECE and reliability bins by fold/signal/station;
- exception counts by configuration/fold.

This is a reporting/completeness gap, not a reason to invalidate the directional headline. It must be fixed before claiming a comprehensive calibration or signal-attribution analysis.

## P&L limitations

P&L and Sharpe are output-only diagnostics and were excluded from ranking. The current model uses: `(PROXY) Simplified P&L: flat $0.0205 fee, $1/trade notional. Structurally wrong for binary options — directional accuracy is the primary sweep metric. See compute_pnl_metrics docstring.`

It does not use historical entry prices, order-book depth, spread, queue position, realistic binary-contract sizing, or the canonical price-dependent fee function. No live-trading conclusion follows from these P&L fields.

## What this run supports

- A reproducible Layer 1 directional baseline on real settlement/METAR data.
- Evidence that the selected configuration beat the always-UP holdout baseline by **3.66pp** on 1,353 predictions.
- Evidence of meaningful temporal variation: fold 3 was materially weaker than folds 1/2/4.
- A small negative impact from including degenerate signals in this run.

## What this run does not support

- A claim of live profitability or tradable Sharpe.
- A claim that 100 configurations were tested.
- A claim that the current best config is stable across geography, seasons beyond these folds, or market-price regimes.
- A complete calibration-target report.
- Exact temperature/bracket forecasting.
- Intraday or time-of-day conclusions.
- A paper-trading configuration.

## Next experiments

1. Persist the missing per-signal/per-station/fold diagnostic payload and rerun the same 18-config Layer 1 baseline if attribution is required.
2. Run signal isolation and ablation: every active signal alone, leave-one-out, and degenerate comparison.
3. Compute the planned signal correlation matrix: direction agreement, confidence correlation, co-firing, and mutual information.
4. Add exact-temperature and settlement-bracket forecast evaluation.
5. Build historical market-price/order-book capture and realistic binary P&L before using Sharpe/P&L operationally.
6. Add geographic/seasonal robustness and block-bootstrap uncertainty before paper trading.
7. Expand the effective parameter grid only after deciding which directional parameters are scientifically justified; `n_configs=100` alone does not expand the current 18-point grid.

## Final assessment

**Layer 1 baseline: informative, not conclusive.** The result is substantially more trustworthy than the earlier pre-fix numbers because lookahead, fold isolation, ranking, and failure-accounting gates were tested. The holdout is above the always-UP baseline, but the result is not yet a complete signal-quality or trading conclusion. The next high-value step is diagnostic persistence and signal attribution—not silently treating proxy P&L as evidence.
