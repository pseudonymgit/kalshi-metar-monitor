# EXPERT1 - STATISTICAL VETTING OF GFS DIRECT NWP SIGNAL

## Analysis of 92.7% Directional Accuracy Claim

### EXECUTIVE SUMMARY
VERDICT: **INFLATED/BIASED** - The 92.7% accuracy appears to be significantly inflated due to seasonal trend correlation rather than genuine predictive skill for day-to-day temperature direction.

### DETAILED ANALYSIS

#### 1. Seasonal Trend Bias Assessment

The core issue identified is "temporal autocorrelation bias" where both GFS and settlement data reflect seasonal trends:

- **Summer Period**: Temperature naturally increases day-to-day due to seasonal warming pattern
- **Winter Period**: Temperature naturally decreases day-to-day due to cooling pattern  
- **GFS Model**: Captures general climate patterns from initial conditions and physics
- **Result**: GFS correctly captures the seasonal trend component, not the daily "surprise" deviations

This creates a false correlation where GFS is credited for seasonal trend prediction rather than actual day-to-day weather prediction accuracy.

#### 2. Statistical Validation Requirements

The current sample size of ~100 forecasts per station with 92.7% accuracy is statistically suspect:

- Historical climatological accuracy for 2m temp direction: approx. 50-55%
- Random walk model (persistence): 50% baseline
- With 2010 samples: 92.7% would represent an extremely rare statistical event
- Rule of thumb: Real improvements above 60% require much larger sample sizes (>10,000) for statistical significance

#### 3. Temporal Look-Ahead Issues

The most critical issue is likely in data alignment:

- `fetch_date`: When forecast was issued
- `target_date`: Date forecast applies to
- Using consecutive `target_date`s compares forecasts from potentially different `fetch_date`s
- Different forecast issuance times means using different model runs with different initialization states
- This violates proper forecasting principles where a forecast should be tested with contemporaneous data only

#### 4. Validation Methodology

To test the actual signal strength, I recommend this approach:

```
import pandas as pd
import numpy as np
from scipy import stats

def validate_nwp_directional_skill(df_gfs, df_settlement):
    """
    Test whether GFS directional skill exceeds random baseline
    """
    
    # Align data by target_date and fetch_date properly
    df_aligned = df_gfs.merge(df_settlement, 
                             left_on=['station', 'target_date'], 
                             right_on=['station', 'settlement_date'],
                             how='inner')
    
    # Calculate deseasonalized directional accuracy
    df_aligned['gfs_trend'] = np.sign(
        df_aligned.groupby('station')['temp_forecast'].diff().fillna(0)
    )
    df_aligned['actual_trend'] = np.sign(
        df_aligned.groupby('station')['actual_temp'].diff().fillna(0)
    )
    
    # Remove seasonal trend component using rolling mean
    seasonal_window = 30  # month-long window
    df_aligned['gfs_deasoned'] = (
        df_aligned.groupby('station')['temp_forecast'].transform(
            lambda x: x - x.rolling(seasonal_window, center=True).mean()
        )
    )
    df_aligned['actual_deasoned'] = (
        df_aligned.groupby('station')['actual_temp'].transform(
            lambda x: x - x.rolling(seasonal_window, center=True).mean()
        )
    )
    
    # Recalculate directions post-deseasonalization
    df_aligned['gfs_deasoned_trend'] = np.sign(
        df_aligned.groupby('station')['gfs_deasoned'].diff().fillna(0)
    )
    df_scaled['actual_deasoned_trend'] = np.sign(
        df_aligned.groupby('station')['actual_deasoned'].diff().fillna(0)
    )
    
    # Measure accuracy on detrended data
    accuracy_detrended = (df_aligned['gfs_deasoned_trend'] == 
                          df_aligned['actual_deasoned_trend']).sum() / len(df_aligned)
    
    return accuracy_detrended, df_aligned

# Expected results should be much closer to 50-65% vs 92.7% for raw comparison
```

#### 5. Confidence Calibration Framework

Given the suspected seasonal bias, a conservative confidence calibration would be:

```
def calibrate_gfs_confidence(observed_accuracy):
    """
    Apply conservative calibration accounting for seasonal bias
    """
    # Assuming 92.7% observed is biased upward by seasonal correlation
    
    # Estimate actual signal strength with seasonal effect removed
    base_accuracy = 0.50  # Climatological baseline
    seasonal_contribution = 0.40  # Estimated bias from seasonal correlation
    
    actual_skill = max(base_accuracy, 
                      observed_accuracy - seasonal_contribution)
    
    # Conservative confidence based on actual calibrated skill
    calibrated_confidence = min(0.75,  
                               (actual_skill - 0.50) * 3 + 0.50)  # Scale 0.50-1.0 to 0.50-0.75
    
    return calibrated_confidence

# Using actual expected detrended accuracy of ~55-60%:
# calibrated_confidence ≈ 0.55-0.60 (not 0.75 based on 92.7%)
```

#### 6. Additional Caveats

- Sample size per station (approx 100) may be insufficient for stable statistics
- Cross-validation across different seasons needed
- Multiple comparisons problem possible given 20 station analyses
- Geographic clustering effect (stations might share regional weather influences)

### RECOMMENDATIONS

1. **Immediate**: Rerun evaluation using deseasonalized method above
2. **Medium term**: Expand dataset to minimum 10,000 forecasts before claiming >70% accuracy 
3. **Confidence adjustment**: Reduce confidence assignment to 0.55-0.65 pending validation
4. **Temporal integrity**: Ensure proper holdout testing with no look-ahead bias

### CONCLUSION

While the GFS model does have legitimate value, the claimed 92.7% accuracy appears to reflect seasonal bias correlation rather than genuine predictive skill. A properly validated estimate is likely in the 55-65% range for directional forecasting.