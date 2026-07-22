#!/usr/bin/env python3
"""
Phase 20.1b-d: Extract remaining monoliths.
Uses AST to properly split files into domain modules.
"""
import ast
import sys
import os

def extract_file(src_path, module_map):
    """
    Extract a monolithic file into multiple modules.
    
    module_map: dict of target_module_name -> list of function/class names to extract
    The original file is kept as-is (backward compat).
    """
    with open(src_path) as f:
        source = f.read()
        lines = source.split('\n')
    
    # Parse AST
    tree = ast.parse(source)
    
    # Collect all top-level definitions with line ranges
    defs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = (node.lineno, node.end_lineno)
    
    # Build each target module
    for module_name, names in module_map.items():
        header = f'"""\n{module_name.capitalize().replace("_", " ")} Module\n\nExtracted from {os.path.basename(src_path)} during Phase 20.1 monolith decomposition.\n"""\n\n'
        
        module_lines = [header]
        
        for name in names:
            if name in defs:
                start, end = defs[name]
                block = lines[start-1:end]  # 1-indexed to 0-indexed
                if block:
                    module_lines.append('\n'.join(block))
                    module_lines.append('\n')
        
        # Also extract the imports/constants section (before first def)
        if defs:
            first_start = min(v[0] for v in defs.values())
            # Find the actual start of code (after docstring)
            in_docstring = False
            import_start = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('"""') and not in_docstring:
                    if stripped.count('"""') == 2:
                        import_start = i + 1
                        break
                    in_docstring = True
                elif in_docstring and '"""' in stripped:
                    import_start = i + 1
                    break
            
            # Get imports and constants (import_start to first_def-1)
            # Filter out empty sections
            if import_start < first_start - 1:
                preamble = lines[import_start:first_start-1]
                if any(l.strip() for l in preamble):
                    module_lines.insert(1, '\n' + '\n'.join(preamble) + '\n')
        
        dst_path = src_path.replace('.py', f'_{module_name}.py')
        # If src is in core/, put in core/
        dst_dir = os.path.dirname(src_path)
        dst_path = os.path.join(dst_dir, f'{module_name}.py')
        
        code = ''.join(module_lines)
        with open(dst_path, 'w') as f:
            f.write(code)
        
        print(f"  Wrote {dst_path} ({len(code.split(chr(10)))} lines)")
    
    return defs


# ============================================================
# 20.1b: metar_monitor.py → data_collector.py, data_processor.py, health_monitor.py
# ============================================================
print("\n=== 20.1b: Extracting metar_monitor.py ===")

# Data collection functions (fetching, parsing, polling)
collector_funcs = [
    'verify_webhook_signature', '_headers_for_nws', '_iso_seconds_z',
    '_nws_collection_url', '_iem_range_url', '_tgftp_latest_url',
    '_obs_tuple', '_to_float_or_none', '_to_int_or_none',
    '_nws_extract_weather_codes', '_nws_optional_metrics',
    '_record_timeout', '_parse_nws_collection', '_parse_iem_csv',
    '_parse_tgftp_text', '_parse_metar_temp_c', '_fetch_range_nws',
    '_parse_utc', '_fetch_nws_latest_single', '_fetch_range_iem',
    '_fetch_latest_tgftp', '_ingest_obs', '_compute_window',
    '_fetch_range_strict', 'fetch_window', 'fetch_latest', 'fetch_now',
    'get_latest_metar', 'set_watchlist', 'get_watchlist',
    'get_metrics', 'set_live_station_universe_resolver',
    '_resolve_live_polling_stations', '_poll_once', '_scheduler_loop',
    '_check_knyc_observation_gap', 'start_scheduler',
    'ensure_scheduler_started', 'stop_scheduler', 'is_scheduler_running',
]

# Data processing functions (signals, temperature, state)
processor_funcs = [
    '_compute_goldilocks_confidence', '_icao_tz_name',
    'reset_station_daily_state', 'get_default_config',
    '_c_to_f', '_parse_iso', '_now_utc_iso', '_parse_iso_utc_optional',
    '_alert_integrity_evaluation_window_seconds',
    '_resolve_missing_ladder_cause', '_is_recent_transition_active',
    '_iso_to_tz', '_to_local', '_maybe_daily_reset_local',
    '_load_cache', '_save_cache', 'ensure_state_loaded', 'get_state',
    '_compute_rejection_reasons',
    '_observation_seconds', '_current_signal_epoch',
    '_record_signal_runtime', '_get_signal_runtime',
    '_emit_signal_alert', 'get_latest_station_signal_runtime',
    '_evaluate_deterministic_signal_layer', '_process_temperature_event',
    '_emit_alert', '_simulate_temperature_for_testing',
    '_send_alert', '_persist_alert_path_truth', '_record_webhook_outcome',
    'log_near_miss', 'log_near_miss_if_cooldown',
    'log_near_miss_if_distance_to_boundary',
    'log_near_miss_if_no_eligible_market', 'log_near_miss_if_epoch_alert_emitted',
]

# Health/state monitoring functions
monitor_funcs = [
    'get_station_ingestion_runtime', 'get_station_ingestion_window_runtime',
    'get_last_nws_fetch_diagnostic', 'get_alert_review_diagnostics',
    'get_latest_station_market_evaluation_context', '_audit_alert',
    'get_recent_alerts', 'get_retention_metrics', 'prune_old_alerts',
    '_run_alert_retention', '_snapshot_alert_queue_stats',
    '_snapshot_station_state', '_restore_station_state',
    '_reset_replay_runtime_state_for_station', 'run_replay_for_station_day',
    '_log_transition_event', '_find_correlated_transition_entry',
    '_annotate_transition_history_market_eval',
    '_annotate_transition_history_alert_path_truth',
    '_normalize_transition_history_query',
    '_load_transition_event_metadata',
    '_build_transition_history_row_from_persisted',
    'get_transition_history', 'get_persisted_transition_history',
    '_prune_transition_events',
    # Alert delivery
    '_queue_alert_for_delivery', '_retry_delivery_batch',
    '_get_pending_deliveries', '_get_failed_alerts',
    '_get_alert_delivery_queue_entries',
    '_mark_alert_delivery_queue_dead_letter',
    '_update_alert_delivery_queue_attempt', '_delete_alert_delivery_queue',
    '_alert_db_path', '_ensure_alert_schema',
    '_persist_signal_state', '_load_signal_state',
    '_load_all_signal_state', '_hydrate_all_signal_state',
]

extract_file('core/metar_monitor.py', {
    'data_collector': collector_funcs,
    'data_processor': processor_funcs,
    'health_monitor': monitor_funcs,
})

# ============================================================
# 20.1c: kalshi_monitor.py → market_monitor.py, price_fetcher.py, order_manager.py
# ============================================================
print("\n=== 20.1c: Extracting kalshi_monitor.py ===")

market_funcs = [
    # Series/station discovery
    '_extract_station_from_settlement_source_url', '_market_strike_value',
    '_directional_strike_window', '_observed_temperature_f',
    '_assemble_structured_snapshot_markets', 'get_default_config',
    '_classify_weather_market_type',
    '_extract_station_from_settlement_market_metadata',
    'map_market_to_station', '_parse_market_expiration',
    'discover_kalshi_weather_markets', 'build_market_derived_station_universe',
    'resolve_settlement_station', 'discover_market_derived_station_codes',
    'build_market_polling_station_universe',
    'get_discovered_weather_market_station_mapping',
    '_format_change', '_parse_target_market_types', '_get_active_stations',
    '_station_local_kalshi_date_token', '_normalize_series_tickers',
    '_configured_target_market_types', '_infer_series_market_type',
    '_series_tickers_for_market_types', '_discover_series_for_stations',
    '_extract_station_from_ticker', 'ensure_series_discovery_loaded',
    'get_series_discovery_cache_snapshot', 'get_series_surface_snapshot',
    'get_cached_series_markets', 'get_station_hydration_cache_probe',
    '_station_local_previous_day', 'ensure_ladder_hydration_prerequisite',
    'get_hydration_prerequisite_state_snapshot',
    'get_kalshi_connectivity_snapshot',
    'get_last_hydration_execution_snapshot',
    'enqueue_station_hydration', 'hydration_queue_snapshot',
    'process_hydration_queue_worker', 'hydrate_station_ladder_snapshot',
    '_build_weather_event_ticker', '_station_to_city_token',
    '_filter_structured_markets', '_record_rejection',
    '_extract_strike_from_ticker', 'classify_proximity',
    '_select_event_ticker_for_series', 'build_structured_snapshot',
    'build_structured_snapshot_from_cache', '_build_ladder_structure',
    '_sort_key', '_determine_bucket', '_normalize_key',
    '_to_price', '_sort_key', '_label_for_market', '_ordered_bounds',
    'get_state', 'get_metrics',
]

price_funcs = [
    '_now_utc_iso', '_persist_rate_limit_entry', '_parse_retry_after',
    '_check_rate_limit', '_kalshi_public_get_with_rate_limit',
    '_sign_request_rsa', '_kalshi_get', '_kalshi_public_get',
    'get_public_markets', '_get_all_public_markets',
]

order_funcs = [
    'kalshi_execution_domain', '_current_kalshi_execution_domain',
    'set_kalshi_execution_domain', 'reset_kalshi_execution_domain',
    'process_ladder_transition', '_send_kalshi_market_alert',
    'send_composed_weather_market_alert', 'check_public_market_changes',
    'get_ladder_state_snapshot',
    # DB persistence
    '_persist_signal_state', '_load_signal_state',
    '_persist_market_cache', '_load_market_cache',
    '_load_all_market_cache', '_hydrate_all_market_cache',
    '_ensure_alert_schema', '_alert_db_path',
    '_load_current_epoch_context', '_derive_attention_phrase',
]

extract_file('core/kalshi_monitor.py', {
    'market_monitor': market_funcs,
    'price_fetcher': price_funcs,
    'order_manager': order_funcs,
})

# ============================================================
# 20.1d: signal_fusion.py → fusion_logic.py, compatibility_checks.py
# ============================================================
print("\n=== 20.1d: Extracting signal_fusion.py ===")

fusion_funcs = [
    'SignalFusionEngine', 'TimeDecaySignalManager',
    'mutual_information_from_boolean_pairs', '_compute_mi_from_preds',
    '_discretize_pred', 'mutual_information_matrix',
    'mutual_information_simple_correlation', 'unique_information_fraction',
    'compute_weights_from_significance', 'dempster_shafer_conflict',
    'apply_conflict_modulation',
]

compat_funcs = [
    'demonstrate_fusion_stack',
]

extract_file('core/signal_fusion.py', {
    'fusion_logic': fusion_funcs,
    'compatibility_checks': compat_funcs,
})

print("\n=== Extraction complete ===")