# Design Documents Creation Report

## Completed Files

### 1. 82-Member Signal Design Doc
**Path**: `/home/node/.openclaw/workspace/prototypes/weather-engine-source/docs/plans/82-MEMBER-SIGNAL-DESIGN.md`
**Sections Written**:
- Data Sources (GEFS 31/6 members, ECMWF 50 members)
- Ensemble Combination Options (equal-weight, skill-weighted, Brier-weighted, log-odds Bayesian)
- Member Normalization Challenges and Approaches
- Per-Station Calibration Curve Implementation
- Single Directional Probability Calculation
- Implementation Recommendations

### 2. Trajectory Lane Design Doc  
**Path**: `/home/node/.openclaw/workspace/prototypes/weather-engine-source/docs/plans/TRAJECTORY-LANE-DESIGN.md`
**Sections Written**:
- Current Implementation Analysis (`p3_trajectory_tracer.py` limitations)
- Design Philosophy: "Heavy Informant, Not Gate"
- Confidence Thresholds and Weight Allocation (traj_quality, w_traj)
- Data Sources and Epoch-Based Analog Matching
- Sequence Matching Methodology (DTW with feature weights)
- Corpus Construction and Climate Zone Pooling
- Output Structure and Integration with GEFS Pipeline
- Implementation Plan (3 phases, 15 hours total)

### 3. Goldilocks Lane Design Doc
**Path**: `/home/node/.openclaw/workspace/prototypes/weather-engine-source/docs/plans/GOLDILOCKS-LANE-DESIGN.md`
**Sections Written**:
- Current Implementation Status (killed as ML, pure deterministic math)
- **CRITICAL FLAG**: KNYC has only 27K observations over 2.6 years (~1 per 50 min) - Goldilocks won't work for KNYC
- Data Sources and Station Coverage Analysis
- Detection Thresholds (0.3°F transient delta, 0.5°F exceedance, 0.2°F reversion)
- Definition of "Fleeting Tick" Microstructure Alert
- Signal Generation Logic and Confidence Calculation
- Implementation Constraints and Station Exclusion List
- Performance Expectations and Integration Recommendations

## Design Questions Requiring Gray Room Input

### 82-Member Signal Questions
1. **Skill differential quantification**: How much better is ECMWF than GEFS historically for temperature probability forecasting?
2. **Calibration curve stability**: How often should per-station calibration curves be refit (daily/weekly/monthly)?
3. **Member dependency**: Are GEFS and ECMWF ensemble members independent or correlated? Should we account for correlation in fusion?
4. **Extreme probability handling**: How to combine very high (>0.9) or very low (<0.1) probabilities from different ensembles?

### Trajectory Lane Questions
1. **Feature selection priority**: Which secondary features (cloud cover, precipitation, dewpoint depression) add meaningful value?
2. **Climate zone definitions**: Are the proposed 5 climate zones (Northeast, South, Midwest, Interior, West) meteorologically meaningful?
3. **Weight cap debate**: Should w_traj be capped at 0.15 as proposed, or higher (e.g., 0.25) for high-confidence trajectory matches?
4. **Missing data handling**: How aggressive should gap-filling be for RH/pressure? Use GEFS forecasts (introduces correlation) or drop features?
5. **Corpus refresh frequency**: Daily update vs weekly rebuild vs monthly recalibration?

### Goldilocks Lane Questions
1. **KNYC handling**: Should we attempt to fill gaps with nearby stations (KLGA, KJFK) or accept exclusion and trade only 18 stations?
2. **Threshold calibration**: Are the current thresholds (0.3°F transient delta, 0.5°F exceedance, 0.2°F reversion) optimal or should they be station-specific?
3. **Signal weighting**: What maximum weight should Goldilocks have in combined probability (suggested: ±10% position adjustment)?
4. **Missing data imputation**: Should we interpolate short gaps (<5 minutes) in ASOS 1-minute data?
5. **Seasonal adjustment**: Should thresholds or confidence calculations vary by season (e.g., larger transient delta in winter)?

## Implementation Notes

- **No source code modified**: All work confined to design documentation in `/docs/plans/`
- **References existing code**: All design documents reference current implementations (`p3_trajectory_tracer.py`, `lane2_goldilocks.py`, database structures)
- **Cross-references Gray Room**: Design decisions acknowledge Gray Room expert input where available
- **Actionable next steps**: Each document includes implementation plans with time estimates

## Recommendations for Gray Room Discussion

1. **Prioritize 82-member signal**: Most immediate value from combining GEFS+ECMWF ensembles
2. **Address KNYC exclusion explicitly**: Goldilocks lane needs clear directive on handling sparse data
3. **Validate trajectory lane scope**: Ensure alignment with existing Phase 3 trajectory code
4. **Set calibration policies**: Establish refresh schedules for all calibration curves

All design documents are ready for Gray Room review and implementation guidance.