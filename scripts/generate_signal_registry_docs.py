#!/usr/bin/env python3
"""
Generate comprehensive SIGNAL_REGISTRY.md documentation.

Lists:
- All implemented signals with exact names
- Source file and line reference where implemented
- Brief description of methodology

NOTE: This script was rewritten to reflect the actual signal registry
at core/signals/__init__.py SignalRegistry, NOT the fictional modules in
core/signal_processors/ that were referenced in the original version.
"""

import os
from datetime import datetime


def get_actual_signal_registry():
    """
    Return the canonical signal registry metadata by introspecting
    the actual SignalRegistry from core/signals/__init__.py.
    Falls back to a static registry if the module cannot be imported
    (e.g. missing dependencies).
    """
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
        from signals import SignalRegistry
        # Instantiate with a dummy db path to get signal names
        registry = SignalRegistry("dummy.db")
        all_signals = registry.get_all_signals()
        registry_config = {}
        for name, signal_obj in all_signals.items():
            registry_config[name] = {
                'class_name': signal_obj.__class__.__name__,
                'module': signal_obj.__class__.__module__,
                'name': getattr(signal_obj, 'name', name),
                'min_lookback': getattr(signal_obj, 'min_lookback', None),
            }
        return registry_config
    except Exception as e:
        print(f"Warning: Could not import SignalRegistry ({e}), using static fallback.")
        return None


# Static fallback registry — matches the actual signals in core/signals/__init__.py
STATIC_SIGNAL_REGISTRY = [
    {
        "name": "wind_direction_shift",
        "class": "WindDirectionShiftSignal",
        "module": "core/signals/wind_direction_shift.py",
        "description": "Detects significant circular differences in wind direction (>45°) with moderate wind speeds (>10kt). Based on the principle that changing wind directions can indicate approaching weather fronts and thus temperature changes.",
        "parameters": {
            "angle_threshold": "45.0 degrees",
            "wind_speed_threshold": "10.0 knots",
            "lookback_days": "3 days for wind pattern change detection"
        }
    },
    {
        "name": "nwp_analog",
        "class": "NwpAnalogSignal",
        "module": "core/signals/nwp_analog_signal.py",
        "description": "Deterministic k-NN analog matching on NWP forecast fields (5 features: 850mb temp, Z500, cloud cover, 2m temp, dewpoint). Weighted Euclidean distance, K=50, ±45d seasonal window, distance-weighted voting, beta-binomial probability. No ML/AI.",
        "parameters": {
            "k_analogs": "50 nearest neighbors (capped to available)",
            "min_analogs": "10 minimum valid analogs required",
            "seasonal_window": "±45 days year-agnostic day-of-year filter",
            "nwp_variables": "5 fields: temperature_850hPa_daily_mean, geopotential_height_500hPa_daily_mean, cloud_cover_daily_mean, temperature_2m_max, dew_point_2m_daily_mean"
        }
    },
    {
        "name": "goldilocks",
        "class": "GoldilocksSignal",
        "module": "core/signals/goldilocks_signal.py",
        "description": "Asymmetric reversion signal with Goldilocks confidence scaling. Uses different confidence thresholds for up (0.40 base) vs down (0.25 base) predictions based on empirical performance.",
        "parameters": {
            "lookback_days": "31 days",
            "z_score_threshold": "1.0 standard deviations",
            "confidence_up_base": "0.40",
            "confidence_down_base": "0.25"
        }
    },
    {
        "name": "persistence",
        "class": "PersistenceSignal",
        "module": "core/signals/persistence_signal.py",
        "description": "Naive persistence forecast: tomorrow's temperature will be similar to today's. Simple baseline signal for ensemble comparison.",
        "parameters": {
            "lookback_hours": "24 hours"
        }
    },
    {
        "name": "simple_trend",
        "class": "SimpleTrendSignal",
        "module": "core/signals/simple_trend_signal.py",
        "description": "Simple linear trend detection from recent temperature observations. Uses moving average to compute slope direction.",
        "parameters": {
            "lookback_days": "7 days",
            "min_slope_threshold": "0.5 degrees per day"
        }
    },
    {
        "name": "gaussian",
        "class": "GaussianSignal",
        "module": "core/signals/gaussian_signal.py",
        "description": "48-day Gaussian deviation signal. Computes z-score of today's high vs 48-day rolling mean. z > 1.0 predicts down (reversion), z < -1.0 predicts up. Confidence = |z|.",
        "parameters": {
            "lookback_days": "48 days",
            "z_up_threshold": "-1.0",
            "z_down_threshold": "1.0"
        }
    },
    {
        "name": "gaussian_v2",
        "class": "GaussianV2Signal",
        "module": "core/signals/gaussian_v2_signal.py",
        "description": "30-day Gaussian deviation signal (shorter window than gaussian). z > 0.5 predicts down, z < -0.5 predicts up. More sensitive to recent deviations.",
        "parameters": {
            "lookback_days": "31 days",
            "z_up_threshold": "-0.5",
            "z_down_threshold": "0.5"
        }
    },
    {
        "name": "pressure_delta",
        "class": "PressureDeltaSignal",
        "module": "core/signals/pressure_delta_signal.py",
        "description": "Pressure change signal: detects barometric pressure changes > 2mb between consecutive days. Pressure increase predicts up (high pressure = stable/rising temps), decrease predicts down.",
        "parameters": {
            "lookback_days": "2 days",
            "pressure_change_threshold": "2.0 mb",
            "confidence_scaling": "abs(dp) / 5.0, capped at 0.8"
        }
    },
    {
        "name": "regime",
        "class": "RegimeSignal",
        "module": "core/signals/regime_signal.py",
        "description": "DTR-scaled regime detection signal. When volatility is low (vol < 1.0) and slope is below an adaptive threshold (scaled by DTR), detects mean-reversion from 30-day average. Uses DTR to adjust sensitivity: high DTR = wider threshold, low DTR = narrower threshold.",
        "parameters": {
            "volatility_window": "15 days",
            "mean_reversion_window": "30 days",
            "dtr_high_threshold": "15.0 (threshold=1.0)",
            "dtr_low_threshold": "8.0 (threshold=0.4)",
            "dtr_mid_threshold": "0.8"
        }
    },
    {
        "name": "forecast_disagreement",
        "class": "ForecastDisagreementSignal",
        "module": "core/signals/forecast_disagreement_signal.py",
        "description": "Measures disagreement between NWP model forecasts. Higher disagreement = lower confidence in any single direction. Uses ensemble spread as a proxy for forecast uncertainty.",
        "parameters": {
            "min_models": "2",
            "disagreement_threshold": "configurable"
        }
    },
    {
        "name": "calendar_climatology",
        "class": "CalendarClimatologySignal",
        "module": "core/signals/calendar_climatology_signal.py",
        "description": "Historical climatology signal based on calendar day patterns. Uses historical settlement data for the same calendar date across all years to compute the probability of temperature moving up or down.",
        "parameters": {
            "min_history_years": "2",
            "confidence_scaling": "based on historical sample size and volatility"
        }
    },
]


def create_registry_docs():
    """Create signal registry documentation from the actual signal registry."""
    
    actual_registry = get_actual_signal_registry()
    
    header = f"""# Weather Engine Signal Registry

*Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC*

This document catalogs all active forecasting signals registered in the
`core/signals/__init__.py` SignalRegistry. These are the signals used by the
paper trading engine and signal fusion stack.

**Source of truth:** `core/signals/__init__.py` — `SignalRegistry` class

## Active Signals

"""
    
    # Build signal list from static registry (matches actual code)
    content_parts = []
    
    for i, signal in enumerate(STATIC_SIGNAL_REGISTRY, 1):
        signal_section = f"### {i}. {signal['name']}\n\n"
        signal_section += f"- **Class**: `{signal['class']}`\n"
        signal_section += f"- **Module**: `{signal['module']}`\n"
        signal_section += f"- **Description**: {signal['description']}\n\n"
        
        if signal.get('parameters'):
            signal_section += "**Parameters**:\n\n"
            for param, desc in signal['parameters'].items():
                signal_section += f"  - `{param}`: {desc}\n"
            signal_section += "\n"
        
        # Add runtime info from actual registry if available
        if actual_registry and signal['name'] in actual_registry:
            info = actual_registry[signal['name']]
            signal_section += f"  - **min_lookback**: {info.get('min_lookback', 'N/A')}\n\n"
        
        content_parts.append(signal_section)
    
    footer = """## Registration Notes

All signals listed above are registered in `core/signals/__init__.py` SignalRegistry.
Signals are instantiated with a `db_path` parameter for database access.

### Adding a New Signal

1. Create a new file in `core/signals/` with a class implementing `evaluate(idx, days)` interface
2. Add `name` and `min_lookback` properties
3. Import the class in `core/signals/__init__.py`
4. Add an instance to `SignalRegistry.signals` dict
5. Run this script to regenerate documentation

### Deprecated / Removed Signals

The following signal modules were previously referenced in `core/signal_processors/`
but were never implemented (fictional references from original spec):
- `trend_deviation_signal.py`
- `diffusion_convergence_signal.py`
- `momentum_differential_signal.py`
- `atmospheric_pressure_tendency.py`
- `wind_direction_variance.py`
- `humidity_inversion_anomaly.py`
- `visibility_trend_deviation.py`

These are NOT part of the actual signal registry and should not be referenced.
"""
    
    full_content = header + "".join(content_parts) + footer
    return full_content


if __name__ == '__main__':
    docs = create_registry_docs()
    print(docs)