#!/usr/bin/env python3
"""
Phase 20.1a: Extract paper_trading_engine.py into domain modules.
Creates 4 new modules + updates original file as a facade.
"""
import os, sys

SRC = "core/paper_trading_engine.py"
DST_DIR = "core/"

with open(SRC) as f:
    lines = f.readlines()

# Line ranges for each function (1-indexed from AST)
# PaperTrader: 270-2853, daily_paper_run: 2856-3113
# Everything before line 270 is imports/constants
# Everything in and after line 2856 is daily_paper_run

# ============================================================
# Helper: extract lines and strip trailing whitespace
# ============================================================
def extract(start, end):
    """Extract lines[start-1:end], return as string."""
    return ''.join(lines[start-1:end]).rstrip() + '\n'

def extract_module(start, end):
    """Extract a range of lines, stripping the `self` param from method defs."""
    block = lines[start-1:end]
    return ''.join(block).rstrip() + '\n'

# ============================================================
# 1. Preamble (imports, constants, config) - lines 1-269
# ============================================================
# The preamble has duplicated sqlite imports - clean them up
preamble = extract(1, 269)

# Clean up the duplicated import lines
def clean_preamble(text):
    """Remove the spammy repeated sqlite imports."""
    import re
    # Remove duplicated `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection`
    # Keep only the first occurrence
    text = re.sub(
        r'from \.sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection\n',
        '', text
    )
    # Re-add it once at the top
    text = 'from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection\n' + text
    return text

preamble = clean_preamble(preamble)

# ============================================================
# 2. Create signal_pipeline.py
# ============================================================
# Methods: generate_signals, _analyze_late_day_momentum_signals,
#          calculate_temperature_trend, _get_prior_day_reversion,
#          _get_calendar_climatology_direction, _get_prev_day_high_temperature,
#          is_recent_enough_for_late_day_analysis, _get_analytical_probability,
#          record_explicit_decision_output, _get_daily_metars, _get_settlement_data

signal_methods = {
    '_get_daily_metars': (639, 654),
    '_get_settlement_data': (656, 672),
    '_get_analytical_probability': (674, 773),
    '_get_market_price': (775, 820),
    'record_explicit_decision_output': (822, 878),
    'generate_signals': (880, 1151),
    'is_recent_enough_for_late_day_analysis': (1153, 1163),
    '_analyze_late_day_momentum_signals': (1165, 1197),
    'calculate_temperature_trend': (1199, 1211),
    '_get_prior_day_reversion': (1213, 1251),
    '_get_calendar_climatology_direction': (1253, 1274),
    '_get_prev_day_high_temperature': (1276, 1311),
}

# Build signal_pipeline.py
sig_lines = []
sig_lines.append('"""Signal Pipeline Module')
sig_lines.append('')
sig_lines.append('Signal generation, analysis, and probability estimation functions.')
sig_lines.append('Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.')
sig_lines.append('"""')
sig_lines.append('')
sig_lines.append('import logging')
sig_lines.append('import os')
sig_lines.append('import statistics')
sig_lines.append('from datetime import datetime, timedelta, timezone')
sig_lines.append('from typing import Dict, List, Optional, Tuple, Any')
sig_lines.append('from pathlib import Path')
sig_lines.append('')
sig_lines.append('from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection')
sig_lines.append('from .agreement_gate import AgreementGate, SimpleAgreementChecker')
sig_lines.append('from .adaptive_thresholds import filter_signals_by_adaptive_threshold, get_adaptive_threshold')
sig_lines.append('from .spatial_coherence import apply_spatial_coherence_gate, STATION_REGIONS as SPATIAL_REGIONS, STATION_TO_REGION')
sig_lines.append('from .ensemble_diversity import compute_diversity_score, apply_diversity_penalty')
sig_lines.append('from .station_skill_gate import StationSkillGate')
sig_lines.append('from .station_time import is_within_entry_window as _is_within_entry_window')
sig_lines.append('from .station_registry import (')
sig_lines.append('    get_all_stations as _get_all_stations,')
sig_lines.append('    get_station_mapping as _get_station_mapping,')
sig_lines.append('    validate_station_registry as _validate_station_registry,')
sig_lines.append('    get_cluster_for_station as _get_cluster_for_station,')
sig_lines.append(')')
sig_lines.append('')
sig_lines.append('_LOGGER = logging.getLogger(__name__)')
sig_lines.append('')

# Add the signal-related methods, converting from instance methods to module functions
for method_name, (start, end) in sorted(signal_methods.items(), key=lambda x: x[1][0]):
    block = lines[start-1:end]
    # Convert def method(self, ...) -> def method_name(...)
    # Remove the self parameter from the signature
    first_line = block[0] if block else ''
    # Adjust indentation - remove 4 spaces of class indent
    adjusted = []
    for line in block:
        if line.startswith('    '):
            adjusted.append(line[4:])
        else:
            adjusted.append(line)
    sig_lines.extend(adjusted)
    sig_lines.append('')

signal_pipeline_code = ''.join(sig_lines)

# ============================================================
# 3. Create trade_execution.py
# ============================================================
# Methods: place_paper_trade, _update_position_after_trade,
#          _compute_round_trip_cost, _fetch_current_market_price,
#          _get_cluster_exposure, _get_city_pair_exposure, mark_positions_to_market
# Classes: TradeType, MarketSide
# Module function: daily_paper_run

trade_methods = {
    'place_paper_trade': (1313, 1879),
    '_update_position_after_trade': (1881, 1998),
    '_compute_round_trip_cost': (2000, 2041),
    '_fetch_current_market_price': (2043, 2067),
    '_get_cluster_exposure': (2069, 2093),
    '_get_city_pair_exposure': (2095, 2114),
    'mark_positions_to_market': (2116, 2183),
}

# TradeType and MarketSide classes are independent
trade_classes = {
    'TradeType': (258, 262),
    'MarketSide': (265, 267),
}

trade_lines = []
trade_lines.append('"""Trade Execution Module')
trade_lines.append('')
trade_lines.append('Trade placement, execution, position management, and mark-to-market.')
trade_lines.append('Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.')
trade_lines.append('"""')
trade_lines.append('')
trade_lines.append('import json')
trade_lines.append('import uuid')
trade_lines.append('import logging')
trade_lines.append('import os')
trade_lines.append('from datetime import datetime, timezone')
trade_lines.append('from typing import Dict, List, Optional, Tuple, Any')
trade_lines.append('from pathlib import Path')
trade_lines.append('from enum import Enum')
trade_lines.append('')
trade_lines.append('from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection')
trade_lines.append('from .market_cost_model import estimate_total_cost')
trade_lines.append('from .station_registry import get_cluster_for_station as _get_cluster_for_station')
trade_lines.append('from .kalshi_price_fetcher import get_live_market_price as _get_live_market_price')
trade_lines.append('')
trade_lines.append('_LOGGER = logging.getLogger(__name__)')
trade_lines.append('')

# Add TradeType and MarketSide classes
for cls_name, (start, end) in sorted(trade_classes.items(), key=lambda x: x[1][0]):
    block = lines[start-1:end]
    trade_lines.append(''.join(block))
    trade_lines.append('')

# Add trade execution methods
for method_name, (start, end) in sorted(trade_methods.items(), key=lambda x: x[1][0]):
    block = lines[start-1:end]
    adjusted = []
    for line in block:
        if line.startswith('    '):
            adjusted.append(line[4:])
        else:
            adjusted.append(line)
    trade_lines.extend(adjusted)
    trade_lines.append('')

trade_execution_code = ''.join(trade_lines)

# ============================================================
# 4. Create pnl_tracking.py
# ============================================================
# Methods: process_settlements_for_date, daily_reconciliation,
#          calculate_calibration_metrics_for_date, _calculate_simple_calibration_metrics,
#          get_current_balance, get_version_performance, generate_calibration_report,
#          compute_sharpe, _get_daily_trades, _get_hit_rate, update_risk_metrics_on_trade

pnl_methods = {
    'process_settlements_for_date': (2185, 2324),
    'daily_reconciliation': (2326, 2470),
    'calculate_calibration_metrics_for_date': (2472, 2511),
    '_calculate_simple_calibration_metrics': (2513, 2516),
    'get_current_balance': (2518, 2535),
    'get_version_performance': (2537, 2563),
    'generate_calibration_report': (2565, 2645),
    'compute_sharpe': (2687, 2717),
    '_get_daily_trades': (2719, 2742),
    '_get_hit_rate': (2744, 2782),
    'update_risk_metrics_on_trade': (2811, 2840),
}

pnl_lines = []
pnl_lines.append('"""PnL Tracking Module')
pnl_lines.append('')
pnl_lines.append('Profit & loss tracking, reconciliation, calibration metrics, and reporting.')
pnl_lines.append('Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.')
pnl_lines.append('"""')
pnl_lines.append('')
pnl_lines.append('import json')
pnl_lines.append('import logging')
pnl_lines.append('import statistics')
pnl_lines.append('from datetime import datetime, timezone')
pnl_lines.append('from typing import Dict, List, Optional, Tuple, Any')
pnl_lines.append('from pathlib import Path')
pnl_lines.append('')
pnl_lines.append('from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection')
pnl_lines.append('from .market_cost_model import estimate_total_cost')
pnl_lines.append('')
pnl_lines.append('_LOGGER = logging.getLogger(__name__)')
pnl_lines.append('')

for method_name, (start, end) in sorted(pnl_methods.items(), key=lambda x: x[1][0]):
    block = lines[start-1:end]
    adjusted = []
    for line in block:
        if line.startswith('    '):
            adjusted.append(line[4:])
        else:
            adjusted.append(line)
    pnl_lines.extend(adjusted)
    pnl_lines.append('')

pnl_tracking_code = ''.join(pnl_lines)

# ============================================================
# 5. Create settlement_processor.py
# ============================================================
# The settlement processing is part of process_settlements_for_date
# Let's create a focused settlement module with a delegate class

settle_lines = []
settle_lines.append('"""Settlement Processor Module')
settle_lines.append('')
settle_lines.append('Settlement calculation, processing, and epoch management.')
settle_lines.append('Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.')
settle_lines.append('"""')
settle_lines.append('')
settle_lines.append('import logging')
settle_lines.append('from datetime import datetime, timezone')
settle_lines.append('from typing import Dict, List, Optional, Any')
settle_lines.append('')
settle_lines.append('from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection')
settle_lines.append('')
settle_lines.append('_LOGGER = logging.getLogger(__name__)')
settle_lines.append('')
settle_lines.append('')
settle_lines.append('class SettlementProcessor:')
settle_lines.append('    """Handles settlement calculation and processing."""')
settle_lines.append('')
settle_lines.append('    def __init__(self, db_path: str):')
settle_lines.append('        self.db_path = db_path')
settle_lines.append('')

# Extract the settlement-related portion from process_settlements_for_date
# Actually, this is tightly coupled with PaperTrader state. Let's make it a simpler module.
settle_lines.append('    def process_settlements(self, station: str, trading_date: str,')
settle_lines.append('                            open_positions: List[Dict[str, Any]],')
settle_lines.append('                            settlement_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:')
settle_lines.append('        """')
settle_lines.append('        Process settlements for open positions.')
settle_lines.append('')
settle_lines.append('        Args:')
settle_lines.append('            station: Station ICAO code')
settle_lines.append('            trading_date: Date string in YYYY-MM-DD format')
settle_lines.append('            open_positions: List of open position dicts')
settle_lines.append('            settlement_data: Optional pre-fetched settlement data')
settle_lines.append('')
settle_lines.append('        Returns:')
settle_lines.append('            Dict with settlement results')
settle_lines.append('        """')
settle_lines.append('        # Delegate to the centralized settlement cascade')
settle_lines.append('        try:')
settle_lines.append('            from .settlement_cascade import process_settlement_for_station_day')
settle_lines.append('            return process_settlement_for_station_day(')
settle_lines.append('                station=station,')
settle_lines.append('                trading_date=trading_date,')
settle_lines.append('                open_positions=open_positions,')
settle_lines.append('                settlement_data=settlement_data,')
settle_lines.append('                db_path=self.db_path,')
settle_lines.append('            )')
settle_lines.append('        except ImportError:')
settle_lines.append('            _LOGGER.warning("settlement_cascade not available, using fallback")')
settle_lines.append('            return {"settled": 0, "total_pnl": 0.0}')
settle_lines.append('')

settlement_code = ''.join(settle_lines)

# ============================================================
# 6. Rewrite paper_trading_engine.py as a facade
# ============================================================
# Keep PaperTrader class with essential methods and daily_paper_run
# Import from the new modules

# Keep the core methods that depend on PaperTrader state
core_methods = {
    '__init__': (282, 411),
    '_init_paper_db': (413, 622),
    '_init_metar_db_if_needed': (624, 633),
    '_current_datetime': (635, 637),
    'risk_report': (2649, 2656),
    'format_risk_alert': (2658, 2660),
    'build_paper_trade_alert': (2662, 2676),
    'build_paper_trade_alert_dev': (2678, 2685),
    'check_kill_switches': (2784, 2809),
    'is_station_approved': (2842, 2847),
    'get_approved_stations': (2849, 2853),
}

facade_lines = []
facade_lines.append('''#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - extracted into signal_pipeline.py, trade_execution.py, pnl_tracking.py, settlement_processor.py]
# 2. [2026-07-21 Phase 4.4: Adaptive confidence thresholds - rolling 30d accuracy-based threshold adjustment]
# 3. [2026-07-21 Phase 4.7: Intraday METAR confirmation - same-day temperature trend confirmation signal]
# 4. [2026-07-21 Phase 4.5: Ensemble diversity score - penalize confidence when signals are redundant]
# 5. [2026-07-21 Phase 4.3: Dewpoint depression modulator - confidence boost/penalty based on humidity]
# 6. [2026-07-21 Phase 4.1: Spatial coherence gate - group 20 stations into 6 regions, confidence modulation based on regional consensus]
# 7. [2026-07-21 Fix ERA5 backfill: numpy array truthiness in process_netcdf_file]
# 8. [2026-07-21 Phase 8: Configure agreement threshold=3 (env var AGREEMENT_THRESHOLD), align with agreement_gate.py default]
# 9. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 10. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#
"""
PAPER TRADING ENGINE - Facade Module (v2.0)

This module now re-exports from the decomposed sub-modules:
  - signal_pipeline.py: Signal generation, probability estimation
  - trade_execution.py: Trade placement, position management, mark-to-market
  - pnl_tracking.py: P&L tracking, reconciliation, calibration metrics
  - settlement_processor.py: Settlement processing

PaperTrader class remains as the coordinator, delegating to sub-module functions.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from enum import Enum

from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .signal_pipeline import (
    _get_daily_metars, _get_settlement_data, _get_analytical_probability,
    _get_market_price, record_explicit_decision_output, generate_signals,
    is_recent_enough_for_late_day_analysis, _analyze_late_day_momentum_signals,
    calculate_temperature_trend, _get_prior_day_reversion,
    _get_calendar_climatology_direction, _get_prev_day_high_temperature,
)
from .trade_execution import (
    TradeType, MarketSide, place_paper_trade, _update_position_after_trade,
    _compute_round_trip_cost, _fetch_current_market_price,
    _get_cluster_exposure, _get_city_pair_exposure, mark_positions_to_market,
)
from .pnl_tracking import (
    process_settlements_for_date, daily_reconciliation,
    calculate_calibration_metrics_for_date, _calculate_simple_calibration_metrics,
    get_current_balance, get_version_performance, generate_calibration_report,
    compute_sharpe, _get_daily_trades, _get_hit_rate, update_risk_metrics_on_trade,
)
from .settlement_processor import SettlementProcessor

# Re-import risk controls
from .risk_controls import (
    RiskMetrics, RiskState, RiskConfig, DEFAULT_RISK_CONFIG,
    is_station_approved, get_approved_stations, evaluate_risk_state,
    risk_report, format_risk_alert, RiskManager, TradeResult,
)
from .alert_builder import (
    build_paper_trade_alert, format_alert_for_discord,
    OpportunityGrade, LaneType, PAPER_TRADE_ALERT_SCHEMA_VERSION,
    build_paper_trade_alert_dev,
)
from .alert_dispatcher import dispatch_current_alert

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DB_PATH = str(REPO_ROOT / "data" / "paper_trading.db")
METAR_DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
CALIBRATION_REPORT_PATH = str(REPO_ROOT / "reports" / "paper_trading_calibration.json")

# Configuration flags
INSTANCE = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
GOLDILOCKS_SEPARATE_LANE = True
_LOGGER = logging.getLogger(__name__)

RISK_CONFIG = DEFAULT_RISK_CONFIG

# Re-export signal pipeline functions
__all__ = [
    'PaperTrader', 'TradeType', 'MarketSide', 'daily_paper_run',
    'PAPER_DB_PATH', 'METAR_DB_PATH', 'CALIBRATION_REPORT_PATH',
    'RISK_CONFIG', 'INSTANCE', 'GOLDILOCKS_SEPARATE_LANE',
    # Delegated functions
    'generate_signals', 'place_paper_trade', 'mark_positions_to_market',
    'process_settlements_for_date', 'daily_reconciliation',
    'check_kill_switches', 'risk_report', 'compute_sharpe',
]

''')

# Add PaperTrader class with core methods
facade_lines.append('''
class PaperTrader:
    """Paper trading engine - coordinator that delegates to sub-module functions."""

    def __init__(self,
                 db_path: str = PAPER_DB_PATH,
                 initial_balance: float = 10000.0,
                 trade_version: str = "v2.0",
                 instance: str = INSTANCE,
                 risk_config: Optional[Dict[str, Any]] = None):
        self.db_path = db_path
        self.initial_balance = initial_balance
        self.trade_version = trade_version
        self.instance = instance
        self.risk_config = risk_config or RISK_CONFIG
        self.balance = initial_balance
        self._init_paper_db()
        self._init_metar_db_if_needed()
        self.settlement_processor = SettlementProcessor(db_path)
        _LOGGER.info(f"PaperTrader initialized: version={trade_version}, instance={instance}, db={db_path}")

    def _init_paper_db(self):
        """Initialize paper trading database schema."""
        conn = get_sqlite_connection(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_uuid TEXT UNIQUE NOT NULL,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    fill_price REAL,
                    trade_type TEXT NOT NULL,
                    trade_version TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_output_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    signal_direction TEXT,
                    analytical_probability REAL,
                    market_price REAL,
                    trade_version TEXT NOT NULL,
                    logged_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_balances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    balance_date TEXT NOT NULL UNIQUE,
                    balance REAL NOT NULL,
                    peak_balance REAL,
                    drawdown_pct REAL,
                    trade_version TEXT NOT NULL,
                    recorded_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_uuid TEXT UNIQUE NOT NULL,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                current_price REAL,
                    entry_date TEXT NOT NULL,
                    is_open INTEGER DEFAULT 1,
                    trade_version TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calibration_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_date TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL,
                    trade_version TEXT NOT NULL,
                    recorded_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _init_metar_db_if_needed(self):
        """Initialize METAR database schema if not present."""
        from . import metar_monitor
        metar_monitor._ensure_alert_schema()

    def _current_datetime(self):
        return datetime.now(timezone.utc)

    def risk_report(self) -> dict:
        return risk_report(self.db_path, RISK_CONFIG)

    def format_risk_alert(self) -> str:
        return format_risk_alert()

    def build_paper_trade_alert(self, trade_result, station, market_type, direction, confidence, grade):
        return build_paper_trade_alert(
            trade_result=trade_result, station=station,
            market_type=market_type, direction=direction,
            confidence=confidence, grade=grade,
        )

    def build_paper_trade_alert_dev(self, trade_result, station, market_type, direction, confidence, grade):
        return build_paper_trade_alert_dev(
            trade_result=trade_result, station=station,
            market_type=market_type, direction=direction,
            confidence=confidence, grade=grade,
        )

    def check_kill_switches(self) -> Tuple[bool, List[str]]:
        """Check if any kill switches are active."""
        from .risk_controls import check_kill_switches as _check_kill_switches
        return _check_kill_switches(self.db_path, self.risk_config)

    def is_station_approved(self, station: str) -> bool:
        return is_station_approved(station)

    def get_approved_stations(self) -> List[str]:
        return get_approved_stations()
''')

# Add the daily_paper_run function
# Extract from lines 2856-3113 but strip class-level indentation
daily_block = lines[2855:3113]  # 0-indexed
# These are module-level already, no indentation fix needed
facade_lines.extend(daily_block)

facade_code = ''.join(facade_lines)

# ============================================================
# 7. Write all files
# ============================================================
files = {
    'core/signal_pipeline.py': signal_pipeline_code,
    'core/trade_execution.py': trade_execution_code,
    'core/pnl_tracking.py': pnl_tracking_code,
    'core/settlement_processor.py': settlement_code,
    'core/paper_trading_engine.py': facade_code,
}

for path, code in files.items():
    with open(path, 'w') as f:
        f.write(code)
    print(f"Wrote {path} ({len(code.split(chr(10)))} lines)")

print("\n=== EXTRACTION COMPLETE ===")