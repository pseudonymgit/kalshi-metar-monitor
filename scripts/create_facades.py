#!/usr/bin/env python3
"""
Phase 20.1: Update original monoliths to import from new modules.
This preserves backward compatibility while decomposing the monoliths.
"""
import os

# ============================================================
# metar_monitor.py facade
# ============================================================
metar_facade = '''#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - extracted into data_collector.py, data_processor.py, health_monitor.py]
# 2. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 3. [2026-07-08 fix(alerts): allow sandbox and development domains to send alerts (not only production)]
# 4. [2026-07-06 C5: Fix is_down branch to use is_daily_low for down-reversion signals in metar_monitor.py]
# 5. [2026-07-05 R4-1.5: Goldilocks confidence split (up/down directional components)]
# 6. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
# 7. [2026-06-17 fix: bypass blocking hydration on health check paths]
# 8. [2026-06-17 fix: schema init ordering + non-fatal hydration for Render deploy]
# 9. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
# 10. [2026-03-21 test: keep canonical series discovery contract]
#
"""
METAR Monitor - Facade Module

This module has been decomposed into:
  - data_collector.py: METAR data fetching, parsing, polling
  - data_processor.py: Signal evaluation, temperature events, state management
  - health_monitor.py: Runtime tracking, diagnostics, alert delivery, replay

All functions are re-exported from the sub-modules for backward compatibility.
"""

# Import everything from sub-modules
from .data_collector import *
from .data_processor import *
from .health_monitor import *

# Preserve the original module-level state
from .data_collector import (
    _c_to_f, _parse_iso, _now_utc_iso, _parse_iso_utc_optional,
    get_default_config, _icao_tz_name, reset_station_daily_state,
    _load_cache, _save_cache, ensure_state_loaded, get_state,
    _compute_rejection_reasons, _to_local, _maybe_daily_reset_local,
    _iso_to_tz, _alert_integrity_evaluation_window_seconds,
    _resolve_missing_ladder_cause, _is_recent_transition_active,
)
from .data_processor import (
    _compute_goldilocks_confidence,
    _observation_seconds, _current_signal_epoch,
    _record_signal_runtime, _get_signal_runtime,
    _emit_signal_alert, get_latest_station_signal_runtime,
    _evaluate_deterministic_signal_layer, _process_temperature_event,
    _emit_alert, _simulate_temperature_for_testing,
    _send_alert, _persist_alert_path_truth, _record_webhook_outcome,
    log_near_miss, log_near_miss_if_cooldown,
    log_near_miss_if_distance_to_boundary,
    log_near_miss_if_no_eligible_market, log_near_miss_if_epoch_alert_emitted,
    verify_webhook_signature,
)
from .health_monitor import (
    get_station_ingestion_runtime, get_station_ingestion_window_runtime,
    get_last_nws_fetch_diagnostic, get_alert_review_diagnostics,
    get_latest_station_market_evaluation_context, _audit_alert,
    get_recent_alerts, get_retention_metrics, prune_old_alerts,
    _run_alert_retention, _snapshot_alert_queue_stats,
    _snapshot_station_state, _restore_station_state,
    _reset_replay_runtime_state_for_station, run_replay_for_station_day,
    _log_transition_event, _find_correlated_transition_entry,
    _annotate_transition_history_market_eval,
    _annotate_transition_history_alert_path_truth,
    _normalize_transition_history_query,
    _load_transition_event_metadata,
    _build_transition_history_row_from_persisted,
    get_transition_history, get_persisted_transition_history,
    _prune_transition_events,
    _queue_alert_for_delivery, _retry_delivery_batch,
    _get_pending_deliveries, _get_failed_alerts,
    _get_alert_delivery_queue_entries,
    _mark_alert_delivery_queue_dead_letter,_update_alert_delivery_queue_attempt,
    _delete_alert_delivery_queue,
    _alert_db_path, _ensure_alert_schema,
    _persist_signal_state, _load_signal_state,
    _load_all_signal_state, _hydrate_all_signal_state,
)

__all__ = [k for k in dir() if not k.startswith("_") or k.startswith("__")] or []
'''

# ============================================================
# kalshi_monitor.py facade
# ============================================================
kalshi_facade = '''#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - extracted into market_monitor.py, price_fetcher.py, order_manager.py]
# 2. [2026-07-08 fix(alerts): allow prod/sbox/dev domains + normalize environment names]
# 3. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
# 4. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
# 5. [2026-06-17 fix: bypass blocking hydration on health check paths]
# 6. [2026-06-17 fix: schema init ordering + non-fatal hydration for Render deploy]
# 7. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
# 8. [2026-03-20 Add authority-path seam comments]
# 9. [2026-03-20 fix: remove kalshi monitor compatibility wrappers]
# 10. [2026-03-20 Extract shared structured snapshot assembly (#276)]
#
"""
Kalshi Monitor - Facade Module

This module has been decomposed into:
  - market_monitor.py: Market discovery, series/station management, snapshots
  - price_fetcher.py: Kalshi API calls, rate limiting, public market queries
  - order_manager.py: Execution domain, ladder transitions, alerts, DB persistence

All functions are re-exported from the sub-modules for backward compatibility.
"""

from .market_monitor import *
from .price_fetcher import *
from .order_manager import *

__all__ = [k for k in dir() if not k.startswith("_") or k.startswith("__")] or []
'''

# ============================================================
# signal_fusion.py facade
# ============================================================
fusion_facade = '''#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - extracted into fusion_logic.py, compatibility_checks.py]
# 2. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 3. [2026-07-06 fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL]
# 4. [2026-07-06 feat: add SAS, conviction score, Brier decomposition, regime adjustment, Bayesian weights]
# 5. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#
"""
Signal Fusion - Facade Module

This module has been decomposed into:
  - fusion_logic.py: SignalFusionEngine, TimeDecaySignalManager, MI/weights/DS theory
  - compatibility_checks.py: Demonstration and compatibility verification

All functions and classes are re-exported from the sub-modules for backward compatibility.
"""

from .fusion_logic import *
from .compatibility_checks import *

__all__ = [k for k in dir() if not k.startswith("_") or k.startswith("__")] or []
'''

# Write facades
files = {
    'core/metar_monitor.py': metar_facade,
    'core/kalshi_monitor.py': kalshi_facade,
    'core/signal_fusion.py': fusion_facade,
}

for path, content in files.items():
    with open(path, 'w') as f:
        f.write(content)
    print(f"Wrote {path} ({len(content.split(chr(10)))} lines)")

print("\n=== Facade creation complete ===")