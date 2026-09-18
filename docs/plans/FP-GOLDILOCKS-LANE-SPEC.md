# FP-GOLDILOCKS-LANE-SPEC.md

## Item 6: Goldilocks Lane Supporting Signals

### Problem Statement
Goldilocks is a lane (detects fleeting temperature microstructure) not just a signal, but:
- Currently only `spike_reversion` is one detection mechanism
- Needs other signals/levers/prediction methods
- Integration with trajectory confirmation unclear
- Multi-signal fusion needed

### Proposed Solution
Design comprehensive goldilocks lane:

1. **Lane Architecture**:
   - Goldilocks as meta-lane coordinating multiple detection mechanisms
   - `spike_reversion` absorbed as one input
   - Add pressure_tendency, dewpoint_depression, cloud_cover as supporting signals

2. **Trajectory Integration**:
   - Use trajectory to confirm/reject goldilocks events
   - Trajectory provides meteorological context
   - Multi-signal fusion for confidence scoring

3. **Multi-Signal Fusion**:
   - Combine spike detection with pressure trends
   - Add dewpoint confirmation
   - Cloud cover modulation

### Data Needed
- ASOS 1-minute observation data quality assessment
- Signal correlation analysis for fusion weights
- Trajectory confirmation validation data

### Code Needed
- `core/goldilocks_lane.py` — Lane coordination framework
- `core/microstructure_fusion.py` — Multi-signal fusion
- `core/trajectory_confirmation.py` — Trajectory-goldilocks integration
- Updates to existing signal registry

### Build Effort
- **Phase 1**: Lane framework (10 hours) — Goldilocks lane coordinator
- **Phase 2**: Signal fusion (12 hours) — Multi-signal integration
- **Phase 3**: Trajectory integration (8 hours) — Confirmation system
- **Phase 4**: Validation (6 hours) — Performance testing
- **Total**: 36 hours (4.5 days)

### Risk Level
**MEDIUM** — Complex signal fusion, risk of overfitting microstructure

### Priority
**P2** — Enhances existing goldilocks capability

### Dependencies
- Completion of trajectory fixes (Item 3)
- Signal correlation analysis

### Improvement Suggestion
**Add pressure_tendency confirmation** — Use pressure_tendency as a confirming signal for goldilocks events. Rapid pressure falls often precede temperature spikes, while rising pressure suggests stability. This should improve goldilocks accuracy by +8-12pp by reducing false positives from instrument noise.