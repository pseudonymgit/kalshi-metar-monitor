#!/usr/bin/env python3
"""
Setup required directories for B-MODE backtest outputs.
"""
import os

directories = [
    'docs/weather-engine/backtests',
    'docs/weather-engine',
    'results'  # For lever config files
]

for directory in directories:
    os.makedirs(directory, exist_ok=True)
    print(f"Created directory: {directory}")