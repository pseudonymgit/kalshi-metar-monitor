"""DB Schema Registry

Centralized schema definitions with version tracking.
Replaces 15+ independent CREATE TABLE IF NOT EXISTS statements
scattered across the codebase.

Schema version: 1.0.0
"""
from typing import Dict, List, Optional

# Schema version tracking
SCHEMA_VERSION = "1.0.0"

# All table schemas keyed by table name
TABLES: Dict[str, str] = {}

# ---------------------------------------------------------------------------
# Trading tables
# ---------------------------------------------------------------------------
TABLES["trades"] = """
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
    strike_price INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["decision_output_log"] = """
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
"""

TABLES["daily_balances"] = """
CREATE TABLE IF NOT EXISTS daily_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance_date TEXT NOT NULL UNIQUE,
    balance REAL NOT NULL,
    peak_balance REAL,
    drawdown_pct REAL,
    trade_version TEXT NOT NULL,
    recorded_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["positions"] = """
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
"""

TABLES["calibration_metrics"] = """
CREATE TABLE IF NOT EXISTS calibration_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    trade_version TEXT NOT NULL,
    recorded_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["trade_journal"] = """
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    station TEXT NOT NULL,
    market TEXT NOT NULL,
    direction TEXT NOT NULL,
    signal_ids TEXT,
    confidence REAL,
    edge REAL,
    market_prob REAL,
    lane TEXT,
    grade TEXT,
    entry_price REAL,
    quantity INTEGER,
    pnl REAL,
    trade_version TEXT,
    instance TEXT,
    metadata TEXT
)
"""

# ---------------------------------------------------------------------------
# Settlement tables
# ---------------------------------------------------------------------------
TABLES["settlement_epochs"] = """
CREATE TABLE IF NOT EXISTS settlement_epochs (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    market_type TEXT,
    local_trading_date TEXT NOT NULL,
    settlement_bucket INTEGER NOT NULL,
    prior_settlement_bucket INTEGER,
    settlement_timestamp_utc TEXT NOT NULL,
    settlement_jump_magnitude INTEGER,
    epoch_status TEXT NOT NULL,
    epoch_close_reason TEXT,
    epoch_close_timestamp_utc TEXT,
    reversion_occurred INTEGER NOT NULL DEFAULT 0,
    first_reversion_timestamp_utc TEXT,
    max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
    duration_at_or_above_settlement_seconds REAL NOT NULL DEFAULT 0,
    duration_strictly_above_settlement_seconds REAL NOT NULL DEFAULT 0,
    terminal_state_reached INTEGER NOT NULL DEFAULT 0,
    settlement_transition_event_id INTEGER,
    last_transition_event_id INTEGER,
    last_transition_timestamp_utc TEXT
)
"""

# ---------------------------------------------------------------------------
# Alert & delivery tables
# ---------------------------------------------------------------------------
TABLES["alerts"] = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT UNIQUE NOT NULL,
    station TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    schema_version TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    sent_at TEXT,
    delivery_attempts INTEGER DEFAULT 0,
    last_error TEXT
)
"""

TABLES["alert_delivery_queue"] = """
CREATE TABLE IF NOT EXISTS alert_delivery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    next_retry_at TEXT
)
"""

TABLES["alert_states"] = """
CREATE TABLE IF NOT EXISTS alert_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT UNIQUE NOT NULL,
    station TEXT NOT NULL,
    state TEXT NOT NULL,
    transition_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["alert_state_history"] = """
CREATE TABLE IF NOT EXISTS alert_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    transitioned_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["alert_throttle"] = """
CREATE TABLE IF NOT EXISTS alert_throttle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    suppress_until TEXT,
    count_24h INTEGER DEFAULT 1,
    UNIQUE(station, alert_type)
)
"""

TABLES["reconciliations"] = """
CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconcilable_type TEXT NOT NULL,
    reconcilable_id TEXT NOT NULL,
    expected_state TEXT NOT NULL,
    actual_state TEXT NOT NULL,
    status TEXT NOT NULL,
    discrepancy TEXT,
    reconciled_at TEXT DEFAULT (datetime('now')),
    UNIQUE(reconcilable_type, reconcilable_id)
)
"""

TABLES["near_miss_audit"] = """
CREATE TABLE IF NOT EXISTS near_miss_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL,
    near_miss_type TEXT NOT NULL,
    observation_time_utc TEXT NOT NULL,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["digest_queue"] = """
CREATE TABLE IF NOT EXISTS digest_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    digest_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    scheduled_for TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    sent_at TEXT
)
"""

# ---------------------------------------------------------------------------
# METAR / signal tables
# ---------------------------------------------------------------------------
TABLES["signal_layer_state"] = """
CREATE TABLE IF NOT EXISTS signal_layer_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name TEXT UNIQUE NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["market_cache"] = """
CREATE TABLE IF NOT EXISTS market_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT UNIQUE NOT NULL,
    station TEXT NOT NULL,
    cache_json TEXT NOT NULL,
    cached_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["transition_events"] = """
CREATE TABLE IF NOT EXISTS transition_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_value TEXT,
    to_value TEXT NOT NULL,
    event_timestamp_utc TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

# ---------------------------------------------------------------------------
# AI/ML forecast tables (B-Mode 3: AI/ML gate open)
# ---------------------------------------------------------------------------
TABLES["ai_forecasts"] = """
CREATE TABLE IF NOT EXISTS ai_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    model TEXT NOT NULL CHECK(model IN ('aigfs', 'graphcast', 'aifs')),
    forecast_date TEXT NOT NULL,
    forecast_hour INTEGER NOT NULL CHECK(forecast_hour IN (0, 6, 12, 18)),
    temp_f REAL,
    confidence REAL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(station, model, forecast_date, forecast_hour)
)
"""

# ---------------------------------------------------------------------------
# Kalshi tables
# ---------------------------------------------------------------------------
TABLES["kalshi_rate_limit"] = """
CREATE TABLE IF NOT EXISTS kalshi_rate_limit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    called_at TEXT NOT NULL
)
"""

TABLES["market_change_events"] = """
CREATE TABLE IF NOT EXISTS market_change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    detected_at TEXT DEFAULT (datetime('now'))
)
"""

# ---------------------------------------------------------------------------
# Divergence tables
# ---------------------------------------------------------------------------
TABLES["divergence_history"] = """
CREATE TABLE IF NOT EXISTS divergence_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_a TEXT NOT NULL,
    platform_b TEXT NOT NULL,
    market_key TEXT NOT NULL,
    price_a REAL,
    price_b REAL,
    spread REAL,
    detected_at TEXT DEFAULT (datetime('now'))
)
"""

TABLES["divergence_opportunities"] = """
CREATE TABLE IF NOT EXISTS divergence_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_key TEXT NOT NULL,
    platform_a TEXT NOT NULL,
    platform_b TEXT NOT NULL,
    entry_spread REAL,
    exit_spread REAL,
    pnl REAL,
    status TEXT DEFAULT 'open',
    entered_at TEXT DEFAULT (datetime('now')),
    exited_at TEXT
)
"""

# ---------------------------------------------------------------------------
# Order / multi-stage execution tables
# ---------------------------------------------------------------------------
TABLES["orders"] = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    station TEXT NOT NULL,
    market_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL,
    order_type TEXT DEFAULT 'limit',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT,
    filled_price REAL,
    filled_quantity INTEGER
)
"""

TABLES["stage_attempts"] = """
CREATE TABLE IF NOT EXISTS stage_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    stage INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    error TEXT
)
"""


def ensure_all_tables(conn) -> int:
    """Create all registered tables on the given connection.
    
    Returns:
        Number of tables created.
    """
    count = 0
    for table_name, ddl in TABLES.items():
        conn.execute(ddl)
        count += 1

    # Create indexes for registered tables
    _ensure_indexes(conn)

    conn.commit()
    return count


# Index definitions for registered tables
_INDEXES = {
    "idx_ai_forecasts_lookup":
        "CREATE INDEX IF NOT EXISTS idx_ai_forecasts_lookup "
        "ON ai_forecasts(forecast_date, station, model)",
}


def _ensure_indexes(conn):
    """Create all registered indexes on the given connection."""
    for idx_name, ddl in _INDEXES.items():
        try:
            conn.execute(ddl)
        except Exception:
            pass  # Table may not exist yet


def get_table_names() -> List[str]:
    """Get list of all registered table names."""
    return list(TABLES.keys())


__all__ = [
    "SCHEMA_VERSION", "TABLES", "ensure_all_tables", "get_table_names",
]