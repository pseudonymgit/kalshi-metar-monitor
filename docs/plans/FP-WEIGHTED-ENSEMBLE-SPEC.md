# FP-WEIGHTED-ENSEMBLE-SPEC.md

## Item 2: Weighted Ensemble + Consensus Boost ("+X" Pattern)

### Problem Statement
Pseudonym's "+0.25" reference from prior work was misinterpreted as quarter-Kelly cap and variance normalization. The correct interpretation is: **agreement consensus boost** — base probability + increase when independent signals agree.

### Design Correction
**The "+0.25" is NOT about:**
- Quarter-Kelly cap (kelly_fraction=0.25 — separate concern)
- Variance normalization (0.25 floor — separate concern)

**The "+0.25" IS about:**
- **Consensus bonus** when agreement threshold met
- Originally tuned for 82-member ensemble (agreement of 60+/82 members)
- With 4 independent signals, agreement semantics change
- May need larger boost (+0.30 to +0.40) for 4-signal core

### Proposed Solution
Design optimal consensus boost mechanism:

1. **Signal Independence Verification**: Ensure 4 truly independent signals:
   - `ecmwf_bias_corrected` (ECMWF IFS forecast)
   - `pressure_delta` (pressure change → temperature)
   - `seasonal_regime` (climatological context)
   - `wind_direction_shift` (weather pattern changes)

2. **Agreement Threshold**: N/4 independent signals must agree on direction
   - 3/4 = strong consensus
   - 4/4 = very strong consensus
   - 2/4 = weak consensus (no boost)

3. **Boost Calibration**:
   - Original: +0.25 for 60+/82 member agreement
   - New: +0.30 to +0.40 for 3/4 signal agreement
   - Tune based on historical agreement→accuracy relationship

4. **Confidence Modulation**: Higher boost when:
   - Signals have high individual confidence
   - Recent agreement has been predictive
   - Regime favors consensus (e.g., frontal regimes)

### Data Needed
- Historical signal agreement patterns from `data/per_parameter_results.json`
- Agreement→accuracy correlation analysis
- Regime-specific agreement effectiveness

### Code Needed
- `core/consensus_boost.py` — Agreement detection and boost application
- `core/signal_independence_validator.py` — Verify signal orthogonality
- `core/boost_calibration.py` — Historical calibration of boost values
- Updates to fusion logic to incorporate consensus boost

### Build Effort
- **Phase 1**: Signal independence analysis (4 hours) — Correlation validation
- **Phase 2**: Agreement detection (6 hours) — N/4 threshold implementation
- **Phase 3**: Boost calibration (6 hours) — Historical tuning of boost values
- **Phase 4**: Integration (4 hours) — Wire into paper trading
- **Total**: 20 hours (2.5 days)

### Risk Level
**LOW** — Conservative improvement based on established consensus patterns

### Priority
**P2** — Consensus boost provides marginal improvements over base signals

### Dependencies
- Completion of 4-signal core ensemble reduction
- Signal correlation validation

### Improvement Suggestion
**Implement regime-aware consensus boost** — Apply larger boosts during regimes where signal agreement is most predictive (e.g., frontal passage regimes where pressure_delta and wind_direction_shift strongly correlate with temperature movement). This should improve accuracy by +2-4pp by amplifying consensus during high-predictability regimes.

### Alignment with Sequential Narrowing Framework
This consensus boost mechanism integrates with Layer 1 (Temperature Belief) of the three-layer cascade architecture. When multiple independent signals agree on temperature direction and range, the consensus boost increases confidence in the probabilistic temperature tracker, enabling more aggressive position sizing through the WhaleWatch conviction multiplier.