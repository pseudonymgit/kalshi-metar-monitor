# FP-WALK-FORWARD-2-BUCKET-SPEC.md

## Item 1: Walk-Forward Validation + 2-Bucket Trade Strategy

### Problem Statement
The current sweep optimization produces an inverted relationship where:
- `confidence_floor=0.80` gives 68% accuracy but only 119 trades (-$479 P&L)
- `confidence_floor=0.55` gives 51.7% accuracy but 2,476 trades (+$490 P&L)

The optimal config optimizes for combined P&L and is inverted — higher confidence produces fewer trades with better accuracy but negative P&L due to fee structure and position sizing.

### Proposed Solution
Design a walk-forward validation approach with regime-specific thresholds and a 2-bucket trade strategy:

1. **Walk-Forward Validation per Regime**:
   - Split data by season (DJF vs JJA) and time-of-day (12z vs 00z)
   - Optimize confidence thresholds independently per regime
   - DJF can trade at lower confidence (0.55-0.65) while JJA needs higher confidence (0.75-0.85)

2. **2-Bucket Trade Strategy**:
   - **High-confidence bucket** (≥0.80): Use Kelly sizing with conservative fraction (0.25-0.50)
   - **Low-confidence bucket** (0.55-0.79): Use fixed sizing (e.g., 10-50 contracts)
   - Different calibration targets for each bucket

### Data Needed
- Historical settlement data with regime labels (DJF/JJA, 12z/00z, continental/coastal)
- Signal confidence distributions per regime from `data/per_parameter_results.json`
- Fee structure analysis per confidence level

### Code Needed
- `core/regime_specific_thresholds.py` — Walk-forward optimizer per regime
- `core/two_bucket_sizing.py` — Dual position sizing strategy
- `core/fee_aware_calibration.py` — Fee-aware calibration targets
- Updates to `scripts/gefs_paper_trading_cron.py` to implement regime-aware thresholds

### Build Effort
- **Phase 1**: Regime analysis (8 hours) — Analyze confidence distributions per regime
- **Phase 2**: Walk-forward implementation (12 hours) — Build regime-specific optimization
- **Phase 3**: 2-bucket sizing (6 hours) — Implement dual sizing strategy
- **Phase 4**: Integration (4 hours) — Wire into paper trading cron
- **Total**: 30 hours (3.75 days)

### Risk Level
**MEDIUM** — Requires careful regime definition and validation to avoid overfitting

### Priority
**P1** — Addresses the core inverted optimization problem

### Dependencies
- Completion of regime analysis from existing sweep data
- Validation of regime definitions against meteorological patterns

### Improvement Suggestion
**Implement seasonally-adjusted confidence thresholds** — DJF regimes can trade profitably at lower confidence (0.55-0.65) due to better signal performance in winter, while JJA needs higher confidence (0.75-0.85) to avoid summer performance collapse. This should improve overall accuracy by +3-5pp while maintaining trade volume.