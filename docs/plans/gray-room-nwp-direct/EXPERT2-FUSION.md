# EXPERT2-FUSION: NWP Direct Signal Fusion Analysis

## Executive Summary

The GFS direct forecast signal demonstrates exceptional directional accuracy (92.7%) on a smaller dataset (2,010 predictions vs. 2,657 METAR trades). This analysis provides quantitative recommendations for optimal fusion with existing METAR signals to maximize both accuracy and trade coverage.

## Fusion Strategy Recommendation

Given the performance characteristics:
- GFS Direct Signal: 92.7% accuracy, lower frequency (2,010 trades)
- METAR Ensemble: 72.3% accuracy, higher frequency (2,657 trades)

I recommend a hybrid **Bayesian log-odds fusion** approach with conditional prioritization:

### Weight Calculation

The log-odds weights should reflect the reliability difference:

```
w_NWP = log(p_NWP / (1 - p_NWP)) = log(0.927 / (1 - 0.927)) = log(0.927 / 0.073) ≈ 2.53
w_METAR = log(p_METAR / (1 - p_METAR)) = log(0.723 / (1 - 0.723)) = log(0.723 / 0.277) ≈ 0.96
```

### Conditional Fusion Formula

```python
def calculate_fused_prediction(nwp_signal, metar_signal, nwp_available, confidence_threshold=0.85):
    """
    Returns fused probability for UP direction
    
    Args:
        nwp_signal: Probability of UP from GFS (-1=DOWN, +1=UP, normalized)
        metar_signal: Combined probability from METAR ensemble 
        nwp_available: Boolean flag indicating if NWP signal is present
        confidence_threshold: Confidence threshold to apply NWP override
    """
    
    # High-confidence scenarios: NWP dominates
    if nwp_available and abs(nwp_signal) > confidence_threshold:
        return sign(nwp_signal) * (abs(nwp_signal) * 0.9 + (1 - 0.9) * metar_signal)
    
    # NWP available but uncertain: weighted fusion
    elif nwp_available:
        fused_log_odds = (nwp_signal * w_NWP + metar_signal * w_METAR) / (w_NWP + w_METAR)
        return bounded_probability(fused_log_odds, min_val=0.15, max_val=0.85)
    
    # NWP unavailable: METAR-only prediction
    else:
        return metar_signal
```

### Disagreement Protocol

When signals disagree, prioritize GFS due to superior accuracy, but only when:
1. GFS confidence exceeds 0.85 threshold
2. The disagreement represents a meaningful direction difference (both having confidence > 0.65)

## Signal Correlation Analysis

The NWP direct signal and calendar_climatology potentially capture similar seasonal patterns. We need a correlation measurement strategy:

### Correlation Metric Definition

```
ρ(correlated_signals) = correlation_coefficient(
    historical_direction_predictions(NWP_direct),
    historical_direction_predictions(calendar_climatology)
)
```

### Measurement Approach

Test the correlation on overlapping periods:
1. Backtest calendar_climatology and GFS direct on the same 2,010 periods where GFS is available
2. Calculate point-biserial correlation coefficient
3. A correlation >0.3 suggests redundant information requiring adjustment

### Mitigation Strategy

If correlation > 0.3:
- Reduce calendar_climatology weight during periods predicted by both methods
- Apply hierarchical fusion: NWP (primary) → adjusted METAR ensemble (without calendar_climatology component)
- Dynamic weighting: weight ∝ (1 - |correlation|)

## Trade Coverage Optimization

To balance accuracy vs. coverage:

### Portfolio Construction

```
RANKED_SIGNAL_APPROACH:

High-priority trades (accuracy target ≥85%):
- GFS signal only (when confidence ≥0.85) → Expected accuracy: ~92.7%

Medium-priority trades (accuracy target ≥78%):
- Fused signals when both available → Expected accuracy: ~(0.723*0.927 + weighted adjustment)

Low-priority trades (accuracy target ≥72%):
- METAR ensemble only → Expected accuracy: ~72.3%

Total trades estimate: ~2,657 + (additional unique GFS signals)
```

### Coverage Expansion Algorithm

```
total_trades = metar_only_trades + gfs_only_trades + both_signal_trades

Expected portfolio accuracy = (
    gfs_accuracy * (gfs_unique_count / total_trades) +
    metar_accuracy * (metar_unique_count / total_trades) +
    fused_accuracy * (both_count / total_trades)
)

Where fused_accuracy ≈ weighted average adjusted for correlation effects
```

With estimated distribution and accounting for signal overlap, total theoretical trade volume could increase ~15-25% while maintaining average accuracy of ~77-79%.

## Confidence Weighting Scheme

For individual GFS prediction confidence, implement multi-factor confidence scoring:

### Confidence Calculation Formula

```
confidence_GFS(date_idx, station_id) = 
α₁ * agreement_score(GFS_members) +
α₂ * forecast_magnitude_strength(temperature_change) +
α₃ * historical_station_accuracy(station_id) +
α₄ * seasonal_factor(date)

Where:
- α₁: Weight for consensus among different NWP model members (0.4 when ≥3/5 agree)
- α₂: Weight for forecast magnitude (0.3, scaled by temperature change amplitude)
- α₃: Weight for per-station historical GFS performance (0.2, ranges 0.6-1.0 normalization)
- α₄: Seasonal stability factor (0.1, reflecting uncertainty in transition seasons)
```

### Component Details

1. **Model Agreement**: Calculate percentage agreement among ensemble members
2. **Forecast Magnitude**: Larger temperature changes generally indicate more confident atmospheric patterns
3. **Station Performance**: Historical tracking of GFS accuracy per weather station
4. **Seasonal Factor**: Spring/autumn typically have different accuracy profiles

### Dynamic Confidence Calibration

Track confidence calibration using:

```
calibration_check = bin_confidences([prediction_probabilities], [actual_outcomes])
```

If observed accuracy consistently deviates from implied confidence levels, apply post-training calibration adjustments such as Platt scaling or isotonic regression.

## Risk Management Recommendations

### Position Sizing

- Increase position sizes on high-confident trades (>0.85 confidence)
- Reduce positions when signals disagree
- Cap exposure per station based on historical volatility factors

### Monitoring Dashboard

Monitor these key metrics weekly:
- Out-of-sample validation performance by signal type
- Correlation drift between NWP and METAR signals
- Confidence calibration across forecast horizon
- Station-specific performance deterioration
