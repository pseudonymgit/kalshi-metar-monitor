-- Setup weather_data.db with schema derived from existing databases in the codebase

-- Create weather_data.db with comprehensive schema encompassing all existing tables

-- Copy of nwp_forecasts structure (likely signal components)
CREATE TABLE IF NOT EXISTS nwp_forecasts (
    id INTEGER PRIMARY KEY,
    fetch_date TEXT,
    target_date TEXT,
    station TEXT,
    model TEXT,
    variable TEXT,
    value REAL,
    fetch_timestamp TEXT
);

-- Settlement epochs (from metar_backfill.db)
CREATE TABLE IF NOT EXISTS settlement_epochs (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    market_type TEXT,
    local_trading_date TEXT,
    date_utc TEXT,
    settlement_bucket INTEGER,
    prior_settlement_bucket INTEGER,
    settlement_timestamp_utc TEXT,
    settlement_jump_magnitude REAL,
    epoch_status TEXT,
    epoch_close_reason TEXT,
    epoch_close_timestamp_utc TEXT,
    reversion_occurred INTEGER DEFAULT 0,
    first_reversion_timestamp_utc TEXT,
    max_excursion_above_settlement REAL DEFAULT 0.0,
    duration_at_or_above_settlement_seconds INTEGER DEFAULT 86400,
    duration_strictly_above_settlement_seconds INTEGER DEFAULT 0,
    terminal_state_reached INTEGER DEFAULT 1,
    settlement_transition_event_id INTEGER,
    last_transition_event_id INTEGER,
    last_transition_timestamp_utc TEXT,
    last_transition_temp_f REAL,
    epoch_sequence INTEGER,
    created_at_utc TEXT
);

-- Daily forecast snapshots (might contain signal information)
CREATE TABLE IF NOT EXISTS daily_forecast_snapshots (
    id INTEGER PRIMARY KEY,
    collection_date TEXT,
    collection_timestamp_utc TEXT,
    station TEXT,
    nws_office TEXT,
    nws_grid_x INTEGER,
    nws_grid_y INTEGER,
    nws_forecast_high_f REAL,
    nws_forecast_low_f REAL,
    nws_raw_json TEXT,
    gfs_forecast_high_f REAL,
    gfs_forecast_low_f REAL,
    gfs_raw_json TEXT,
    disagreement_f REAL,
    signal_direction TEXT,
    signal_confidence REAL,
    ensemble_prediction TEXT,
    ensemble_confidence REAL,
    actual_high_f REAL,
    actual_direction TEXT,
    actual_settlement_bucket INTEGER,
    actual_prior_bucket INTEGER,
    status TEXT DEFAULT 'forecast_collected',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- METAR observations for signal components
CREATE TABLE IF NOT EXISTS metar_observations (
    id INTEGER PRIMARY KEY,
    station TEXT,
    date_utc TEXT,
    timestamp_utc TEXT,
    temp_f REAL,
    temp_c REAL,
    dewpoint_f REAL,
    dewpoint_c REAL,
    wind_direction_deg INTEGER,
    wind_speed_kt REAL,
    wind_gust_kt REAL,
    pressure_mb REAL,
    sea_level_pressure_mb REAL,
    visibility_mi REAL,
    ceiling_ft INTEGER,
    raw_metar TEXT,
    source TEXT,
    ingestion_timestamp_utc TEXT
);

-- Daily stats (potential source for signals)
CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY,
    station TEXT,
    date_local TEXT,
    date_utc TEXT,
    max_temp_f REAL,
    min_temp_f REAL,
    avg_temp_f REAL,
    observation_count INTEGER,
    first_observation_utc TEXT,
    last_observation_utc TEXT,
    ingestion_timestamp_utc TEXT
);

-- Add signals table if needed (custom addition for experiments)
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    signal_value REAL,
    signal_timestamp TEXT,
    ensemble_id TEXT DEFAULT 'single',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Add settlements table matching daily_forecast_snapshots actual values
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    settlement_value REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (station, target_date)
);