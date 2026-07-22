#!/usr/bin/env python3
"""Add __all__ exports to key monolith decomposition modules."""
import ast
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (filename, __all__ list)
modules = [
    ('core/fusion_logic.py', [
        'SignalFusionEngine', 'TimeDecaySignalManager',
        'mutual_information_from_boolean_pairs', 'mutual_information_matrix',
        'mutual_information_simple_correlation', 'unique_information_fraction',
        'compute_weights_from_significance', 'dempster_shafer_conflict',
        'apply_conflict_modulation', 'compute_signal_agreement_score',
        'compute_local_ece', 'compute_conviction_score', 'should_take_trade',
        'decompose_brier_score', 'adjust_confidence_by_regime',
        'update_signal_weights_bayesian', 'enhance_with_agreement_and_conviction',
        'get_recent_calibration_performance', 'fit_calibration',
        'compute_fusion_weights', 'fuse_signals', 'update', 'compute_reliability',
        'adjust_confidence', 'get_lop_weight', 'get_all_reliabilities',
        'get_reliability_report',
    ]),
    ('core/compatibility_checks.py', ['demonstrate_fusion_stack']),
    ('core/signal_pipeline.py', [
        'record_explicit_decision_output', 'generate_signals',
        'is_recent_enough_for_late_day_analysis', 'calculate_temperature_trend',
    ]),
    ('core/trade_execution.py', ['TradeType', 'MarketSide', 'place_paper_trade', 'mark_positions_to_market']),
    ('core/pnl_tracking.py', [
        'process_settlements_for_date', 'daily_reconciliation',
        'calculate_calibration_metrics_for_date', 'get_current_balance',
        'get_version_performance', 'generate_calibration_report',
        'compute_sharpe', 'update_risk_metrics_on_trade',
    ]),
    ('core/settlement_processor.py', ['SettlementProcessor']),
    ('core/data_collector.py', [
        'verify_webhook_signature', 'fetch_window', 'fetch_latest', 'fetch_now',
        'get_latest_metar', 'set_watchlist', 'get_watchlist', 'get_metrics',
        'set_live_station_universe_resolver', 'start_scheduler',
        'ensure_scheduler_started', 'stop_scheduler', 'is_scheduler_running',
        'log_near_miss', 'log_near_miss_if_cooldown',
        'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market',
        'log_near_miss_if_epoch_alert_emitted',
    ]),
    ('core/data_processor.py', [
        'reset_station_daily_state', 'get_default_config', 'ensure_state_loaded',
        'get_state', 'get_latest_station_signal_runtime',
        'log_near_miss', 'log_near_miss_if_cooldown',
        'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market',
        'log_near_miss_if_epoch_alert_emitted',
    ]),
    ('core/health_monitor.py', [
        'get_station_ingestion_runtime', 'get_station_ingestion_window_runtime',
        'get_last_nws_fetch_diagnostic', 'get_alert_review_diagnostics',
        'get_latest_station_market_evaluation_context', 'get_recent_alerts',
        'get_retention_metrics', 'prune_old_alerts', 'run_replay_for_station_day',
        'get_transition_history', 'get_persisted_transition_history',
        'log_near_miss', 'log_near_miss_if_cooldown',
        'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market',
        'log_near_miss_if_epoch_alert_emitted',
    ]),
    ('core/market_monitor.py', [
        'get_default_config', 'map_market_to_station', 'discover_kalshi_weather_markets',
        'build_market_derived_station_universe', 'resolve_settlement_station',
        'discover_market_derived_station_codes', 'build_market_polling_station_universe',
        'get_discovered_weather_market_station_mapping', 'ensure_series_discovery_loaded',
        'get_series_discovery_cache_snapshot', 'get_series_surface_snapshot',
        'get_cached_series_markets', 'get_station_hydration_cache_probe',
        'ensure_ladder_hydration_prerequisite', 'get_hydration_prerequisite_state_snapshot',
        'get_kalshi_connectivity_snapshot', 'get_last_hydration_execution_snapshot',
        'enqueue_station_hydration', 'hydration_queue_snapshot',
        'process_hydration_queue_worker', 'hydrate_station_ladder_snapshot',
        'classify_proximity', 'build_structured_snapshot',
        'build_structured_snapshot_from_cache', 'get_state', 'get_metrics',
    ]),
    ('core/price_fetcher.py', ['get_public_markets', '_now_utc_iso']),
    ('core/order_manager.py', [
        'kalshi_execution_domain', 'set_kalshi_execution_domain',
        'reset_kalshi_execution_domain', 'process_ladder_transition',
        'send_composed_weather_market_alert', 'check_public_market_changes',
        'get_ladder_state_snapshot',
    ]),
    ('core/kalshi_monitor.py', [
        'kalshi_execution_domain', 'set_kalshi_execution_domain',
        'reset_kalshi_execution_domain', 'get_default_config', 'get_state',
        'get_metrics', 'get_public_markets', 'map_market_to_station',
        'discover_kalshi_weather_markets', 'build_market_derived_station_universe',
        'resolve_settlement_station', 'discover_market_derived_station_codes',
        'build_market_polling_station_universe',
        'get_discovered_weather_market_station_mapping', 'ensure_series_discovery_loaded',
        'get_series_discovery_cache_snapshot', 'get_series_surface_snapshot',
        'get_cached_series_markets', 'get_station_hydration_cache_probe',
        'ensure_ladder_hydration_prerequisite', 'get_hydration_prerequisite_state_snapshot',
        'get_kalshi_connectivity_snapshot', 'get_last_hydration_execution_snapshot',
        'enqueue_station_hydration', 'hydration_queue_snapshot',
        'process_hydration_queue_worker', 'hydrate_station_ladder_snapshot',
        'classify_proximity', 'build_structured_snapshot',
        'build_structured_snapshot_from_cache', 'process_ladder_transition',
        'send_composed_weather_market_alert', 'check_public_market_changes',
        'get_ladder_state_snapshot',
    ]),
    ('core/metar_monitor.py', [
        'verify_webhook_signature', 'reset_station_daily_state', 'get_default_config',
        'ensure_state_loaded', 'get_state', 'get_station_ingestion_runtime',
        'get_station_ingestion_window_runtime', 'get_last_nws_fetch_diagnostic',
        'get_latest_station_signal_runtime', 'run_replay_for_station_day',
        'get_transition_history', 'get_persisted_transition_history',
        'get_alert_review_diagnostics', 'get_latest_station_market_evaluation_context',
        'get_recent_alerts', 'get_retention_metrics', 'prune_old_alerts',
        'fetch_window', 'fetch_latest', 'fetch_now', 'get_latest_metar',
        'set_watchlist', 'get_watchlist', 'get_metrics',
        'set_live_station_universe_resolver', 'start_scheduler',
        'ensure_scheduler_started', 'stop_scheduler', 'is_scheduler_running',
        'log_near_miss', 'log_near_miss_if_cooldown',
        'log_near_miss_if_distance_to_boundary', 'log_near_miss_if_no_eligible_market',
        'log_near_miss_if_epoch_alert_emitted',
    ]),
    ('core/db_schema.py', ['SCHEMA_REGISTRY', 'get_schema_version', 'init_schema', 'migrate_schema', 'SCHEMA_VERSION', 'METADATA_TABLE_NAME']),
]

added = 0
for fname, all_list in modules:
    if not os.path.exists(fname):
        print(f"MISSING: {fname}")
        continue
    
    with open(fname) as f:
        content = f.read()
    
    if '__all__' in content:
        print(f"SKIP (has __all__): {fname}")
        continue
    
    # Format __all__ - split into multiple lines if > 80 chars
    all_items_str = ', '.join(f"'{x}'" for x in all_list)
    
    # Find a good insertion point - after last top-level import, before first function/class
    tree = ast.parse(content)
    lines = content.split('\n')
    
    # Find the line of the last top-level Import/ImportFrom
    last_import_line = 0
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if hasattr(n, 'lineno') and n.lineno > last_import_line:
                # Check it's a top-level import (not inside a function/class)
                for parent in ast.walk(tree):
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if hasattr(parent, 'lineno') and hasattr(parent, 'end_lineno'):
                            if parent.lineno <= n.lineno <= parent.end_lineno:
                                break
                else:
                    last_import_line = n.lineno
    
    if last_import_line > 0:
        insert_line = last_import_line  # After last import
    else:
        # No imports - find first function/class
        first_def_line = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first_def_line = n.lineno
                break
        insert_line = (first_def_line or 1) - 1
    
    # Find blank line after insert_line
    blank_line = insert_line
    while blank_line < len(lines) - 1 and lines[blank_line].strip() != '':
        blank_line += 1
    
    all_block = [f'__all__ = [{all_items_str}]']
    all_block.append('')
    
    indent = ''
    lines[blank_line:blank_line] = all_block
    
    with open(fname, 'w') as f:
        f.write('\n'.join(lines))
    
    print(f"ADDED __all__: {fname} (inserted after line {blank_line})")
    added += 1

print(f"\nDone. Added __all__ to {added} files.")
