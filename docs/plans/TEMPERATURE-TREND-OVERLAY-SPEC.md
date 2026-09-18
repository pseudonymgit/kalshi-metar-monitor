# Microstructure Trend Overlay — Expert Spec & Build Plan

**Purpose:** Detect when temperature is statistically approaching a Kalshi settlement bucket boundary using real-time METAR/T-group data, and apply as a confidence modifier in the SignalRegistry pipeline.

## Background

The Goldilocks investigation revealed that:
- Original spike-detection concept is dead (no real-time 1-min temp source)
- But the *statistical approach* toward a bucket boundary IS detectable from METAR data
- Events cluster overnight (23:00-05:00 local time) — this is a LOW market phenomenon
- Surviving opportunity: confidence modifier, not standalone lane

## Architecture

```
METAR/T-group (5-15 min) → Trend Engine → f024 Modifier → SignalRegistry
```

The trend overlay monitors real-time temperature observations and answers one question: **"Is temperature currently trending toward a bucket boundary?"**

## Algorithm

### Input
- Real-time METAR temperature (integer °F) or T-group (0.1°C)
- 5-15 minute observation cadence
- Last 10-15 observations (~1-3 hours of history)

### Computation (per station, every tick)

```
1. Compute short-term trend (ST)
   ST = slope of last N observations (N=5, ~25-60 min window)
   
2. Compute boundary proximity (BP)
   BP = distance from current temp to nearest integer °F boundary
   If current temp is 74.3°F → distance to 75°F = 0.7°F
   
3. Compute trend projection (TP)
   TP = ST × expected_time_to_boundary
   Does current trend cross the boundary within the observation horizon?
   
4. Compute confidence modifier (f024)
   If TP > 0 AND BP < 0.5°F (approaching boundary):
     modifier = min(1.0, TP / 0.5) × (1.0 - BP / 0.5)
   Else if TP < 0 AND BP < 0.5°F (receding from boundary):
     modifier = 0.0 (no overlay needed)
   Else:
     modifier = 0.0
```

### Direction sensitivity
- For **HIGH markets**: upward trend toward boundary → increase confidence
- For **LOW markets**: downward trend toward boundary → increase confidence
- For LOW markets specifically: apply **overnight boost** (22:00-06:00 local time)

### Modifier application
```
f024 = after all other confidence adjustments
If direction == "up" AND temperature trending up toward boundary:
    f024_boost = min(base_boost × proximity_factor, 0.2)
If direction == "down" AND temperature trending down toward boundary:
    f024_boost = min(base_boost × proximity_factor, 0.2)
```

## Signals Where This Adds Value

The trend overlay provides independent signal that is NOT captured by existing pipeline signals:

1. **GEFS baseline signals** — NWP models predict daily max/min, not real-time approach
2. **calendar_climatology** — historical averages, not current conditions
3. **seasonal_regime** — broad regime, not minute-level trend
4. **spike_reversion** — detects spikes after they happen, not trending approach

The overlay captures the **"temperature is moving toward a boundary RIGHT NOW"** state that no other signal measures.

## Falsification Tests

| Test | Threshold | Method |
|---|---|---|
| F1: Information overlap | Correlation < 0.3 with all existing signals | Compute Pearson r against gaussian, calendar_climatology, seasonal_regime, spike_reversion |
| F2: Additive accuracy | +1-3pp accuracy when added to ensemble | Sweep with and without f024 enabled |
| F3: LOW market effectiveness | +2pp for LOW markets specifically | Split sweep by HIGH vs LOW market type |
| F4: Nighttime boost effectiveness | +2pp for overnight hours | Hour-level accuracy breakdown |
| F5: No overfit | Within 2pp of holdout | Test on last 6 months of data |
| F6: Feature importance | f024 contributes non-zero weight | Check feature importance in sweep output |

## Implementation Plan

### File: `core/signals/temperature_trend_signal.py`

```python
class TemperatureTrendSignal:
    """f024 — Microstructure Trend Overlay"""
    
    def __init__(self, window_short=5, window_long=15, 
                 boundary_threshold=0.5, max_boost=0.2,
                 overnight_only=True, overnight_start=22, overnight_end=6):
        # ... initialization
        pass
    
    def evaluate(self, station, current_temp, observations_hist, local_hour):
        """
        Args:
            current_temp: Current METAR temp (°F, integer or float)
            observations_hist: List of (timestamp, temp) tuples from last 1-3 hours
            local_hour: Current local hour (0-23)
        
        Returns:
            dict with confidence_modifier, trend_strength, boundary_proximity
        """
        # 1. Filter observations by station
        # 2. Check overnight window
        # 3. Compute short-term trend (linear regression on last N points)
        # 4. Compute boundary proximity
        # 5. Combine into confidence modifier
        # 6. Apply overnight boost if applicable
        pass
```

### Sweep Parameters
- `window_short`: Number of observations for short-term trend (3, 5, 7)
- `window_long`: Number of observations for baseline (10, 15, 20)  
- `boundary_threshold`: Proximity to boundary in °F (0.3, 0.5, 0.7)
- `max_boost`: Maximum confidence boost (0.1, 0.2, 0.3)
- `overnight_only`: Apply only during overnight hours (true/false)
- `overnight_boost_multiplier`: Extra boost for overnight (1.0, 1.5, 2.0)

### Registration
Add to `SignalRegistry` as signal #17:
```python
from core.signals.temperature_trend_signal import TemperatureTrendSignal

signal_registry.register(
    "temperature_trend",
    TemperatureTrendSignal(...),
    description="f024 — Microstructure trend overlay from real-time METAR data"
)
```

### Integration Points
- SignalRegistry: register as 17th signal
- C3 LaneManager: applied as confidence modifier in main lane
- No standalone lane — modifier only

## Data Sources
- **Real-time METAR**: NWS API (integer °F, 5-15 min) — already available
- **T-group**: NWS API (0.1°C) — mathematically derived from METAR but provides finer resolution for trend detection
- **Historical test data**: IEM ASOS DB (for backtesting)

## Backtesting
- Use IEM ASOS data (integer °F, ~8% coverage for most stations) to simulate METAR feed
- For KNYC: use full 1-min resolution, sample at METAR-like frequency
- Test on 2024-01-01 to 2026-08-03 (full dataset)
- Verify signals add +1-3pp to ensemble accuracy

## Rollout
1. ✅ Build spec and register in SignalRegistry
2. ✅ Backtest on historical data
3. ✅ Add sweep parameters
4. Run full sweep (integrated with ASOS calibration supplement)
5. Review and iterate

## Constraints
- No ML — deterministic math only
- No additional API calls beyond existing METAR feed
- Must handle missing observations gracefully
- Must work with integer °F input (T-group decimals are nice-to-have)
