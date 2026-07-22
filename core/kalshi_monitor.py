#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1c: Monolith decomposition - facade module re-exporting from market_monitor.py, price_fetcher.py, order_manager.py]
# 2. [2026-07-08 fix(alerts): allow prod/sbox/dev domains + normalize environment names (production->prod, sandbox->sbox, development->dev)]
# 3. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
# 4. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
# 5. [2026-06-17 fix: bypass blocking hydration on health check paths]
# 6. [2026-06-17 fix: schema init ordering + non-fatal hydration for Render deploy]
# 7. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
# 8. [2026-03-20 Add authority-path seam comments]
# 9. [2026-03-20 fix: remove kalshi monitor compatibility wrappers]
# 10. [2026-03-20 Extract shared structured snapshot assembly (#276)]

"""
Kalshi Monitor — FACADE MODULE

This module now re-exports from decomposed sub-modules:
  - market_monitor.py: Market discovery, station mapping, series discovery, hydration
  - price_fetcher.py:  Kalshi API connectivity, rate limiting, public GET methods
  - order_manager.py:  Execution domain, ladder transitions, composed alerts, signal/market cache

All public and external private API surface is preserved.
Import from core.kalshi_monitor continues to work for all existing consumers.
"""

import os
import threading

# =============================================================================
# 1. Re-exports from market_monitor.py
# =============================================================================
from .market_monitor import (
    # Public API
    get_default_config,
    map_market_to_station,
    discover_kalshi_weather_markets,
    build_market_derived_station_universe,
    resolve_settlement_station,
    discover_market_derived_station_codes,
    build_market_polling_station_universe,
    get_discovered_weather_market_station_mapping,
    ensure_series_discovery_loaded,
    get_series_discovery_cache_snapshot,
    get_series_surface_snapshot,
    get_cached_series_markets,
    get_station_hydration_cache_probe,
    ensure_ladder_hydration_prerequisite,
    get_hydration_prerequisite_state_snapshot,
    get_kalshi_connectivity_snapshot,
    get_last_hydration_execution_snapshot,
    enqueue_station_hydration,
    hydration_queue_snapshot,
    process_hydration_queue_worker,
    hydrate_station_ladder_snapshot,
    classify_proximity,
    build_structured_snapshot,
    build_structured_snapshot_from_cache,
    get_state,
    get_metrics,
    # Private API (used by app.py and other modules)
    _configured_target_market_types,
    _extract_station_from_settlement_source_url,
    _market_strike_value,
    _directional_strike_window,
    _observed_temperature_f,
    _assemble_structured_snapshot_markets,
    _classify_weather_market_type,
    _extract_station_from_settlement_market_metadata,
    _parse_market_expiration,
    _format_change,
    _parse_target_market_types,
    _get_active_stations,
    _station_local_kalshi_date_token,
    _normalize_series_tickers,
    _infer_series_market_type,
    _series_tickers_for_market_types,
    _discover_series_for_stations,
    _extract_station_from_ticker,
    _station_local_previous_day,
    _build_weather_event_ticker,
    _station_to_city_token,
    _filter_structured_markets,
    _extract_strike_from_ticker,
    _select_event_ticker_for_series,
    _build_ladder_structure,
    # Module-level variables needed by other modules
    _DISCOVERED_WEATHER_MARKETS_BY_STATION,
    _DISCOVERED_WEATHER_MARKETS,
    _SERIES_BY_STATION,
    _SERIES_DISCOVERED,
    _SERIES_MARKETS_CACHE,
    _SERIES_EVENTS_CACHE,
    _SERIES_LOCK,
    _HYDRATION_PREREQUISITE_STATE,
    _SERIES_DISCOVERY_ATTEMPT_COUNT,
    _LAST_SERIES_DISCOVERY_SUCCESS_UTC,
    _LAST_SERIES_DISCOVERY_ERROR,
    _MARKETS_CACHE_POPULATION_COUNT,
    _LAST_HYDRATION_EXECUTION,
    _hydration_queue,
    _hydration_backoff_until,
    _last_hydration_request_ts,
    _last_market_state,
    _last_market_check_summary,
    _ladder_state,
    _ladder_event_keys,
    _LADDER_LOCK,
    _PROXIMITY_LOCK,
    _LAST_PROXIMITY_REGIME,
    _PROXIMITY_RANK,
)

# =============================================================================
# 2. Re-exports from price_fetcher.py
# =============================================================================
from .price_fetcher import (
    # Public API
    get_public_markets,
    # Private API
    _now_utc_iso,
    _persist_rate_limit_entry,
    _parse_retry_after,
    _check_rate_limit,
    _kalshi_public_get_with_rate_limit,
    _sign_request_rsa,
    _kalshi_get,
    _kalshi_public_get,
    _get_all_public_markets,
)

# =============================================================================
# 3. Re-exports from order_manager.py
# =============================================================================
from .order_manager import (
    # Public API
    kalshi_execution_domain,
    set_kalshi_execution_domain,
    reset_kalshi_execution_domain,
    process_ladder_transition,
    send_composed_weather_market_alert,
    check_public_market_changes,
    get_ladder_state_snapshot,
    # Private API
    _current_kalshi_execution_domain,
    _send_kalshi_market_alert,
    _persist_signal_state,
    _load_signal_state,
    _persist_market_cache,
    _load_market_cache,
    _load_all_market_cache,
    _hydrate_all_market_cache,
    _ensure_alert_schema,
    _alert_db_path,
    _load_current_epoch_context,
    _derive_attention_phrase,
)

# =============================================================================
# 4. Compatibility shim: _STATION_CITY_TOKEN_MAP
# =============================================================================
# This is derived from core.station_registry. Kept here for backward compatibility
# with app.py and other modules that import it directly.
try:
    from .station_registry import get_station_mapping as _registry_get_mapping
    _STATION_CITY_TOKEN_MAP = {
        icao: info.get("kalshi_token", "")
        for icao, info in _registry_get_mapping().items()
    }
except Exception:
    # Fallback if station_registry import fails (shouldn't happen in normal operation)
    _STATION_CITY_TOKEN_MAP = {
        "KDEN": "DEN", "KLAX": "LAX", "KNYC": "NYC", "KPHL": "PHIL",
        "KMDW": "CHI", "KMIA": "MIA", "KAUS": "AUS",
    }

# =============================================================================
# 5. __all__
# =============================================================================
__all__ = [
    'kalshi_execution_domain', 'set_kalshi_execution_domain',
    'reset_kalshi_execution_domain', 'get_default_config', 'get_state',
    'get_metrics', 'get_public_markets', 'map_market_to_station',
    'discover_kalshi_weather_markets', 'build_market_derived_station_universe',
    'resolve_settlement_station', 'discover_market_derived_station_codes',
    'build_market_polling_station_universe',
    'get_discovered_weather_market_station_mapping',
    'ensure_series_discovery_loaded', 'get_series_discovery_cache_snapshot',
    'get_series_surface_snapshot', 'get_cached_series_markets',
    'get_station_hydration_cache_probe', 'ensure_ladder_hydration_prerequisite',
    'get_hydration_prerequisite_state_snapshot', 'get_kalshi_connectivity_snapshot',
    'get_last_hydration_execution_snapshot', 'enqueue_station_hydration',
    'hydration_queue_snapshot', 'process_hydration_queue_worker',
    'hydrate_station_ladder_snapshot', 'classify_proximity',
    'build_structured_snapshot', 'build_structured_snapshot_from_cache',
    'process_ladder_transition', 'send_composed_weather_market_alert',
    'check_public_market_changes', 'get_ladder_state_snapshot',
    '_STATION_CITY_TOKEN_MAP',
]
