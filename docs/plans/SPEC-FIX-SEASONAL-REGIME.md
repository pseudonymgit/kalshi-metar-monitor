# SPEC: Fix SeasonalRegimeClassifier Hardcoded Thresholds

## Problem
`SeasonalRegimeClassifier` uses hardcoded universal thresholds (40/50/60/70°F) regardless of station. A 50°F day is "early winter" in Atlanta but "early spring" in Minneapolis. Introduces station-correlated systematic bias.

## Fix: Z-Score-Based Classification
Replace hardcoded thresholds with z-score from `station_climatology` table:

```python
z_score = (current_temp - climatology_mean) / climatology_std
if z_score < -2.0: regime = "extreme_cold"
elif z_score < -1.0: regime = "cold"  
elif z_score < 1.0: regime = "neutral"  # return None
elif z_score < 2.0: regime = "warm"
else: regime = "extreme_warm"
```

## Design
- Load station climatology from existing `station_climatology` table (already populated)
- Cache lookups in memory
- Fallback to hardcoded thresholds if climatology unavailable
- Regime→direction mapping: cold→DOWN, warm→UP
- Confidence scales with |z_score|: 0.95 for |z|>2.0, 0.6 for |z|>0.5, None for |z|<0.5

## Files to Modify
- `core/signals/__init__.py` — SeasonalRegimeClassifier class
- May need to add `station_climatology` DB path

## Test
- KATL in October at 50°F → should produce different regime than KMSP in October at 50°F
- KMSP in January at 50°F → "extreme_warm" (z > 2.0)
- KPHX in January at 50°F → "cold" (z < -1.0)