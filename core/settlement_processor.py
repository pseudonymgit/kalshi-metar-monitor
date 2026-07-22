"""Settlement Processor Module

Settlement calculation, processing, and epoch management.
Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
__all__ = ['SettlementProcessor']


_LOGGER = logging.getLogger(__name__)

