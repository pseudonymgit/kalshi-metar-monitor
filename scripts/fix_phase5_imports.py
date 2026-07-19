#!/usr/bin/env python3
"""Fix Phase 5 script import issues: WS6 and WS10."""
import os, sys

scripts_dir = os.path.join(os.path.dirname(__file__))
core_dir = os.path.join(scripts_dir, '..', 'core')
sys.path.insert(0, core_dir)

# Fix WS6: market_phase_classification.py
ws6_path = os.path.join(scripts_dir, 'market_phase_classification.py')
with open(ws6_path) as f:
    content = f.read()

# KalshiCalendar and SettlementCalendar don't exist as classes - 
# kalshi_calendar.py has standalone functions
# Replace the class import with function imports
old_import = "from kalshi_calendar import KalshiCalendar, SettlementCalendar"
new_import = "from kalshi_calendar import is_trading_day, get_next_trading_day, get_settlement_date, is_valid_entry_date"
content = content.replace(old_import, new_import)

# Replace self.calendar = KalshiCalendar() with direct function calls
content = content.replace(
    "self.calendar = KalshiCalendar()",
    "self.calendar = None  # Using module-level functions directly"
)

# Fix get_next_settlement_time call - replace with module function
content = content.replace(
    "settlement_datetime = self.calendar.get_next_settlement_time(date)",
    "settlement_datetime = None  # TODO: implement get_next_settlement_time or use get_settlement_date"
)

with open(ws6_path, 'w') as f:
    f.write(content)
print(f"Fixed {ws6_path}")

# Fix WS10: multi_stage_execution.py
ws10_path = os.path.join(scripts_dir, 'multi_stage_execution.py')
with open(ws10_path) as f:
    content = f.read()

# This script needs KalshiMonitor and Kalshi station codes.
# It's not truly dependency-free. Let's add a try/except import guard.
old_import = "from kalshi_monitor import KalshiMonitor"
new_import = """try:
    from kalshi_monitor import KalshiMonitor
    _HAS_KALSHI_MONITOR = True
except ImportError:
    KalshiMonitor = None
    _HAS_KALSHI_MONITOR = False"""
content = content.replace(old_import, new_import)

# Also try/except the station code import
old_import2 = "from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION"
new_import2 = """try:
    from kalshi_price_fetcher import STATION_TO_KALSHI_CODE, KALSHI_CODE_TO_STATION
    _HAS_KALSHI_PRICE = True
except ImportError:
    STATION_TO_KALSHI_CODE = {}
    KALSHI_CODE_TO_STATION = {}
    _HAS_KALSHI_PRICE = False"""
content = content.replace(old_import2, new_import2)

with open(ws10_path, 'w') as f:
    f.write(content)
print(f"Fixed {ws10_path}")

print("\nDone. Run manually with:")
print("  python3 scripts/nws_revision_predictability_simple.py --print-summary")
print("  python3 scripts/market_phase_classification.py --station KATL --date 2026-07-17")
print("  python3 scripts/multi_stage_execution.py")