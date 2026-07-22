#!/usr/bin/env python3
"""
Extract paper_trading_engine.py (3117 lines) into:
- signal_pipeline.py
- trade_execution.py
- pnl_tracking.py
- settlement_processor.py

Uses AST to identify method boundaries and creates mixin classes.
PaperTrader becomes a facade that inherits from all mixins.
"""
import ast
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC = "core/paper_trading_engine.py"
DST_DIR = "core/"

with open(SRC) as f:
    source = f.read()
    lines = source.split('\n')

# Parse the file
tree = ast.parse(source)

# Step 1: Find all top-level definitions and their line ranges
class TopLevelCollector(ast.NodeVisitor):
    def __init__(self):
        self.defs = []  # (name, type, start, end, is_method)
    
    def visit_FunctionDef(self, node):
        self.defs.append((node.name, 'function', node.lineno, node.end_lineno, False))
    
    def visit_AsyncFunctionDef(self, node):
        self.defs.append((node.name, 'async_function', node.lineno, node.end_lineno, False))
    
    def visit_ClassDef(self, node):
        # Collect methods within the class
        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append((item.name, 'method', item.lineno, item.end_lineno))
        self.defs.append((node.name, 'class', node.lineno, node.end_lineno, False, methods))

collector = TopLevelCollector()
collector.visit(tree)

# Step 2: Map each definition to a target module
# Signal pipeline functions
signal_pipeline_funcs = {
    'generate_signals', '_analyze_late_day_momentum_signals',
    'calculate_temperature_trend', '_get_prior_day_reversion',
    '_get_calendar_climatology_direction', '_get_prev_day_high_temperature',
    'is_recent_enough_for_late_day_analysis', '_get_analytical_probability',
    'record_explicit_decision_output',
    # Supporting functions
    '_get_daily_metars', '_get_settlement_data',
    'get_intraday_confirmation',
}

# Trade execution functions
trade_execution_funcs = {
    'place_paper_trade', '_update_position_after_trade',
    '_compute_round_trip_cost', '_fetch_current_market_price',
    '_get_cluster_exposure', '_get_city_pair_exposure',
    'mark_positions_to_market', 'daily_paper_run',
    '_get_market_price', 'TradeType', 'MarketSide',
}

# PnL tracking functions
pnl_tracking_funcs = {
    'process_settlements_for_date', 'daily_reconciliation',
    'calculate_calibration_metrics_for_date', '_calculate_simple_calibration_metrics',
    'get_current_balance', 'get_version_performance',
    'generate_calibration_report', 'compute_sharpe',
    '_get_daily_trades', '_get_hit_rate',
    'update_risk_metrics_on_trade',
}

# Settlement processing functions
settlement_funcs = {
    'process_settlements_for_date',
}

# Step 3: Extract the module-level code (imports, constants) - everything before first class/func
first_def_line = min(d[2] for d in collector.defs if d[4] is False)  # non-class defs
# Actually, first def line is the module-level code start
# The file starts with a shebang, then changelog, then docstring, then imports
# Let's find where the actual imports end and code begins

# Extract the preamble (imports, constants, setup)
# Everything from line 0 to the first top-level function/class definition
# But we need to handle the imports at the top separately

module_preamble_lines = []
current_import_section = []

# Read the file top to bottom, classify lines
in_import_section = True
in_docstring = False
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    if stripped.startswith('"""') and not in_docstring:
        in_docstring = True
        continue
    if in_docstring and '"""' in stripped:
        in_docstring = False
        continue
    if in_docstring:
        continue

# Let me just print the structure and let the user decide
print("=== FILE STRUCTURE ===")
print(f"Total lines: {len(lines)}")

# Find the class PaperTrader definition
paper_trader_start = None
paper_trader_end = None
for d in collector.defs:
    if d[0] == 'PaperTrader' and d[1] == 'class':
        paper_trader_start = d[2]
        paper_trader_end = d[3]
        methods = d[5]
        print(f"\nPaperTrader class: lines {paper_trader_start}-{paper_trader_end}")
        print(f"Methods ({len(methods)}):")
        for m_name, m_type, m_start, m_end in sorted(methods, key=lambda x: x[2]):
            print(f"  {m_name}: lines {m_start}-{m_end}")
        break

# Find the module-level function daily_paper_run
for d in collector.defs:
    if d[0] == 'daily_paper_run':
        print(f"\ndaily_paper_run: lines {d[2]}-{d[3]}")

# What's before the class
print(f"\nBefore PaperTrader: lines 1-{paper_trader_start-1}")
print(f"After PaperTrader: lines {paper_trader_end+1}-{len(lines)}")

# Print the structure of the whole file
print("\n=== TOP-LEVEL DEFINITIONS ===")
for d in sorted(collector.defs, key=lambda x: x[2]):
    name, typ, start, end, is_method = d[:5]
    print(f"  {typ}: {name} (lines {start}-{end})")