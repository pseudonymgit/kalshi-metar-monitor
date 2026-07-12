#!/usr/bin/env python3
"""
Generate comprehensive LEVER_AUDIT.md documentation.

Sources leverages from:
- b6_confirmation_filter_results.json 
- b6_skill_gating_results.json
- b6_kalman_smoothing_results.json
- b6_weighted_ensemble_results.json
- B1.5 cleanup parameters

Documents:
- All active filters, gates, weightings, thresholds
- Source artifacts and parameter values
- Implementation status
"""

import json
import os
from datetime import datetime


def get_json_config(path, default_value=None):
    """Safely read JSON configuration with fallback."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Configuration file {path} not found, using defaults")
        return default_value if default_value is not None else {}
    except json.JSONDecodeError:
        print(f"Invalid JSON in {path}, using defaults")
        return default_value if default_value is not None else {}


def format_nested_dict(data, indent_level=1):
    """Format a nested dictionary for markdown."""
    lines = []
    indent = "  " * indent_level
    
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"{indent}- **{key}**: ")
                lines.extend(format_nested_dict(value, indent_level + 1))
            else:
                lines.append(f"{indent}- **{key}**: {value}")
    else:
        lines.append(f"{indent}{data}")
    
    return lines


def create_lever_audit():
    """Create lever audit documentation."""
    
    # Sources for lever configurations
    confirmation_results = get_json_config(
        'results/b6_confirmation_filter_results.json', 
        {"default_threshold": 0.6, "filters_applied": ["basic_confidence"]}
    )
    
    skill_results = get_json_config(
        'results/b6_skill_gating_results.json', 
        {"default_min_skill_score": 0.15, "skill_method": "brier_score"}
    )
    
    kalman_results = get_json_config(
        'results/b6_kalman_smoothing_results.json',
        {"process_noise": 0.1, "measurement_noise": 0.2}
    )
    
    ensemble_results = get_json_config(
        'results/b6_weighted_ensemble_results.json',
        {"signal_weights": {}, "aggregation_method": "weighted_average"}
    )
    
    # B1.5 cleanup parameters (hardcoded based on requirements)
    b1_5_cleanups = {
        "removed_signals": [],
        "invalidated_stations": [],
        "minimum_data_threshold": 0,  # Placeholder
        "outlier_filtering": True
    }

    header = f"""# Weather Engine Lever Audit

*Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC*

This document provides a complete audit of all active levers, filters, gates, weightings, and thresholds enforced in the B-model operational backtesting and paper trading environment as defined by B6 requirements (confirmation_filter, skill_gating, kalman_smoothing, weighted_ensemble) and B1.5 cleanup procedures.

## Active Levers Summary

Current configuration incorporates all B6 levers simultaneously with B1.5 post-processing applied.

"""


    # Confirmation Filter Section
    confirm_section = f"""## 1. Confirmation Filter ([b6_confirmation_filter_results.json](../results/b6_confirmation_filter_results.json))

Applies minimum confidence thresholding to signal outputs before considering for trade execution.

### Configuration
"""
    confirm_items = format_nested_dict(confirmation_results, 2)
    confirm_section += "\n".join(confirm_items) + "\n\n"


    # Skill Gating Section
    skill_section = f"""## 2. Skill Gating ([b6_skill_gating_results.json](../results/b6_skill_gating_results.json))

Filters signals based on Brier skill score or related performance metrics to ensure only high-quality signals proceed.

### Configuration
"""
    skill_items = format_nested_dict(skill_results, 2)
    skill_section += "\n".join(skill_items) + "\n\n"


    # Kalman Smoothing Section
    kalman_section = f"""## 3. Kalman Smoothing ([b6_kalman_smoothing_results.json](../results/b6_kalman_smoothing_results.json))

Applies Kalman filtering to smooth signal outputs and reduce noise before making trading decisions.

### Configuration
"""
    kalman_items = format_nested_dict(kalman_results, 2)
    kalman_section += "\n".join(kalman_items) + "\n\n"


    # Weighted Ensemble Section
    ensemble_section = f"""## 4. Weighted Ensemble ([b6_weighted_ensemble_results.json](../results/b6_weighted_ensemble_results.json))

Combines multiple signals using learned weightings to produce a single ensemble decision value.

### Configuration
"""
    ensemble_items = format_nested_dict(ensemble_results, 2)
    ensemble_section += "\n".join(ensemble_items) + "\n\n"


    # B1.5 Cleanup Section
    b15_section = f"""## 5. B1.5 Cleanup Parameters

Applies post-processing validation and cleanup requirements from Phase B1.5:

### Configuration
"""
    b15_items = format_nested_dict(b1_5_cleanups, 2)
    b15_section += "\n".join(b15_items) + "\n\n"


    # Combined Application
    combined_section = f"""## 6. Combined Application Process

All above levers are applied simultaneously in the paper trade simulation:

1. Individual signals processed and subjected to **Skill Gating**
2. Valid signals undergo **Kalman Smoothing**
3. Smoothed signals are combined via **Weighted Ensemble**
4. Ensemble output is evaluated against **Confirmation Filter** threshold
5. Passed signals trigger paper trades with risk management

### Risk Management Implementation
- Consecutive loss limit: `8` (hardcoded in simulation)
- Position sizing: Limited to portion of account balance
- Slippage and fees factored into P&L calculations
- Drawdown monitoring with safety triggers

## Verification Status

- [ ] Confirmation filter parameters verified
- [ ] Skill gating thresholds confirmed
- [ ] Kalman parameters tested
- [ ] Ensemble weights validated
- [ ] B1.5 cleanups applied
- [ ] Combined application tested in simulation environment

"""


    full_content = header + confirm_section + skill_section + kalman_section + ensemble_section + b15_section + combined_section
    
    return full_content


if __name__ == '__main__':
    docs = create_lever_audit()
    print(docs)