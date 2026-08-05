# 82-Member Signal Design Document

## Overview
This document outlines the design for combining GEFS (31/6 members) and ECMWF TIGGE (50 members) ensemble forecasts into a unified 82-member probabilistic signal.

## Data Sources

### GEFS Ensemble (data/gefs_archive.db)
- **Step 24 (f024)**: 31 members available
- **Steps 0-21**: Only 6 members available (operational limitation)
- **Time resolution**: 6-hourly forecasts
- **Variables**: Temperature (2m), humidity, pressure, wind

### ECMWF TIGGE Ensemble (data/tigge_archive.db) 
- **50 members** available consistently across all forecast steps
- **Time resolution**: 6-hourly forecasts  
- **Variables**: Temperature (2m), humidity, pressure, wind
- **Note**: ECMWF members are generally higher skill than GEFS

## Ensemble Combination Options

### Option 1: Equal-Weight Fusion
```python
def equal_weight_fusion(gefs_probs, ecmwf_probs):
    """Simple arithmetic mean of both ensemble probabilities."""
    return (gefs_probs + ecmwf_probs) / 2
```
**Pros**: Simple, no calibration needed
**Cons**: Ignores member skill differences

### Option 2: Skill-Weighted Fusion
```python
def skill_weighted_fusion(gefs_probs, ecmwf_probs, gefs_skill, ecmwf_skill):
    """Weight ensembles by their historical skill scores."""
    total_skill = gefs_skill + ecmwf_skill
    return (gefs_probs * gefs_skill + ecmwf_probs * ecmwf_skill) / total_skill
```
**Skill metrics**: Brier Skill Score, ROC AUC, reliability
**Pros**: Accounts for differential performance
**Cons**: Requires ongoing skill calibration

### Option 3: Brier-Weighted Fusion
```python
def brier_weighted_fusion(gefs_probs, ecmwf_probs, gefs_brier, ecmwf_brier):
    """Weight inversely by Brier score (lower Brier = higher weight)."""
    gefs_weight = 1 / gefs_brier
    ecmwf_weight = 1 / ecmwf_brier
    total_weight = gefs_weight + ecmwf_weight
    return (gefs_probs * gefs_weight + ecmwf_probs * ecmwf_weight) / total_weight
```
**Pros**: Directly optimizes for probability accuracy
**Cons**: Sensitive to Brier score estimation

### Option 4: Log-Odds Bayesian Fusion
```python
def log_odds_bayesian_fusion(gefs_probs, ecmwf_probs, prior=0.5):
    """Combine probabilities using log-odds Bayesian approach."""
    def prob_to_log_odds(p):
        return math.log(p / (1 - p))
    
    def log_odds_to_prob(lo):
        return 1 / (1 + math.exp(-lo))
    
    gefs_lo = prob_to_log_odds(gefs_probs)
    ecmwf_lo = prob_to_log_odds(ecmwf_probs)
    prior_lo = prob_to_log_odds(prior)
    
    # Simple average of log odds (could be weighted)
    combined_lo = (gefs_lo + ecmwf_lo + prior_lo) / 3
    return log_odds_to_prob(combined_lo)
```
**Pros**: Mathematically sound, handles probability extremes well
**Cons**: More complex, requires careful calibration

## Member Normalization Challenges

### GEFS Member Inconsistency
- **Step 24**: 31 members → robust statistics
- **Steps 0-21**: 6 members → higher variance, less reliable

### Normalization Approaches

#### Approach A: Member-Count Normalization
```python
def normalize_by_member_count(gefs_prob, ecmwf_prob):
    """Weight by number of members in each ensemble."""
    total_members = 31 + 50  # 81 total (GEFS f024) or 6 + 50 = 56 (earlier steps)
    gefs_weight = 31 / total_members if step == 24 else 6 / total_members
    ecmwf_weight = 50 / total_members
    return gefs_prob * gefs_weight + ecmwf_prob * ecmwf_weight
```

#### Approach B: Variance-Stabilized Normalization
```python
def variance_stabilized_normalization(gefs_prob, ecmwf_prob):
    """Account for different ensemble sizes in variance estimation."""
    # GEFS variance scales with 1/sqrt(n_members)
    gefs_n = 31 if step == 24 else 6
    ecmwf_n = 50
    
    gefs_variance = 1 / math.sqrt(gefs_n)
    ecmwf_variance = 1 / math.sqrt(ecmwf_n)
    
    # Weight inversely by variance
    gefs_weight = 1 / gefs_variance
    ecmwf_weight = 1 / ecmwf_variance
    total_weight = gefs_weight + ecmwf_weight
    
    return (gefs_prob * gefs_weight + ecmwf_prob * ecmwf_weight) / total_weight
```

#### Approach C: Calibration-Curve Normalization
```python
def calibration_curve_normalization(prob, ensemble_type, step):
    """Apply per-station calibration curves to each ensemble."""
    calibration_curve = load_calibration_curve(ensemble_type, step)
    return calibration_curve.calibrate(prob)
```

## Per-Station Calibration

### Calibration Curve Implementation
Each station requires separate calibration curves for:
- GEFS f024 (31 members)
- GEFS f000-f021 (6 members) 
- ECMWF TIGGE (50 members)

### Curve Fitting Method
```python
def fit_calibration_curve(forecast_probs, observed_outcomes):
    """Fit isotonic regression or logistic calibration curve."""
    # Use isotonic regression for non-parametric calibration
    from sklearn.isotonic import IsotonicRegression
    
    ir = IsotonicRegression(out_of_bounds='clip')
    ir.fit(forecast_probs, observed_outcomes)
    return ir
```

### Curve Storage
Calibration curves should be stored in `data/calibration_curves.json` with structure:
```json
{
  "station": "KNYC",
  "ensemble_type": "gefs_f024", 
  "calibration_points": [[0.1, 0.08], [0.3, 0.25], [0.5, 0.48], [0.7, 0.72], [0.9, 0.88]],
  "n_samples": 1500,
  "last_updated": "2026-08-05"
}
```

## Single Directional Probability Calculation

### From Combined Spread
```python
def directional_probability_from_spread(ensemble_members, threshold):
    """Calculate probability from member count exceeding threshold."""
    members_above = sum(1 for member in ensemble_members if member > threshold)
    return members_above / len(ensemble_members)
```

### Confidence Intervals
```python
def binomial_confidence_interval(prob, n_members, confidence=0.90):
    """Calculate confidence interval for ensemble probability."""
    from statsmodels.stats.proportion import proportion_confint
    count = prob * n_members
    lower, upper = proportion_confint(count, n_members, alpha=1-confidence)
    return lower, upper
```

## Implementation Recommendations

1. **Start with equal-weight fusion** for simplicity
2. **Implement per-station calibration curves** for each ensemble type
3. **Use variance-stabilized normalization** to handle member count differences
4. **Consider log-odds Bayesian fusion** for optimal probability combination
5. **Monitor ensemble skill differentials** and adjust weights accordingly

## Gray Room Input Needed

1. **Skill differential quantification**: How much better is ECMWF than GEFS historically?
2. **Calibration curve stability**: How often should curves be refit?
3. **Member dependency**: Are GEFS and ECMWF members independent or correlated?
4. **Extreme probability handling**: How to combine very high/low probabilities from different ensembles?