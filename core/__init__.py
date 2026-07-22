"""
Kalshi METAR Monitor — Weather Market Trading Engine

Core package containing signal fusion, paper/live trading, METAR ingestion,
Kalshi market monitoring, and alert infrastructure.

Sub-packages:
    core/signals/          — Signal registry and individual signal implementations
    core/signal_processors/ — Signal processing utilities
    core/trading_dashboard/ — Trading dashboard (Flask-based)

Key modules:
    core/signal_fusion.py         — Facade: re-exports from fusion_logic + compatibility_checks
    core/paper_trading_engine.py  — Facade: re-exports from signal_pipeline, trade_execution, etc.
    core/metar_monitor.py         — METAR data ingestion (canonical source)
    core/kalshi_monitor.py        — Kalshi market data (canonical source)
    core/sqlite_utils.py          — Centralized DB connection pool + schema registry
    core/db_schema.py             — Schema registry with version tracking
    core/trading_modes.py         — Paper/live trading mode separation
    core/paper_trader.py          — Paper trading simulation
    core/live_trader.py           — Live trading with risk controls
"""

__version__ = "2.0.0"
__all__ = []

# Import key modules for easy access
from . import (
    sqlite_utils,
    db_schema,
    signal_fusion,
    trading_modes,
    paper_trader,
    live_trader,
    signals,
)