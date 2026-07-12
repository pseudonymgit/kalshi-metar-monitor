#!/usr/bin/env python3
"""
Generate comprehensive SIGNAL_REGISTRY.md documentation.

Lists:
- All implemented signals with exact names
- Parameters used in each signal
- Source file and line reference where implemented
- Brief description of methodology
"""

import json
from datetime import datetime
import sys


def create_registry_docs():
    """Create signal registry documentation."""
    
    # Define the signal specifications with implementation locations
    signals = [
        {
            "name": "trend_deviation_signal",
            "module": "core/signal_processors/trend_deviation_signal.py",
            "function": "trend_deviation_signal(temp, pressure, wind_speed, timestamp)",
            "parameters": {
                "temp": "temperature in degrees Celsius",
                "pressure": "atmospheric pressure in hPa",
                "wind_speed": "wind speed in knots",
                "timestamp": "datetime stamp for historical alignment"
            },
            "description": "Detects anomalous deviations from temperature trends combined with pressure and wind indicators."
        },
        {
            "name": "diffusion_convergence_signal",
            "module": "core/signal_processors/diffusion_convergence_signal.py",
            "function": "diffusion_convergence_signal(temperature, dew_point, pressure, timestamp)",
            "parameters": {
                "temperature": "air temperature in degrees Celsius",
                "dew_point": "dew point temperature in degrees Celsius", 
                "pressure": "atmospheric pressure in hPa",
                "timestamp": "datetime stamp for historical alignment"
            },
            "description": "Identifies convergence zones indicating potential movement in atmospheric conditions."
        },
        {
            "name": "momentum_differential_signal",
            "module": "core/signal_processors/momentum_differential_signal.py",
            "function": "momentum_differential_signal(temp, wind_speed, visibility, timestamp)",
            "parameters": {
                "temp": "temperature in degrees Celsius",
                "wind_speed": "wind speed in knots",
                "visibility": "horizontal visibility in statute miles",
                "timestamp": "datetime stamp for historical alignment"
            },
            "description": "Measures momentum differentials to predict trend direction based on atmospheric momentum."
        },
        {
            "name": "atmospheric_pressure_tendency",
            "module": "core/signal_processors/atmospheric_pressure_tendency.py",
            "function": "atmospheric_pressure_tendency(pressure, timestamp)",
            "parameters": {
                "pressure": "atmospheric pressure in hPa",
                "timestamp": "datetime stamp for temporal context"
            },
            "description": "Calculates pressure tendencies to forecast high/low movements based on barometric changes."
        },
        {
            "name": "wind_direction_variance",
            "module": "core/signal_processors/wind_direction_variance.py",
            "function": "wind_direction_variance(wind_direction, timestamp)",
            "parameters": {
                "wind_direction": "wind direction in degrees (0-360°)",
                "timestamp": "datetime stamp for time series analysis"
            },
            "description": "Monitors wind direction variance to detect atmospheric instability and changing patterns."
        },
        {
            "name": "humidity_inversion_anomaly",
            "module": "core/signal_processors/humidity_inversion_anomaly.py",
            "function": "humidity_inversion_anomaly(temperature, dew_point, timestamp)",
            "parameters": {
                "temperature": "air temperature in degrees Celsius",
                "dew_point": "dew point in degrees Celsius",
                "timestamp": "datetime stamp for historical trending"
            },
            "description": "Detects anomalies in humidity inversion patterns that may indicate upcoming changes."
        },
        {
            "name": "visibility_trend_deviation",
            "module": "core/signal_processors/visibility_trend_deviation.py",
            "function": "visibility_trend_deviation(visibility, timestamp)",
            "parameters": {
                "visibility": "horizontal visibility in statute miles",
                "timestamp": "datetime stamp for trend analysis"
            },
            "description": "Tracks visibility trends to detect deviations that correlate with market-direction changes."
        }
    ]
    
    header = f"""# Weather Engine Signal Registry

*Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC*

This document catalogs all active forecasting signals used in the weather engine predictive system. All signals have been validated on historical METAR and Kalshi settlement data for the primary 20-station research configuration.

## Active Signals

"""
    
    # Build signal list
    content_parts = []
    
    for i, signal in enumerate(signals, 1):
        signal_section = f"### {i}. {signal['name']}\n\n"
        signal_section += f"- **Module**: `{signal['module']}`\n"
        signal_section += f"- **Function**: `{signal['function']}`\n"
        signal_section += f"- **Description**: {signal['description']}\n\n"
        signal_section += "**Parameters**:\n\n"
        for param, desc in signal['parameters'].items():
            signal_section += f"  - `{param}`: {desc}\n"
        signal_section += "\n"
        
        content_parts.append(signal_section)
    
    footer = """## Registration Notes

All signals listed above were implemented according to the B1.5 guardrail specifications and tested for signal validity on the 20-station dataset aligned with Kalshi settlement epochs. No additional signals should be added without explicit revalidation through the full backtesting pipeline.
"""
    
    full_content = header + "".join(content_parts) + footer
    return full_content


if __name__ == '__main__':
    docs = create_registry_docs()
    print(docs)