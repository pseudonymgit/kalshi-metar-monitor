#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: FACADE — metar_monitor.py now re-exports from data_collector.py, data_processor.py, health_monitor.py]
# 2. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 3. [2026-07-08 fix(alerts): allow sandbox and development domains to send alerts (not only production)]
# 4. [2026-07-06 C5: Fix is_down branch to use is_daily_low for down-reversion signals in metar_monitor.py]
# 5. [2026-07-05 R4-1.5: Goldilocks confidence split (up/down directional components)]
# 6. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
# 7. [2026-06-17 fix: bypass blocking hydration on health check paths]
# 8. [2026-06-17 fix: schema init ordering + non-fatal hydration for Render deploy]
# 9. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
# 10. [2026-03-20 Add authority-path seam comments]

"""
METAR Monitor — FACADE MODULE

This module now re-exports from decomposed sub-modules:
  - data_collector.py:  METAR data collection, NWS/IEM/TGFTP fetching, scheduler
  - data_processor.py:  Observation processing, signal evaluation, state management
  - health_monitor.py:  Monitoring, diagnostics, alert audit, transition history

All public and externally-imported private API is preserved.
Import from core.metar_monitor continues to work for all existing consumers.
"""

from __future__ import annotations

# =============================================================================
# 1. Re-exports from data_collector.py
# =============================================================================
from .data_collector import (
    # Public API
    verify_webhook_signature,
    fetch_window,
    fetch_latest,
    fetch_now,
    get_latest_metar,
    set_watchlist,
    get_watchlist,
    get_metrics,
    set_live_station_universe_resolver,
    start_scheduler,
    ensure_scheduler_started,
    stop_scheduler,
    is_scheduler_running,
    # Private symbols referenced by other modules / tests
    _headers_for_nws,
    _iso_seconds_z,
    _nws_collection_url,
    _iem_range_url,
    _tgftp_latest_url,
    _obs_tuple,
    _to_float_or_none,
    _to_int_or_none,
    _nws_extract_weather_codes,
    _nws_optional_metrics,
    _record_timeout,
    _parse_nws_collection,  # imported by tests/test_metar_optional_observation_metrics.py
    _parse_iem_csv,
    _parse_tgftp_text,      # imported by tests/test_metar_optional_observation_metrics.py
    _fetch_range_nws,       # imported by tests/test_nws_fetch_diagnostic_logging.py
    _fetch_nws_latest_single,
    _fetch_range_iem,
    _fetch_latest_tgftp,
    _ingest_obs,            # used by tests, debug_test.py, tools/testing/replay_observations.py
    _compute_window,
    _fetch_range_strict,
    _resolve_live_polling_stations,  # used by tests
    _poll_once,              # used by tests
    _scheduler_loop,
    _check_knyc_observation_gap,
)

# =============================================================================
# 2. Re-exports from data_processor.py
# =============================================================================
from .data_processor import (
    # Public API
    reset_station_daily_state,
    get_default_config,
    ensure_state_loaded,
    get_state,
    get_latest_station_signal_runtime,
    # Private symbols referenced by other modules / tests
    _now_utc_iso,           # imported by kalshi_monitor.py, price_fetcher.py, order_manager.py, near_miss_audit.py
    _compute_goldilocks_confidence,   # used by test_goldilocks_detection.py
    _icao_tz_name,
    _c_to_f,
    _parse_iso,                       # used by tests
    _parse_iso_utc_optional,          # used by tests
    _alert_integrity_evaluation_window_seconds,
    _resolve_missing_ladder_cause,    # used by tests
    _is_recent_transition_active,
    _iso_to_tz,
    _to_local,                        # used by tests
    _maybe_daily_reset_local,         # used by tests
    _load_cache,
    _save_cache,                      # used by tests
    _compute_rejection_reasons,
    _observation_seconds,
    _current_signal_epoch,
    _record_signal_runtime,
    _get_signal_runtime,
    _emit_signal_alert,               # used by tests
    _evaluate_deterministic_signal_layer,  # used by tests
    _process_temperature_event,       # used extensively by tests
    _emit_alert,                      # used by tests
    _simulate_temperature_for_testing,  # used by app.py, tests
    _send_alert,                      # used by alert_retry_queue.py, tests
    # Module-level state (used by tests directly)
    _STATE_LOCK,              # used by tests
    _KALSHI_RATE_LIMIT_LOCK,  # used by tests
    _SIGNAL_OBSERVATION_WINDOWS,  # used by tests
    _SIGNAL_STATION_LAST_EMIT,    # used by tests
    _SIGNAL_BOUNDARY_LAST_EMIT,   # used by tests
    _SIGNAL_EPOCH_COUNTER,        # used by tests
    _SIGNAL_GOLDILOCKS_EPOCH_TRACKER,  # used by tests
    _LATEST_SIGNAL_RUNTIME,       # used by tests
    _LAST_SETTLEMENT_UP_TS,       # used by tests
    _MISSING_LADDER_DEDUPE,       # used by tests
    _KALSHI_LAST_CALL_TS,         # used by tests
    _STATE,                       # used by tests
    _ALERT_LOGGER,                # used by tests
    _SCHEDULER_THREAD,            # imported by app.py
    _last_seen_iso,
    _last_obs,
)

# =============================================================================
# 3. Re-exports from health_monitor.py
# =============================================================================
from .health_monitor import (
    # Public API
    get_station_ingestion_runtime,
    get_station_ingestion_window_runtime,
    get_last_nws_fetch_diagnostic,
    get_alert_review_diagnostics,
    get_latest_station_market_evaluation_context,
    get_recent_alerts,
    get_retention_metrics,
    prune_old_alerts,
    run_replay_for_station_day,
    get_transition_history,           # imported by replay_parity_validator.py, tests
    get_persisted_transition_history, # imported by tests
    # Private symbols referenced by other modules / tests
    _audit_alert,                     # used by tests
    _snapshot_alert_queue_stats,      # used by tests
    _snapshot_station_state,          # used by tests
    _restore_station_state,           # used by tests
    _reset_replay_runtime_state_for_station,
    _log_transition_event,            # used by tests
    _find_correlated_transition_entry,
    _annotate_transition_history_market_eval,  # used by tests
    _annotate_transition_history_alert_path_truth,
    _normalize_transition_history_query,
    _load_transition_event_metadata,
    _build_transition_history_row_from_persisted,
    _prune_transition_events,
    _queue_alert_for_delivery,        # used by tests
    _retry_delivery_batch,            # used by tests
    _get_pending_deliveries,          # used by tests
    _get_failed_alerts,               # used by app.py, tests
    _get_alert_delivery_queue_entries,
    _mark_alert_delivery_queue_dead_letter,  # used by app.py, tests
    _update_alert_delivery_queue_attempt,    # used by tests
    _delete_alert_delivery_queue,     # used by tests
    # Module-level state (used by tests directly)
    _TRANSITION_HISTORY,              # used by tests
    _TRANSITION_LOCK,                 # used by tests
)

# =============================================================================
# 4. Near-miss audit re-exports (also re-exported from data modules)
# =============================================================================
from .near_miss_audit import (
    log_near_miss,
    log_near_miss_if_cooldown,
    log_near_miss_if_distance_to_boundary,
    log_near_miss_if_no_eligible_market,
    log_near_miss_if_epoch_alert_emitted,
)

# =============================================================================
# 5. Soft imports (may-fail gracefully)
# =============================================================================
# Ensure alert schema — deferred imports handled inside functions

# =============================================================================
# 6. __all__ — complete public API contract
# =============================================================================
__all__ = [
    'verify_webhook_signature',
    'reset_station_daily_state',
    'get_default_config',
    'ensure_state_loaded',
    'get_state',
    'get_station_ingestion_runtime',
    'get_station_ingestion_window_runtime',
    'get_last_nws_fetch_diagnostic',
    'get_latest_station_signal_runtime',
    'run_replay_for_station_day',
    'get_transition_history',
    'get_persisted_transition_history',
    'get_alert_review_diagnostics',
    'get_latest_station_market_evaluation_context',
    'get_recent_alerts',
    'get_retention_metrics',
    'prune_old_alerts',
    'fetch_window',
    'fetch_latest',
    'fetch_now',
    'get_latest_metar',
    'set_watchlist',
    'get_watchlist',
    'get_metrics',
    'set_live_station_universe_resolver',
    'start_scheduler',
    'ensure_scheduler_started',
    'stop_scheduler',
    'is_scheduler_running',
    'log_near_miss',
    'log_near_miss_if_cooldown',
    'log_near_miss_if_distance_to_boundary',
    'log_near_miss_if_no_eligible_market',
    'log_near_miss_if_epoch_alert_emitted',
]
