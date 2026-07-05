# Weather Engine Backfill Strategy: Closing the 310-Day METAR/NWP Gap

**Document Version:** 1.0  
**Date:** July 3, 2026  
**Project:** Weather Engine - Data Completeness Initiative  
**Status:** Approved for Execution  

---

## Overview

The current weather engine operation has a significant historical data gap between METAR observations and NWP (Numerical Weather Prediction) forecasts of approximately **310 days** as of July 3, 2026. This document outlines a comprehensive strategy to reduce or close this gap while minimizing computational overhead and respecting the project's "standalone scripts only" constraint for backfill/backtesting operations.

---

## Current Situation Analysis

### The Gap
- **METAR Collection:** Operational since January 2026 (approximately day 25 of year)
- **NWP Collection:** Recently initiated with live collection (as of July 3)
- **Historical NWP:** Limited to 30+ days of backfill via `nwp_backfill_30d.py`
- **Result:** 310-day gap between historical METAR and historical NWP data

### Impact on Operations
- **Calibration Issues:** Historical calibration relies only on same-day METAR-METAR correlations without validation against earlier NWP forecasts
- **Backtesting Limitations:** Cannot evaluate ensemble methods using historical NWP data for the period from January to early June
- **Signal Development:** Limits ability to evaluate the effectiveness of forecast disagreement signals over time

---

## Backfill Strategy Options

### Option 1: Complete Historical NWP Backfill
**Approach:** Backfill all NWP data from January 1, 2026, to July 3, 2026 (185 days) for all 4 models × 20 stations
- **Pros:** Completes the historical dataset, enables full ensemble evaluation, enhances training for ML/AI models
- **Cons:** Very high computational cost, requires 20GB+ of API traffic, may take weeks to complete
- **Feasibility:** Marginal - exceeds practical Open-Meteo usage limits
- **Recommendation:** **Not recommended** for initial sprint

### Option 2: Selective High-Impact Backfill  
**Approach:** Backfill only a subset of high-value days:
  - All days where extreme temperatures occurred (threshold: >90°F or <25°F at any station)
  - Days immediately before high-impact market events
  - Sample of diverse weather patterns (every Monday from Jan-June)
- **Pros:** Balanced approach, focused on high-information-value days, manageable data volume
- **Cons:** Leaves significant temporal gaps, may not satisfy statistical requirements
- **Recommendation:** **Recommended for medium-term planning**

### Option 3: Synthetic Data Generation  
**Approach:** Create synthetic NWP-to-METAR correlations based on available overlap period
- Develop statistical models linking NWP characteristics to METAR outcomes during the 30-day overlap period
- Apply these relationships to historical METAR-only epochs to simulate equivalent NWP forecasts
- **Pros:** Provides continuous timeline for backtesting, computationally efficient
- **Cons:** Introduces modeling assumptions, may not reflect true forecast quality
- **Recommendation:** **Not recommended** for primary approach; may serve as auxiliary technique

### Option 4: Parallel Collection with Retrospective Adjustment
**Approach:** Continue forward collection while conducting incremental backfill over next 6 months, accepting the gap for now
- Focus on establishing live data reliability for the next 30 days
- Conduct weekly backfill of another 1-2 weeks of data during off-hours
- **Pros:** Maintains operational readiness, spreads computational load
- **Cons:** Gap persists for extended period, backtesting delayed accordingly
- **Recommendation:** **Recommended for current implementation**

### Option 5: Hybrid Approach (Selected Strategy)
**Approach:** Combine practical elements of above options:
1. **Immediate (Week 1):** Backfill 30 days preceding NWP collection start date (June 3 to July 3) 
2. **Short-term (Week 2-4):** Conduct selective backfill of extreme weather days (May 1 to June 3) 
3. **Medium-term (Month 2):** Execute focused historical analysis to assess if gap closure is operationally necessary
- **Pros:** Meets immediate needs while maintaining computational feasibility; provides data for near-term backtesting
- **Cons:** Some gap continues to exist, requires phased rollout
- **Recommendation:** **RECOMMENDED APPROACH** - Balance risk, reward, and feasibility

---

## Recommended Path Forward: Hybrid Approach (Option 5)

### Phase 1: Critical Gap Completion (Week 1)
**Scope:** Backfill NWP historical data from **June 3, 2026 to July 2, 2026** (30 days)
**Reasoning:** 
- Covers immediate pre-collection period 
- Ensures smooth transition from backtesting to live collection
- Provides 60-day historical data span (May-July)

**Resources Required:**
- `nwp_backfill_30d.py` enhanced for rate limiting
- Estimated run time: 12-15 hours (sequential per model)
- Computational cost: Moderate (4 models × 20 stations × 30 days = 2,400 data days)

**Deliverable:** Complete historical data from June 3 to July 2 enabling:
- Continuity for NWP data analysis
- Baseline comparison for forecast accuracy analysis
- Initial ensemble backtesting capability

### Phase 2: Extreme Value Augmentation (Week 2-4)  
**Scope:** Backfill NWP data for significant weather events during April-May 2026
**Selection Criteria:**
- Days with >90°F or <30°F peak temperatures at any station
- Days preceded significant weather pattern changes
- Selected weekend days to maximize historical variance

**Tools Required:**
- Custom selection script to identify high-value target dates
- Enhanced `nwp_backfill_30d.py` with date range constraints
- Estimated processing: Variable based on target date selection (~10-15 days)

**Deliverable:** Enriched historical dataset focusing on high-variance conditions, enhancing signal evaluation.

### Phase 3: Operational Necessity Assessment (Month 2)
**Objective:** Determine if additional historical closure is operationally justified
- Evaluate model performance gains from historical expansion
- Identify whether 60+days of NWP data is sufficient for development needs
- Assess whether remaining gap significantly impairs operational capabilities

**Deliverable:** Decision memo on future backfill needs.

---

## Technical Implementation Guidelines

### Rate Limiting and Robustness  
The `nwp_backfill_30d.py` already includes retry logic, but for extended backfill campaigns:
```python
def enhanced_rate_limiting():
    # Implement jitter to distribute requests
    base_wait = random.uniform(1.2, 3.5)  # Variable delay per request
    return base_wait
```

### Data Integrity Checks
- Verify completeness of each NWP model for targeted dates
- Implement automated quality checks on historical NWP-METAR correlations
- Validate that synthetic temperature ranges from NWP models are physically reasonable

### Execution Monitoring
- Implement logging with progress percentages 
- Create checkpoints after each 5-day segment to enable resumption
- Monitor for API usage thresholds to prevent hitting daily limits

---

## Resource Requirements

### Computational
- **Server Time:** 15-20 hours for Phase 1 execution
- **Network Usage:** Approximately 0.5 – 1GB of download depending on model availability
- **Disk Space:** Additional 50-100MB for expanded NWP database

### Operational
- **Human Oversight:** Initial run requires monitoring, subsequent runs can be scheduled
- **Schedule:** Execution during off-peak hours recommended (2AM-6AM UTC)

---

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|---------|------------|
| API Rate Limiting | Medium | Medium | Built-in delays, checkpoint/retry, distributed requests |
| Historical Data Unavailability | Low | Medium | Verify Open-Meteo historical reach, fallback to closest available |
| Computational Bottleneck | Low | Medium | Execute during off-peak, scale horizontally if needed |
| Incorrect Historical Correlations | Medium | High | Extensive validation, outlier detection before integration |

---

## Success Metrics

- **Completion Rate:** Target 95%+ of selected dates fully backfilled
- **Data Quality:** Historical NWP-METAR correlations should match expected meteorological relationships
- **Performance:** Backfill runs complete without manual intervention once initiated
- **Gap Reduction:** Reduce current ~310-day gap to <60 days for immediate operational needs

---

## Next Steps

1. **Authorize Phase 1 execution** of the strategy (backfill June 3-Jul 2 to enable July 3 NWP collection)
2. **Enhance `nwp_backfill_30d.py`** with additional robustness features 
3. **Begin Phase 1 execution** immediately following approval
4. **Evaluate operational utility** of expanded historical dataset after Phase 1 completion

---

## Conclusion

This hybrid strategy balances immediate operational requirements against long-term data completeness, acknowledging both the value of comprehensive historical data and the computational limitations of the free-tier APIs available. The phased approach enables validation of data utility while managing resource constraints.

By focusing on critical gap closure and high-utility data augmentation, this strategy should provide the historical depth necessary for meaningful ensemble evaluation while keeping resource expenditure reasonable.

---
**Document Approved By:** Dan Gabriel  
**Date of Authorization:** July 3, 2026