#!/usr/bin/env python3
"""
B-MODE MASTER RUNNER - Full Ensemble Backtest Suite + Paper Trade Validation (All 20 Stations, All Levers)

Generates the four core backtest deliverables plus two living docs:
1. Full 20-station ensemble backtest
2. Top 7 performing stations backtest
3. Per-signal split analysis for clean signals
4. Realistic paper-trade validation with all B6 levers
5. Signal registry documentation
6. Lever audit documentation

All results based on real METAR data aligned with Kalshi ground truth.
"""

import json
import os
from datetime import datetime
import sys


def main():
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    
    # Import individual backtest modules to ensure they're runnable
    import generate_all_station_backtest
    import generate_top7_backtest
    import generate_per_signal_analysis
    import generate_paper_trade_simulation
    import generate_signal_registry_docs
    import generate_lever_audit_docs
    
    # Execute each component in sequence with proper naming convention
    print(f"Starting B-MODE backtest suite execution at {timestamp}...")
    
    # 1. Full 20-station ensemble backtest
    print("1. Running 20-station ensemble backtest...")
    full_results = generate_all_station_backtest.run_backtest()
    full_output_path = f'docs/weather-engine/backtests/{timestamp}_full_20station_backtest.json'
    os.makedirs(os.path.dirname(full_output_path), exist_ok=True)
    with open(full_output_path, 'w') as f:
        json.dump(full_results, f, indent=2)
    print(f"   Output saved to {full_output_path}")
    
    # 2. Top 7 station subset analysis
    print("2. Running top 7 station backtest...")
    top7_results = generate_top7_backtest.run_backtest()
    top7_output_path = f'docs/weather-engine/backtests/{timestamp}_top7_station_backtest.json'
    with open(top7_output_path, 'w') as f:
        json.dump(top7_results, f, indent=2)
    print(f"   Output saved to {top7_output_path}")
    
    # 3. Per-signal analysis
    print("3. Running per-signal analysis...")
    per_signal_results = generate_per_signal_analysis.analyze_signals()
    per_signal_output_path = f'docs/weather-engine/backtests/{timestamp}_per_signal_split_backtest.json'
    with open(per_signal_output_path, 'w') as f:
        json.dump(per_signal_results, f, indent=2)
    print(f"   Output saved to {per_signal_output_path}")
    
    # 4. Paper-trade simulation
    print("4. Running realistic paper-trade simulation...")
    paper_trade_results = generate_paper_trade_simulation.simulate_paper_trades()
    paper_trade_output_path = f'docs/weather-engine/backtests/{timestamp}_paper_trade_backtest.json'
    with open(paper_trade_output_path, 'w') as f:
        json.dump(paper_trade_results, f, indent=2)
    print(f"   Output saved to {paper_trade_output_path}")
    
    # 5. Signal registry documentation
    print("5. Generating signal registry documentation...")
    signal_docs = generate_signal_registry_docs.create_registry_docs()
    signal_docs_path = 'docs/weather-engine/SIGNAL_REGISTRY.md'
    with open(signal_docs_path, 'w') as f:
        f.write(signal_docs)
    print(f"   Documentation saved to {signal_docs_path}")
    
    # 6. Lever audit documentation
    print("6. Generating lever audit documentation...")
    lever_docs = generate_lever_audit_docs.create_lever_audit()
    lever_docs_path = 'docs/weather-engine/LEVER_AUDIT.md'
    with open(lever_docs_path, 'w') as f:
        f.write(lever_docs)
    print(f"   Documentation saved to {lever_docs_path}")
    
    print(f"\nAll B-MODE deliverables completed at {timestamp}:")
    print(f"  - {full_output_path}")
    print(f"  - {top7_output_path}")
    print(f"  - {per_signal_output_path}")
    print(f"  - {paper_trade_output_path}")
    print(f"  - {signal_docs_path}")
    print(f"  - {lever_docs_path}")


if __name__ == '__main__':
    main()